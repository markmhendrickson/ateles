#!/usr/bin/env python3
"""
Monedula — Daily Payments Daemon
Named after Corvus monedula (jackdaw — moneta = money).

Runs once per day via launchd StartCalendarInterval.
Checks Google Calendar for yesterday's sessions that trigger payment obligations,
sends a Telegram preview, waits for operator approval, executes payments, and
sends a Telegram confirmation.

Usage:
  python3 monedula.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta
from pathlib import Path

# Cloudflare fronts the hosted Neotoma instance and blocks urllib's default
# User-Agent with a 1010 "browser signature" 403. Any explicit UA passes.
NEOTOMA_USER_AGENT = "ateles-neotoma-sync/1.0"

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
# lib/notify integration (path bootstrap required before import)
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from lib.daemon_runtime.logging_setup import configure_daemon_logging

try:
    from lib.notify import Notifier  # noqa: E402

    _notifier: Notifier | None = Notifier.from_neotoma()
except Exception:  # lib unavailable or Neotoma unreachable at import time
    _notifier = None


def _notify(message: str, priority: str = "info") -> None:
    """Send via lib/notify if available; silently skip if not."""
    if _notifier is None:
        return
    try:
        from lib.notify import Priority

        p = getattr(Priority, priority.upper(), Priority.INFO)
        _notifier.send(message, priority=p, handler="monedula")
    except Exception:
        pass


# Activity-log channel (CyphorhinusBot observation feed).
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
STATE_FILE = Path(__file__).parent / ".monedula_last_run"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
TELEGRAM_ALLOWED_USER_ID = os.environ.get("TELEGRAM_ALLOWED_USER_ID", "")
# TELEGRAM_TOPIC_MONEDULA is the thread ID for Monedula notifications.
# Legacy alias: TELEGRAM_TOPIC_PAYMENTS is also accepted for backwards compatibility.
TELEGRAM_TOPIC_MONEDULA = os.environ.get(
    "TELEGRAM_TOPIC_MONEDULA", ""
) or os.environ.get("TELEGRAM_TOPIC_PAYMENTS", "")
NEOTOMA_BEARER_TOKEN = os.environ.get("NEOTOMA_BEARER_TOKEN", "")
NEOTOMA_BASE_URL = os.environ.get("NEOTOMA_BASE_URL", "")

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOG_DIR.mkdir(parents=True, exist_ok=True)


# Rotating + repeat-suppressing (lib/daemon_runtime/logging_setup.py):
# unbounded retry logging filled a 926 GB disk on 2026-08-18.
log = configure_daemon_logging("monedula", also_stdout=True)

# ---------------------------------------------------------------------------
# Idempotency guard
# ---------------------------------------------------------------------------


def _require_base_url() -> str:
    """Return NEOTOMA_BASE_URL, or raise with a directly actionable message.

    Deliberately has NO localhost default: local hosting was retired
    2026-08-04, so a fallback would point every write at a dead port and fail
    silently rather than loudly.
    """
    if not NEOTOMA_BASE_URL:
        raise RuntimeError(
            'NEOTOMA_BASE_URL is not set. It must point at the Neotoma instance (e.g. https://neotoma.markmhendrickson.com). Local hosting was retired 2026-08-04 and http://localhost:9180 no longer serves anything, so there is deliberately no default: a silent fallback would send writes at a dead port. Under launchd the plist supplies this; for an ad-hoc run, export it or source ~/.config/neotoma/.env first.'
        )
    return NEOTOMA_BASE_URL.rstrip("/")


def _check_already_ran_today() -> bool:
    """Return True if this daemon already ran today (idempotency guard)."""
    if STATE_FILE.exists():
        contents = STATE_FILE.read_text().strip()
        if contents == date.today().isoformat():
            return True
    return False


def _mark_ran_today() -> None:
    STATE_FILE.write_text(date.today().isoformat())


def _clear_run_state() -> None:
    if STATE_FILE.exists():
        STATE_FILE.unlink()


# ---------------------------------------------------------------------------
# Calendar: fetch yesterday's events
# ---------------------------------------------------------------------------


def _yesterday() -> date:
    return date.today() - timedelta(days=1)


def fetch_yesterday_events() -> list[dict] | None:
    """
    Use gws CLI to fetch all calendar events for yesterday.
    Returns list of event dicts (each with at least 'summary').

    Returns None on FAILURE, which is distinct from [] meaning "yesterday had
    no events". Callers must not treat a failed fetch as an empty day: the
    calendar leg runs at most once per day, so a failure that reads as "no
    sessions" silently skips a real payment obligation (this is what caused
    the 2026-07-30 therapy session to go unnotified and unpaid).
    """
    import shutil

    gws = shutil.which("gws")
    if not gws:
        log.error("gws CLI not found in PATH — cannot check calendar")
        return None

    yest = _yesterday()
    time_min = yest.strftime("%Y-%m-%dT00:00:00+02:00")
    time_max = yest.strftime("%Y-%m-%dT23:59:59+02:00")

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
            return None

        data = json.loads(result.stdout)
        items = data.get("items") or []
        log.info(f"Fetched {len(items)} calendar event(s) for {yest.isoformat()}")
        for item in items:
            log.debug(f"  Event: {item.get('summary', '(no title)')!r}")
        return items

    except json.JSONDecodeError as exc:
        log.error(f"Failed to parse gws output: {exc}")
        return None
    except Exception as exc:
        log.error(f"Calendar fetch error: {exc}")
        return None


# ---------------------------------------------------------------------------
# Neotoma: fetch due payment tasks
# ---------------------------------------------------------------------------


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


def _task_to_preview_item(task: dict) -> dict:
    """
    Convert a Neotoma task entity into a generic preview item dict
    compatible with the preview builder.
    """
    fields = task.get("snapshot") or task.get("fields") or task
    name = str(fields.get("title") or fields.get("name") or "(unnamed task)")
    due = str(fields.get("due_date") or "")
    description = str(fields.get("description") or "")
    entity_id = task.get("entity_id") or task.get("id") or ""
    return {
        "source": "task",
        "name": name,
        "due_date": due,
        "description": description,
        "entity_id": entity_id,
    }


# ---------------------------------------------------------------------------
# Telegram helpers
# ---------------------------------------------------------------------------


def telegram_send(text: str) -> None:
    """
    Send a Telegram message via the shared Node.js send.mjs helper,
    falling back to telegram-send CLI.
    """
    import shutil

    node = shutil.which("node")
    send_script = PROJECT_ROOT / "execution" / "lib" / "telegram" / "send.mjs"
    if node and send_script.exists():
        try:
            args = [node, str(send_script), "--text", text]
            if TELEGRAM_TOPIC_MONEDULA:
                args += ["--thread-id", TELEGRAM_TOPIC_MONEDULA]
            subprocess.run(args, timeout=15, capture_output=True, env=os.environ)
            return
        except Exception as exc:
            log.warning(f"send.mjs failed: {exc}, trying fallback")

    telegram_cmd = shutil.which("telegram-send")
    if telegram_cmd:
        try:
            subprocess.run(
                [telegram_cmd, text], timeout=15, capture_output=True, env=os.environ
            )
        except Exception as exc:
            log.warning(f"telegram-send fallback failed: {exc}")


def telegram_long_poll_once(timeout_sec: int = 120) -> str | None:
    """
    Long-poll Telegram getUpdates for one incoming message from the allowed user
    in the correct chat.

    Returns the message text (stripped) if a matching message arrives within
    timeout_sec, or None on timeout.

    Uses a file-based offset tracker to avoid reprocessing old messages.
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set — cannot poll")
        return None

    offset_file = Path(__file__).parent / ".monedula_tg_offset"
    offset = 0
    if offset_file.exists():
        try:
            offset = int(offset_file.read_text().strip())
        except ValueError:
            offset = 0

    deadline = time.monotonic() + timeout_sec
    allowed_user_id = (
        int(TELEGRAM_ALLOWED_USER_ID) if TELEGRAM_ALLOWED_USER_ID else None
    )
    chat_id = int(TELEGRAM_CHAT_ID)

    log.info(f"Polling Telegram for reply (timeout={timeout_sec}s, offset={offset})...")

    while time.monotonic() < deadline:
        remaining = int(deadline - time.monotonic())
        if remaining <= 0:
            break

        poll_timeout = min(remaining, 30)  # max 30s per request
        url = (
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
            f"?offset={offset}&timeout={poll_timeout}&allowed_updates=message"
        )

        try:
            with urllib.request.urlopen(url, timeout=poll_timeout + 5) as resp:
                data = json.loads(resp.read())
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            log.warning(f"Telegram getUpdates request failed: {exc} — retrying")
            time.sleep(2)
            continue
        except json.JSONDecodeError as exc:
            log.warning(f"Telegram getUpdates JSON parse error: {exc}")
            time.sleep(2)
            continue

        updates = data.get("result") or []
        for update in updates:
            update_id = update.get("update_id", 0)
            offset = max(offset, update_id + 1)
            offset_file.write_text(str(offset))

            msg = update.get("message") or {}
            from_user = msg.get("from") or {}
            msg_chat = msg.get("chat") or {}
            user_id = from_user.get("id")
            msg_chat_id = msg_chat.get("id")

            # Filter to correct chat and allowed user
            if msg_chat_id != chat_id:
                continue
            if allowed_user_id and user_id != allowed_user_id:
                continue

            text = (msg.get("text") or "").strip()
            if text:
                log.info(f"Received Telegram reply: {text!r}")
                return text

    log.info("Telegram poll timed out — no reply received")
    return None


