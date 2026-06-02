#!/usr/bin/env python3
"""
Sylvia — Recurring Task Lifecycle Daemon
Named after Sylvia (warblers) — tireless, methodical, foraging daily without missing a cycle.

Runs once daily via launchd StartCalendarInterval.

Two scans per run:

1. Neotoma task scan — find tasks with a `recurrence` field set:
   - Roll due_date forward after completion.
   - Create Google Calendar events for tasks that lack one.
   - On due date: audience=agent tasks are left for Apis; audience=human tasks
     get a Telegram reminder.

2. Google Calendar scan — find upcoming events (next 7 days) with no matching
   Neotoma task; import them as new tasks (audience=human by default).

Neotoma is authoritative for recurrence rules. Calendar is output/import surface only.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
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
# lib/notify integration
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

try:
    from lib.notify import Notifier

    _notifier: Notifier | None = Notifier.from_neotoma()
except Exception:
    _notifier = None


def _notify(message: str, priority: str = "info") -> None:
    if _notifier is None:
        return
    try:
        from lib.notify import Priority

        p = getattr(Priority, priority.upper(), Priority.INFO)
        _notifier.send(message, priority=p, handler="sylvia")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Constants / paths
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).parent.parent.parent.parent
LOG_DIR = Path.home() / "Library" / "Logs" / "ateles"
LOG_FILE = LOG_DIR / "sylvia.log"
STATE_FILE = Path(__file__).parent / ".sylvia_last_run"

NEOTOMA_BEARER_TOKEN = os.environ.get("NEOTOMA_BEARER_TOKEN", "")
NEOTOMA_BASE_URL = os.environ.get(
    "NEOTOMA_BASE_URL", "https://neotoma.markmhendrickson.com"
)
NEOTOMA_CMD = shutil.which("neotoma") or "neotoma"
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
TELEGRAM_TOPIC_SYLVIA = os.environ.get("TELEGRAM_TOPIC_SYLVIA", "") or os.environ.get(
    "TELEGRAM_TOPIC_TASKS", ""
)

# How many days ahead to check for upcoming due dates when sending reminders.
REMINDER_LOOKAHEAD_DAYS = int(os.environ.get("SYLVIA_REMINDER_LOOKAHEAD_DAYS", "2"))
# How many days ahead to scan Calendar for new events to import.
CALENDAR_LOOKAHEAD_DAYS = int(os.environ.get("SYLVIA_CALENDAR_LOOKAHEAD_DAYS", "7"))

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
    format="%(asctime)s [sylvia] %(levelname)s %(message)s",
    handlers=[
        _FlushingFileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Idempotency guard
# ---------------------------------------------------------------------------


def _already_ran_today() -> bool:
    if STATE_FILE.exists():
        return STATE_FILE.read_text().strip() == date.today().isoformat()
    return False


def _mark_ran_today() -> None:
    STATE_FILE.write_text(date.today().isoformat())


# ---------------------------------------------------------------------------
# Telegram helpers
# ---------------------------------------------------------------------------


def _telegram_send(text: str) -> None:
    import shutil

    node = shutil.which("node")
    send_script = PROJECT_ROOT / "execution" / "lib" / "telegram" / "send.mjs"
    if node and send_script.exists():
        try:
            args = [node, str(send_script), "--text", text]
            if TELEGRAM_TOPIC_SYLVIA:
                args += ["--thread-id", TELEGRAM_TOPIC_SYLVIA]
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


# ---------------------------------------------------------------------------
# Neotoma helpers
# ---------------------------------------------------------------------------


def _neotoma_cli(*args: str, input_data: str | None = None) -> dict | list:
    """Run a neotoma CLI command and return parsed JSON output."""
    cmd = [NEOTOMA_CMD, "--offline", "--json"] + list(args)
    env = {**os.environ, "NEOTOMA_ENV": "production"}
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=30,
        input=input_data,
        env=env,
    )
    output = result.stdout.strip()
    if result.returncode != 0:
        raise RuntimeError(f"neotoma CLI failed ({result.returncode}): {result.stderr.strip()[:300]}")
    parsed = json.loads(output)
    if isinstance(parsed, dict) and parsed.get("error"):
        raise RuntimeError(f"neotoma CLI error: {parsed['error']}")
    return parsed


def fetch_recurring_tasks() -> list[dict]:
    """Return all Neotoma tasks with a recurrence field set."""
    try:
        data = _neotoma_cli("entities", "list", "--entity-type", "task", "--limit", "200")
        items: list[dict] = []
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            items = data.get("entities") or data.get("items") or data.get("data") or []

        recurring = []
        for t in items:
            fields = t.get("snapshot", {}).get("snapshot") or t.get("fields") or t
            if fields.get("recurrence"):
                recurring.append(t)

        log.info(f"Neotoma: {len(items)} tasks total, {len(recurring)} with recurrence")
        return recurring
    except Exception as exc:
        log.warning(f"Neotoma task fetch failed: {exc}")
        return []


def _task_fields(task: dict) -> dict:
    return task.get("snapshot", {}).get("snapshot") or task.get("fields") or task


def _roll_due_date(recurrence: str, current_due: str) -> str | None:
    """
    Roll a due_date forward by one recurrence interval.
    Supports: 'weekly', 'biweekly', 'monthly', 'Xw' (weeks), 'Xd' (days).
    Returns new ISO date string, or None if recurrence rule not recognised.
    """
    try:
        base = date.fromisoformat(current_due)
    except ValueError:
        return None

    rule = recurrence.strip().lower()
    if rule in ("daily", "1d"):
        return (base + timedelta(days=1)).isoformat()
    if rule in ("weekly", "1w", "7d"):
        return (base + timedelta(weeks=1)).isoformat()
    if rule in ("biweekly", "2w", "14d", "fortnightly"):
        return (base + timedelta(weeks=2)).isoformat()
    if rule in ("monthly",):
        # Advance by ~30 days; simple approximation
        m = base.month + 1
        y = base.year + (m - 1) // 12
        m = ((m - 1) % 12) + 1
        try:
            return base.replace(year=y, month=m).isoformat()
        except ValueError:
            # End-of-month edge case — use last day of next month
            import calendar

            last_day = calendar.monthrange(y, m)[1]
            return base.replace(year=y, month=m, day=last_day).isoformat()

    # 'Nw' / 'Nd' patterns
    m = re.fullmatch(r"(\d+)w", rule)
    if m:
        return (base + timedelta(weeks=int(m.group(1)))).isoformat()
    m = re.fullmatch(r"(\d+)d", rule)
    if m:
        return (base + timedelta(days=int(m.group(1)))).isoformat()

    log.warning(f"Unrecognised recurrence rule: {recurrence!r}")
    return None


# ---------------------------------------------------------------------------
# Google Calendar helpers
# ---------------------------------------------------------------------------


def fetch_upcoming_calendar_events(days: int = 7) -> list[dict]:
    import shutil

    gws = shutil.which("gws")
    if not gws:
        log.warning("gws CLI not found — skipping Calendar scan")
        return []

    today = date.today()
    time_min = today.strftime("%Y-%m-%dT00:00:00+02:00")
    time_max = (today + timedelta(days=days)).strftime("%Y-%m-%dT23:59:59+02:00")

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
            log.warning(f"gws calendar list failed: {result.stderr.strip()[:200]}")
            return []
        data = json.loads(result.stdout)
        items = data.get("items") or []
        log.info(f"Calendar: {len(items)} upcoming event(s) over next {days} days")
        return items
    except Exception as exc:
        log.warning(f"Calendar fetch error: {exc}")
        return []


def create_calendar_event(title: str, event_date: str) -> bool:
    """Create a Google Calendar event via gws CLI. Returns True on success."""
    import shutil

    gws = shutil.which("gws")
    if not gws:
        return False

    body = {
        "summary": title,
        "start": {"date": event_date},
        "end": {"date": event_date},
    }
    try:
        result = subprocess.run(
            [
                gws,
                "calendar",
                "events",
                "insert",
                "--params",
                json.dumps({"calendarId": "primary"}),
                "--body",
                json.dumps(body),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            env=os.environ,
        )
        if result.returncode == 0:
            log.info(f"Created Calendar event: {title!r} on {event_date}")
            return True
        log.warning(f"Calendar insert failed for {title!r}: {result.stderr.strip()[:200]}")
        return False
    except Exception as exc:
        log.warning(f"Calendar insert error: {exc}")
        return False


def _calendar_event_matches_task(event: dict, task_title: str, task_due: str) -> bool:
    """Loose match: same title (case-insensitive) and date within 1 day."""
    event_title = (event.get("summary") or "").lower().strip()
    task_title_lower = task_title.lower().strip()
    if event_title != task_title_lower:
        return False
    start = event.get("start") or {}
    event_date = start.get("date") or (start.get("dateTime") or "")[:10]
    if not event_date or not task_due:
        return False
    try:
        delta = abs((date.fromisoformat(event_date) - date.fromisoformat(task_due)).days)
        return delta <= 1
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Main logic
# ---------------------------------------------------------------------------


def process_recurring_tasks(
    tasks: list[dict], calendar_events: list[dict]
) -> list[str]:
    """
    For each recurring task:
    - If completed and due_date past: roll forward + reset to pending.
    - If no matching Calendar event: create one.
    - If due within REMINDER_LOOKAHEAD_DAYS and audience=human: queue reminder.

    Returns list of reminder messages to send.
    """
    today = date.today()
    lookahead = today + timedelta(days=REMINDER_LOOKAHEAD_DAYS)
    reminders: list[str] = []

    for task in tasks:
        entity_id = task.get("id") or task.get("entity_id") or ""
        fields = _task_fields(task)
        title = str(fields.get("name") or fields.get("title") or "(unnamed)")
        due_str = str(fields.get("due_date") or "")
        status = str(fields.get("status") or "").lower()
        recurrence = str(fields.get("recurrence") or "")
        audience = str(fields.get("audience") or "agent").lower()

        # Roll forward completed tasks
        if status in ("done", "complete", "completed") and due_str:
            try:
                due_date = date.fromisoformat(due_str)
            except ValueError:
                due_date = None

            if due_date and due_date <= today:
                new_due = _roll_due_date(recurrence, due_str)
                if new_due and NEOTOMA_BEARER_TOKEN:
                    try:
                        _neotoma_cli("store", "--entities", json.dumps([
                            {"entity_type": "task", "target_id": entity_id, "due_date": new_due, "status": "pending"}
                        ]))
                        log.info(f"Rolled {title!r}: due_date {due_str} → {new_due}")
                        due_str = new_due
                    except Exception as exc:
                        log.warning(f"Failed to roll {title!r}: {exc}")

        # Ensure a Calendar event exists for the upcoming due date
        if due_str:
            has_event = any(
                _calendar_event_matches_task(ev, title, due_str)
                for ev in calendar_events
            )
            if not has_event:
                create_calendar_event(title, due_str)

        # Send reminder for human-audience tasks due soon
        if audience == "human" and due_str:
            try:
                due_date = date.fromisoformat(due_str)
                if today <= due_date <= lookahead:
                    days_left = (due_date - today).days
                    if days_left == 0:
                        when = "today"
                    elif days_left == 1:
                        when = "tomorrow"
                    else:
                        when = f"in {days_left} days ({due_str})"
                    reminders.append(f"📌 *{title}* due {when}")
            except ValueError:
                pass

    return reminders


def import_calendar_tasks(
    calendar_events: list[dict], existing_tasks: list[dict]
) -> int:
    """
    For each upcoming Calendar event with no matching Neotoma task, create one.
    Returns count of tasks created.
    """
    if not NEOTOMA_BEARER_TOKEN:
        return 0

    created = 0
    # Routine/noise patterns to skip — daily schedule blocks that aren't tasks
    _noise_patterns = re.compile(
        r"^(wake|work|busy|sleep|lights out|prepare for bed|bed prep|fall asleep|"
        r"bimba|walk bimba|🌅|😴|🛌|🐶)",
        re.IGNORECASE,
    )

    for event in calendar_events:
        title = (event.get("summary") or "").strip()
        if not title:
            continue
        if _noise_patterns.match(title):
            log.debug(f"Skipping noise event: {title!r}")
            continue
        start = event.get("start") or {}
        event_date = start.get("date") or (start.get("dateTime") or "")[:10]
        if not event_date:
            continue

        # Check if any existing task matches
        matched = False
        for task in existing_tasks:
            fields = _task_fields(task)
            task_title = str(fields.get("name") or fields.get("title") or "")
            task_due = str(fields.get("due_date") or "")
            if _calendar_event_matches_task(event, task_title, task_due):
                matched = True
                break

        if matched:
            continue

        # Infer audience: if title contains agent-domain keywords, mark agent
        agent_keywords = {"payment", "pagament", "pago", "deploy", "release", "pr"}
        audience = (
            "agent"
            if any(kw in title.lower() for kw in agent_keywords)
            else "human"
        )

        try:
            _neotoma_cli(
                "store",
                "--entities", json.dumps([{
                    "entity_type": "task",
                    "name": title,
                    "due_date": event_date,
                    "audience": audience,
                    "status": "pending",
                    "notes": "imported from Google Calendar by Sylvia",
                }]),
            )
            log.info(f"Imported Calendar task: {title!r} due {event_date} (audience={audience})")
            created += 1
        except Exception as exc:
            log.warning(f"Failed to import task {title!r}: {exc}")

    return created


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    log.info("Sylvia starting.")

    if _already_ran_today():
        log.info("Already ran today — exiting.")
        return

    _mark_ran_today()

    # Fetch data
    recurring_tasks = fetch_recurring_tasks()
    calendar_events = fetch_upcoming_calendar_events(days=CALENDAR_LOOKAHEAD_DAYS)

    # Process recurring tasks: roll forward, sync to Calendar, collect reminders
    reminders = process_recurring_tasks(recurring_tasks, calendar_events)

    # Import Calendar events that have no matching Neotoma task
    imported = import_calendar_tasks(calendar_events, recurring_tasks)
    if imported:
        log.info(f"Imported {imported} new task(s) from Calendar")

    # Send consolidated reminder if any human-audience tasks are due soon
    if reminders:
        msg_lines = ["🗓️ *Sylvia* — upcoming tasks requiring your attention:", ""]
        msg_lines.extend(reminders)
        _telegram_send("\n".join(msg_lines))
        log.info(f"Sent {len(reminders)} reminder(s) via Telegram")
    else:
        log.info("No human-audience reminders to send today")

    log.info("Sylvia run complete.")


if __name__ == "__main__":
    _notify("sylvia started", priority="info")
    try:
        main()
        _notify("sylvia run complete", priority="info")
    except Exception as exc:
        log.exception(f"Sylvia fatal error: {exc}")
        _notify(f"sylvia fatal error: {exc}", priority="blocker")
        try:
            _telegram_send(f"🔴 Sylvia fatal error: {exc}")
        except Exception:
            pass
        sys.exit(1)
