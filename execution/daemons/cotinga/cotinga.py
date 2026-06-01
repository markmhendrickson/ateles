#!/usr/bin/env python3
"""
Cotinga — Daily Event-Prep Briefing Daemon
Named after Cotinga, a genus of brightly coloured birds known for their
striking appearance — appropriate for a daemon that makes each day vivid
and well-prepared.

Runs once daily at 05:30 Madrid time via launchd StartCalendarInterval.

Two-phase design:
  Phase 1 (runs at 05:30, fast ~30s): Fetch today's calendar events,
    cross-reference attendees against Neotoma, emit a shallow Telegram
    briefing with known context. For each unknown attendee and each meeting
    needing deeper prep, create task entities in Neotoma and spawn async
    Claude agents to do the heavy lifting.
  Phase 2 (async, runs in background): Spawned Claude agents complete
    participant research, agenda generation, and pre-event tasks. Each
    agent sends its own Telegram follow-up when done.

Usage:
  python3 cotinga.py
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
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# Env bootstrap (launchd does not source shell profiles)
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
    _notifier: "Notifier | None" = Notifier.from_neotoma()
except Exception:
    _notifier = None


def _notify(message: str, priority: str = "info") -> None:
    if _notifier is None:
        return
    try:
        from lib.notify import Priority
        p = getattr(Priority, priority.upper(), Priority.INFO)
        _notifier.send(message, priority=p, handler="cotinga")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).parent.parent.parent.parent
LOG_DIR = Path.home() / "Library" / "Logs" / "ateles"
LOG_FILE = LOG_DIR / "cotinga.log"
STATE_FILE = Path(__file__).parent / ".cotinga_last_run"

MADRID_TZ = ZoneInfo("Europe/Madrid")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
TELEGRAM_TOPIC_COTINGA = os.environ.get("TELEGRAM_TOPIC_COTINGA", "")
NEOTOMA_BEARER_TOKEN = os.environ.get("NEOTOMA_BEARER_TOKEN", "")
NEOTOMA_BASE_URL = os.environ.get("NEOTOMA_BASE_URL", "https://neotoma.markmhendrickson.com")

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
    format="%(asctime)s [cotinga] %(levelname)s %(message)s",
    handlers=[
        _FlushingFileHandler(LOG_FILE),
    ],
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


LOCK_FILE = Path(__file__).parent / ".cotinga_lock"


def _acquire_lock() -> bool:
    """Return True if we got the lock, False if another instance is running."""
    if LOCK_FILE.exists():
        try:
            pid = int(LOCK_FILE.read_text().strip())
            # Check if that process is still alive
            import signal
            os.kill(pid, 0)
            log.warning(f"Another Cotinga instance is running (pid {pid}) — exiting.")
            return False
        except (ProcessLookupError, ValueError):
            pass  # stale lock
    LOCK_FILE.write_text(str(os.getpid()))
    return True


def _release_lock() -> None:
    LOCK_FILE.unlink(missing_ok=True)


def _check_already_ran_today() -> bool:
    if STATE_FILE.exists():
        return STATE_FILE.read_text().strip() == date.today().isoformat()
    return False


def _mark_ran_today() -> None:
    STATE_FILE.write_text(date.today().isoformat())


# ---------------------------------------------------------------------------
# Calendar: fetch upcoming events
# ---------------------------------------------------------------------------


def fetch_upcoming_events() -> list[dict]:
    """Fetch events for today (midnight-to-midnight Madrid time) from all calendars.

    Queries each calendar in parallel and merges results, deduplicating by event id.
    Excludes birthday and holiday calendars which are noise in a daily brief.
    """
    import shutil
    from concurrent.futures import ThreadPoolExecutor, as_completed

    gws = shutil.which("gws")
    if not gws:
        log.error("gws CLI not found in PATH")
        return []

    now = datetime.now(tz=MADRID_TZ)
    start_of_day = datetime(now.year, now.month, now.day, 0, 0, 0, tzinfo=MADRID_TZ)
    end_of_day = datetime(now.year, now.month, now.day, 23, 59, 59, tzinfo=MADRID_TZ)
    time_min = start_of_day.isoformat()
    time_max = end_of_day.isoformat()

    # All personal/family calendars to include; exclude birthday and holiday feeds
    CALENDAR_IDS = [
        "markmhendrickson@gmail.com",                          # primary
        "kce7ml7l9bjtbj9ndsatnaf87o@group.calendar.google.com",  # Tontitos
        "family01227972405407168266@group.calendar.google.com",   # Family
    ]

    def _fetch_one(cal_id: str) -> list[dict]:
        params = {
            "calendarId": cal_id,
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
                log.error(f"gws calendar events list failed for {cal_id}: {result.stderr.strip()[:200]}")
                return []
            data = json.loads(result.stdout)
            items = data.get("items") or []
            log.info(f"Calendar {cal_id!r}: {len(items)} event(s)")
            return items
        except (json.JSONDecodeError, Exception) as exc:
            log.error(f"Calendar fetch error for {cal_id}: {exc}")
            return []

    # Fetch all calendars in parallel
    all_events: dict[str, dict] = {}  # keyed by event id to deduplicate
    with ThreadPoolExecutor(max_workers=len(CALENDAR_IDS)) as pool:
        futures = {pool.submit(_fetch_one, cal_id): cal_id for cal_id in CALENDAR_IDS}
        for future in as_completed(futures):
            for event in future.result():
                eid = event.get("id")
                if eid and eid not in all_events:
                    all_events[eid] = event

    # Sort merged results by start time
    def _sort_key(ev: dict) -> str:
        start = ev.get("start") or {}
        return start.get("dateTime") or start.get("date") or ""

    merged = sorted(all_events.values(), key=_sort_key)
    log.info(f"Fetched {len(merged)} unique event(s) across {len(CALENDAR_IDS)} calendars")
    return merged


def _extract_attendees(event: dict) -> list[dict]:
    """Return list of attendee dicts (name, email) from a calendar event."""
    attendees = event.get("attendees") or []
    result = []
    for a in attendees:
        email = a.get("email", "").lower()
        # Skip self and calendar resource rooms
        if not email or "resource.calendar.google.com" in email:
            continue
        result.append({
            "name": a.get("displayName") or email.split("@")[0],
            "email": email,
            "organizer": a.get("organizer", False),
            "self": a.get("self", False),
        })
    return result


def _event_start_madrid(event: dict) -> datetime | None:
    """Return the event start as a Madrid-timezone datetime, or None."""
    start = event.get("start") or {}
    dt_str = start.get("dateTime") or start.get("date")
    if not dt_str:
        return None
    try:
        if "T" in dt_str:
            dt = datetime.fromisoformat(dt_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=MADRID_TZ)
            return dt.astimezone(MADRID_TZ)
        else:
            # All-day event
            d = date.fromisoformat(dt_str)
            return datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=MADRID_TZ)
    except ValueError:
        return None


_ROUTINE_TITLES = {
    "wake", "work", "busy", "focus", "lunch", "break",
    "prepare for bed", "lights out", "fall asleep", "sleep",
    "walk bimba", "ana bed prep",
}

def _is_routine(event: dict) -> bool:
    """True if the event is a personal routine block that shouldn't appear in the brief."""
    title = (event.get("summary") or "").lower().strip()
    # Strip leading emoji (up to first space after emoji block)
    import re
    title_clean = re.sub(r"^[\U00010000-\U0010ffff☀-⟿︀-️\U0001f300-\U0001f9ff]+\s*", "", title).strip()
    for pattern in _ROUTINE_TITLES:
        if title_clean == pattern or title_clean.startswith(pattern):
            return True
    return False


