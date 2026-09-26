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
_DEFAULT_BASE_URL = "https://neotoma.markmhendrickson.com"
NEOTOMA_BASE_URL = os.environ.get("NEOTOMA_BASE_URL", _DEFAULT_BASE_URL)
BEARER_ENV = "NEOTOMA_BEARER_TOKEN"  # gitleaks:allow
_NEOTOMA_ENV_PATH = Path.home() / ".config" / "neotoma" / ".env"


def log(msg: str) -> None:
    """Diagnostic to stderr — never pollutes hook stdout (which is context/JSON)."""
    sys.stderr.write(f"[session-integrity] {msg}\n")


# ---------------------------------------------------------------------------
# Neotoma credentials (ateles#1261 follow-up, audit ent_b66293f0dcc8c887d4fdbeae)
# ---------------------------------------------------------------------------
#
# stop_finalizer.py skipped ALL 45 runs in the audited session with "no
# bearer token" — the Stop hook's environment simply never carries
# NEOTOMA_BEARER_TOKEN, so the session-integrity layer never ran at all for
# that session's whole lifetime. `emit_harness_event_raw` used to read only
# `os.environ.get(BEARER_ENV)` and silently return when it was empty.
#
# `_neotoma_credentials()` is the ONE place that changes: env first, then a
# fallback read of `~/.config/neotoma/.env` (same file, same two keys,
# `execution/scripts/sync_skills.py`'s `_load_env` and
# `execution/scripts/session_language.py`'s `_bearer_token` already read this
# way — reusing that shape rather than inventing a third). Parses only
# NEOTOMA_BASE_URL / NEOTOMA_BEARER_TOKEN; never logs either value.
#
# Read at CALL time, not import time — a test (or a long-lived process) that
# changes the environment or the file between calls must see the change.


def _read_neotoma_env_file() -> dict[str, str]:
    """Parse `NEOTOMA_BASE_URL` / `NEOTOMA_BEARER_TOKEN` out of
    ~/.config/neotoma/.env, if it exists and is readable. Never raises —
    an absent, unreadable, or malformed file yields {} (fail open; the
    caller falls back to the process environment, which may also be
    empty — that is the "still missing" case the warning covers, not a
    condition this function itself needs to distinguish)."""
    found: dict[str, str] = {}
    try:
        if not _NEOTOMA_ENV_PATH.exists():
            return found
        for line in _NEOTOMA_ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if key not in ("NEOTOMA_BASE_URL", BEARER_ENV):
                continue
            found[key] = value.strip().strip('"').strip("'")
    except OSError:
        return {}
    return found


def neotoma_credentials() -> tuple[str, str]:
    """(base_url, token) — environment first, then
    ~/.config/neotoma/.env for whichever of the two is still missing.
    Either or both may come back empty; the caller decides what that means
    (emit_harness_event_raw treats an empty token as "still missing" and
    warns once per session rather than skipping silently, per audit
    ent_b66293f0dcc8c887d4fdbeae).
    """
    base_url = os.environ.get("NEOTOMA_BASE_URL", "")
    token = os.environ.get(BEARER_ENV, "")
    if not base_url or not token:
        from_file = _read_neotoma_env_file()
        if not base_url:
            base_url = from_file.get("NEOTOMA_BASE_URL", "")
        if not token:
            token = from_file.get(BEARER_ENV, "")
    return (base_url or _DEFAULT_BASE_URL), token


def _warn_missing_credentials_once(session_id: str, log_tag: str) -> None:
    """One VISIBLE stderr warning per session when credentials are still
    missing after checking both the environment and the dotenv fallback —
    replacing the silent per-invocation skip audit ent_b66293f0dcc8c887d4fdbeae
    found (45 skips, zero visible warnings, nothing stored or checked for the
    whole session). Tracked in this session's state file so a Stop hook that
    fires more than once (multiple sibling hooks, or a retried Stop) warns
    exactly once rather than once per hook per stop.
    """
    state = load_state(session_id) if session_id else {}
    if state.get("warned_missing_neotoma_credentials"):
        return
    sys.stderr.write(
        f"[{log_tag}] WARNING: NEOTOMA_BEARER_TOKEN is not set in the "
        "environment and could not be found in ~/.config/neotoma/.env — "
        "this session's session-integrity checks (harness_event emission, "
        "Stop-hook enforcement) are being SKIPPED, not silently passed. Set "
        "NEOTOMA_BEARER_TOKEN in the environment or in "
        "~/.config/neotoma/.env to restore them.\n"
    )
    if session_id:
        state["warned_missing_neotoma_credentials"] = True
        save_state(session_id, state)


