"""
lib/daemon_runtime/task_lifecycle.py — the task status state machine.

Task #1 of the "Task-spine loop + cloud-hosted swarm" plan
(Neotoma plan ent_aff87747b49e338790568af6). Promotes `task.status` from an
ad-hoc string the dispatcher only *logged* into a real lifecycle that the
dispatcher *owns* and writes at every transition, so no task can silently fall
off the loop:

    pending ─▶ routed ─▶ executing ─▶ verified ─▶ done        (happy path)
                 │           │
                 │           ├─▶ failed ─▶ routed   (retry, by the watchdog)
                 │           └─▶ failed ─▶ blocked  (retries exhausted)
                 ├─▶ awaiting_approval ─▶ routed | declined   (gate checkpoint)
              blocked ─▶ routed                     (operator remediation)

Design split (so this module stays focused and the SSE loop never blocks):
  * THIS module owns the *vocabulary*, the *transition graph*, the *retry
    policy* (max attempts + backoff schedule), and the status-write I/O.
  * Apis (the dispatcher) writes ROUTED / EXECUTING / DONE / FAILED /
    AWAITING_APPROVAL inline as it dispatches — no inline sleeping.
  * The stall watchdog (plan task ent_3cdd75de7cd279e7170c2ac8) consumes the
    retry policy here to re-dispatch FAILED tasks with backoff out-of-band and
    escalate once attempts are exhausted.

I/O mirrors gating.py exactly: httpx + bearer token, fail-OPEN — a status write
that can't reach Neotoma logs and returns False but never raises into the
dispatch loop. Only declared task-schema fields are written (status,
blocked_reason, result, remediation_id); the persistent per-task attempt counter
needs a new schema field and is tracked as a follow-up (the watchdog can hold
attempts in memory until then).
"""

from __future__ import annotations

import logging
import os
from enum import Enum

import httpx

log = logging.getLogger(__name__)

NEOTOMA_BASE_URL = os.environ.get(
    "NEOTOMA_BASE_URL", "https://neotoma.markmhendrickson.com"
)
NEOTOMA_BEARER_TOKEN = os.environ.get("NEOTOMA_BEARER_TOKEN", "")


class TaskStatus(str, Enum):
    """Canonical task lifecycle vocabulary.

    Values that already existed in prod data (pending, done, blocked, declined,
    superseded) keep their spelling so this layers onto live tasks without a
    migration; the new states (routed, executing, verified, failed,
    awaiting_approval) name the transitions the dispatcher now makes explicit.
    """

    PENDING = "pending"               # created, not yet routed
    ROUTED = "routed"                 # dispatcher resolved an owner/skill
    EXECUTING = "executing"           # T4 agent subprocess spawned
    VERIFIED = "verified"             # outcome checked (optional pre-done gate)
    DONE = "done"                     # terminal success
    FAILED = "failed"                 # transient failure; watchdog may retry
    BLOCKED = "blocked"               # needs operator (retries exhausted / blocker)
    AWAITING_APPROVAL = "awaiting_approval"  # held at a gate checkpoint
    AWAITING_INPUT = "awaiting_input"  # parked: under-specified, needs operator context (readiness gate)
    DECLINED = "declined"             # operator rejected
    SUPERSEDED = "superseded"         # replaced by another task


# Terminal states the dispatcher/watchdog never transition OUT of automatically.
# (BLOCKED is intentionally NOT terminal — operator remediation reopens it.)
TERMINAL: frozenset[str] = frozenset(
    {TaskStatus.DONE.value, TaskStatus.DECLINED.value, TaskStatus.SUPERSEDED.value}
)

# States that mean "work may still be dispatched from here".
ACTIVE: frozenset[str] = frozenset(
    {
        TaskStatus.PENDING.value,
        TaskStatus.ROUTED.value,
        TaskStatus.EXECUTING.value,
        TaskStatus.VERIFIED.value,
        TaskStatus.FAILED.value,
        TaskStatus.BLOCKED.value,
        TaskStatus.AWAITING_APPROVAL.value,
        TaskStatus.AWAITING_INPUT.value,
    }
)

