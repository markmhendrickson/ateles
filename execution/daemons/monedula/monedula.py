#!/usr/bin/env python3
"""
Monedula — Recurring Payments Daemon
Named after Corvus monedula (jackdaw — moneta = money).

Runs on a ~15-minute poll via launchd StartInterval. Each tick:

  1. Fetches calendar events covering roughly the last 26 hours (today plus
     the tail of yesterday, to catch a late session across midnight).
  2. Selects sessions whose END time has already passed, that match an
     active payment profile's calendar_keywords, and that have no
     notified-marker yet ("event-end detection" — replaces the old
     scan-yesterday-at-8am model).
  3. Sends ONE email per newly-ended session (the attendance-confirmation
     + payment preview) and records a pending-approval marker.
  4. Sweeps every marker still awaiting approval: reads the Gmail thread
     for an operator reply, executes the payment on approval, rolls the
     linked task's due_date either way, and never re-notifies or
     double-pays a session already marked paid/skipped.

Email (via `lib/approval/email_channel.py`, the swarm's standardized
email-reply-to-approve module built for ateles#276/#277) is the ONLY
operator-facing channel for payment previews, approvals, and confirmations.
Telegram has been removed from this path entirely — lib/notify + activity-log
calls may remain for daemon-health/error pings only.

Each approval request carries a session-scoped token
(`lib/approval/tokens.token_for(event_id, session=session_date)`) in its
subject, and `lib/approval/tokens.parse_verdict` requires that token to be
echoed back in the (quoted) reply subject before a verdict counts — this is
what stops a stale reply for a PRIOR session from re-approving the NEXT one
(the exact failure token_for's docstring documents, ateles 2026-07-22).

Usage:
  python3 monedula.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Bootstrap: load env from ~/.config/neotoma/.env before anything else.
# (launchd does not source shell profiles)
# ---------------------------------------------------------------------------

_NEOTOMA_ENV_FILE = Path.home() / ".config" / "neotoma" / ".env"
if _NEOTOMA_ENV_FILE.exists():
    for _line in _NEOTOMA_ENV_FILE.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

# ---------------------------------------------------------------------------
# Local package imports (path bootstrap required before import)
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
_DAEMON_DIR = Path(__file__).resolve().parent
if str(_DAEMON_DIR) not in sys.path:
    sys.path.insert(0, str(_DAEMON_DIR))

import markers  # noqa: E402
from lib.approval import email_channel  # noqa: E402
from lib.approval.tokens import parse_verdict, subject_marker, token_for  # noqa: E402
from lib.daemon_runtime.logging_setup import configure_daemon_logging  # noqa: E402

try:
    from lib.notify import Notifier  # noqa: E402

    _notifier: "Notifier | None" = Notifier.from_neotoma()
except Exception:  # lib unavailable or Neotoma unreachable at import time
    _notifier = None


def _notify(message: str, priority: str = "info") -> None:
    """
    Send via lib/notify if available; silently skip if not.

    Daemon-health/error pings ONLY — no payment preview, approval, or
    confirmation content may go through this path (see module docstring).
    """
    if _notifier is None:
        return
    try:
        from lib.notify import Priority

        p = getattr(Priority, priority.upper(), Priority.INFO)
        _notifier.send(message, priority=p, handler="monedula")
    except Exception:
        pass


# Activity-log channel (CyphorhinusBot observation feed) — health/error only.
try:
    from lib.activity import ActivityLogger  # noqa: E402

    _activity: "ActivityLogger | None" = ActivityLogger(agent="monedula")
except Exception:
    _activity = None


# ---------------------------------------------------------------------------
# Constants / paths
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).parent.parent.parent.parent  # ateles repo root

# Cloudflare fronts the hosted Neotoma instance and blocks urllib's default
# User-Agent with a 1010 "browser signature" 403. Any explicit UA passes.
NEOTOMA_USER_AGENT = "ateles-neotoma-sync/1.0"

NEOTOMA_BEARER_TOKEN = os.environ.get("NEOTOMA_BEARER_TOKEN", "")
NEOTOMA_BASE_URL = os.environ.get("NEOTOMA_BASE_URL", "")

# Madrid is UTC+1 (winter) / UTC+2 (summer). We don't have a tz database
# dependency here, so use a fixed UTC+2 offset for query windows only (the
# window is deliberately generous — see fetch_recent_events — so a 1-hour
# DST mismatch does not cause a session to be missed).
_MADRID_OFFSET = timezone(timedelta(hours=2))

# Operator address for approval emails. Env-sourced (swarm convention: same
# OPERATOR_EMAIL var as riparia/cotinga/apis), never a hardcoded literal — per
# CLAUDE.md "Operator identity (name, email) ... read from env ... not literals
# in daemon code." MONEDULA_OPERATOR_EMAIL overrides for a payment-specific inbox.
OPERATOR_EMAIL = (
    os.environ.get("MONEDULA_OPERATOR_EMAIL", "").strip()
    or os.environ.get("OPERATOR_EMAIL", "").strip()
)

# Calendar lookback window for ended-session detection. Default is one week
# (168h) so a run is forgiving of many missed 15-min ticks (e.g. laptop asleep
# over a weekend) and still catches a session that ended days ago. Override via
# MONEDULA_LOOKBACK_HOURS. NOTE: a wide window relies on the notified-marker
# file to avoid re-emailing already-handled sessions; MONEDULA_MAX_NOTIFY_PER_RUN
# is a hard backstop that caps how many notify emails a single tick may send, so
# an empty/reset marker file can never trigger an unbounded email burst.
LOOKBACK_HOURS = int(os.environ.get("MONEDULA_LOOKBACK_HOURS", "168"))
MAX_NOTIFY_PER_RUN = int(os.environ.get("MONEDULA_MAX_NOTIFY_PER_RUN", "6"))

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

# Rotating + repeat-suppressing (lib/daemon_runtime/logging_setup.py):
# unbounded retry logging filled a 926 GB disk on 2026-08-18 (ateles#420). A
# 15-minute poll cadence makes this daemon exactly the kind of tight-loop
# caller that fix was written for.
log = configure_daemon_logging("monedula", also_stdout=True)


# ---------------------------------------------------------------------------
# Calendar: fetch recent events + event-end detection
# ---------------------------------------------------------------------------


def fetch_recent_events(now: datetime | None = None, lookback_hours: int = LOOKBACK_HOURS) -> list[dict]:
    """
    Use gws CLI to fetch calendar events covering [now - lookback_hours, now
    + a small forward margin] in Europe/Madrid. Returns list of event dicts.
    Returns empty list on any failure (fail-safe).
    """
    import shutil

    gws = shutil.which("gws")
    if not gws:
        log.error("gws CLI not found in PATH — cannot check calendar")
        return []

    now = now or datetime.now(_MADRID_OFFSET)
    time_min = (now - timedelta(hours=lookback_hours)).isoformat()
    # Small forward margin so an event ending in the next few minutes isn't
    # missed by clock skew between this host and the calendar; it will only
    # be selected once its end time is actually in the past (see
    # select_newly_ended_sessions).
    time_max = (now + timedelta(hours=1)).isoformat()

    params = {
        "calendarId": "primary",
        "singleEvents": True,
        "orderBy": "startTime",
        "timeMin": time_min,
        "timeMax": time_max,
    }

    try:
        result = subprocess.run(
            [gws, "calendar", "events", "list", "--params", json.dumps(params)],
            capture_output=True,
            text=True,
            timeout=30,
            env=os.environ,
        )
        if result.returncode != 0:
            log.error(f"gws calendar events list failed: {result.stderr.strip()[:300]}")
            return []

        data = json.loads(result.stdout)
        items = data.get("items") or []
        log.info(f"Fetched {len(items)} calendar event(s) in lookback window")
        return items

    except json.JSONDecodeError as exc:
        log.error(f"Failed to parse gws output: {exc}")
        return []
    except Exception as exc:
        log.error(f"Calendar fetch error: {exc}")
        return []


def _event_id(event: dict) -> str:
    return str(event.get("id") or event.get("iCalUID") or "")


def _event_end_dt(event: dict) -> datetime | None:
    """
    Return the timezone-aware end datetime for a TIMED event, or None for
    all-day events (no precise end) or malformed events.
    """
    end = event.get("end") or {}
    end_dt_str = end.get("dateTime")
    if not end_dt_str:
        # All-day event (end.date only) — no precise end time, ignored for
        # event-end detection per spec.
        return None
    try:
        return datetime.fromisoformat(end_dt_str)
    except ValueError:
        log.warning(f"Unparseable event end.dateTime: {end_dt_str!r}")
        return None


def _event_end_date_iso(event: dict) -> str:
    """ISO date (YYYY-MM-DD) of the event's end, used as the marker key date."""
    end_dt = _event_end_dt(event)
    if end_dt:
        return end_dt.date().isoformat()
    # Fallback for events we otherwise ignore — keeps marker keys well-formed
    # if ever called on an all-day event.
    end = event.get("end") or {}
    return str(end.get("date") or "")