def _is_meeting(event: dict) -> bool:
    """True if the event has multiple attendees (i.e. is a real meeting)."""
    attendees = _extract_attendees(event)
    # Filter out self
    others = [a for a in attendees if not a["self"]]
    return len(others) > 0


# ---------------------------------------------------------------------------
# Neotoma: look up known attendees
# ---------------------------------------------------------------------------


def _neotoma_get(path: str) -> dict | list | None:
    """Make a GET request to the Neotoma API."""
    if not NEOTOMA_BEARER_TOKEN or not NEOTOMA_BASE_URL:
        return None
    try:
        url = f"{NEOTOMA_BASE_URL.rstrip('/')}{path}"
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {NEOTOMA_BEARER_TOKEN}",
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as exc:
        log.debug(f"Neotoma GET {path} failed: {exc}")
        return None


def lookup_person_in_neotoma(email: str, name: str) -> dict | None:
    """
    Try to find a person entity in Neotoma by email (preferred) or name.
    Returns the snapshot dict if found, None otherwise.
    """
    # Search by email first
    data = _neotoma_get(
        f"/api/entities?entity_type=person&search={urllib.parse.quote(email)}&limit=5"
    )
    if data:
        entities = data.get("entities") or []
        for e in entities:
            snap = e.get("snapshot") or {}
            if email.lower() in (snap.get("email") or "").lower():
                return snap

    # Fall back to name search
    data = _neotoma_get(
        f"/api/entities?entity_type=person&search={urllib.parse.quote(name)}&limit=5"
    )
    if data:
        entities = data.get("entities") or []
        for e in entities:
            snap = e.get("snapshot") or {}
            return snap  # return first name match

    return None


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------


