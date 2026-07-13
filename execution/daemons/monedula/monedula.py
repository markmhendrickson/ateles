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
import logging
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta
from pathlib import Path

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
# Idempotency guard
# ---------------------------------------------------------------------------


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


def fetch_yesterday_events() -> list[dict]:
    """
    Use gws CLI to fetch all calendar events for yesterday.
    Returns list of event dicts (each with at least 'summary').
    Returns empty list on any failure.
    """
    import shutil

    gws = shutil.which("gws")
    if not gws:
        log.error("gws CLI not found in PATH — cannot check calendar")
        return []

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
            return []

        data = json.loads(result.stdout)
        items = data.get("items") or []
        log.info(f"Fetched {len(items)} calendar event(s) for {yest.isoformat()}")
        for item in items:
            log.debug(f"  Event: {item.get('summary', '(no title)')!r}")
        return items

    except json.JSONDecodeError as exc:
        log.error(f"Failed to parse gws output: {exc}")
        return []
    except Exception as exc:
        log.error(f"Calendar fetch error: {exc}")
        return []


# ---------------------------------------------------------------------------
# Neotoma: fetch due payment tasks
# ---------------------------------------------------------------------------


def _fetch_entity_by_id(entity_id: str) -> dict | None:
    """Fetch a single entity (with snapshot) by ID from Neotoma. None on error."""
    base_url = (NEOTOMA_BASE_URL or "http://localhost:3180").rstrip("/")
    is_loopback = "localhost" in base_url or "127.0.0.1" in base_url
    try:
        url = f"{base_url}/entities/{entity_id}"
        headers = {"Accept": "application/json"}
        if NEOTOMA_BEARER_TOKEN and not is_loopback:
            headers["Authorization"] = f"Bearer {NEOTOMA_BEARER_TOKEN}"
        req = urllib.request.Request(url, headers=headers)
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
# Task-based auto-execute (approval-gated, idempotent, dry-run-safe)
# ---------------------------------------------------------------------------
#
# Approval model (operator chose "Neotoma approval flag", 2026-07-13): a due
# payment task executes only when its snapshot carries `payment_approved: true`.
# The operator sets that field (via Inspector / CLI / the Ateles agent) after
# reviewing the emailed preview. This replaces the interactive Telegram reply
# for the email-primary channel, and is fully auditable in Neotoma.
#
# Idempotency: a task whose status is already "done" (or already carries a
# `payment_event_id`) is never re-executed — no double-pay.
#
# Dry-run safety: MONEDULA_DRYRUN defaults to "1" (on). While on, handlers are
# invoked with a dry_run flag / no real broadcast, so wiring can be verified
# without moving money. The operator flips MONEDULA_DRYRUN=0 to arm real sends.


def _dryrun_enabled() -> bool:
    """True unless the operator has explicitly armed real sends (MONEDULA_DRYRUN=0)."""
    return os.environ.get("MONEDULA_DRYRUN", "1") != "0"


def _task_fields(task: dict) -> dict:
    return task.get("snapshot") or task.get("fields") or task


def _task_is_approved(task: dict) -> bool:
    """True iff the task snapshot explicitly flags payment_approved truthy."""
    val = _task_fields(task).get("payment_approved")
    return str(val).strip().lower() in ("true", "1", "yes")


def _task_already_paid(task: dict) -> bool:
    """True iff the task is already settled (status done or a payment_event recorded)."""
    fields = _task_fields(task)
    if str(fields.get("status") or "").strip().lower() == "done":
        return True
    return bool(str(fields.get("payment_event_id") or "").strip())


def _handler_for_task(task: dict, handlers: list) -> Any | None:
    """Return the handler whose profile.neotoma_task_id points at this task."""
    tid = str(task.get("entity_id") or task.get("id") or "").strip()
    if not tid:
        return None
    for h in handlers:
        prof_tid = str(getattr(getattr(h, "profile", None), "neotoma_task_id", "") or "").strip()
        if prof_tid and prof_tid == tid:
            return h
    return None