@dataclass
class EndedSessionMatch:
    handler: Any
    match: dict
    event: dict
    event_id: str
    session_date: str
    end_dt: datetime


def select_newly_ended_sessions(
    events: list[dict], handlers: list, now: datetime | None = None
) -> list[EndedSessionMatch]:
    """
    Select events that:
      (a) have a precise END time that is now in the past (timed events
          only — all-day events are ignored, they have no precise end),
      (b) match an active payment profile's calendar_keywords via
          handler.matches(), AND
      (c) have no marker yet (not already notified/awaiting/paid/skipped).

    Returns one EndedSessionMatch per (handler, matched event) pair.
    """
    now = now or datetime.now(_MADRID_OFFSET)
    out: list[EndedSessionMatch] = []

    for handler in handlers:
        try:
            handler_matches = handler.matches(events)
        except Exception as exc:
            log.error(
                f"[{getattr(handler, 'name', '?')}] matches() raised: {exc} — "
                f"skipping handler this tick"
            )
            continue

        for match in handler_matches:
            event = match.get("event") or {}
            end_dt = _event_end_dt(event)
            if end_dt is None:
                continue  # all-day event or malformed — ignore for end-detection

            if end_dt > now:
                continue  # session hasn't ended yet — future end, skip

            event_id = _event_id(event)
            if not event_id:
                log.warning(f"[{handler.name}] Matched event has no id — cannot mark, skipping")
                continue

            session_date = _event_end_date_iso(event)
            if markers.exists(event_id, session_date):
                continue  # already notified (or later) for this session

            out.append(
                EndedSessionMatch(
                    handler=handler,
                    match=match,
                    event=event,
                    event_id=event_id,
                    session_date=session_date,
                    end_dt=end_dt,
                )
            )

    return out


