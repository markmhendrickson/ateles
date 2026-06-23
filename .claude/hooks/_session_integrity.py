#!/usr/bin/env python3
"""Shared helpers for the Ateles session-integrity hooks.

Implements layer 1 of docs/session_integrity.md (the Claude Code lifecycle
hooks). These hooks are MECHANICAL and FAIL-OPEN: any unexpected error, a
missing bearer token, or an unreachable Neotoma must never crash a session
or block a clean exit on a false negative. Enforcement (the Stop finalizer)
blocks ONLY when it can positively determine the session is write-bearing
and unlinked/turn-less.

Design contract (see docs/session_integrity.md):
  - A session is "write-bearing" if it issued any non-bookkeeping Neotoma
    write. We approximate this from the transcript by detecting any Neotoma
    store/correct/create_relationship/submit_* tool use whose entity_type is
    not purely conversation/conversation_message bookkeeping.
  - An integral session has (a) >=1 bound plan and (b) >=1 stored turn pair.
  - Genuine no-op sessions (no writes) are exempt — never blocked (grace path).

State is kept in a per-session JSON file under the project .claude/.session_state/
keyed by session_id, so SessionStart / Stop share context without re-parsing.

No third-party imports — stdlib only (urllib), so the hook runs in any
environment without a venv.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

DEFAULT_PLAN_ID = "ent_99ace4dd6673aa36ed08b1fe"  # Ateles Agent Swarm Architecture plan
BOOKKEEPING_TYPES = {"conversation", "conversation_message", "agent_message"}
# Durable insight artifacts whose presence means the session captured a learning
# (used by the Stop hook's /end nudge — task #3 of the task-spine plan).
LEARNING_TYPES = {
    "learning", "note", "standing_rule", "architectural_decision",
    "strategy_drift_signal", "lesson", "recap_message",
}
NEOTOMA_BASE_URL = os.environ.get("NEOTOMA_BASE_URL", "https://neotoma.markmhendrickson.com")
BEARER_ENV = "NEOTOMA_BEARER_TOKEN"  # gitleaks:allow


def log(msg: str) -> None:
    """Diagnostic to stderr — never pollutes hook stdout (which is context/JSON)."""
    sys.stderr.write(f"[session-integrity] {msg}\n")


def read_hook_input() -> dict:
    """Claude Code passes a JSON event object on stdin. Fail-open to {}."""
    try:
        raw = sys.stdin.read()
        return json.loads(raw) if raw.strip() else {}
    except Exception as exc:  # noqa: BLE001 — fail open
        log(f"could not parse hook stdin: {exc}")
        return {}


def state_dir() -> Path:
    root = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    d = Path(root) / ".claude" / ".session_state"
    d.mkdir(parents=True, exist_ok=True)
    return d


def state_path(session_id: str) -> Path:
    safe = "".join(c for c in (session_id or "unknown") if c.isalnum() or c in "-_")
    return state_dir() / f"{safe or 'unknown'}.json"


def load_state(session_id: str) -> dict:
    p = state_path(session_id)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:  # noqa: BLE001
            return {}
    return {}


def save_state(session_id: str, state: dict) -> None:
    try:
        state_path(session_id).write_text(json.dumps(state))
    except Exception as exc:  # noqa: BLE001
        log(f"could not persist state: {exc}")


# ---------------------------------------------------------------------------
# Transcript inspection — the source of truth for "did this session write?"
# ---------------------------------------------------------------------------

def scan_transcript(transcript_path: str | None) -> dict:
    """Walk the JSONL transcript and summarize integrity-relevant signals.

    Returns: {turns: int, wrote_domain: bool, bound_plan: bool, write_types: set}
    Fail-open: an unreadable transcript yields a conservative no-op summary
    (turns=0, wrote_domain=False) so the finalizer does not block on it.
    """
    summary = {
        "turns": 0, "wrote_domain": False, "bound_plan": False,
        "bound_task": False, "captured_learning": False, "write_types": set(),
    }
    if not transcript_path or not os.path.exists(transcript_path):
        return summary
    try:
        with open(transcript_path, "r") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                _inspect_event(ev, summary)
    except Exception as exc:  # noqa: BLE001
        log(f"transcript scan failed (fail-open): {exc}")
    return summary


def _inspect_event(ev: dict, summary: dict) -> None:
    role = ev.get("role") or ev.get("type")
    if role in ("user", "assistant"):
        summary["turns"] += 1
    # Tool uses may be nested in message content blocks.
    for block in _iter_tool_uses(ev):
        name = (block.get("name") or "").lower()
        if not name:
            continue
        is_neotoma_write = (
            "store" in name or "correct" in name
            or "create_relationship" in name or "submit_" in name
        )
        if not is_neotoma_write:
            continue
        payload = block.get("input") or {}
        etypes = _entity_types_in(payload)
        # A write counts as domain (non-bookkeeping) if it touches any type
        # outside the bookkeeping set, OR it's a correct/relationship/submit
        # on a domain entity. Plan binding is detected from any plan touch.
        domain_types = etypes - BOOKKEEPING_TYPES
        if "plan" in etypes or _mentions_plan(payload):
            summary["bound_plan"] = True
        # Plan-optionality (task-spine plan): a session may anchor to a TASK
        # instead of a plan. A PART_OF link alongside a task entity counts.
        if _mentions_task_binding(payload):
            summary["bound_task"] = True
        if etypes & LEARNING_TYPES:
            summary["captured_learning"] = True
        if domain_types or "correct" in name or "submit_" in name or "create_relationship" in name:
            # create_relationship between two bookkeeping msgs is itself
            # bookkeeping; only count it as domain if a non-bookkeeping id appears.
            if "create_relationship" in name and not domain_types and not _mentions_plan(payload):
                continue
            summary["wrote_domain"] = True
            summary["write_types"].update(domain_types)


def _iter_tool_uses(ev: dict):
    content = ev.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                yield block
    msg = ev.get("message")
    if isinstance(msg, dict):
        yield from _iter_tool_uses(msg)


def _entity_types_in(payload: dict) -> set:
    types: set = set()
    if not isinstance(payload, dict):
        return types
    if isinstance(payload.get("entity_type"), str):
        types.add(payload["entity_type"])
    for ent in payload.get("entities", []) or []:
        if isinstance(ent, dict) and isinstance(ent.get("entity_type"), str):
            types.add(ent["entity_type"])
    return types


def _mentions_plan(payload: dict) -> bool:
    blob = json.dumps(payload) if payload else ""
    return '"plan"' in blob or "plan_id" in blob or DEFAULT_PLAN_ID in blob


def _mentions_task_binding(payload: dict) -> bool:
    """True when a write anchors the session to a task: a PART_OF relationship
    alongside a task entity in the same payload (the 'self-contained task stands
    alone' case). Loose by design, mirroring _mentions_plan — the goal is not to
    flag a session that legitimately anchored its work to a task instead of a
    plan. Linking to a pre-existing task by id alone is not detected here."""
    if not isinstance(payload, dict):
        return False
    rels = payload.get("relationships") or []
    has_part_of = any(
        "part_of" in str(r.get("relationship_type", "")).lower()
        for r in rels
        if isinstance(r, dict)
    )
    return has_part_of and "task" in _entity_types_in(payload)


# ---------------------------------------------------------------------------
# harness_event audit emission (best-effort, fail-open)
# ---------------------------------------------------------------------------

def emit_harness_event(session_id: str, summary: dict, integrity_status: str) -> None:
    """Write one harness_event recording the session integrity outcome.

    Schema 689230f4-cd83-49b6-baa7-a752cf70629d. Best-effort: no token or a
    network error is logged and swallowed — never blocks the hook.
    """
    token = os.environ.get(BEARER_ENV)
    if not token:
        log("no bearer token — skipping harness_event emission")
        return
    body = {
        "idempotency_key": f"harness-event-session-integrity-{session_id}-{int(time.time())}",
        "observation_source": "workflow_state",
        "entities": [{
            "entity_type": "harness_event",
            "event_type": "session_integrity_check",
            "harness": "claude_code",
            "session_id": session_id,
            "integrity_status": integrity_status,  # integral | violated | exempt
            "turns": summary.get("turns", 0),
            "wrote_domain": summary.get("wrote_domain", False),
            "bound_plan": summary.get("bound_plan", False),
            "bound_task": summary.get("bound_task", False),
            "captured_learning": summary.get("captured_learning", False),
            "write_types": sorted(summary.get("write_types", []) or []),
        }],
    }
    try:
        req = urllib.request.Request(
            f"{NEOTOMA_BASE_URL}/store",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            resp.read()
    except Exception as exc:  # noqa: BLE001
        log(f"harness_event emission failed (non-fatal): {exc}")
