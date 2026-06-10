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


def _name_from_event_summary(summary: str, email: str) -> str | None:
    """Best-effort: pull a human name for an attendee out of the event title.

    Calendar attendee records frequently lack a displayName, leaving only the
    email local-part (e.g. "amelia"). The event summary often carries the full
    name (e.g. "VC — Amelia Leigner (ProspectCRM)"). If the email local-part
    matches a capitalized token in the summary, return the fuller name found
    around it. Returns None when no confident match is found.
    """
    import re as _re

    if not summary or not email:
        return None
    local = email.split("@")[0].lower()
    # First-name guess: split local-part on common separators
    first_token = _re.split(r"[._\-]", local)[0]
    if len(first_token) < 2:
        return None

    # Find capitalized name sequences in the summary (e.g. "Amelia Leigner")
    name_runs = _re.findall(r"[A-Z][a-zA-Z'’\-]+(?:\s+[A-Z][a-zA-Z'’\-]+)*", summary)
    for run in name_runs:
        tokens = run.split()
        if tokens and tokens[0].lower() == first_token:
            # Cap at two tokens (first + last) to avoid trailing words
            return " ".join(tokens[:2])
    return None


def _extract_attendees(event: dict) -> list[dict]:
    """Return list of attendee dicts (name, email) from a calendar event.

    Name resolution order: explicit displayName -> name parsed from the event
    summary -> email local-part. This keeps shallow briefs from showing bare
    "amelia" when the title already says "Amelia Leigner".
    """
    attendees = event.get("attendees") or []
    summary = event.get("summary") or ""
    result = []
    for a in attendees:
        email = a.get("email", "").lower()
        # Skip self and calendar resource rooms
        if not email or "resource.calendar.google.com" in email:
            continue
        name = (
            a.get("displayName")
            or _name_from_event_summary(summary, email)
            or email.split("@")[0]
        )
        result.append({
            "name": name,
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


def _event_end_madrid(event: dict) -> datetime | None:
    """Return the event end as a Madrid-timezone datetime, or None."""
    end = event.get("end") or {}
    dt_str = end.get("dateTime") or end.get("date")
    if not dt_str:
        return None
    try:
        if "T" in dt_str:
            dt = datetime.fromisoformat(dt_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=MADRID_TZ)
            return dt.astimezone(MADRID_TZ)
        d = date.fromisoformat(dt_str)
        return datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=MADRID_TZ)
    except ValueError:
        return None


def _format_time_range(event: dict) -> str:
    """Return an hours-first time label, e.g. "8–10h" or "8:30–10h" or "all day".

    Minutes are shown only when non-zero. The trailing "h" marks 24h clock.
    """
    start = event.get("start") or {}
    is_all_day = "date" in start and "dateTime" not in start
    if is_all_day:
        return "all day"

    start_dt = _event_start_madrid(event)
    end_dt = _event_end_madrid(event)

    def _hm(dt: datetime) -> str:
        return dt.strftime("%-H:%M") if dt.minute else dt.strftime("%-H")

    if start_dt is None:
        return "?"
    if end_dt is None or end_dt <= start_dt:
        return f"{_hm(start_dt)}h"
    return f"{_hm(start_dt)}–{_hm(end_dt)}h"


# Keyword -> emoji for non-meeting events. First match (substring, case-insensitive)
# wins; falls back to 📌 when nothing matches. Keep specific terms before generic.
_EVENT_EMOJI = [
    (("pool", "swim", "piscina", "natación", "natacion"), "🏊"),
    (("gym", "fitness", "workout", "strength", "lift", "training", "entrenamiento"), "🏋️"),
    (("yoga",), "🧘"),
    (("run", "running", "jog"), "🏃"),
    (("coffee", "café", "cafe", "espresso"), "☕"),
    (("lunch", "brunch", "almuerzo", "comida"), "🥗"),
    (("dinner", "cena", "supper"), "🍽️"),
    (("breakfast", "desayuno"), "🥐"),
    (("pickup", "pick up", "pick-up", "recoger", "drop off", "drop-off", "dropoff"), "🚗"),
    (("flight", "fly", "vuelo", "airport", "aeropuerto"), "✈️"),
    (("train", "tren", "renfe"), "🚆"),
    (("doctor", "dentist", "medico", "médico", "clinic", "appointment", "cita"), "🩺"),
    (("therapy", "therapist", "terapia"), "🛋️"),
    (("haircut", "peluquer", "barber"), "💇"),
    (("call", "phone", "zoom", "meet", "llamada"), "📞"),
    (("birthday", "cumpleaños", "cumple"), "🎂"),
    (("school", "colegio", "escuela", "class", "clase"), "🎒"),
    (("groceries", "shopping", "compra", "supermercado"), "🛒"),
    (("parents", "family", "familia", "visiting", "visit"), "👪"),
    (("travel", "trip", "viaje", "hotel"), "🧳"),
]


def _event_emoji(event: dict, is_meeting: bool) -> str:
    """Pick a meaningful emoji for the event. Meetings always get 🤝."""
    if is_meeting:
        return "🤝"
    title = (event.get("summary") or "").lower()
    for keywords, emoji in _EVENT_EMOJI:
        if any(kw in title for kw in keywords):
            return emoji
    return "📌"


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


def telegram_send(text: str, parse_mode: str | None = None) -> None:
    """Send a Telegram message directly via the Bot API using Python urllib (no subprocess).

    parse_mode: None = plain text (default; literal * and _ are safe). Pass "HTML"
    to enable Telegram HTML formatting (<b>, <blockquote>, etc.) — the caller must
    then escape <>& in any literal text via html.escape().
    """
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
    if parse_mode:
        payload["parse_mode"] = parse_mode
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

0. **Determine the meeting's NATURE first — before any other research.** Ground this in
   Neotoma + Gmail context; do NOT assume every meeting is a Neotoma/Ateles business
   meeting. Classify it as exactly one of:
   - "business_neotoma": a Neotoma/Ateles BD, partnership, investor, customer, or hiring
     meeting where Neotoma/Ateles positioning is genuinely relevant.
   - "personal_or_legal": a personal, family, legal, financial, medical, or life-admin
     meeting (e.g. estate planning for Mark's family, a lawyer about personal matters,
     a doctor). Neotoma/Ateles BD framing is NOT relevant here.
   - "other": social, advisory, or ambiguous; relevant only if Neotoma/Ateles genuinely
     comes up.
   How to decide:
   a. Search Gmail for the intro thread / prior emails with the attendee(s):
      `gws gmail users messages list --params '{{"userId":"me","q":"from:<email> OR to:<email>","maxResults":8}}'`
      Read the actual thread to learn WHY they are meeting (the intro context usually states it).
   b. Search Neotoma (mcp__mcpsrv_neotoma__retrieve_entity_by_identifier by email then name)
      for the person/company and any prior interaction notes.
   c. Use the event title/description as a hint, but the email thread is the source of truth.
   STATE the classification explicitly at the top of the brief as a "🧭 Meeting nature" line
   with one sentence on WHY (e.g. "Personal estate-planning intro for Mark's family's
   Spain-US structuring, via Austin Desautels — not a Neotoma BD meeting.").
   If nature is "personal_or_legal" or "other-with-no-Neotoma-angle", you MUST OMIT the
   "🔍 Overlap with Neotoma/Ateles" and "⚡ Live convergence" sections entirely and instead
   produce a brief appropriate to the meeting's real purpose (who they are, what Mark wants
   from the meeting, what to prepare/ask). Do not shoehorn Neotoma/Ateles in.

1. **Participant research** — for each attendee:
   a. Search Neotoma (retrieve_entity_by_identifier with their email, then name) for any
      existing person/company entities; pull role, company, prior interactions, notes.
   b. If NOT found in Neotoma:
      - Use the Gmail thread from step 0 to extract name, role, company, context.
      - Search LinkedIn via web search for their name + email domain to confirm role/company.
      - Create a person entity in Neotoma (entity_type=person, name, email, role, company,
        notes with context from Gmail/LinkedIn). Do NOT invent a profession from thin signals.
   c. Note whether we've met them before (prior Gmail threads or Neotoma entities).

2. **Company / background research** — for the attendee's employer or relevant context:
   a. Web search to find what they do, their standing/reputation, recent notable news, and —
      for the SPECIFIC meeting purpose from step 0 — what's most relevant to know going in.
   b. **Recent news and publications** — last 6 months of relevant news, plus any articles,
      essays, talks, or posts by the attendee personally. Use these for conversation hooks.

3. **ONLY IF nature == "business_neotoma" (or a real Neotoma/Ateles angle exists):**
   a. Overlap with Neotoma/Ateles — 1-3 concrete, specific bullets (competitive,
      complementary, or strategic). If no meaningful overlap, omit the section.
   b. Live convergence — query `mcp__mcpsrv_neotoma__list_recent_changes` (14 days), open
      tasks/issues, and the Ateles plan (ent_99ace4dd6673aa36ed08b1fe); surface 1-3 things
      actively being built right now that speak to this attendee. Omit if not applicable.
   For "personal_or_legal" / "other" meetings, SKIP this entire step.

4. **Pre-event tasks** — identify concrete prep steps (materials to review, questions to
   answer in advance). For each: create a Neotoma task (entity_type=task, domain=preparation,
   due_date={event_date}, status=open) and note the entity_id. Note a suggested owning agent
   in the task description if another agent could do it.

5. **Meeting brief** — compose, fitted to the meeting's nature:
   a. Goals: 2-3 concrete outcomes Mark should aim for in THIS meeting (not generic BD goals).
   b. Agenda: ordered talking points (5-8 max), appropriate to the real purpose.
   c. Context: 1-2 sentences per attendee — who they are, relevant history.
   d. Open questions: anything Mark should clarify or resolve.

6. **Store the brief** — store a `meeting_prep` entity in Neotoma (the purpose-built schema
   for meeting briefs; do NOT use checkpoint_brief). Set these TOP-LEVEL fields (flat):
   - `title`: e.g. "Meeting prep: {event_title} {event_date} {event_time}"
   - `subject_name`: the primary counterpart's full name
   - `meeting_date`: "{event_date}"; `meeting_time_local`: "{event_time}"
   - `counterpart_company`: their company/firm; `counterpart_role`: their role
   - `context_summary`: the full brief text (same content sent to Telegram)
   - `questions_to_ask`: array of open questions; `success_criteria`: array of goals
   - `strongest_overlaps`: array of Neotoma/Ateles overlap bullets (ONLY if business_neotoma;
     otherwise omit or empty)
   - `source_urls`: array of any cited URLs; `notes`: pre-event tasks + their Neotoma IDs
   - `data_source`: "cotinga_deep_prep"; `status`: "ready"
   Link the entity REFERS_TO the meeting event. Keep every field at the top level.

7. **Send Telegram** — send the complete brief as Telegram HTML via:
   node {PROJECT_ROOT}/execution/lib/telegram/send.mjs --text "<brief_html>" --html {telegram_thread_flag}

   FORMATTING RULES (parse_mode=HTML):
   - Wrap the ENTIRE brief body in a single <blockquote>...</blockquote> so it renders
     indented with a left bar. The title line goes ABOVE the blockquote, in <b>...</b>.
   - HTML-escape every piece of literal text: replace & with &amp;, < with &lt;, > with &gt;.
     (So "GA&P" becomes "GA&amp;P".) Asterisks and underscores are SAFE in HTML mode — write
     REFERS_TO and any * literally; do NOT use Markdown ** or _.
   - Use ONLY these tags: <b> (section headers / emphasis), <blockquote>, and <a href>.
     No <h1>, no Markdown, no horizontal rules.
   - Section headers are the emoji + label in <b>, mixed case exactly as shown below — NOT
     uppercase. Use "- " hyphen bullets inside sections.

Format the Telegram HTML message as (do not output the ===TEMPLATE=== delimiter lines):
===TEMPLATE START===
📅 <b>Deep prep: {event_title} ({event_time})</b>
<blockquote>🧭 <b>Meeting nature</b>
- [one sentence: classification + why]

👥 <b>Participants</b>
- [one line per attendee: name, role/company, first meeting or met N times]

🎯 <b>Goals</b>
- [2-3 goals fitted to this meeting]

📋 <b>Agenda</b>
- [5-8 talking points]

📰 <b>Recent news / background</b>
- [1-3 bullets relevant to the meeting purpose]

[ONLY for business_neotoma meetings, otherwise omit both sections:]
🔍 <b>Overlap with Neotoma/Ateles</b>
- [1-3 specific bullets]

⚡ <b>Live convergence</b>
- [1-3 bullets of what's being built right now]

📝 <b>Open questions</b>
- [pre-meeting questions]

✅ <b>Pre-event tasks created</b>
- [task names and Neotoma IDs]</blockquote>
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
# Phase 1: shallow briefing
# ---------------------------------------------------------------------------


def build_shallow_briefing(
    events: list[dict],
    attendee_lookup: dict[str, dict | None],
    today_str: str,
) -> str:
    """Build the fast Phase 1 Telegram message as Telegram HTML.

    The schedule body is wrapped in a <blockquote> so it stands out with a
    left-indent bar. All literal text is HTML-escaped; only the blockquote and
    bold tags are emitted by us. Send with parse_mode="HTML".
    """
    import html as _html

    def esc(s: str) -> str:
        return _html.escape(s, quote=False)

    header = f"☀️ <b>Cotinga — daily prep for {esc(today_str)}</b>"

    if not events:
        return header + "\n\n<blockquote>No events today. Clear schedule.</blockquote>"

    body: list[str] = []
    events_shown = 0
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

        time_label = _format_time_range(event)
        is_meeting = _is_meeting(event)
        emoji = _event_emoji(event, is_meeting)
        # Hours-first format, e.g. "8–10h: 🏋️ Fitness"
        body.append(f"{esc(time_label)}: {emoji} {esc(title)}")

        attendees = _extract_attendees(event)
        others = [a for a in attendees if not a["self"]]

        for a in others:
            known = attendee_lookup.get(a["email"])
            if known:
                role = known.get("role") or known.get("title") or ""
                company = known.get("company") or ""
                context = ", ".join(filter(None, [role, company]))
                tail = f" — {esc(context)}" if context else " (known)"
                body.append(f"    👤 {esc(a['name'])}{tail}")
            else:
                body.append(f"    👤 {esc(a['name'])} — first meeting (deep prep running)")

        events_shown += 1

    if events_shown == 0:
        return header + "\n\n<blockquote>No events today. Clear schedule.</blockquote>"

    return header + "\n\n<blockquote>" + "\n".join(body) + "</blockquote>"


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

    if not events:
        log.info("No upcoming events — sending clear-schedule notice.")
        telegram_send(f"☀️ Cotinga — {today_str}\nNo events in the next 48 hours. Clear schedule.")
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

    # Send Phase 1 shallow briefing immediately (HTML — blockquote + bold header)
    briefing = build_shallow_briefing(events, attendee_lookup, today_str)
    log.info("Sending Phase 1 shallow briefing via Telegram...")
    telegram_send(briefing, parse_mode="HTML")

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