def select_newly_due_one_offs(handlers: list, now: datetime | None = None) -> list[EndedSessionMatch]:
    """
    Select one-off (due_date-triggered) payment obligations that are due
    today or overdue and have no marker yet.

    One-off invoices (profile.due_date set, no calendar_keywords /
    calendar_recurring_event_id / calendar_event_ids — see
    handlers/wise_transfer.py:WiseTransferHandler.matches) have no calendar
    event to attach an end-time to, so they cannot be selected by
    select_newly_ended_sessions (which requires a real event.end.dateTime).
    This is the parallel path for that trigger kind — same
    notify-email-then-sweep approval flow, keyed by a synthetic event_id
    (f"one_off:{profile.name}:{due_date}") so a marker still dedupes it
    across ticks and the token stays session-scoped (ateles#382 landed this
    trigger kind on main after this branch diverged; preserved here rather
    than dropped by the email-approval rebuild).
    """
    now = now or datetime.now(_MADRID_OFFSET)
    out: list[EndedSessionMatch] = []

    for handler in handlers:
        try:
            handler_matches = handler.matches([])
        except Exception as exc:
            log.error(
                f"[{getattr(handler, 'name', '?')}] matches() raised: {exc} — "
                f"skipping handler this tick"
            )
            continue

        for match in handler_matches:
            if match.get("trigger") != "due_date":
                continue
            due_date_str = str(match.get("due_date") or "")
            if not due_date_str:
                continue

            event_id = f"one_off:{handler.name}:{due_date_str}"
            if markers.exists(event_id, due_date_str):
                continue  # already notified (or later) for this invoice

            out.append(
                EndedSessionMatch(
                    handler=handler,
                    match=match,
                    event={},
                    event_id=event_id,
                    session_date=due_date_str,
                    end_dt=now,
                )
            )

    return out


# ---------------------------------------------------------------------------
# Neotoma: due-linked-task reminder (informational only — never triggers a
# payment by itself; see run()). Preserved from the pre-rebuild main() so a
# payment profile's linked task going overdue with no matching calendar/
# one-off trigger still surfaces to the operator instead of going silent.
# ---------------------------------------------------------------------------


def _require_base_url() -> str:
    """Return NEOTOMA_BASE_URL, or raise with a directly actionable message.

    Deliberately has NO localhost default: local hosting was retired
    2026-08-04, so a fallback would point every write at a dead port and fail
    silently rather than loudly.
    """
    if not NEOTOMA_BASE_URL:
        raise RuntimeError(
            "NEOTOMA_BASE_URL is not set. It must point at the Neotoma instance "
            "(e.g. https://neotoma.markmhendrickson.com). Local hosting was "
            "retired 2026-08-04 and http://localhost:9180 no longer serves "
            "anything, so there is deliberately no default: a silent fallback "
            "would send writes at a dead port. Under launchd the plist supplies "
            "this; for an ad-hoc run, export it or source "
            "~/.config/neotoma/.env first."
        )
    return NEOTOMA_BASE_URL.rstrip("/")


def _fetch_entity_by_id(entity_id: str) -> dict | None:
    """Fetch a single entity (with snapshot) by ID from Neotoma. None on error."""
    base_url = _require_base_url()
    is_loopback = "localhost" in base_url or "127.0.0.1" in base_url
    try:
        url = f"{base_url}/entities/{entity_id}"
        headers = {"Accept": "application/json"}
        if NEOTOMA_BEARER_TOKEN and not is_loopback:
            headers["Authorization"] = f"Bearer {NEOTOMA_BEARER_TOKEN}"
        req = urllib.request.Request(url, headers=headers)
        req.add_header("User-Agent", NEOTOMA_USER_AGENT)
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        log.warning(f"Neotoma entity fetch failed for {entity_id}: {exc}")
        return None


def fetch_due_payment_tasks(handlers: list | None = None) -> list[dict]:
    """
    Return the payment tasks that are due today or overdue, scoped STRICTLY to
    the tasks explicitly linked to active payment profiles via
    `profile.neotoma_task_id`.

    This is deliberately NOT a keyword/domain scan of the whole task corpus —
    that produced false positives (any finance-domain or BTC-mentioning task).
    Only tasks a payment profile actually points at are payment tasks.

    Returns a list of task dicts (each the raw entity with a 'snapshot').
    Falls back to empty list on any error or if no handlers/links exist.
    """
    if not NEOTOMA_BASE_URL:
        log.warning("NEOTOMA_BASE_URL not set — skipping task scan")
        return []

    if not handlers:
        log.info("No payment handlers — skipping linked-task scan.")
        return []

    today = date.today().isoformat()

    # Collect the canonical task IDs declared by active payment profiles.
    task_ids: list[str] = []
    for h in handlers:
        tid = getattr(getattr(h, "profile", None), "neotoma_task_id", "") or ""
        tid = tid.strip()
        if tid and tid not in task_ids:
            task_ids.append(tid)

    if not task_ids:
        log.info("No payment profiles declare a neotoma_task_id — no linked tasks.")
        return []

    def _fields(task: dict) -> dict:
        return task.get("snapshot") or task.get("fields") or task

    due_tasks: list[dict] = []
    for tid in task_ids:
        entity = _fetch_entity_by_id(tid)
        if not entity:
            continue
        fields = _fields(entity)
        due = str(fields.get("due_date") or "")
        if due and due[:10] <= today:
            due_tasks.append(entity)

    log.info(
        f"Neotoma linked-task scan: {len(task_ids)} profile task(s) checked, "
        f"{len(due_tasks)} due today or overdue"
    )
    for t in due_tasks:
        fields = _fields(t)
        log.debug(
            f"  Task: {fields.get('title') or fields.get('name')!r} due={fields.get('due_date')!r}"
        )

    return due_tasks


