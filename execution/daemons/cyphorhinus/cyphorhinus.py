#!/usr/bin/env python3
"""
Cyphorhinus — Activity-log reply-routing daemon.

Cyphorhinus genus: wrens, known for exceptionally complex song. This daemon is
the *transport* for the swarm's passive observation channel:

  - Agents emit lifecycle events (started / finished / failed / skipped /
    escalated) via lib.activity → CyphorhinusBot Telegram chat + activity_log
    entities in Neotoma.
  - The operator can REPLY to any activity message in CyphorhinusBot to ask a
    follow-up question or steer the agent about that specific job.

This daemon closes the loop: it long-polls CyphorhinusBot's getUpdates for
operator replies, maps each reply's reply_to_message.message_id back to the
originating job (via the telegram_message_id field stamped on activity_log
entities by lib.activity), and writes an `operator_followup` entity in Neotoma
linked REFERS_TO the activity_log. The target agent picks the followup up on its
next poll (scheduled daemons) or via SSE (always-on daemons).

It does NOT itself execute follow-up actions — it only routes. Per the design
decision (decision:reply-routing-sse-neotoma-2026-05-27): Neotoma is the source
of truth; transport is decoupled from execution.

Environment:
  CYPHORHINUS_TELEGRAM_BOT_TOKEN   CyphorhinusBot token (separate from Ateles)
  CYPHORHINUS_TELEGRAM_CHAT_ID     Operator chat id
  NEOTOMA_BASE_URL / NEOTOMA_BEARER_TOKEN
  CYPHORHINUS_POLL_TIMEOUT         getUpdates long-poll timeout (default 50s)

Runs as a launchd agent — see com.ateles.cyphorhinus.plist.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# ── Path + env bootstrap ────────────────────────────────────────────────────
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_NEOTOMA_ENV_FILE = Path.home() / ".config" / "neotoma" / ".env"
if _NEOTOMA_ENV_FILE.exists():
    for _line in _NEOTOMA_ENV_FILE.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            _v = _v.strip().strip('"').strip("'")
            _existing = os.environ.get(_k.strip(), "")
            if not _existing or (_existing.startswith("__") and _existing.endswith("__")):
                os.environ[_k.strip()] = _v

# ── Logging ─────────────────────────────────────────────────────────────────
LOG_DIR = Path.home() / "Library" / "Logs" / "ateles"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "cyphorhinus.log"


class _FlushingFileHandler(logging.FileHandler):
    def emit(self, record: logging.LogRecord) -> None:
        super().emit(record)
        self.flush()


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [cyphorhinus] %(levelname)s %(message)s",
    handlers=[_FlushingFileHandler(LOG_FILE)],
)
log = logging.getLogger("cyphorhinus")

# ── Config ──────────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ.get("CYPHORHINUS_TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("CYPHORHINUS_TELEGRAM_CHAT_ID", "").strip()
NEOTOMA_BASE_URL = os.environ.get("NEOTOMA_BASE_URL", "http://localhost:3180").rstrip("/")
NEOTOMA_BEARER_TOKEN = os.environ.get("NEOTOMA_BEARER_TOKEN", "")
POLL_TIMEOUT = int(os.environ.get("CYPHORHINUS_POLL_TIMEOUT", "50"))

# Persist the getUpdates offset so we don't reprocess replies across restarts.
OFFSET_FILE = Path(__file__).parent / ".cyphorhinus_offset"


# ── Neotoma helpers ─────────────────────────────────────────────────────────
def _neotoma_query(entity_type: str, *, search: str | None = None, limit: int = 5) -> list[dict]:
    """POST /entities/query. Returns the entities list (empty on failure)."""
    if not NEOTOMA_BEARER_TOKEN:
        return []
    body: dict = {"entity_type": entity_type, "limit": limit, "include_snapshots": True}
    if search:
        body["search"] = search
    req = urllib.request.Request(
        f"{NEOTOMA_BASE_URL}/entities/query",
        data=json.dumps(body).encode(),
        method="POST",
        headers={
            "Authorization": f"Bearer {NEOTOMA_BEARER_TOKEN}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.load(r).get("entities", [])
    except Exception as exc:
        log.warning(f"Neotoma query failed ({entity_type}): {exc}")
        return []


def _find_job_by_message_id(message_id: int) -> dict | None:
    """Find the activity_log entity whose telegram_message_id matches.

    Returns a dict with job_id, agent, summary, status, and the activity_log
    entity_id — or None if no match.
    """
    # Pull recent activity_log entities and match in-process. (The query API has
    # no field-equality filter for arbitrary snapshot fields, so we scan recent.)
    entities = _neotoma_query("activity_log", limit=200)
    for e in entities:
        snap = e.get("snapshot") or {}
        inner = snap.get("snapshot") or snap
        if inner.get("telegram_message_id") == message_id:
            return {
                "entity_id": e.get("entity_id"),
                "job_id": inner.get("job_id"),
                "agent": inner.get("agent"),
                "summary": inner.get("summary"),
                "status": inner.get("status"),
            }
    return None


def _store_operator_followup(*, job: dict, reply_text: str, reply_message_id: int) -> str | None:
    """Write an operator_followup entity linked REFERS_TO the activity_log.

    Returns the new entity_id, or None on failure.
    """
    if not NEOTOMA_BEARER_TOKEN:
        return None
    payload = {
        "idempotency_key": f"followup-{job['job_id']}-{reply_message_id}",
        "observation_source": "human",
        "entities": [
            {
                "entity_type": "operator_followup",
                "canonical_name": f"followup:{job['job_id']}:{reply_message_id}",
                "job_id": job["job_id"],
                "agent": job["agent"],
                "content": reply_text,
                "in_reply_to_summary": job.get("summary", ""),
                "status": "pending",
            }
        ],
    }
    if job.get("entity_id"):
        payload["relationships"] = [
            {
                "relationship_type": "REFERS_TO",
                "source_index": 0,
                "target_entity_id": job["entity_id"],
            }
        ]
    req = urllib.request.Request(
        f"{NEOTOMA_BASE_URL}/store",
        data=json.dumps(payload).encode(),
        method="POST",
        headers={
            "Authorization": f"Bearer {NEOTOMA_BEARER_TOKEN}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.load(r)
            ents = data.get("entities", [])
            return ents[0].get("entity_id") if ents else None
    except Exception as exc:
        log.warning(f"Failed to store operator_followup: {exc}")
        return None


# ── Telegram helpers ────────────────────────────────────────────────────────
def _tg_get_updates(offset: int) -> list[dict]:
    """Long-poll getUpdates. Returns the list of update objects."""
    params = urllib.parse.urlencode(
        {"offset": offset, "timeout": POLL_TIMEOUT, "allowed_updates": json.dumps(["message"])}
    )
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates?{params}"
    try:
        with urllib.request.urlopen(url, timeout=POLL_TIMEOUT + 10) as r:
            data = json.load(r)
            if not data.get("ok"):
                log.warning(f"getUpdates not ok: {data}")
                return []
            return data.get("result", [])
    except urllib.error.URLError as exc:
        log.warning(f"getUpdates network error: {exc}")
        return []
    except Exception as exc:
        log.warning(f"getUpdates error: {exc}")
        return []


def _tg_send(text: str, reply_to: int | None = None) -> None:
    """Send a Telegram message (best-effort acknowledgement)."""
    payload: dict = {"chat_id": CHAT_ID, "text": text}
    if reply_to is not None:
        payload["reply_to_message_id"] = reply_to
    thread_id = os.environ.get("TELEGRAM_TOPIC_CYPHORHINUS", "").strip()
    if thread_id:
        payload["message_thread_id"] = int(thread_id)
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        data=json.dumps(payload).encode(),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception as exc:
        log.warning(f"sendMessage error: {exc}")


# ── Offset persistence ──────────────────────────────────────────────────────
def _load_offset() -> int:
    try:
        return int(OFFSET_FILE.read_text().strip())
    except Exception:
        return 0


def _save_offset(offset: int) -> None:
    try:
        OFFSET_FILE.write_text(str(offset))
    except Exception:
        pass


# ── Reply handling ──────────────────────────────────────────────────────────
def _handle_reply(message: dict) -> None:
    """Process one operator reply that quotes an activity message."""
    reply_to = message.get("reply_to_message") or {}
    replied_id = reply_to.get("message_id")
    text = (message.get("text") or "").strip()
    reply_msg_id = message.get("message_id")

    if not replied_id or not text:
        return  # not a reply, or empty — ignore (passive channel)

    job = _find_job_by_message_id(replied_id)
    if not job:
        log.info(f"Reply to message {replied_id} matched no known job — ignoring")
        _tg_send(
            "↪️ I couldn't match that to a tracked job (it may have scrolled out "
            "of the recent window). Try replying to a more recent activity line.",
            reply_to=reply_msg_id,
        )
        return

    followup_id = _store_operator_followup(
        job=job, reply_text=text, reply_message_id=reply_msg_id
    )
    if followup_id:
        log.info(
            f"operator_followup {followup_id} stored for job {job['job_id']} "
            f"(agent={job['agent']})"
        )
        _tg_send(
            f"✅ Routed to {job['agent']} (job {job['job_id']}). "
            f"It will pick this up on its next cycle.",
            reply_to=reply_msg_id,
        )
    else:
        _tg_send(
            "⚠️ Couldn't store your follow-up in Neotoma — try again shortly.",
            reply_to=reply_msg_id,
        )


# ── Main loop ───────────────────────────────────────────────────────────────
def main() -> None:
    if not BOT_TOKEN or not CHAT_ID:
        log.error(
            "CYPHORHINUS_TELEGRAM_BOT_TOKEN / CYPHORHINUS_TELEGRAM_CHAT_ID not set — "
            "cannot poll for replies. Exiting."
        )
        sys.exit(1)

    log.info("Cyphorhinus reply-router started. Long-polling CyphorhinusBot for replies.")
    offset = _load_offset()
    log.info(f"Resuming from getUpdates offset={offset}")

    while True:
        try:
            updates = _tg_get_updates(offset)
            for upd in updates:
                offset = max(offset, upd.get("update_id", 0) + 1)
                msg = upd.get("message")
                if not msg:
                    continue
                # Only handle messages from the configured operator chat.
                if str(msg.get("chat", {}).get("id")) != str(CHAT_ID):
                    continue
                try:
                    _handle_reply(msg)
                except Exception as exc:
                    log.warning(f"Error handling reply: {exc}")
            if updates:
                _save_offset(offset)
        except Exception as exc:
            log.error(f"Poll loop error: {exc}")
            time.sleep(5)


if __name__ == "__main__":
    main()