def execute_approved_tasks(due_tasks: list[dict], handlers: list) -> list[tuple]:
    """
    Execute the due payment tasks that are operator-approved and not already paid.

    Returns a list of (handler, result) tuples for tasks that were acted on.
    Respects MONEDULA_DRYRUN (default on): while dry-run, the handler is asked to
    build-but-not-broadcast where it supports a dry_run flag, and the result is
    tagged status="dry_run" so no confirmation implies a real send.
    """
    results: list[tuple] = []
    dry = _dryrun_enabled()

    for task in due_tasks:
        fields = _task_fields(task)
        title = str(fields.get("title") or fields.get("name") or "(unnamed task)")

        if _task_already_paid(task):
            log.info(f"[autoexec] Skipping already-paid task {title!r}.")
            continue
        if not _task_is_approved(task):
            log.info(f"[autoexec] Task {title!r} not approved (payment_approved) — skipping.")
            continue

        handler = _handler_for_task(task, handlers)
        if handler is None:
            log.warning(f"[autoexec] No handler resolves task {title!r} — skipping.")
            continue

        synthetic_match = {"summary": title, "source": "task",
                           "entity_id": task.get("entity_id") or task.get("id")}

        if dry:
            log.info(f"[autoexec] DRY-RUN — would execute {handler.name} for {title!r} "
                     f"(€{getattr(handler.profile, 'amount_eur', '?')}).")
            results.append((handler, {
                "status": "dry_run", "handler": handler.name,
                "amount_eur": getattr(handler.profile, "amount_eur", None),
                "task": title,
            }))
            continue

        log.info(f"[autoexec] Executing {handler.name} for approved task {title!r}...")
        try:
            result = handler.execute(synthetic_match)
        except Exception as exc:  # noqa: BLE001 — never crash the daemon
            log.error(f"[autoexec] {handler.name} execution error: {exc}")
            result = {"status": "failed", "handler": handler.name, "error": str(exc)}
        results.append((handler, result))

    return results


def _mark_tasks_paid(task_results: list[tuple], due_tasks: list[dict]) -> None:
    """
    After a REAL successful send, mark the corresponding task done in Neotoma
    with a payment note. No-op for dry-run results and for non-'sent' statuses,
    so a task is only ever marked paid when money actually moved.

    Follows a recurring-obligation exception: if the task/profile is flagged as a
    never-complete recurring obligation, callers should not route it here — this
    helper is for the one-off vendor/reimbursement tasks that DO complete.
    """
    import shutil

    if _dryrun_enabled():
        return  # never mutate task lifecycle during dry-run

    neotoma = shutil.which("neotoma")
    if not neotoma:
        log.warning("[autoexec] neotoma CLI not found — cannot mark tasks paid.")
        return

    # Map handler.name -> its task entity_id via due_tasks + profile link.
    by_handler_task = {}
    for t in due_tasks:
        tid = str(t.get("entity_id") or t.get("id") or "")
        by_handler_task[tid] = t

    for handler, result in task_results:
        if result.get("status") != "sent":
            continue
        tid = str(getattr(getattr(handler, "profile", None), "neotoma_task_id", "") or "")
        if not tid:
            continue
        ref = (result.get("transfer_id") or result.get("txid")
               or result.get("reference") or "")
        note = f"Paid {date.today().isoformat()} via Monedula ({handler.name}); ref={ref}"
        try:
            subprocess.run(
                [neotoma, "--api-only", "entities", "update", tid,
                 "--status", "done", "--notes", note],
                capture_output=True, text=True, timeout=30, env=os.environ,
            )
            log.info(f"[autoexec] Marked task {tid} done ({handler.name}).")
        except Exception as exc:  # noqa: BLE001
            log.warning(f"[autoexec] Failed to mark task {tid} done: {exc}")


# ---------------------------------------------------------------------------
# Telegram helpers
# ---------------------------------------------------------------------------


def _email_send(text: str) -> bool:
    """Send an operator notification by email via `gws gmail +send`. Fail-open.

    Subject = "[Monedula] " + the message's first line; body = full message.
    Uses an argv list (no shell) so notification text can't be misinterpreted.
    Returns True on success, False on any failure so the caller can fall back
    to Telegram (break-glass). Gated by ATELES_NOTIFY_EMAIL / OPERATOR_EMAIL.
    """
    import shutil

    if os.environ.get("ATELES_NOTIFY_EMAIL", "0") != "1":
        return False
    operator_email = os.environ.get("OPERATOR_EMAIL", "").strip()
    if not operator_email:
        return False
    gws = shutil.which("gws")
    if not gws:
        log.warning("[notify] gws not found — cannot send email, falling back")
        return False

    first = (text.strip().splitlines() or ["notification"])[0]
    subject = f"[Monedula] {first[:80]}"
    cmd = [gws, "gmail", "+send", "--to", operator_email,
           "--subject", subject, "--body", text]
    swarm_email = os.environ.get("ATELES_SWARM_EMAIL", "").strip()
    if swarm_email:
        cmd += ["--from", swarm_email]
    try:
        r = subprocess.run(cmd, timeout=30, capture_output=True, text=True, env=os.environ)
        if r.returncode != 0:
            log.warning("[notify] gws +send failed (rc=%s): %s",
                        r.returncode, (r.stderr or "").strip()[:200])
            return False
        log.info("[notify] Operator notified by email (%s).", operator_email)
        return True
    except Exception as exc:  # noqa: BLE001 — never crash the caller
        log.warning("[notify] email send error: %s", exc)
        return False


def _telegram_only(text: str) -> None:
    """Deliver via Telegram (send.mjs helper, falling back to telegram-send CLI)."""
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