def build_due_task_reminder_email(due_tasks: list[dict]) -> tuple[str, str]:
    """Build (subject, html_body) for an informational reminder email.

    Sent when a payment profile's linked Neotoma task is due/overdue but no
    matching calendar session or one-off due_date fired this tick — e.g. the
    calendar event was never created. This sends NO approval token and
    triggers no payment; it is a signal that something may need the
    operator's attention, nothing more.
    """
    subject = f"Monedula: {len(due_tasks)} linked task(s) due with no matching trigger"
    items = []
    for t in due_tasks:
        fields = t.get("snapshot") or t.get("fields") or t
        name = str(fields.get("title") or fields.get("name") or "(unnamed task)")
        due = str(fields.get("due_date") or "")
        items.append(f"<li>{_html_escape(name)} — due {_html_escape(due)}</li>")
    body = f"""
<div style="font-family: -apple-system, sans-serif; font-size: 14px; color: #1a1a1a;">
  <p><b>Linked payment task(s) due, but no matching calendar session or
  one-off trigger fired this tick.</b></p>
  <ul>
    {''.join(items)}
  </ul>
  <p style="color:#666; font-size:12px;">
    Informational only — no payment was proposed or approved by this email.
  </p>
</div>
""".strip()
    return subject, body


# ---------------------------------------------------------------------------
# Email templates
# ---------------------------------------------------------------------------


