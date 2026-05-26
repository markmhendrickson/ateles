#!/usr/bin/env python3
"""
Gorilla — Ateles health & fitness daemon.

Gorilla genus: gorillas. T3 daemon in the Ateles swarm. The first mammal-genus
exception to the bird/plant naming convention (chosen for the raw-strength
mnemonic); supersedes the previously-planned Salvia health-data slot.

Proactive counterpart to the user-invocable Gorilla agent (`.claude/skills/gorilla`).
The skill handles on-demand logging, analysis, and consultation; this daemon
pushes two unprompted signals to the operator:

  1. Weekly training summary — once per ISO week, in the configured window
     (default Sunday 18:00 Europe/Madrid): session count, days trained,
     locations, and exercise/volume rollup when set detail is available.
  2. Inactivity nudge — when no workout_session has been logged for
     GORILLA_NUDGE_AFTER_DAYS, at most once per nudge window.

Reads workout_session entities from Neotoma; never writes them (logging is the
skill's job). Notifications route through lib/notify (Telegram-primary).

AAuth sub: gorilla@ateles-swarm
Startup sequence (T3 daemon pattern):
  1. Load env from ~/.config/neotoma/.env
  2. Load agent_definition from Neotoma via lib/daemon_runtime
  3. Load AAuth signer
  4. Load priority_rubric from Neotoma via lib/notify
  5. Poll on a fixed interval; fire summary / nudge when their windows open

Environment variables:
  NEOTOMA_BEARER_TOKEN          Neotoma API auth token
  NEOTOMA_BASE_URL              Neotoma API base URL
  TELEGRAM_BOT_TOKEN            Telegram bot token
  TELEGRAM_CHAT_ID              Telegram chat ID
  TELEGRAM_TOPIC_GORILLA        Telegram topic ID for Gorilla notifications (optional)
  GORILLA_AGENT_DEFINITION_ID   Neotoma entity ID for Gorilla's agent_definition (optional)
  GORILLA_POLL_INTERVAL         Poll interval in seconds (default: 3600 = hourly)
  GORILLA_SUMMARY_WEEKDAY       Weekday for the weekly summary, Mon=0..Sun=6 (default: 6)
  GORILLA_SUMMARY_HOUR          Local hour (0-23) the summary window opens (default: 18)
  GORILLA_LOOKBACK_DAYS         Days of history the weekly summary covers (default: 7)
  GORILLA_NUDGE_AFTER_DAYS      Inactivity threshold for a nudge (default: 4)
  GORILLA_DRY_RUN               Set to "1" to log without sending notifications
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

# ── Path bootstrap ────────────────────────────────────────────────────────────
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from lib.daemon_runtime import (  # noqa: E402
    AAuthSigner,
    AgentLoader,
    SSEClient,  # noqa: F401 — imported for consistency; Gorilla uses polling not SSE
)
from lib.notify import Notifier, Priority  # noqa: E402

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("gorilla")

# ── Config ────────────────────────────────────────────────────────────────────
DAEMON_NAME = "gorilla"

NEOTOMA_BASE_URL = os.environ.get(
    "NEOTOMA_BASE_URL", "https://neotoma.markmhendrickson.com"
).rstrip("/")
NEOTOMA_BEARER_TOKEN = os.environ.get("NEOTOMA_BEARER_TOKEN", "")

POLL_INTERVAL = int(os.environ.get("GORILLA_POLL_INTERVAL", "3600"))  # hourly
SUMMARY_WEEKDAY = int(os.environ.get("GORILLA_SUMMARY_WEEKDAY", "6"))  # Sunday
SUMMARY_HOUR = int(os.environ.get("GORILLA_SUMMARY_HOUR", "18"))
LOOKBACK_DAYS = int(os.environ.get("GORILLA_LOOKBACK_DAYS", "7"))
NUDGE_AFTER_DAYS = int(os.environ.get("GORILLA_NUDGE_AFTER_DAYS", "4"))
DRY_RUN = os.environ.get("GORILLA_DRY_RUN", "0") == "1"

# State file to avoid re-sending a summary/nudge across poll cycles and restarts
_STATE_FILE = Path(__file__).parent / ".gorilla_state.json"


# ── State management ──────────────────────────────────────────────────────────


def _load_state() -> dict:
    """Load persisted state (last_summary_week, last_nudge_date) from disk."""
    if _STATE_FILE.exists():
        try:
            return json.loads(_STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {"last_summary_week": None, "last_nudge_date": None}


def _save_state(state: dict) -> None:
    """Persist state to local state file."""
    if DRY_RUN:
        return
    try:
        _STATE_FILE.write_text(json.dumps(state, indent=2))
    except OSError as exc:
        log.warning(f"[{DAEMON_NAME}] Failed to save state: {exc}")


# ── Neotoma reads ─────────────────────────────────────────────────────────────


def _fetch_recent_sessions(days: int) -> list[dict]:
    """
    Fetch workout_session entities and return those dated within the last `days`.

    Each returned dict is normalized to: {entity_id, date (date|None), location,
    notes, status, exercises (list)}. Exercise/set detail lives in raw_fragments
    on the v2.0 schema, so we read it defensively from either snapshot or
    raw_fragments.
    """
    import httpx

    if not NEOTOMA_BEARER_TOKEN:
        log.debug(f"[{DAEMON_NAME}] No NEOTOMA_BEARER_TOKEN — cannot read sessions")
        return []

    url = f"{NEOTOMA_BASE_URL}/entities"
    params = {
        "entity_type": "workout_session",
        "limit": 200,
        "include_snapshots": "true",
    }
    try:
        resp = httpx.get(
            url,
            params=params,
            headers={"Authorization": f"Bearer {NEOTOMA_BEARER_TOKEN}"},
            timeout=20,
        )
        resp.raise_for_status()
        entities = resp.json().get("entities", [])
    except Exception as exc:
        log.error(f"[{DAEMON_NAME}] Failed to fetch workout sessions: {exc}")
        return []

    cutoff = date.today() - timedelta(days=days)
    sessions: list[dict] = []
    for ent in entities:
        snap = ent.get("snapshot") or {}
        raw = ent.get("raw_fragments") or {}
        session_date = _parse_session_date(snap, raw)
        if session_date is None or session_date < cutoff:
            continue
        sessions.append(
            {
                "entity_id": ent.get("entity_id", ""),
                "date": session_date,
                "location": snap.get("location") or raw.get("location") or "",
                "notes": snap.get("notes") or "",
                "status": snap.get("status") or "",
                "session_type": raw.get("session_type") or snap.get("session_type") or "",
                "exercises": _coerce_exercises(snap.get("exercises") or raw.get("exercises")),
            }
        )
    sessions.sort(key=lambda s: s["date"])
    return sessions


def _parse_session_date(snap: dict, raw: dict):
    """Best-effort extraction of a session date from snapshot/raw_fragments."""
    raw_value = snap.get("date") or raw.get("date") or raw.get("started_at") or ""
    if not raw_value:
        return None
    text = str(raw_value)[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _coerce_exercises(value) -> list[dict]:
    """Normalize an exercises field that may be a list, JSON string, or None."""
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except (ValueError, TypeError):
            return []
    return []


# ── Analysis ──────────────────────────────────────────────────────────────────


def _session_volume_kg(session: dict) -> float:
    """Total working-set volume (weight_kg × reps) for one session, 0 if unknown."""
    total = 0.0
    for ex in session.get("exercises", []):
        for s in ex.get("sets", []) or []:
            try:
                total += float(s.get("weight_kg") or 0) * float(s.get("reps") or 0)
            except (TypeError, ValueError):
                continue
    return total


def compose_weekly_summary(sessions: list[dict], days: int) -> str:
    """Build the weekly-summary message body from the lookback window's sessions."""
    if not sessions:
        return (
            f"🦍 Weekly training summary — no workouts logged in the last {days} days. "
            "Tell me about your next session and I'll log it."
        )

    trained_days = sorted({s["date"] for s in sessions})
    locations = sorted({s["location"] for s in sessions if s["location"]})
    total_volume = sum(_session_volume_kg(s) for s in sessions)

    lines = [
        f"🦍 Weekly training summary (last {days} days)",
        f"• {len(sessions)} session(s) across {len(trained_days)} day(s)",
    ]
    types = sorted({s["session_type"] for s in sessions if s["session_type"]})
    if types:
        lines.append(f"• Focus: {', '.join(types)}")
    if locations:
        lines.append(f"• Locations: {', '.join(locations)}")
    if total_volume > 0:
        lines.append(f"• Working volume: {total_volume:,.0f} kg")
    lines.append("Ask me for a deeper breakdown or progression on any lift.")
    return "\n".join(lines)