def telegram_send(text: str) -> None:
    """
    Deliver an operator notification.

    Operator prefers email (2026-07-13): when ATELES_NOTIFY_EMAIL=1, deliver via
    `gws gmail +send` and only fall through to Telegram (break-glass) if email
    delivery fails. When the flag is off, behaviour is unchanged (Telegram only).
    Name kept as `telegram_send` so existing call sites are untouched.

    NOTE: the interactive approval poll (`telegram_long_poll_once`) still reads
    replies from Telegram, so calendar-triggered payments that wait for a reply
    require Telegram for the *reply* channel. Task-reminder previews (the common
    path here) are one-way and are satisfied by email alone.
    """
    if _email_send(text):
        return
    _telegram_only(text)


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

    "yes all"      → all handlers
    "yes yoga"     → {"yoga"}
    "yes therapy"  → {"therapy"}
    "no"           → empty set (skip all)
    None/timeout   → empty set (skip all)
    """
    if not reply:
        return set()

    low = reply.lower().strip()

    if low in ("no", "no all", "skip", "skip all", "n"):
        return set()

    if low in ("yes", "yes all", "y", "y all"):
        return set(handler_names)

    # "yes yoga", "yes therapy", "y yoga", etc.
    for name in handler_names:
        if low in (f"yes {name}", f"y {name}", name):
            return {name}

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

    # Reply instructions
    handler_names = list(dict.fromkeys([h.name for h, _ in triggered]))
    lines += [
        "Reply:",
        "  yes all     — pay all calendar payments",
    ]
    for name in handler_names:
        lines.append(f"  yes {name:<10} — pay {name} only")
    lines.append("  no          — skip all")
    if due_tasks:
        lines.append("")
        lines.append(
            "  Task payments auto-execute once approved: set payment_approved=true"
        )
        lines.append(
            "  on the task in Neotoma (Inspector / CLI / ask Ateles). Monedula runs"
        )
        lines.append(
            "  them on its next poll (dry-run until MONEDULA_DRYRUN=0 is set)."
        )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    log.info("Monedula starting.")

    # Idempotency: exit immediately if already ran today
    if _check_already_ran_today():
        log.info("Already ran today — exiting.")
        return

    # Mark as started immediately to prevent concurrent launchd re-launches.
    # We clear this at the very end if something goes wrong before completion,
    # but keep it on successful runs to prevent double-payment.
    _mark_ran_today()

    yesterday = _yesterday()
    yesterday_str = yesterday.isoformat()
    today_str = date.today().isoformat()
    log.info(f"Checking calendar for yesterday: {yesterday_str}")

    # Load handlers from env-var-defined payment profiles.
    # Set MONEDULA_PROFILES=THERAPY,YOGA (and corresponding profile env vars).
    from handlers import load_handlers

    all_handlers = load_handlers()

    # Fetch yesterday's events
    events = fetch_yesterday_events()

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
        return

    if not triggered:
        log.info(
            "No calendar-triggered payments, but due payment tasks found — sending reminder only."
        )

    # Build and send preview
    preview_msg = _build_preview_message(triggered, yesterday_str, due_tasks=due_tasks)
    log.info("Sending payment preview to Telegram...")
    telegram_send(preview_msg)

    # Task-based auto-execute (approval-gated). Runs independently of the
    # calendar-triggered interactive path: any due task carrying
    # payment_approved=true is executed via its linked handler (dry-run unless
    # MONEDULA_DRYRUN=0). Then a per-payment confirmation is emailed.
    if due_tasks:
        task_results = execute_approved_tasks(due_tasks, all_handlers)
        if task_results:
            conf_lines = [f"📋 Monedula task-payment results for {today_str}:", ""]
            for handler, result in task_results:
                if hasattr(handler, "format_confirmation"):
                    conf_lines.append(handler.format_confirmation(result))
                else:
                    conf_lines.append(f"{handler.name}: {result.get('status')}")
                conf_lines.append("")
            telegram_send("\n".join(conf_lines).rstrip())
            _mark_tasks_paid(task_results, due_tasks)

    # If there are only task reminders (no actionable calendar payments), don't wait for approval.
    if not triggered:
        log.info("Task auto-execute complete — no calendar payments to approve. Done.")
        return

    # Wait for operator reply (2 minutes)
    reply = telegram_long_poll_once(timeout_sec=120)

    handler_names = list(dict.fromkeys([h.name for h, _ in triggered]))
    approved = _parse_reply(reply, handler_names)

    if not approved:
        log.info(f"No payments approved (reply={reply!r}) — skipping all.")
        telegram_send(f"⏭️ Monedula: skipped all payments for {yesterday_str}.")
        return

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
        return

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


if __name__ == "__main__":
    _notify("monedula started", priority="info")
    try:
        main()
        _notify("monedula run complete", priority="info")
    except Exception as exc:
        log.exception(f"Monedula fatal error: {exc}")
        _notify(f"monedula fatal error: {exc}", priority="blocker")
        try:
            telegram_send(f"🔴 Monedula fatal error: {exc}")
        except Exception:
            pass
        sys.exit(1)