def telegram_send(text: str) -> None:
    """Send a Telegram message directly via the Bot API using Python urllib (no subprocess)."""
    import json
    import urllib.request
    import urllib.error

    bot_token = TELEGRAM_BOT_TOKEN
    chat_id = TELEGRAM_CHAT_ID

    # Fall back to reading from env file if module-level vars are empty
    if not bot_token or not chat_id:
        log.warning("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set — cannot send")
        return

    payload: dict = {"chat_id": chat_id, "text": text}
    if TELEGRAM_TOPIC_COTINGA:
        payload["message_thread_id"] = TELEGRAM_TOPIC_COTINGA

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    data = json.dumps(payload).encode("utf-8")

    try:
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read())
            msg_id = body.get("result", {}).get("message_id")
            log.info(f"Telegram send OK (message_id={msg_id})")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:300]
        log.error(f"Telegram HTTP error {exc.code}: {body}")
    except Exception as exc:
        log.error(f"Telegram send failed: {exc}")


# ---------------------------------------------------------------------------
# Neotoma task creation
# ---------------------------------------------------------------------------


def create_neotoma_task(title: str, description: str, due_date: str, priority: str = "p2") -> str | None:
    """
    Create a task entity in Neotoma via the REST API.
    Returns entity_id on success, None on failure.
    """
    if not NEOTOMA_BEARER_TOKEN or not NEOTOMA_BASE_URL:
        return None

    payload = json.dumps({
        "entities": [{
            "entity_type": "task",
            "name": title,
            "description": description,
            "due_date": due_date,
            "priority": priority,
            "status": "open",
            "domain": "preparation",
        }],
        "idempotency_key": f"cotinga-task-{title[:40].replace(' ', '-')}-{due_date}",
    }).encode()

    try:
        url = f"{NEOTOMA_BASE_URL.rstrip('/')}/api/store"
        req = urllib.request.Request(
            url,
            data=payload,
            headers={
                "Authorization": f"Bearer {NEOTOMA_BEARER_TOKEN}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        entities = data.get("entities") or []
        if entities:
            eid = entities[0].get("entity_id")
            log.info(f"Created Neotoma task: {title!r} → {eid}")
            return eid
    except Exception as exc:
        log.warning(f"Neotoma task creation failed for {title!r}: {exc}")
    return None


# ---------------------------------------------------------------------------
# Async deep-prep agent spawn
# ---------------------------------------------------------------------------


def spawn_deep_prep_agent(event: dict, attendees: list[dict], event_dt: datetime) -> None:
    """
    Spawn a background Claude agent to do deep participant research,
    agenda generation, and talking points for a meeting.
    The agent sends its own Telegram follow-up when done.
    """
    import shutil

    claude = shutil.which("claude")
    if not claude:
        log.warning("claude CLI not found — cannot spawn deep-prep agent")
        return

    event_title = event.get("summary") or "(untitled)"
    event_time = event_dt.strftime("%H:%M")
    event_date = event_dt.strftime("%Y-%m-%d")
    attendee_list = "\n".join(
        f"  - {a['name']} <{a['email']}>" for a in attendees if not a["self"]
    )

    description = (event.get("description") or "").strip()[:500]
    location = (event.get("location") or "").strip()

    telegram_thread_flag = (
        f"--topic {TELEGRAM_TOPIC_COTINGA}" if TELEGRAM_TOPIC_COTINGA else ""
    )

    prompt = f"""You are Cotinga, the daily event-prep agent for Mark Hendrickson (markmhendrickson@gmail.com).

Your job is to prepare a deep briefing for this upcoming meeting and send it via Telegram.

## Meeting details
- Title: {event_title}
- Date/time: {event_date} at {event_time} Madrid time
- Location: {location or '(none)'}
- Description: {description or '(none)'}

## Attendees (excluding Mark)
{attendee_list or '(no external attendees — this is a solo event)'}

## Your tasks (complete all, then send Telegram summary)

0. **Neotoma event + contact enrichment** — before any web research, query Neotoma for
   pre-existing context on this meeting and its participants:
   a. Search for a matching `event` entity: call `mcp__mcpsrv_neotoma__retrieve_entities`
      with `entity_type=event` and `search="{event_title} {event_date}"`. Also try
      searching by each attendee's name. If found, read the full `description` field —
      it may contain email-sourced context (agenda, docs shared, background, prior
      commitments, source email ID) that is not present in the calendar entry.
   b. For each attendee, call `mcp__mcpsrv_neotoma__retrieve_entity_by_identifier` with
      their email address, then their name, to find existing `contact` entities. Pull
      the full snapshot — role, company, notes, prior interactions, relationship status.
   c. From any matched event entity, also call `mcp__mcpsrv_neotoma__retrieve_related_entities`
      to surface linked contacts, tasks, or other entities attached to this meeting.
   d. Incorporate all Neotoma-sourced context into the brief — treat it as higher-fidelity
      than web search since it reflects prior direct interactions and email content.
   e. If no Neotoma event entity is found, proceed with calendar description as-is.

1. **Participant research** — for each attendee:
   a. Search Neotoma (mcp__mcpsrv_neotoma__retrieve_entity_by_identifier with their email,
      then name) for any existing person/company entities.
   b. If found: pull their snapshot — role, company, prior interactions, notes.
   c. If NOT found in Neotoma:
      - Search Gmail for emails to/from their address:
        `gws gmail users messages list --params '{{"userId":"me","q":"from:<email> OR to:<email>","maxResults":5}}'`
        Read the most relevant message(s) to extract name, role, company, context.
      - Search LinkedIn via web search for their name + email domain to find role/company.
      - Create a person entity in Neotoma with everything found (entity_type=person,
        name, email, role, company, notes with context from Gmail/LinkedIn).
   d. Note whether we've met them before (any prior Gmail threads or Neotoma entities).

2. **Company / organisation research** — for each attendee's employer:
   a. Web search their company name to find: founding thesis, product/service description,
      portfolio companies (if VC/investor), recent news, team size, stage/funding.
   b. If the attendee is an investor: research their fund's thesis, check-size, stage focus,
      and any portfolio companies that overlap in category with Neotoma or Ateles.
   c. **Recent news and publications** — web search for:
      - News about the company or fund in the last 6 months (funding rounds, launches,
        press coverage, blog posts, announcements).
      - Any articles, essays, talks, or posts written by or featuring the attendee personally
        (LinkedIn posts, Substack, conference talks, interviews, podcasts).
      - Use these to understand their current thinking and surface natural conversation hooks.
   d. Surface explicit overlap with Neotoma and/or Ateles:
      - **Competitive overlap**: are they funding or building anything in the structured
        memory, knowledge graph, MCP/agent tooling, or AI-ops space?
      - **Complementary overlap**: portfolio companies or products that Neotoma/Ateles
        could directly integrate with, sell to, or partner with?
      - **Strategic angle**: why would this person / firm care about Neotoma or Ateles
        specifically — from their thesis, portfolio gaps, or personal history?
   e. Include a "🔍 Overlap with Neotoma/Ateles" section in the brief (1-3 bullet points,
      concrete and specific — not generic). If no meaningful overlap, say so explicitly.

3. **Neotoma/Ateles activity convergence** — query Neotoma to surface recent internal activity
   that might resonate with this attendee's interests or thesis:
   a. Pull recent changes: `mcp__mcpsrv_neotoma__list_recent_changes` (last 14 days) —
      scan for new entity types, schema additions, decisions, or major feature work that
      overlaps with the attendee's domain.
   b. Pull recent task/issue activity: retrieve open tasks and GitHub issues in Neotoma
      that touch areas relevant to the attendee (e.g. if they're a VC interested in
      agent memory, surface any recent schema work on memory or MCP tooling).
   c. Pull the Ateles plan entity (ent_99ace4dd6673aa36ed08b1fe) — check decisions and
      next_steps for anything that aligns with what the attendee works on.
   d. Synthesise into 1-3 concrete "shared momentum" points: things Neotoma or Ateles
      is actively building *right now* that speak directly to the attendee's interests —
      not just product positioning, but live development activity that signals direction.
   e. Include a "⚡ Live convergence" section in the brief — what's actively happening
      in Neotoma/Ateles that this person would find directly relevant today.

4. **Pre-event tasks** — identify any concrete preparation steps:
   - Materials to review, docs to prepare, questions to answer in advance
   - For each task: create a task entity in Neotoma (entity_type=task, domain=preparation,
     due_date={event_date}, status=open) and note the entity_id.
   - If a task can be done by another agent (e.g. research, a code issue),
     note the suggested agent name in the task description.

5. **Meeting brief** — compose:
   a. Goals: 2-3 concrete outcomes Mark should aim for
   b. Agenda: ordered talking points (5-8 bullet points max)
   c. Context: 1-2 sentences per attendee — who they are, any relevant history
   d. Open questions: anything Mark should clarify or resolve

6. **Store the brief** — store a checkpoint_brief entity in Neotoma:
   entity_type=checkpoint_brief (schema: b0bfcfab-1f07-4526-8fa5-d5ace343b004)
   with the full brief as the body field, linked REFERS_TO the meeting event.

7. **Send Telegram** — send the complete brief to Telegram via:
   node {PROJECT_ROOT}/execution/lib/telegram/send.mjs --text "<brief>" --plain {telegram_thread_flag}

   IMPORTANT — plain text only. Telegram renders the message as plain text (we pass --plain).
   Do NOT use any Markdown syntax: no **bold**, no *italics*, no _underscores_, no `code`,
   no # headings, and no --- horizontal rules. Write entity references like REFERS_TO and
   CORRECTS literally (underscores are fine in plain text). Use the emoji section headers and
   plain hyphen "- " bullets exactly as shown below. Anything you wrap in asterisks will show
   the asterisks literally, so omit them entirely.

Format the Telegram message as (plain text, emoji headers, hyphen bullets — no markdown).
Do not include the surrounding lines below — they only delimit the template:
===TEMPLATE START===
📅 Cotinga deep prep: {event_title} ({event_time})

👥 Participants
[one line per attendee: name, role/company, "first meeting" or "met N times"]

🎯 Goals
[2-3 bullet goals]

📋 Agenda
[5-8 bullet talking points]

📰 Recent news / publications
[1-3 bullets: notable recent news about their company, or articles/posts by the person]

🔍 Overlap with Neotoma/Ateles
[1-3 bullets: competitive, complementary, or strategic overlap — specific, not generic]

⚡ Live convergence
[1-3 bullets: what Neotoma/Ateles is actively building right now that speaks to this person's interests]

📝 Open questions
[any pre-meeting questions to resolve]

✅ Pre-event tasks created
[list task names and Neotoma IDs]
===TEMPLATE END===

Work through all steps, then stop.
"""

    log.info(f"Spawning deep-prep agent for: {event_title!r}")
    try:
        import re
        safe_title = re.sub(r"[^A-Za-z0-9_-]", "_", event_title[:20])
        log_path = LOG_DIR / f"cotinga_deepprep_{event_date}_{safe_title}.log"
        subprocess.Popen(
            [claude, "--print", "--dangerously-skip-permissions", prompt],
            env=os.environ,
            stdout=open(log_path, "w"),
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        log.info(f"Deep-prep agent spawned for {event_title!r} (background)")
    except Exception as exc:
        log.warning(f"Failed to spawn deep-prep agent for {event_title!r}: {exc}")


# ---------------------------------------------------------------------------
# Task digest: due and overdue tasks
# ---------------------------------------------------------------------------


def fetch_due_tasks() -> list[dict]:
    """Fetch Neotoma tasks due today or overdue (due_date <= today, status not done/completed)."""
    if not NEOTOMA_BEARER_TOKEN or not NEOTOMA_BASE_URL:
        return []
    today = date.today().isoformat()
    path = (
        f"/api/entities?entity_type=task"
        f"&limit=50"
        f"&include_snapshots=true"
    )
    data = _neotoma_get(path)
    if not data:
        return []
    entities = data.get("entities") or []
    due = []
    for e in entities:
        snap = (e.get("snapshot") or {}).get("snapshot") or e.get("snapshot") or {}
        status = (snap.get("status") or "").lower()
        if status in ("done", "completed", "cancelled", "canceled"):
            continue
        due_date = snap.get("due_date") or ""
        if due_date and due_date <= today:
            due.append({
                "title": snap.get("title") or snap.get("name") or "(untitled)",
                "due_date": due_date,
                "status": snap.get("status") or "pending",
                "priority": snap.get("priority") or "",
                "overdue": due_date < today,
            })
    due.sort(key=lambda t: t["due_date"])
    log.info(f"Task digest: {len(due)} due/overdue task(s)")
    return due


def build_task_digest(tasks: list[dict]) -> str:
    """Format due/overdue tasks as a Telegram section."""
    if not tasks:
        return ""
    lines = ["📋 Tasks due today / overdue", ""]
    for t in tasks:
        prefix = "🔴" if t["overdue"] else "🟡"
        date_label = f"(due {t['due_date']})" if t["overdue"] else "(due today)"
        lines.append(f"{prefix} {t['title']} {date_label}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Phase 1: shallow briefing
# ---------------------------------------------------------------------------


def build_shallow_briefing(
    events: list[dict],
    attendee_lookup: dict[str, dict | None],
    today_str: str,
) -> str:
    """Build the fast Phase 1 Telegram message."""
    lines = [f"☀️ Cotinga — daily prep for {today_str}", ""]

    if not events:
        lines.append("No events in the next 48 hours. Clear schedule.")
        return "\n".join(lines)

    events_shown = 0
    meetings_with_deepprep = 0
    seen_titles: set[str] = set()  # dedup by normalised title

    for event in events:
        # Skip pure routine/personal blocks
        if _is_routine(event):
            continue

        title = event.get("summary") or "(untitled)"

        # Deduplicate: skip if a very similar title already shown
        import re as _re
        norm_title = _re.sub(r"[^a-z0-9]", "", title.lower())
        if norm_title in seen_titles:
            continue
        seen_titles.add(norm_title)

        # Detect all-day events (start has "date" not "dateTime")
        start = event.get("start") or {}
        is_all_day = "date" in start and "dateTime" not in start
        event_dt = _event_start_madrid(event)
        time_str = "all day" if is_all_day else (event_dt.strftime("%H:%M") if event_dt else "?")

        is_meeting = _is_meeting(event)
        icon = "🤝" if is_meeting else "📌"
        lines.append(f"{icon} {title} — {time_str}")

        attendees = _extract_attendees(event)
        others = [a for a in attendees if not a["self"]]

        for a in others:
            known = attendee_lookup.get(a["email"])
            if known:
                role = known.get("role") or known.get("title") or ""
                company = known.get("company") or ""
                context = ", ".join(filter(None, [role, company]))
                lines.append(f"  👤 {a['name']}{' — ' + context if context else ' (known)'}")
            else:
                lines.append(f"  👤 {a['name']} — first meeting (research queued)")

        if is_meeting:
            lines.append("  🔍 Deep prep: queued in background")
            meetings_with_deepprep += 1

        lines.append("")
        events_shown += 1

    if events_shown == 0:
        lines.append("Clear schedule.")
    elif meetings_with_deepprep > 0:
        lines.append("Deep briefs will arrive separately as agents complete.")
    return "\n".join(lines)


def append_task_digest(briefing: str, tasks: list[dict]) -> str:
    """Append task digest section to an existing briefing string."""
    digest = build_task_digest(tasks)
    if not digest:
        return briefing
    return briefing.rstrip() + "\n\n" + digest


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    log.info("Cotinga starting.")

    if not _acquire_lock():
        return

    try:
        _main()
    finally:
        _release_lock()


def _main() -> None:
    if _check_already_ran_today():
        log.info("Already ran today — exiting.")
        return

    _mark_ran_today()

    today_str = date.today().isoformat()
    log.info(f"Running daily event prep for {today_str}")

    # Fetch upcoming events
    events = fetch_upcoming_events()

    # Fetch due/overdue tasks (runs in parallel with event prep)
    due_tasks = fetch_due_tasks()

    if not events:
        log.info("No upcoming events — sending clear-schedule notice.")
        base = f"☀️ Cotinga — {today_str}\nNo events in the next 48 hours. Clear schedule."
        telegram_send(append_task_digest(base, due_tasks))
        return

    # Phase 1: fast attendee lookup in Neotoma
    all_attendees: dict[str, dict] = {}  # email → {name, email, ...}
    for event in events:
        for a in _extract_attendees(event):
            if not a["self"] and a["email"] not in all_attendees:
                all_attendees[a["email"]] = a

    attendee_lookup: dict[str, dict | None] = {}
    for email, a in all_attendees.items():
        attendee_lookup[email] = lookup_person_in_neotoma(email, a["name"])
        log.info(
            f"Attendee {a['name']} <{email}>: "
            f"{'known' if attendee_lookup[email] else 'unknown'}"
        )

    # Send Phase 1 shallow briefing immediately
    briefing = build_shallow_briefing(events, attendee_lookup, today_str)
    briefing = append_task_digest(briefing, due_tasks)
    log.info("Sending Phase 1 shallow briefing via Telegram...")
    telegram_send(briefing)

    # Phase 2: spawn async deep-prep agents for each meeting
    for event in events:
        if not _is_meeting(event):
            continue

        event_dt = _event_start_madrid(event)
        if event_dt is None:
            continue

        attendees = _extract_attendees(event)
        spawn_deep_prep_agent(event, attendees, event_dt)
        # Brief stagger to avoid hammering the API
        time.sleep(2)

    log.info("Cotinga Phase 1 complete. Deep-prep agents running in background.")


if __name__ == "__main__":
    _notify("cotinga started", priority="info")
    try:
        main()
        _notify("cotinga Phase 1 complete", priority="info")
    except Exception as exc:
        log.exception(f"Cotinga fatal error: {exc}")
        _notify(f"cotinga fatal error: {exc}", priority="blocker")
        try:
            telegram_send(f"🔴 Cotinga fatal error: {exc}")
        except Exception:
            pass
        sys.exit(1)