def compose_nudge(last_session: dict | None, days_since: int | None) -> str:
    """Build the inactivity-nudge message body."""
    if last_session is None or days_since is None:
        return (
            "🦍 No workouts on record yet. When you train next, tell me the lifts "
            "and I'll start tracking your progression."
        )
    return (
        f"🦍 It's been {days_since} days since your last session "
        f"({last_session['date'].isoformat()}"
        + (f", {last_session['session_type']}" if last_session["session_type"] else "")
        + "). Want to plan the next one?"
    )


# ── Tick ──────────────────────────────────────────────────────────────────────


def _now_local(notifier: Notifier) -> datetime:
    """Current time in the notifier's configured timezone (defaults to Madrid)."""
    return notifier._now_local()  # noqa: SLF001 — reuse the rubric's tz resolution


async def tick(notifier: Notifier, state: dict) -> dict:
    """
    Run one poll cycle: decide whether the summary or nudge window is open and,
    if so, send and record it. Returns updated state.
    """
    now = _now_local(notifier)
    iso_week = f"{now.isocalendar().year}-W{now.isocalendar().week:02d}"

    # ── Weekly summary ──
    summary_due = (
        now.weekday() == SUMMARY_WEEKDAY
        and now.hour >= SUMMARY_HOUR
        and state.get("last_summary_week") != iso_week
    )
    if summary_due:
        sessions = _fetch_recent_sessions(LOOKBACK_DAYS)
        body = compose_weekly_summary(sessions, LOOKBACK_DAYS)
        log.info(f"[{DAEMON_NAME}] Weekly summary due ({iso_week}): {len(sessions)} session(s)")
        if DRY_RUN:
            log.info(f"[{DAEMON_NAME}] DRY RUN — would send:\n{body}")
        else:
            notifier.send(body, priority=Priority.INFO, handler=DAEMON_NAME)
        state["last_summary_week"] = iso_week
        _save_state(state)
        return state

    # ── Inactivity nudge ──
    sessions = _fetch_recent_sessions(max(NUDGE_AFTER_DAYS * 2, LOOKBACK_DAYS))
    last_session = sessions[-1] if sessions else None
    days_since = (now.date() - last_session["date"]).days if last_session else None

    nudge_due = (
        (last_session is None or (days_since is not None and days_since >= NUDGE_AFTER_DAYS))
        and state.get("last_nudge_date") != now.date().isoformat()
    )
    if nudge_due:
        body = compose_nudge(last_session, days_since)
        log.info(f"[{DAEMON_NAME}] Inactivity nudge due (days_since={days_since})")
        if DRY_RUN:
            log.info(f"[{DAEMON_NAME}] DRY RUN — would send:\n{body}")
        else:
            notifier.send(body, priority=Priority.INFO, handler=DAEMON_NAME)
        state["last_nudge_date"] = now.date().isoformat()
        _save_state(state)

    return state