# Allowed transitions. Keyed by from-state → set of legal to-states.
_TRANSITIONS: dict[str, frozenset[str]] = {
    TaskStatus.PENDING.value: frozenset(
        {
            TaskStatus.ROUTED.value,
            TaskStatus.AWAITING_APPROVAL.value,
            TaskStatus.AWAITING_INPUT.value,
            TaskStatus.BLOCKED.value,
            TaskStatus.DECLINED.value,
            TaskStatus.SUPERSEDED.value,
        }
    ),
    TaskStatus.ROUTED.value: frozenset(
        {
            TaskStatus.EXECUTING.value,
            TaskStatus.AWAITING_APPROVAL.value,
            TaskStatus.AWAITING_INPUT.value,
            TaskStatus.BLOCKED.value,
            TaskStatus.DECLINED.value,
        }
    ),
    TaskStatus.EXECUTING.value: frozenset(
        {
            TaskStatus.VERIFIED.value,
            TaskStatus.DONE.value,
            TaskStatus.FAILED.value,
            TaskStatus.BLOCKED.value,
        }
    ),
    TaskStatus.VERIFIED.value: frozenset(
        {TaskStatus.DONE.value, TaskStatus.FAILED.value, TaskStatus.BLOCKED.value}
    ),
    TaskStatus.FAILED.value: frozenset(
        {TaskStatus.ROUTED.value, TaskStatus.BLOCKED.value, TaskStatus.DECLINED.value}
    ),
    TaskStatus.AWAITING_APPROVAL.value: frozenset(
        {TaskStatus.ROUTED.value, TaskStatus.DECLINED.value, TaskStatus.BLOCKED.value}
    ),
    # Operator supplies the missing context → re-route; or decline/block.
    TaskStatus.AWAITING_INPUT.value: frozenset(
        {TaskStatus.ROUTED.value, TaskStatus.DECLINED.value, TaskStatus.BLOCKED.value}
    ),
    # Operator remediation reopens a blocked task for re-dispatch.
    TaskStatus.BLOCKED.value: frozenset(
        {TaskStatus.ROUTED.value, TaskStatus.DECLINED.value, TaskStatus.SUPERSEDED.value}
    ),
}


def normalize(status: str | None) -> str:
    return (status or "").strip().lower()


def can_transition(from_status: str | None, to_status: str | None) -> bool:
    """True if from→to is a legal lifecycle transition.

    Unknown from-states are permissive (return True) so this never blocks a
    write on legacy/ad-hoc status values it doesn't recognize — the goal is to
    *record* progress, not to police it. Re-entering the same state is allowed
    (idempotent SSE replays / re-dispatch)."""
    f = normalize(from_status)
    t = normalize(to_status)
    if not t:
        return False
    if f == t:
        return True  # idempotent re-entry / SSE replay
    if f in TERMINAL:
        return False  # terminal states never move (DONE/DECLINED/SUPERSEDED)
    if f not in _TRANSITIONS:
        return True  # unknown (legacy/ad-hoc) origin → don't block
    return t in _TRANSITIONS[f]


# ── Retry policy (consumed by the stall watchdog, plan task ent_3cdd75…) ──────

# Max dispatch attempts before a FAILED task is escalated to BLOCKED.
MAX_ATTEMPTS = max(1, int(os.environ.get("APIS_MAX_TASK_ATTEMPTS", "3")))
# Exponential backoff base + cap (seconds).
_BACKOFF_BASE = max(1, int(os.environ.get("APIS_RETRY_BACKOFF_BASE_SECONDS", "30")))
_BACKOFF_CAP = max(_BACKOFF_BASE, int(os.environ.get("APIS_RETRY_BACKOFF_CAP_SECONDS", "900")))


def backoff_seconds(attempt: int) -> int:
    """Backoff before the Nth retry (attempt is 1-based). 30s, 60s, 120s, … capped."""
    n = max(1, attempt)
    return min(_BACKOFF_CAP, _BACKOFF_BASE * (2 ** (n - 1)))


def attempts_exhausted(attempt: int) -> bool:
    return attempt >= MAX_ATTEMPTS


# ── Status write I/O (fail-open, mirrors gating.mark_task_declined) ───────────


def _correct(entity_id: str, field: str, value, idempotency_key: str) -> bool:
    if not NEOTOMA_BEARER_TOKEN:
        log.warning("[lifecycle] no bearer token — cannot write %s=%r", field, value)
        return False
    body = {
        "entity_id": entity_id,
        "entity_type": "task",
        "field": field,
        "value": value,
        "idempotency_key": idempotency_key,
    }
    try:
        resp = httpx.post(
            f"{NEOTOMA_BASE_URL}/correct",
            headers={"Authorization": f"Bearer {NEOTOMA_BEARER_TOKEN}"},
            json=body,
            timeout=15,
        )
        resp.raise_for_status()
        return True
    except Exception as exc:  # noqa: BLE001 — fail open, never crash dispatch
        log.warning("[lifecycle] failed to write task %s %s: %s", entity_id, field, exc)
        return False


