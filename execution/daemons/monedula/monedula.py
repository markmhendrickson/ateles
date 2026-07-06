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

Email (via `gws gmail`, see gmail_channel.py) is the ONLY operator-facing
channel for payment previews, approvals, and confirmations. Telegram has
been removed from this path entirely — lib/notify + activity-log calls may
remain for daemon-health/error pings only.

Usage:
  python3 monedula.py
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
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

import gmail_channel  # noqa: E402
import markers  # noqa: E402

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
LOG_DIR = Path.home() / "Library" / "Logs" / "ateles"
LOG_FILE = LOG_DIR / "monedula.log"

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

LOG_DIR.mkdir(parents=True, exist_ok=True)


class _FlushingFileHandler(logging.FileHandler):
    def emit(self, record: logging.LogRecord) -> None:
        super().emit(record)
        self.flush()


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [monedula] %(levelname)s %(message)s",
    handlers=[
        _FlushingFileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)


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


def build_notify_email(ended: EndedSessionMatch) -> tuple[str, str]:
    """Build (subject, html_body) for a per-session payment-approval email."""
    profile = ended.handler.profile
    summary = ended.match.get("summary", profile.label)
    end_local = ended.end_dt.astimezone(_MADRID_OFFSET).strftime("%Y-%m-%d %H:%M")

    subject = f"Monedula: approve {profile.label} payment ({ended.session_date})"

    # Reuse handler.preview() text as a fallback detail line so
    # handler-specific preview logic (masked IBAN/address, task id) is not
    # duplicated here.
    try:
        preview_text = ended.handler.preview(ended.match)
    except Exception as exc:
        log.warning(f"[{profile.name}] preview() raised while building email: {exc}")
        preview_text = ""

    body = f"""
<div style="font-family: -apple-system, sans-serif; font-size: 14px; color: #1a1a1a;">
  <p><b>{_html_escape(profile.label)} session ended.</b></p>
  <ul>
    <li><b>Amount:</b> &euro;{profile.amount_eur}</li>
    {_rail_details_html(profile)}
    <li><b>Session:</b> {_html_escape(summary)}</li>
    <li><b>Ended:</b> {_html_escape(end_local)} (Europe/Madrid)</li>
    <li><b>Neotoma task:</b> {_html_escape(profile.neotoma_task_id or '(unlinked)')}</li>
  </ul>
  <pre style="background:#f5f5f5; padding:8px; white-space:pre-wrap;">{_html_escape(preview_text)}</pre>
  <p><b>Reply YES to approve payment (this confirms you attended), or NO to skip
  (no payment, due date rolls forward).</b></p>
  <p style="color:#666; font-size:12px;">
    This email is the attendance confirmation for this session — a calendar
    entry alone is not proof of attendance. Only an explicit YES triggers
    payment.
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
        subject, body = build_notify_email(ended)

        log.info(f"[{profile.name}] Sending notify email for session {ended.session_date}...")
        send_result = gmail_channel.send_email(OPERATOR_EMAIL, subject, body)

        # Require BOTH ids: the sweep needs a thread id AND a message id to
        # find the operator's reply. A marker written without a thread id is
        # skipped by every future sweep → the session sits awaiting_approval
        # forever (never paid, never re-notified since the marker exists).
        # Treat a missing thread id like a send failure: no marker, retry next
        # tick.
        if not send_result or not send_result.get("id") or not send_result.get("threadId"):
            log.error(
                f"[{profile.name}] Notify email for session {ended.session_date} "
                f"returned incomplete ids (id={bool(send_result and send_result.get('id'))}, "
                f"threadId={bool(send_result and send_result.get('threadId'))}) — "
                f"will retry next tick (no marker written)."
            )
            continue

        marker = markers.Marker(
            event_id=ended.event_id,
            date=ended.session_date,
            profile_name=profile.name,
            gmail_thread_id=send_result.get("threadId", ""),
            gmail_message_id=send_result.get("id", ""),
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
            f"{ended.session_date} (thread={marker.gmail_thread_id})"
        )


# ---------------------------------------------------------------------------
# Pass 2: reply sweep (approve/skip pending markers)
# ---------------------------------------------------------------------------


def _handler_by_profile_name(handlers: list, profile_name: str):
    for h in handlers:
        if h.name == profile_name:
            return h
    return None


def sweep_pending_approvals(handlers: list) -> None:
    pending = markers.pending_awaiting_approval()
    if not pending:
        return

    for marker in pending:
        handler = _handler_by_profile_name(handlers, marker.profile_name)
        if handler is None:
            log.warning(
                f"No active handler for profile {marker.profile_name!r} "
                f"(marker {marker.key}) — leaving pending."
            )
            continue

        if not marker.gmail_thread_id or not marker.gmail_message_id:
            log.warning(f"Marker {marker.key} missing Gmail ids — cannot sweep, leaving pending.")
            continue

        try:
            reply_text = gmail_channel.find_operator_reply(
                marker.gmail_thread_id, marker.gmail_message_id
            )
        except Exception as exc:
            # Fail-safe: any error in fetch/parse must NOT execute a payment.
            log.error(
                f"[{marker.profile_name}] Reply fetch error for {marker.key}: {exc} "
                f"— leaving pending."
            )
            continue

        if reply_text is None:
            continue  # no reply yet — leave awaiting_approval

        decision = gmail_channel.parse_approval_reply(reply_text)
        if decision is None:
            log.info(
                f"[{marker.profile_name}] Reply for {marker.key} not recognised as "
                f"yes/no — leaving pending."
            )
            continue

        if decision == "skip":
            _handle_skip(handler, marker)
        elif decision == "approve":
            _handle_approve(handler, marker)


def _handle_skip(handler, marker: "markers.Marker") -> None:
    profile = handler.profile
    log.info(f"[{profile.name}] Reply=skip for {marker.key} — rolling due_date, no payment.")
    markers.update_status(marker.event_id, marker.date, "skipped")
    roll_due_date(profile, reason=f"skipped session {marker.date}")

    subject, body = build_skip_email(profile, marker.date)
    gmail_channel.send_email(OPERATOR_EMAIL, subject, body)


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
    gmail_channel.send_email(OPERATOR_EMAIL, subject, body)

    # Wise/BTC handlers already roll due_date internally on a successful
    # send (see handlers/*.py _update_task). Only roll here if execute()
    # did NOT report success, to avoid a double-roll on the happy path.
    if status != "sent":
        roll_due_date(profile, reason=f"payment not confirmed sent ({status}) for {marker.date}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def run() -> None:
    log.info("Monedula tick starting.")

    from handlers import load_handlers

    all_handlers = load_handlers()
    if not all_handlers:
        log.info("No active payment handlers — nothing to do this tick.")
        return

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
        return

    now = datetime.now(_MADRID_OFFSET)
    events = fetch_recent_events(now=now)

    ended_sessions = select_newly_ended_sessions(events, all_handlers, now=now)
    if ended_sessions:
        log.info(
            f"Newly-ended sessions this tick: "
            f"{[(e.handler.name, e.session_date) for e in ended_sessions]}"
        )
        notify_ended_sessions(ended_sessions)
    else:
        log.info("No newly-ended matched sessions this tick.")

    sweep_pending_approvals(all_handlers)

    log.info("Monedula tick complete.")


def main() -> None:
    run()


if __name__ == "__main__":
    _notify("monedula tick started", priority="info")
    try:
        main()
        _notify("monedula tick complete", priority="info")
    except Exception as exc:
        log.exception(f"Monedula fatal error: {exc}")
        _notify(f"monedula fatal error: {exc}", priority="blocker")
        sys.exit(1)