# ---------------------------------------------------------------------------
# Payment dispatch logic
# ---------------------------------------------------------------------------


def _parse_reply(reply: str | None, handler_names: list[str]) -> set[str]:
    """
    Parse the operator's Telegram reply and return the set of handler names
    to execute.

    Payments are ATTENDANCE-GATED: every recurring payment (yoga, therapy)
    pays for a session the operator was meant to attend. A calendar event is
    NOT proof of attendance — sessions get skipped. So approval requires an
    affirmative that names attendance, and a bare "yes" no longer blanket-
    approves. Accepted forms:

    "attended all"      / "paid all"        → all handlers
    "attended yoga"     / "yes yoga"        → {"yoga"}
    "attended therapy"  / "yes therapy"     → {"therapy"}
    "no" / "skipped"    / None/timeout      → empty set (skip all)

    Per-handler "yes <name>" / "y <name>" / "<name>" still work as an
    affirmation that the named session was attended. A naked "yes"/"yes all"
    with no session named is REJECTED (returns empty set) so the operator
    can't reflexively approve a skipped session.
    """
    if not reply:
        return set()

    low = reply.lower().strip()

    if low in ("no", "no all", "skip", "skip all", "skipped", "n", "didn't go", "did not attend"):
        return set()

    # Blanket approval requires an explicit "attended"/"paid"/"all" token —
    # a naked "yes"/"yes all" is intentionally NOT enough.
    if low in ("attended all", "paid all", "yes attended", "attended", "all attended"):
        return set(handler_names)

    # Per-handler attendance confirmation.
    approved: set[str] = set()
    for name in handler_names:
        if low in (
            f"attended {name}",
            f"paid {name}",
            f"yes {name}",
            f"y {name}",
            f"{name} attended",
            name,
        ):
            approved.add(name)
    if approved:
        return approved

    if low in ("yes", "yes all", "y", "y all"):
        log.warning(
            "Bare 'yes' received but payments are attendance-gated — "
            "need 'attended all' or 'attended <session>'. Treating as skip."
        )
        return set()

    log.warning(f"Unrecognised reply: {reply!r} — treating as skip all")
    return set()