def read_hook_input() -> dict:
    """Claude Code passes a JSON event object on stdin. Fail-open to {}.

    The harness contract is always a JSON object, but `json.loads` will
    happily parse a top-level array/string/number/null too — every caller
    immediately does `ev.get(...)`, so a non-dict result must be coerced to
    `{}` here rather than left to raise `AttributeError` in six different
    call sites. Found while adding `test_decision_shape_gate.py`: the fix
    was first patched locally into that one hook's own stdin-parsing inline
    copy, which is the "extend the mechanism that already generalizes, not a
    parallel one" mistake CLAUDE.md names — moved here so every caller of
    this shared helper gets it, not just one.
    """
    try:
        raw = sys.stdin.read()
        ev = json.loads(raw) if raw.strip() else {}
        return ev if isinstance(ev, dict) else {}
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

def emit_harness_event_raw(
    idempotency_slug: str, entity_fields: dict, log_tag: str = "harness-event",
    session_id: str = "",
) -> None:
    """POST one `harness_event` entity to Neotoma. Shared by every Stop hook
    in this checkout that emits an audit row — `emit_harness_event` below
    (session-integrity) and `decision_shape_gate.py`'s decision-shape check.

    `idempotency_slug` becomes `harness-event-{idempotency_slug}-{timestamp}`.
    `entity_fields` is merged onto the required `entity_type`/`harness` keys
    as-is, so each caller owns its own event vocabulary (e.g. `event_type`,
    `integrity_status`) rather than this function imposing one shape on all
    callers — the two current callers intentionally use different schemas for
    what "the outcome" means (a tri-state enum vs. a bool + finding list).
    `log_tag` names the caller in stderr diagnostics, so a missing-token or
    network-failure line points at the hook that actually failed rather than
    always reading "[session-integrity]" regardless of which hook called in.
    `session_id`, when given, scopes the once-per-session missing-credentials
    warning (see `_warn_missing_credentials_once`) — omitted, the warning is
    still printed but not deduplicated across calls.

    CREDENTIALS (ateles#1261 follow-up, audit ent_b66293f0dcc8c887d4fdbeae):
    `neotoma_credentials()` checks the environment first, then falls back to
    ~/.config/neotoma/.env — the same fallback `sync_skills.py` /
    `session_language.py` already use — so a Stop hook whose environment
    simply lacks NEOTOMA_BEARER_TOKEN (the exact condition that skipped ALL
    45 runs in the audited session) still gets credentials when the dotenv
    file has them. When BOTH are still empty after the fallback, this emits
    one VISIBLE warning per session (not a silent skip) and returns.

    Schema 689230f4-cd83-49b6-baa7-a752cf70629d. Best-effort otherwise: a
    network error is logged and swallowed — never blocks the hook.
    """
    base_url, token = neotoma_credentials()
    if not token:
        _warn_missing_credentials_once(session_id, log_tag)
        return
    body = {
        "idempotency_key": f"harness-event-{idempotency_slug}-{int(time.time())}",
        "observation_source": "workflow_state",
        "entities": [{
            "entity_type": "harness_event",
            "harness": "claude_code",
            **entity_fields,
        }],
    }
    try:
        req = urllib.request.Request(
            f"{base_url}/store",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            resp.read()
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"[{log_tag}] harness_event emission failed (non-fatal): {exc}\n")


def emit_harness_event(session_id: str, summary: dict, integrity_status: str) -> None:
    """Write one harness_event recording the session integrity outcome.

    Schema 689230f4-cd83-49b6-baa7-a752cf70629d. Best-effort: no token or a
    network error is logged and swallowed — never blocks the hook (enforced
    by emit_harness_event_raw).
    """
    emit_harness_event_raw(f"session-integrity-{session_id}", {
        "event_type": "session_integrity_check",
        "session_id": session_id,
        "integrity_status": integrity_status,  # integral | violated | exempt
        "turns": summary.get("turns", 0),
        "wrote_domain": summary.get("wrote_domain", False),
        "bound_plan": summary.get("bound_plan", False),
        "bound_task": summary.get("bound_task", False),
        "captured_learning": summary.get("captured_learning", False),
        "write_types": sorted(summary.get("write_types", []) or []),
    }, log_tag="session-integrity", session_id=session_id)