# ── Main ──────────────────────────────────────────────────────────────────────


async def main() -> None:
    log.info(f"[{DAEMON_NAME}] Starting up...")
    log.info(
        f"[{DAEMON_NAME}] poll_interval={POLL_INTERVAL}s "
        f"summary_weekday={SUMMARY_WEEKDAY} summary_hour={SUMMARY_HOUR} "
        f"lookback_days={LOOKBACK_DAYS} nudge_after_days={NUDGE_AFTER_DAYS} "
        f"dry_run={DRY_RUN}"
    )

    # 1. Load agent_definition from Neotoma
    agent_def = AgentLoader(DAEMON_NAME).load()
    log.info(
        f"[{DAEMON_NAME}] agent_definition: status={agent_def.status} "
        f"grant={agent_def.agent_grant} sub={agent_def.aauth_sub}"
    )

    # 2. Load AAuth signer
    signer = AAuthSigner.from_key_file(DAEMON_NAME)
    if signer.is_stub:
        log.warning(
            f"[{DAEMON_NAME}] AAuth keypair not minted yet — "
            "observations attributed to operator token"
        )

    # 3. Load notification rubric
    notifier = Notifier.from_neotoma()
    notifier.send(
        f"{DAEMON_NAME} started (health & fitness summaries + nudges, dry_run={DRY_RUN})",
        priority=Priority.INFO,
        handler=DAEMON_NAME,
    )

    # 4. Load persisted state
    state = _load_state()
    log.info(
        f"[{DAEMON_NAME}] State loaded: last_summary_week={state.get('last_summary_week')} "
        f"last_nudge_date={state.get('last_nudge_date')}"
    )

    # 5. Poll loop
    log.info(f"[{DAEMON_NAME}] Starting poll loop (interval={POLL_INTERVAL}s)...")
    while True:
        try:
            state = await tick(notifier, state)
        except Exception as exc:
            log.error(f"[{DAEMON_NAME}] Tick error: {exc}", exc_info=True)
            notifier.send(
                f"{DAEMON_NAME} tick error: {exc}",
                priority=Priority.BLOCKER,
                handler=DAEMON_NAME,
            )

        await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info(f"[{DAEMON_NAME}] Stopped by operator.")