def _html_escape(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _rail_details_html(profile) -> str:
    if profile.payment_type == "btc":
        addr = profile.btc_address or "(not configured)"
        addr_preview = (addr[:10] + "…" + addr[-6:]) if len(addr) > 20 else addr
        return f"<li><b>Rail:</b> Bitcoin &mdash; {_html_escape(addr_preview)}</li>"
    else:
        ref = profile.wise_reference or "(no reference)"
        return (
            f"<li><b>Rail:</b> Wise transfer &mdash; reference "
            f"{_html_escape(ref)}</li>"
        )


def build_notify_email(ended: EndedSessionMatch, token: str) -> tuple[str, str]:
    """Build (subject, html_body) for a per-session payment-approval email.

    `token` (from lib/approval/tokens.token_for) is embedded in the subject
    via subject_marker() — tokens.parse_verdict requires it to be echoed back
    in the (quoted) reply subject before a verdict counts, which is what
    stops a stale reply for a PRIOR session from re-approving this one.
    """
    profile = ended.handler.profile
    summary = ended.match.get("summary", profile.label)
    is_one_off = ended.match.get("trigger") == "due_date"
    end_local = ended.end_dt.astimezone(_MADRID_OFFSET).strftime("%Y-%m-%d %H:%M")

    subject = (
        f"Monedula: approve {profile.label} payment ({ended.session_date}) "
        f"{subject_marker(token)}"
    )

    # Reuse handler.preview() text as a fallback detail line so
    # handler-specific preview logic (masked IBAN/address, task id) is not
    # duplicated here.
    try:
        preview_text = ended.handler.preview(ended.match)
    except Exception as exc:
        log.warning(f"[{profile.name}] preview() raised while building email: {exc}")
        preview_text = ""

    if is_one_off:
        # A one-off invoice has no session to attend — asking for ATTENDED
        # would be nonsensical. APPROVE/SKIP is the right vocabulary here;
        # _parse_attendance_verdict accepts plain APPROVE for a one_off:
        # marker (see its docstring).
        headline = f"{_html_escape(profile.label)} invoice due."
        instruction = (
            "Reply APPROVE to send payment, or SKIP to skip (due date rolls "
            "forward)."
        )
        footer = "Only an explicit APPROVE reply triggers payment."
        row_label, row_value = "Due:", end_local
    else:
        headline = f"{_html_escape(profile.label)} session ended."
        instruction = (
            "Reply ATTENDED to approve payment (this confirms you attended), "
            "or SKIP to skip (no payment, due date rolls forward)."
        )
        footer = (
            "This email is the attendance confirmation for this session — a "
            "calendar entry alone is not proof of attendance, so a bare "
            '"yes" is not accepted. Only an explicit ATTENDED reply triggers '
            "payment."
        )
        row_label, row_value = "Ended:", end_local

    body = f"""
<div style="font-family: -apple-system, sans-serif; font-size: 14px; color: #1a1a1a;">
  <p><b>{headline}</b></p>
  <ul>
    <li><b>Amount:</b> &euro;{profile.amount_eur}</li>
    {_rail_details_html(profile)}
    <li><b>Session:</b> {_html_escape(summary)}</li>
    <li><b>{row_label}</b> {_html_escape(row_value)} (Europe/Madrid)</li>
    <li><b>Neotoma task:</b> {_html_escape(profile.neotoma_task_id or '(unlinked)')}</li>
  </ul>
  <pre style="background:#f5f5f5; padding:8px; white-space:pre-wrap;">{_html_escape(preview_text)}</pre>
  <p><b>{instruction}</b></p>
  <p style="color:#666; font-size:12px;">
    {footer}
  </p>
</div>
""".strip()

    return subject, body


def build_confirmation_email(profile, result: dict, confirmation_text: str = "") -> tuple[str, str]:
    """Build (subject, html_body) for a post-payment confirmation email."""
    subject = f"Monedula: {profile.label} payment confirmation"

    status = result.get("status")
    if status == "sent":
        detail = "<p style=\"color:#0a7a2f;\"><b>Payment sent.</b></p>"
    elif status == "manual_required":
        detail = "<p style=\"color:#b36b00;\"><b>Manual action required.</b></p>"
    else:
        detail = "<p style=\"color:#b00020;\"><b>Payment failed.</b></p>"

    extra_block = ""
    if confirmation_text:
        extra_block = (
            f'<pre style="background:#f5f5f5; padding:8px; white-space:pre-wrap;">'
            f"{_html_escape(confirmation_text)}</pre>"
        )

    body = f"""
<div style="font-family: -apple-system, sans-serif; font-size: 14px; color: #1a1a1a;">
  {detail}
  <p>{_html_escape(profile.label)} &mdash; &euro;{profile.amount_eur}</p>
  {extra_block}
  <pre style="background:#f5f5f5; padding:8px; white-space:pre-wrap;">{_html_escape(json.dumps(result, indent=2, default=str))}</pre>
</div>
""".strip()

    return subject, body


def build_skip_email(profile, session_date: str) -> tuple[str, str]:
    subject = f"Monedula: {profile.label} skipped for {session_date}"
    body = f"""
<div style="font-family: -apple-system, sans-serif; font-size: 14px; color: #1a1a1a;">
  <p>No payment made for {_html_escape(profile.label)} ({session_date}) — marked skipped.</p>
  <p>Due date has been rolled forward to the next session.</p>
</div>
""".strip()
    return subject, body


# ---------------------------------------------------------------------------
# Neotoma task update helpers (roll due_date only — never mark complete)
# ---------------------------------------------------------------------------


def _find_next_session_due_date(profile) -> str | None:
    """
    Search Google Calendar for the next event matching this profile's
    keywords, returning (next event date + 1 day) as an ISO date string.
    Mirrors the existing per-handler helper so due_date rolls consistently
    whether execute() already rolled it (Wise/BTC handlers do this
    internally on send) or monedula.py needs to roll it itself (skip path).
    """
    import shutil

    gws = shutil.which("gws")
    if not gws:
        log.warning(f"[{profile.name}] gws CLI not found — cannot look up next event date")
        return None

    today = date.today()
    time_min = today.strftime("%Y-%m-%dT00:00:00+02:00")
    time_max = (today + timedelta(days=92)).strftime("%Y-%m-%dT23:59:59+02:00")

    for query in profile.calendar_keywords:
        params = {
            "calendarId": "primary",
            "singleEvents": True,
            "orderBy": "startTime",
            "q": query,
            "timeMin": time_min,
            "timeMax": time_max,
        }
        try:
            result = subprocess.run(
                [gws, "calendar", "events", "list", "--params", json.dumps(params)],
                capture_output=True,
                text=True,
                timeout=30,
                env=os.environ,
            )
            if result.returncode != 0:
                continue
            data = json.loads(result.stdout)
            for item in data.get("items") or []:
                summary_low = (item.get("summary") or "").lower()
                if any(kw in summary_low for kw in profile.calendar_keywords):
                    start = item.get("start", {})
                    event_date_str = start.get("date") or start.get("dateTime", "")[:10]
                    if event_date_str:
                        event_date = date.fromisoformat(event_date_str)
                        due = event_date + timedelta(days=1)
                        return due.isoformat()
        except Exception as exc:
            log.warning(f"[{profile.name}] Calendar search error (query={query!r}): {exc}")

    return None


def roll_due_date(profile, reason: str) -> None:
    """
    Roll the linked Neotoma task's due_date to the next matching session.
    NEVER sets status to done/completed — yoga/therapy tasks are recurring
    obligations, not one-off tasks (see project CLAUDE.md standing rule).
    """
    import shutil

    task_id = profile.neotoma_task_id
    if not task_id:
        log.warning(f"[{profile.name}] No neotoma_task_id configured — cannot roll due_date")
        return

    neotoma = shutil.which("neotoma")
    if not neotoma:
        log.warning(f"[{profile.name}] neotoma CLI not found — cannot roll due_date")
        return

    next_due = _find_next_session_due_date(profile)
    if not next_due:
        log.warning(
            f"[{profile.name}] Could not find next event date — due_date not rolled ({reason})"
        )
        return

    try:
        res = subprocess.run(
            [neotoma, "--api-only", "entities", "update", task_id, "--due-date", next_due],
            capture_output=True,
            text=True,
            timeout=30,
            env=os.environ,
        )
        if res.returncode != 0:
            log.warning(f"[{profile.name}] due_date roll failed: {res.stderr.strip()[:200]}")
        else:
            log.info(f"[{profile.name}] due_date rolled to {next_due} ({reason})")
    except Exception as exc:
        log.warning(f"[{profile.name}] due_date roll error: {exc}")


# ---------------------------------------------------------------------------
# Pass 1: notify newly-ended sessions
# ---------------------------------------------------------------------------


def notify_ended_sessions(ended_sessions: list[EndedSessionMatch]) -> None:
    # Hard backstop: with a wide LOOKBACK_HOURS, an empty/reset marker file
    # could otherwise queue a large burst of emails in one tick. Cap it and
    # log loudly if we hit the cap so the operator notices rather than the
    # daemon silently spamming or silently dropping.
    if len(ended_sessions) > MAX_NOTIFY_PER_RUN:
        log.warning(
            f"{len(ended_sessions)} ended sessions to notify exceeds "
            f"MAX_NOTIFY_PER_RUN={MAX_NOTIFY_PER_RUN}; sending the first "
            f"{MAX_NOTIFY_PER_RUN} this tick, the rest next tick. If this is "
            f"unexpected, the marker file may have been reset."
        )
        _notify(
            f"monedula: {len(ended_sessions)} sessions pending notify — capped "
            f"at {MAX_NOTIFY_PER_RUN}/tick; check marker state",
            priority="blocker",
        )
        ended_sessions = ended_sessions[:MAX_NOTIFY_PER_RUN]

    for ended in ended_sessions:
        profile = ended.handler.profile

        # Session-scoped token: the operator's reply must echo this token
        # (via the quoted subject) before it counts as a verdict for THIS
        # session specifically — see tokens.token_for docstring for the
        # stale-reply-re-approves-the-next-session failure this closes.
        token = token_for(ended.event_id, session=ended.session_date)
        subject, body = build_notify_email(ended, token=token)

        log.info(f"[{profile.name}] Sending notify email for session {ended.session_date}...")
        sent = email_channel.send_request(subject, body, to=OPERATOR_EMAIL)

        if not sent:
            log.error(
                f"[{profile.name}] Notify email for session {ended.session_date} "
                f"failed to send — will retry next tick (no marker written)."
            )
            continue

        marker = markers.Marker(
            event_id=ended.event_id,
            date=ended.session_date,
            profile_name=profile.name,
            token=token,
            notified_at=datetime.now(timezone.utc).isoformat(),
            status="awaiting_approval",
            # Persist the match dict (incl. raw calendar event) so the
            # reply sweep — which runs on a LATER tick, after this tick's
            # `events` list is gone — can still call handler.execute(match)
            # without re-deriving it from a fresh (possibly different)
            # calendar fetch.
            extra={"match": ended.match},
        )
        markers.save(marker)
        markers.mirror_to_neotoma(marker)
        log.info(
            f"[{profile.name}] Notified + marker written for session "
            f"{ended.session_date} (token={marker.token})"
        )


# ---------------------------------------------------------------------------
# Pass 2: reply sweep (approve/skip pending markers)
# ---------------------------------------------------------------------------

# Attendance-asserting words this daemon accepts as the FIRST word of an
# approving reply. A calendar event is NOT proof of attendance — this is the
# same gate main's Telegram-era _parse_reply enforced (locked in by
# test_parse_reply.py), preserved here rather than relaxed to bare
# "APPROVE"/"YES" now that lib/approval.tokens.parse_verdict owns token
# matching. Do not add "APPROVE"/"YES" to this set without an operator
# decision — see the PR comment for why.
_ATTENDANCE_WORDS = ("ATTENDED", "PAID")


def _parse_attendance_verdict(
    text: str, token: str, require_attendance: bool = True
) -> bool | None:
    """Return True (approve), False (skip), or None (no verdict).

    Layers Monedula's attendance-gate requirement on top of
    lib/approval.tokens.parse_verdict's token-scoped SKIP/APPROVE parsing:
    parse_verdict alone accepts a bare "YES" as approval, which main's
    pre-existing Telegram-era gate deliberately rejected for a CALENDAR
    SESSION (a calendar event is not proof of attendance). So when
    require_attendance is True (the default — recurring, attendance-gated
    sessions), a verdict only counts as APPROVE when the operator's own
    (unquoted) first line starts with an attendance word ("attended"/"paid").
    require_attendance=False is for one-off invoices (see
    select_newly_due_one_offs) — there is no session to attend, so plain
    APPROVE/YES from parse_verdict is accepted as-is. SKIP is unaffected
    either way: any of parse_verdict's SKIP forms skip.
    """
    verdict = parse_verdict(text, token)
    if verdict is not True:
        return verdict  # False (skip) or None (no verdict) pass through as-is

    if not require_attendance:
        return True

    # verdict is True (parse_verdict saw APPROVE/YES) — additionally require
    # the operator's own first line to assert attendance explicitly.
    up = text.upper()
    body = up
    for marker in ("\nON ", "\n-----ORIGINAL", "\n________"):
        idx = body.find(marker)
        if idx > 0:
            body = body[:idx]
            break
    for line in body.splitlines():
        s = line.strip().rstrip(".!")
        if not s or s.startswith(">") or s.startswith("RE:") or s.startswith("[ATELES]"):
            continue
        first = s.split()[0] if s.split() else ""
        if first in _ATTENDANCE_WORDS:
            return True
        break  # first substantive line decides; a bare YES here is not enough
    log.info(
        "Reply carried a recognised token and an APPROVE/YES verdict, but no "
        "attendance-asserting word (attended/paid) on the first line — "
        "treating as no verdict (attendance-gated, not a bare yes/no)."
    )
    return None


def _handler_by_profile_name(handlers: list, profile_name: str):
    for h in handlers:
        if h.name == profile_name:
            return h
    return None


def sweep_pending_approvals(handlers: list) -> None:
    pending = markers.pending_awaiting_approval()
    if not pending:
        return

    # One read_replies call per pending marker's token: token_for is
    # session-scoped, so each pending session has a distinct token and reply
    # matching stays precise even with several sessions awaiting approval at
    # once. `on_reply_message` persists the replying message id onto the
    # marker so the confirmation can reply IN THAT THREAD.
    for marker in pending:
        handler = _handler_by_profile_name(handlers, marker.profile_name)
        if handler is None:
            log.warning(
                f"No active handler for profile {marker.profile_name!r} "
                f"(marker {marker.key}) — leaving pending."
            )
            continue

        if not marker.token:
            log.warning(f"Marker {marker.key} has no token — cannot sweep, leaving pending.")
            continue

        seen_reply_id: dict[str, str] = {}

        def _on_reply(tok: str, mid: str, _store: dict = seen_reply_id) -> None:
            _store["id"] = mid

        try:
            replies = email_channel.read_replies(
                [marker.token], on_reply_message=_on_reply
            )
        except Exception as exc:
            # Fail-safe: any error in fetch/parse must NOT execute a payment.
            log.error(
                f"[{marker.profile_name}] Reply fetch error for {marker.key}: {exc} "
                f"— leaving pending."
            )
            continue

        if not replies:
            continue  # no reply yet — leave awaiting_approval

        if seen_reply_id.get("id"):
            markers.update_status(
                marker.event_id, marker.date, marker.status,
                reply_message_id=seen_reply_id["id"],
            )
            marker.reply_message_id = seen_reply_id["id"]

        # Most recent matching reply decides, mirroring read_replies' ordering.
        reply_text = replies[-1]
        is_one_off = marker.event_id.startswith("one_off:")
        decision = _parse_attendance_verdict(
            reply_text, marker.token, require_attendance=not is_one_off
        )
        if decision is None:
            log.info(
                f"[{marker.profile_name}] Reply for {marker.key} not recognised as "
                f"an attendance-asserting approve/skip — leaving pending."
            )
            continue

        if decision is False:
            _handle_skip(handler, marker)
        elif decision is True:
            _handle_approve(handler, marker)


def _send_confirmation(marker: "markers.Marker", subject: str, body: str) -> None:
    """Send a confirmation/skip email, in-thread when we have a reply to
    reply to, otherwise as a standalone notification. Best-effort — a failure
    here must never roll back a decision already recorded in the marker."""
    if marker.reply_message_id:
        ok = email_channel.reply_in_thread(marker.reply_message_id, body)
        if ok:
            return
        log.warning(
            f"[{marker.profile_name}] in-thread confirmation failed for "
            f"{marker.key} — falling back to a standalone email."
        )
    email_channel.send_request(subject, body, to=OPERATOR_EMAIL)


def _handle_skip(handler, marker: "markers.Marker") -> None:
    profile = handler.profile
    log.info(f"[{profile.name}] Reply=skip for {marker.key} — rolling due_date, no payment.")
    markers.update_status(marker.event_id, marker.date, "skipped")
    roll_due_date(profile, reason=f"skipped session {marker.date}")

    subject, body = build_skip_email(profile, marker.date)
    _send_confirmation(marker, subject, body)


def _handle_approve(handler, marker: "markers.Marker") -> None:
    profile = handler.profile

    # Idempotency: never double-pay. Re-read the marker fresh and confirm
    # it is still awaiting_approval immediately before executing — guards
    # against a race with another tick/process.
    current = markers.get(marker.event_id, marker.date)
    if current is None or current.status != "awaiting_approval":
        log.warning(
            f"[{profile.name}] Marker {marker.key} status is "
            f"{getattr(current, 'status', 'MISSING')!r}, not awaiting_approval "
            f"— refusing to execute (idempotency guard)."
        )
        return

    log.info(f"[{profile.name}] Reply=approve for {marker.key} — executing payment.")
    # Mark 'approved' before execute() so a crash mid-execute does not leave
    # the marker looking re-approvable; execute() itself may still fail, in
    # which case we do not advance to 'paid'.
    markers.update_status(marker.event_id, marker.date, "approved")

    match = marker.extra.get("match") or {"event": {}, "summary": profile.label}

    _job = _activity.started(f"executing {profile.name} payment") if _activity else None
    try:
        result = handler.execute(match)
    except Exception as exc:
        log.error(f"[{profile.name}] execute() raised for {marker.key}: {exc}")
        if _job:
            _job.failed(f"{profile.name} payment error: {type(exc).__name__}")
        # Fail-safe: leave marker at 'approved' (not paid) — a human must
        # review; do NOT roll back to awaiting_approval (that could invite
        # a second, possibly divergent, execute() on the next tick).
        _notify(
            f"monedula: {profile.name} payment execute() failed — needs manual review",
            priority="blocker",
        )
        return

    if _job:
        _job.finished(f"{profile.name} payment executed")

    status = result.get("status")
    markers.update_status(
        marker.event_id,
        marker.date,
        "paid" if status == "sent" else "approved",
        result_status=status,
    )

    if hasattr(handler, "format_confirmation"):
        try:
            confirmation_text = handler.format_confirmation(result)
        except Exception:
            confirmation_text = json.dumps(result, default=str)
    else:
        confirmation_text = json.dumps(result, default=str)

    subject, body = build_confirmation_email(profile, result, confirmation_text=confirmation_text)
    _send_confirmation(marker, subject, body)

    # Wise/BTC handlers already roll due_date internally on a successful
    # send (see handlers/*.py _update_task). Only roll here if execute()
    # did NOT report success, to avoid a double-roll on the happy path.
    if status != "sent":
        roll_due_date(profile, reason=f"payment not confirmed sent ({status}) for {marker.date}")


def escalate_strandings(strandings: list) -> list:
    """Escalate active profiles the daemon cannot act on. Returns those filed.

    Thin seam over ``strandings.escalate`` so the daemon's own module owns the
    notifier wiring and a failure here can never take the payment run down —
    an escalation that raises would turn a reporting defect into an outage.
    """
    if not strandings:
        return []
    try:
        from strandings import escalate

        return escalate(strandings, notify=_notify)
    except Exception as exc:  # noqa: BLE001
        log.exception(f"could not escalate stranded payment profiles: {exc}")
        return []


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def run() -> bool:
    """Run one Monedula tick. Returns True for a clean run.

    Returns False when an ACTIVE payment profile was stranded — i.e. a
    payment that should have been possible was not (see escalate_strandings,
    ateles#553/#599). The entrypoint turns that into a non-zero exit, so a
    run that could not pay no longer looks like a clean run.
    """
    log.info("Monedula tick starting.")

    from handlers import load_handlers

    # Collect every ACTIVE profile the loader could not act on and escalate
    # before any early return below — a stranded profile is an operational
    # defect regardless of whether the rest of the tick had anything to do.
    strandings: list = []
    all_handlers = load_handlers(strandings)
    escalate_strandings(strandings)

    if not all_handlers:
        log.info("No active payment handlers — nothing to do this tick.")
        return not strandings

    # Fail safe if the operator address is unconfigured: without it, notify
    # would send approval emails to an empty recipient (or a reply sweep would
    # have nothing to watch). Skip and page rather than mis-send.
    if not OPERATOR_EMAIL:
        log.error(
            "OPERATOR_EMAIL / MONEDULA_OPERATOR_EMAIL not set — cannot send "
            "approval emails; skipping tick."
        )
        _notify(
            "monedula: OPERATOR_EMAIL unset — approval emails cannot be sent",
            priority="blocker",
        )
        return not strandings

    if not email_channel.email_enabled():
        log.error(
            "ATELES_NOTIFY_EMAIL not set to '1' — email approval channel is "
            "disarmed; skipping tick (no Telegram fallback exists any more)."
        )
        _notify(
            "monedula: ATELES_NOTIFY_EMAIL disarmed — approval emails cannot "
            "be sent",
            priority="blocker",
        )
        return not strandings

    now = datetime.now(_MADRID_OFFSET)
    events = fetch_recent_events(now=now)

    ended_sessions = select_newly_ended_sessions(events, all_handlers, now=now)
    # One-off (due_date-triggered) invoices are evaluated every tick,
    # independent of the calendar fetch above — see select_newly_due_one_offs
    # (ateles#382/#417: a one-off must not wait on the calendar leg).
    due_one_offs = select_newly_due_one_offs(all_handlers, now=now)
    newly_triggered = ended_sessions + due_one_offs
    if newly_triggered:
        log.info(
            f"Newly-triggered payments this tick: "
            f"{[(e.handler.name, e.session_date) for e in newly_triggered]}"
        )
        notify_ended_sessions(newly_triggered)
    else:
        log.info("No newly-triggered payments this tick.")

    sweep_pending_approvals(all_handlers)

    # Informational-only: a linked task due/overdue with no matching trigger
    # this tick (e.g. the calendar event was never created) still deserves a
    # signal, even though nothing here proposes or approves a payment.
    # KNOWN LIMITATION: unlike the once-daily cadence this had before the
    # email-approval rebuild, this now re-fires on every ~15-min tick the
    # condition persists (no marker/dedup wired for this informational path)
    # — noisier, not lossier. Worth a follow-up if it proves too chatty.
    try:
        due_tasks = fetch_due_payment_tasks(all_handlers)
    except Exception as exc:
        log.warning(f"due-task reminder scan failed (non-fatal): {exc}")
        due_tasks = []
    if due_tasks:
        subject, body = build_due_task_reminder_email(due_tasks)
        email_channel.send_request(subject, body, to=OPERATOR_EMAIL)

    log.info("Monedula tick complete.")
    return not strandings


def main() -> bool:
    return run()


if __name__ == "__main__":
    _notify("monedula tick started", priority="info")
    try:
        clean = main()
        if clean:
            _notify("monedula tick complete", priority="info")
        else:
            # A stranded profile means a payment did not happen. Exiting 0
            # here is what let stranded profiles read as clean runs before
            # ateles#553/#599.
            log.error(
                "Monedula tick completed with STRANDED payment profiles — "
                "see the escalations filed in Neotoma."
            )
            sys.exit(1)
    except Exception as exc:
        log.exception(f"Monedula fatal error: {exc}")
        _notify(f"monedula fatal error: {exc}", priority="blocker")
        sys.exit(1)
