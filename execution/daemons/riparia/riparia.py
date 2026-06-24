#!/usr/bin/env python3
"""
Riparia — email reply-routing daemon (E3 of the execution loop).

Riparia (sand martin, a hirundine) is the email successor to Cyphorhinus: where
Cyphorhinus long-polls Telegram for operator replies to activity messages, Riparia
polls the dedicated swarm Gmail inbox for operator replies to a task's execution-run
thread and routes them back to that run's conversation.

Flow (mirrors cyphorhinus.py; transport decoupled from execution per
decision:reply-routing-sse-neotoma-2026-05-27):

  - Apis sends run kickoff/outcome on a Gmail thread keyed by the task
    (lib.daemon_runtime.run_email, E2): subject carries a [#ent_<task_id>] token and
    the thread root Message-ID is <run-{task_id}-{run_key}@{domain}>.
  - The operator REPLIES in that thread to steer the run.
  - Riparia polls the swarm inbox, recovers the task id from the reply (subject token,
    or the References chain via run_email.parse_task_id), finds the run conversation
    (PART_OF the task), and appends the reply as an agent_message(role=user,
    sender_kind=operator) PART_OF that conversation (session_finalize.append_turn).
  - The responsible agent picks the reply up on its next cycle / via SSE.

Riparia ROUTES, it does not execute. Email REPLACES Telegram as the preferred
transport (decision:email_replaces_telegram_as_transport); Cyphorhinus is retired
once this daemon is proven live.

Environment:
  ATELES_SWARM_EMAIL        dedicated swarm mailbox (the From/To of run threads)
  OPERATOR_EMAIL            operator address (only their replies are routed)
  NEOTOMA_BASE_URL / NEOTOMA_BEARER_TOKEN
  RIPARIA_POLL_INTERVAL     seconds between polls (default 60)
  RIPARIA_MAX_FETCH         messages per poll (default 25)

Runs as a launchd agent. Fail-open throughout (stdlib + the daemon_runtime libs);
a missing token / gws / mailbox logs and no-ops, never crashes.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

# ── Path + env bootstrap (standalone script — add repo root for lib imports) ──
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

import httpx  # noqa: E402

from lib.daemon_runtime.run_email import parse_task_id  # noqa: E402
from lib.daemon_runtime.session_finalize import append_turn  # noqa: E402

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_DIR = Path.home() / "Library" / "Logs" / "ateles"
LOG_DIR.mkdir(parents=True, exist_ok=True)


class _FlushingFileHandler(logging.FileHandler):
    def emit(self, record: logging.LogRecord) -> None:
        super().emit(record)
        self.flush()


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [riparia] %(levelname)s %(message)s",
    handlers=[_FlushingFileHandler(LOG_DIR / "riparia.log")],
)
log = logging.getLogger("riparia")

# ── Config ──────────────────────────────────────────────────────────────────
DAEMON_NAME = "riparia"
SWARM_EMAIL = os.environ.get("ATELES_SWARM_EMAIL", "").strip()
OPERATOR_EMAIL = os.environ.get("OPERATOR_EMAIL", "").strip()
NEOTOMA_BASE_URL = os.environ.get("NEOTOMA_BASE_URL", "https://neotoma.markmhendrickson.com").rstrip("/")
NEOTOMA_BEARER_TOKEN = os.environ.get("NEOTOMA_BEARER_TOKEN", "")
POLL_INTERVAL = int(os.environ.get("RIPARIA_POLL_INTERVAL", "60"))
MAX_FETCH = int(os.environ.get("RIPARIA_MAX_FETCH", "25"))
PROCESSED_LABEL = "Riparia/processed"


# ── Pure routing logic (unit-tested) ─────────────────────────────────────────


def should_process(msg: dict, operator_email: str) -> bool:
    """True when a polled message is an unprocessed operator reply to a run thread.

    Requires: not already labelled processed; sender is the operator (when an
    operator address is configured); and the subject carries a recoverable task id.
    """
    if PROCESSED_LABEL in (msg.get("labels") or []):
        return False
    sender = (msg.get("sender") or "").lower()
    if operator_email and operator_email.lower() not in sender:
        return False
    return parse_task_id(msg.get("subject") or "") is not None


def _summary_of(conv: dict) -> str:
    snap = conv.get("snapshot") or {}
    inner = snap.get("snapshot") or snap
    return f"{inner.get('summary', '')} {inner.get('name', '')}"


def select_run_conversation(
    conversations: list[dict], task_id: str, run_key: str | None = None
) -> str | None:
    """Pick the run conversation for a task from query results.

    Run conversations are created with a summary containing 'task <id>' and
    'run <run_key>' (lib.daemon_runtime.session_finalize.build_run_conversation_payload).
    Prefer an exact run_key match; otherwise the most recent conversation that
    references the task. Returns the entity_id or None.
    """
    matches = [c for c in conversations if f"task {task_id}" in _summary_of(c)]
    if not matches:
        return None
    if run_key:
        keyed = [c for c in matches if f"run {run_key}" in _summary_of(c)]
        if keyed:
            matches = keyed

    def _ts(c: dict) -> str:
        return c.get("last_observation_at") or c.get("created_at") or ""

    matches.sort(key=_ts, reverse=True)
    return matches[0].get("entity_id") or matches[0].get("id")


# ── gws Gmail I/O (isolated, fail-open) ──────────────────────────────────────


def _poll_unread(max_count: int) -> list[dict]:
    """Poll the swarm inbox via `gws gmail +triage --format json`.

    Returns dicts: {id, sender, subject, labels}. Mirrors turdus._poll_gmail_messages.
    """
    try:
        result = subprocess.run(
            ["gws", "gmail", "+triage", "--max", str(max_count), "--format", "json"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            log.warning(f"[{DAEMON_NAME}] gws gmail +triage failed (rc={result.returncode}): "
                        f"{result.stderr[:200]}")
            return []
        data = json.loads(result.stdout)
        raw = data.get("messages", []) if isinstance(data, dict) else data
        if not isinstance(raw, list):
            return []
        return [{
            "id": m.get("id", ""),
            "sender": m.get("from", ""),
            "subject": m.get("subject", ""),
            "labels": m.get("labels", []),
        } for m in raw]
    except FileNotFoundError:
        log.warning(f"[{DAEMON_NAME}] gws CLI not found — Gmail polling unavailable")
        return []
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as exc:
        log.warning(f"[{DAEMON_NAME}] poll error: {exc}")
        return []


def _get_message_body(message_id: str) -> str:
    """Fetch the plain-text body via `gws gmail +read`. Best-effort → '' on failure.

    Confirmed against gws 0.22.5 (2026-06-24): `+read --id <id> --format json`
    returns body_text / body_html. +triage does not expose the body, so the routed
    reply text comes from here; on failure the caller falls back to the subject.
    """
    try:
        result = subprocess.run(
            ["gws", "gmail", "+read", "--id", message_id, "--format", "json"],
            capture_output=True, text=True, timeout=20,
        )
        if result.returncode != 0:
            return ""
        # gws prefixes a keyring line on stderr but stdout is clean JSON; be lenient.
        out = result.stdout
        start = out.find("{")
        data = json.loads(out[start:]) if start >= 0 else {}
        if isinstance(data, dict):
            return (data.get("body_text") or data.get("body_html") or "").strip()
    except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        pass
    return ""


def _label_processed(message_id: str) -> None:
    """Best-effort: mark a message handled so it is not re-polled."""
    try:
        subprocess.run(
            ["gws", "gmail", "messages", "label", message_id, "--add", PROCESSED_LABEL],
            capture_output=True, text=True, timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass


# ── Neotoma I/O ──────────────────────────────────────────────────────────────


def _find_run_conversations(task_id: str) -> list[dict]:
    """Query Neotoma for conversations referencing the task (fail-open [])."""
    if not NEOTOMA_BEARER_TOKEN:
        return []
    try:
        resp = httpx.post(
            f"{NEOTOMA_BASE_URL}/entities/query",
            headers={"Authorization": f"Bearer {NEOTOMA_BEARER_TOKEN}"},
            json={"entity_type": "conversation", "search": task_id, "limit": 25,
                  "include_snapshots": True},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json().get("entities", [])
    except Exception as exc:  # noqa: BLE001
        log.warning(f"[{DAEMON_NAME}] conversation query failed for {task_id}: {exc}")
        return []


def _route_reply(msg: dict) -> None:
    """Route one operator reply into its run conversation. Fail-open."""
    task_id = parse_task_id(msg.get("subject") or "")
    if not task_id:
        return
    conv_id = select_run_conversation(_find_run_conversations(task_id), task_id)
    if not conv_id:
        log.info(f"[{DAEMON_NAME}] no run conversation for task {task_id} "
                 f"(msg {msg.get('id')}) — leaving for a later poll")
        return
    body = _get_message_body(msg.get("id", "")) or f"(operator reply; body unavailable) re: {msg.get('subject','')}"
    ok = append_turn(
        conversation_id=conv_id, role="user", content=body, sender_kind="operator",
        idempotency_key=f"reply-{msg.get('id')}",
    )
    if ok:
        log.info(f"[{DAEMON_NAME}] routed operator reply (msg {msg.get('id')}) → "
                 f"conversation {conv_id} (task {task_id})")
        _label_processed(msg.get("id", ""))
    else:
        log.warning(f"[{DAEMON_NAME}] failed to append reply (msg {msg.get('id')}) "
                    f"to {conv_id} — will retry next poll (append is idempotent)")


# ── Main loop ─────────────────────────────────────────────────────────────────


def poll_once() -> int:
    """One poll pass. Returns the number of replies routed (best-effort)."""
    routed = 0
    for msg in _poll_unread(MAX_FETCH):
        if not should_process(msg, OPERATOR_EMAIL):
            continue
        try:
            _route_reply(msg)
            routed += 1
        except Exception as exc:  # noqa: BLE001 — one bad message must not stop the loop
            log.warning(f"[{DAEMON_NAME}] error routing msg {msg.get('id')}: {exc}")
    return routed


def main() -> None:
    if not SWARM_EMAIL:
        log.error("ATELES_SWARM_EMAIL not set — the dedicated swarm mailbox must be "
                  "provisioned and gws-authed before Riparia can route replies. Exiting.")
        sys.exit(1)
    log.info(f"Riparia reply-router started. Polling {SWARM_EMAIL} every {POLL_INTERVAL}s "
             f"for operator replies to run threads.")
    while True:
        try:
            n = poll_once()
            if n:
                log.info(f"[{DAEMON_NAME}] routed {n} operator repl{'y' if n == 1 else 'ies'}")
        except Exception as exc:  # noqa: BLE001
            log.error(f"[{DAEMON_NAME}] poll loop error: {exc}")
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