def set_task_status(
    task_entity_id: str,
    status: "TaskStatus | str",
    *,
    handler: str,
    from_status: str | None = None,
    reason: str | None = None,
    result: str | None = None,
    remediation_id: str | None = None,
    key_suffix: str = "",
) -> bool:
    """Write a task's lifecycle status (plus optional companions) to Neotoma.

    Best-effort and fail-open: returns True only if the status write succeeded.
    Logs (but does not refuse) a transition the graph considers illegal, so an
    unexpected status value is still recorded rather than dropped.

    Companion fields are written when supplied:
      reason          → blocked_reason   (why it stalled/blocked)
      result          → result           (outcome summary on success)
      remediation_id  → remediation_id   (link to the operator's fix)

    `key_suffix` disambiguates the idempotency key so re-entering a state across
    distinct dispatch triggers (created / approved / retry) is not deduped into a
    no-op. Same logical transition + same suffix is idempotent on SSE replay.
    """
    value = status.value if isinstance(status, TaskStatus) else normalize(status)

    if from_status is not None and not can_transition(from_status, value):
        log.info(
            "[lifecycle] unusual transition %s→%s for task %s (writing anyway)",
            normalize(from_status), value, task_entity_id,
        )

    suffix = f"-{key_suffix}" if key_suffix else ""
    ok = _correct(
        task_entity_id, "status", value,
        idempotency_key=f"taskstatus-{handler}-{task_entity_id}-{value}{suffix}",
    )
    if reason is not None:
        _correct(
            task_entity_id, "blocked_reason", reason,
            idempotency_key=f"taskreason-{handler}-{task_entity_id}-{value}{suffix}",
        )
    if result is not None:
        _correct(
            task_entity_id, "result", result,
            idempotency_key=f"taskresult-{handler}-{task_entity_id}-{value}{suffix}",
        )
    if remediation_id is not None:
        _correct(
            task_entity_id, "remediation_id", remediation_id,
            idempotency_key=f"taskremediation-{handler}-{task_entity_id}{suffix}",
        )
    return ok


# ── Self-test (pure logic; run once the model/classifier outage clears) ───────


def _selftest() -> int:
    checks: dict[str, bool] = {}

    # Vocabulary / sets
    checks["done_terminal"] = TaskStatus.DONE.value in TERMINAL
    checks["blocked_not_terminal"] = TaskStatus.BLOCKED.value not in TERMINAL
    checks["executing_active"] = TaskStatus.EXECUTING.value in ACTIVE

    # Happy path
    checks["pending_to_routed"] = can_transition("pending", "routed")
    checks["routed_to_executing"] = can_transition("routed", "executing")
    checks["executing_to_done"] = can_transition("executing", "done")
    checks["verified_to_done"] = can_transition("verified", "done")

    # Failure / recovery
    checks["executing_to_failed"] = can_transition("executing", "failed")
    checks["failed_to_routed_retry"] = can_transition("failed", "routed")
    checks["failed_to_blocked"] = can_transition("failed", "blocked")
    checks["blocked_reopen"] = can_transition("blocked", "routed")

    # Gate
    checks["pending_to_awaiting"] = can_transition("pending", "awaiting_approval")
    checks["awaiting_to_declined"] = can_transition("awaiting_approval", "declined")

    # Readiness gate (awaiting_input)
    checks["routed_to_awaiting_input"] = can_transition("routed", "awaiting_input")
    checks["awaiting_input_to_routed"] = can_transition("awaiting_input", "routed")
    checks["awaiting_input_active"] = TaskStatus.AWAITING_INPUT.value in ACTIVE

    # Illegal / guards
    checks["done_is_terminal_move"] = not can_transition("done", "executing")
    checks["no_skip_pending_to_done"] = not can_transition("pending", "done")
    checks["same_state_ok"] = can_transition("executing", "executing")
    checks["unknown_origin_permissive"] = can_transition("weird_legacy", "done")
    checks["case_insensitive"] = can_transition("PENDING", "Routed")

    # Retry policy
    checks["backoff_monotonic"] = backoff_seconds(1) < backoff_seconds(2) < backoff_seconds(3)
    checks["backoff_capped"] = backoff_seconds(99) <= _BACKOFF_CAP
    checks["exhaust"] = attempts_exhausted(MAX_ATTEMPTS) and not attempts_exhausted(1)

    ok = all(checks.values())
    for k, v in checks.items():
        print(f"[{'PASS' if v else 'FAIL'}] {k}")
    return 0 if ok else 1


if __name__ == "__main__":
    import sys

    sys.exit(_selftest())