def _build_preview_message(
    triggered: list[tuple],
    yesterday_str: str,
    due_tasks: list[dict] | None = None,
) -> str:
    """Build the Telegram preview message for all triggered payments."""
    lines = [f"💸 Monedula — payment check for {yesterday_str}", ""]

    # Calendar-triggered payments
    if triggered:
        lines.append("📅 *Calendar-triggered payments*")
        lines.append("")
        lines.append(
            "⚠️ These are scheduled from the calendar — a calendar event is "
            "NOT proof you attended. Only confirm sessions you actually went to."
        )
        lines.append("")
        for handler, matches in triggered:
            for match in matches:
                lines.append(handler.preview(match))
                lines.append("")

    # Neotoma task-based reminders
    if due_tasks:
        lines.append("📋 *Due payment tasks (Neotoma)*")
        lines.append("")
        for task in due_tasks:
            fields = task.get("snapshot") or task.get("fields") or task
            name = str(fields.get("title") or fields.get("name") or "(unnamed)")
            due = str(fields.get("due_date") or "")
            description = str(fields.get("description") or "")
            overdue = due and due < yesterday_str
            due_label = f"⚠️ overdue since {due}" if overdue else f"due {due}"
            lines.append(f"  • {name} ({due_label})")
            if description:
                # Show first 120 chars of description as context
                short_desc = description[:120].rstrip()
                if len(description) > 120:
                    short_desc += "…"
                lines.append(f"    {short_desc}")
        lines.append("")

    # Reply instructions — attendance-gated. A bare "yes" is intentionally
    # not accepted; the operator must confirm attendance per session.
    handler_names = list(dict.fromkeys([h.name for h, _ in triggered]))
    lines += [
        "Reply (confirm attendance — pays only for sessions you attended):",
        "  attended all       — confirm & pay all",
    ]
    for name in handler_names:
        lines.append(f"  attended {name:<9} — confirm & pay {name} only")
    lines.append("  no / skipped       — skip all (no payment)")
    if due_tasks:
        lines.append(
            "  (task reminders above are FYI — reply to approve calendar payments)"
        )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Stranded profiles (ateles#553)
# ---------------------------------------------------------------------------


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


def main() -> bool:
    """Run one Monedula tick. Returns True for a clean run.

    Returns False when an ACTIVE payment profile was stranded — i.e. a payment
    that should have been possible was not. The entrypoint turns that into a
    non-zero exit, so a run that could not pay no longer looks to launchd (or
    to anyone reading the log) exactly like a run with nothing to do.
    """
    log.info("Monedula starting.")

    yesterday = _yesterday()
    yesterday_str = yesterday.isoformat()

    # Load handlers from env-var-defined payment profiles.
    # Set MONEDULA_PROFILES=THERAPY,YOGA (and corresponding profile env vars).
    from handlers import load_handlers

    # Collect every ACTIVE profile the loader could not act on. Each of these
    # used to be a bare WARNING and a `continue`: the run exited clean, so a
    # payment that could not fire was indistinguishable from a day with
    # nothing due. Escalated below (ateles#553).
    strandings: list = []
    all_handlers = load_handlers(strandings)

    # Escalate before any early return below. A stranded profile is an
    # operational defect regardless of whether the rest of the run had
    # anything to do, and the "nothing to do" path returns early — which is
    # precisely how these stayed invisible for 17 days.
    escalate_strandings(strandings)

    # The daily claim gates the CALENDAR leg only — never the whole run.
    #
    # Recurring payments are attendance-gated: they need yesterday's events,
    # and that fetch must happen at most once per day. One-off invoices have
    # no session and no event; they fire on a due date alone.
    #
    # Those two things used to share one gate. `_check_already_ran_today()`
    # sat at the top of main() and returned BEFORE load_handlers() ran, so a
    # one-off profile created after the day's calendar run stayed invisible
    # until the next calendar day — with nothing in the log to say so. A
    # same-day invoice could not be paid at all, and two rental payments
    # (2026-08-15, 2026-08-18) were sent by hand because of it.
    #
    # Now the guard scopes to the calendar fetch, and one-offs are evaluated
    # on every ~15-minute tick.
    calendar_done = _check_already_ran_today()
    if calendar_done:
        log.info("Calendar leg already ran today — one-off profiles only.")
        events: list | None = []
    else:
        log.info(f"Checking calendar for yesterday: {yesterday_str}")
        events = None  # set by the fetch below

    # Fetch yesterday's events BEFORE claiming the day.
    #
    # The calendar leg runs at most once per day. Marking the day before the
    # fetch meant a failed fetch still consumed the only attempt, with no
    # retry — so a transient outage silently skipped that day's payments
    # entirely. The daily run drifted to just after midnight, when this host
    # is asleep with no network, and every attempt failed for a week straight
    # (the 2026-07-30 therapy session went unnotified and unpaid as a result).
    #
    # Now a failed fetch leaves the day unclaimed, so the next launchd tick
    # (~15 min) retries until the network is back.
    if not calendar_done:
        events = fetch_yesterday_events()

        if events is None:
            log.error(
                "Calendar fetch FAILED — leaving today unclaimed so the next tick "
                "retries. Not treating this as 'no sessions yesterday'."
            )
            _notify(
                f"monedula: calendar fetch failed for {yesterday_str} — "
                "recurring payment detection is DOWN; will retry next tick",
                priority="blocker",
            )
            # Do NOT return: a calendar outage says nothing about whether an
            # invoice is due. Recurring payments are skipped this tick (no
            # events to attend-gate against), one-offs continue below.
            events = []
        else:
            # Calendar fetch succeeded — claim the day so concurrent launchd
            # re-launches (and later ticks) skip the calendar leg.
            _mark_ran_today()

    # Find triggered handlers from calendar
    triggered: list[tuple] = []  # [(handler, [match, ...]), ...]
    for handler in all_handlers:
        matches = handler.matches(events)
        if matches:
            triggered.append((handler, matches))

    if triggered:
        log.info(f"Triggered handlers: {[h.name for h, _ in triggered]}")

    # Fetch due payment tasks from Neotoma, scoped to profile-linked task IDs only.
    due_tasks = fetch_due_payment_tasks(all_handlers)

    # Abort early only if there's truly nothing to show
    if not triggered and not due_tasks:
        log.info(
            "No payment handlers triggered and no due payment tasks — nothing to do."
        )
        return not strandings

    if not triggered:
        log.info(
            "No calendar-triggered payments, but due payment tasks found — sending reminder only."
        )

    # Build and send preview
    preview_msg = _build_preview_message(triggered, yesterday_str, due_tasks=due_tasks)
    log.info("Sending payment preview to Telegram...")
    telegram_send(preview_msg)

    # If there are only task reminders (no actionable calendar payments), don't wait for approval.
    if not triggered:
        log.info("Task reminders sent — no calendar payments to approve. Done.")
        return not strandings

    # Wait for operator reply (2 minutes)
    reply = telegram_long_poll_once(timeout_sec=120)

    handler_names = list(dict.fromkeys([h.name for h, _ in triggered]))
    approved = _parse_reply(reply, handler_names)

    if not approved:
        log.info(f"No payments approved (reply={reply!r}) — skipping all.")
        telegram_send(f"⏭️ Monedula: skipped all payments for {yesterday_str}.")
        return not strandings

    log.info(f"Approved handlers: {approved}")

    # Execute approved payments
    all_results = []
    for handler, matches in triggered:
        if handler.name not in approved:
            log.info(f"Skipping {handler.name} (not approved).")
            continue
        for match in matches:
            log.info(f"Executing {handler.name} payment...")
            _job = _activity.started(f"executing {handler.name} payment") if _activity else None
            try:
                result = handler.execute(match)
                all_results.append((handler, result))
                log.info(f"{handler.name} result: {result}")
                if _job:
                    # Keep summary generic — no amounts, IBANs, or memos.
                    _job.finished(f"{handler.name} payment executed")
            except Exception as _exc:
                if _job:
                    _job.failed(f"{handler.name} payment error: {type(_exc).__name__}")
                raise

    # Send confirmation
    if not all_results:
        telegram_send(f"⚠️ Monedula: no payments executed for {yesterday_str}.")
        return not strandings

    confirmation_lines = [f"📋 Monedula results for {yesterday_str}:", ""]
    for handler, result in all_results:
        if hasattr(handler, "format_confirmation"):
            conf = handler.format_confirmation(result)
        else:
            conf = json.dumps(result, indent=2)
        confirmation_lines.append(conf)
        confirmation_lines.append("")

    confirmation_msg = "\n".join(confirmation_lines).rstrip()
    log.info("Sending confirmation to Telegram...")
    telegram_send(confirmation_msg)
    log.info("Monedula run complete.")
    return not strandings


if __name__ == "__main__":
    _notify("monedula started", priority="info")
    try:
        clean = main()
        if clean:
            _notify("monedula run complete", priority="info")
        else:
            # A stranded profile means a payment did not happen. Exiting 0
            # here is what let sixteen consecutive days of this read as
            # sixteen clean runs (ateles#553).
            log.error(
                "Monedula run completed with STRANDED payment profiles — "
                "see the escalations filed in Neotoma."
            )
            sys.exit(1)
    except Exception as exc:
        log.exception(f"Monedula fatal error: {exc}")
        _notify(f"monedula fatal error: {exc}", priority="blocker")
        try:
            telegram_send(f"🔴 Monedula fatal error: {exc}")
        except Exception:
            pass
        sys.exit(1)
