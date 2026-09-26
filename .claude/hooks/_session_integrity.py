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

The foundation planning model supersedes the plan-binding convention, but the
workflow that replaces it is not implemented yet (ateles#965 and its blockers).
Until that cutover, this module also computes a *shadow* planning-spine audit.
It is emitted and tested, but it does not replace the compatibility classifier
above or retire the standing plan-maintenance policy prematurely.

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
# Transitional, current-instance field census for PM-5/PM-10.  The full
# planning-type registry and write refusal belong to ateles#965; shadow mode
# deliberately starts with the legacy `plan` type whose direct session writes
# are the collision mechanism the foundation names.
DERIVED_PLAN_PROGRESS_FIELDS = frozenset({
    "status",
    "outcome",
    "progress",
    "completion",
    "todos",
    "todos_pending",
    "next_steps",
    "blockers",
    "open_count",
    "terminal_count",
    "last_activity",
    "landed_fraction",
})
QUEUED_TASK_STATUSES = frozenset({"queued"})
NEOTOMA_BASE_URL = os.environ.get("NEOTOMA_BASE_URL", "https://neotoma.markmhendrickson.com")
BEARER_ENV = "NEOTOMA_BEARER_TOKEN"  # gitleaks:allow


def log(msg: str) -> None:
    """Diagnostic to stderr — never pollutes hook stdout (which is context/JSON)."""
    sys.stderr.write(f"[session-integrity] {msg}\n")


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
        "planning_spine_findings": [],
        "planning_spine_status": "not_observed",
        "queued_workstreams": [],
        # Internal counters used only while walking the ordered transcript.
        "_task_count": 0,
        "_current_workstream": None,
        "_pending_dispatches": 0,
        "_workboard_refreshed": False,
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
    _finalize_planning_spine(summary)
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
        payload = block.get("input") or {}

        # Dispatch is not a Neotoma write, but its position relative to task
        # creation is part of the sequential-admission checkpoint.
        if _is_dispatch_tool(name):
            if summary.get("_pending_dispatches", 0) > 0:
                summary["_pending_dispatches"] -= 1
            continue

        is_neotoma_write = (
            "store" in name or "correct" in name
            or "create_relationship" in name or "submit_" in name
        )
        if not is_neotoma_write:
            continue
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

        if "session_digest" in etypes or (
            payload.get("entity_type") == "session_digest"
        ):
            summary["_workboard_refreshed"] = True

        _audit_plan_progress_write(name, payload, summary)
        if "store" in name and "task" in etypes:
            _audit_task_store(payload, summary)

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


def _is_dispatch_tool(name: str) -> bool:
    """Whether a tool call actually sends work to an executor.

    Harness task chips are intentionally excluded: they are proposals, not
    dispatched work.  The current explicit paths are Ateles routing and the
    Claude Code Agent tool; more harness carriers can be added when their
    dispatch verb is known and tested.
    """
    lowered = (name or "").lower()
    return lowered == "agent" or lowered.endswith("__route_task")


def _task_entities(payload: dict) -> list[dict]:
    if not isinstance(payload, dict):
        return []
    return [
        entity for entity in (payload.get("entities") or [])
        if isinstance(entity, dict) and entity.get("entity_type") == "task"
    ]


def _part_of_targets(payload: dict) -> list[str]:
    targets: list[str] = []
    if not isinstance(payload, dict):
        return targets
    for relationship in payload.get("relationships") or []:
        if not isinstance(relationship, dict):
            continue
        if str(relationship.get("relationship_type", "")).upper() != "PART_OF":
            continue
        target = relationship.get("target_entity_id") or relationship.get("target_ref")
        targets.append(str(target or "<unresolved>"))
    return targets


def _add_planning_finding(summary: dict, code: str, **detail: object) -> None:
    finding = {"code": code, **detail}
    if finding not in summary["planning_spine_findings"]:
        summary["planning_spine_findings"].append(finding)


def _audit_plan_progress_write(name: str, payload: dict, summary: dict) -> None:
    """Record direct session writes to derived progress on the legacy plan type."""
    if not isinstance(payload, dict):
        return
    fields: set[str] = set()
    if "correct" in name and payload.get("entity_type") == "plan":
        field = payload.get("field") or payload.get("field_name")
        if isinstance(field, str):
            fields.add(field)
    if "store" in name:
        for entity in payload.get("entities") or []:
            if isinstance(entity, dict) and entity.get("entity_type") == "plan":
                fields.update(str(field) for field in entity if field != "entity_type")
    forbidden = sorted(fields & DERIVED_PLAN_PROGRESS_FIELDS)
    if forbidden:
        _add_planning_finding(
            summary,
            "session_authored_planning_progress",
            fields=forbidden,
        )


def _audit_task_store(payload: dict, summary: dict) -> None:
    """Audit task ascent and sequential workstream admission in one store call.

    Today task creation calls contain one task and relationship list.  A batch
    containing several tasks cannot prove which implicit relationship belongs
    to which task, so it is reported as unobservable rather than guessed.
    """
    tasks = _task_entities(payload)
    if not tasks:
        return
    targets = _part_of_targets(payload)
    for task in tasks:
        summary["_task_count"] += 1
        task_index = summary["_task_count"]
        if len(tasks) != 1:
            _add_planning_finding(
                summary,
                "task_ascent_unobservable",
                task_index=task_index,
                reason="multiple_tasks_in_one_store",
            )
        elif not targets:
            _add_planning_finding(summary, "task_missing_ascent", task_index=task_index)
        elif len(targets) > 1:
            _add_planning_finding(
                summary,
                "task_multiple_ascents",
                task_index=task_index,
                part_of_count=len(targets),
            )

        # Domain is the explicit workstream key.  Where it is absent, the one
        # ascent target is the best observable boundary; if neither exists the
        # audit has already reported that it cannot prove an ascent.
        domain = task.get("domain")
        workstream = str(domain or (targets[0] if len(targets) == 1 else ""))
        if not workstream:
            continue
        status = str(task.get("status") or "").lower()
        current = summary.get("_current_workstream")
        if current is None:
            if status in QUEUED_TASK_STATUSES:
                _append_unique(summary["queued_workstreams"], workstream)
                continue
            _start_workstream(summary, workstream, task)
            continue
        if workstream == current:
            if status not in QUEUED_TASK_STATUSES and not _already_dispatched(task):
                summary["_pending_dispatches"] += 1
            continue
        if status in QUEUED_TASK_STATUSES:
            _append_unique(summary["queued_workstreams"], workstream)
            continue
        if not _admission_checkpoint_complete(summary):
            _add_planning_finding(
                summary,
                "workstream_admitted_before_checkpoint",
                task_index=task_index,
                workstream=workstream,
                missing=_missing_checkpoint_parts(summary),
            )
            continue
        _start_workstream(summary, workstream, task)


def _already_dispatched(task: dict) -> bool:
    executor = str(task.get("executor") or "").strip().lower()
    return bool(executor and executor not in {"unassigned", "unassigned; claim through ateles swarm"})


def _start_workstream(summary: dict, workstream: str, task: dict) -> None:
    summary["_current_workstream"] = workstream
    summary["_workboard_refreshed"] = False
    summary["_pending_dispatches"] = 0 if _already_dispatched(task) else 1
    if workstream in summary["queued_workstreams"]:
        summary["queued_workstreams"].remove(workstream)


def _admission_checkpoint_complete(summary: dict) -> bool:
    return bool(summary.get("_workboard_refreshed")) and summary.get("_pending_dispatches", 0) == 0


def _missing_checkpoint_parts(summary: dict) -> list[str]:
    missing = []
    if not summary.get("_workboard_refreshed"):
        missing.append("workboard_refresh")
    if summary.get("_pending_dispatches", 0) > 0:
        missing.append("dispatch")
    return missing


def _append_unique(items: list, value: object) -> None:
    if value not in items:
        items.append(value)


def _finalize_planning_spine(summary: dict) -> None:
    if summary["planning_spine_findings"]:
        summary["planning_spine_status"] = "violated"
    elif summary.get("_task_count", 0):
        summary["planning_spine_status"] = "ready"
    else:
        summary["planning_spine_status"] = "not_observed"


# ---------------------------------------------------------------------------
# harness_event audit emission (best-effort, fail-open)
# ---------------------------------------------------------------------------

def emit_harness_event_raw(
    idempotency_slug: str, entity_fields: dict, log_tag: str = "harness-event",
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

    Schema 689230f4-cd83-49b6-baa7-a752cf70629d. Best-effort: no token or a
    network error is logged and swallowed — never blocks the hook.
    """
    token = os.environ.get(BEARER_ENV)
    if not token:
        sys.stderr.write(f"[{log_tag}] no bearer token — skipping harness_event emission\n")
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
            f"{NEOTOMA_BASE_URL}/store",
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
    planning_codes = [
        finding.get("code", "unknown")
        for finding in summary.get("planning_spine_findings", [])
        if isinstance(finding, dict)
    ]
    emit_harness_event_raw(f"session-integrity-{session_id}", {
        "event_type": "session_integrity_check",
        "session_id": session_id,
        # Declared harness_event fields.  The historical detail fields below
        # are preserved for compatibility; these two make the outcome visible
        # on ordinary snapshot reads instead of only in raw_fragments.
        "status": integrity_status,
        "summary": (
            "Session integrity " + integrity_status
            + "; planning-spine shadow audit "
            + summary.get("planning_spine_status", "not_observed")
            + (f" ({', '.join(planning_codes)})" if planning_codes else "")
        ),
        "integrity_status": integrity_status,  # integral | violated | exempt
        "turns": summary.get("turns", 0),
        "wrote_domain": summary.get("wrote_domain", False),
        "bound_plan": summary.get("bound_plan", False),
        "bound_task": summary.get("bound_task", False),
        "captured_learning": summary.get("captured_learning", False),
        "write_types": sorted(summary.get("write_types", []) or []),
        "planning_spine_status": summary.get("planning_spine_status", "not_observed"),
        "planning_spine_findings": summary.get("planning_spine_findings", []),
        "queued_workstreams": summary.get("queued_workstreams", []),
    }, log_tag="session-integrity")
