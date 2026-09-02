"""
lib/daemon_runtime/block_kind.py — typed blocks, so a re-evaluation sweep is
safe by construction rather than by careful regex.

Motivation
----------
Today ``task.blocked_reason`` is free text. A sweep that wants to unblock
work has to pattern-match prose, and the two cases it must distinguish read
almost identically:

    "low confidence and high blast radius — propose alternatives"
        ← a MECHANICAL inference. Under ateles#682 this fired on tasks whose
          only output was a written report. Re-evaluating it is correct.

    "operator must decide whether to make Neotoma a hard dependency"
        ← an OPERATOR judgment. Clearing this automatically would mean the
          checkpoint mechanism does not exist.

Both are "blocked"; only one may be swept. Regex over free text either clears
too much or too little, so the gate records **what kind** of block it wrote and
**what condition would clear it** at the moment it writes the block.

The classes
-----------
``REEVALUABLE``   — the reason is a *condition the swarm can re-check itself*:
                    a mechanical inference, an unreachable dependency, a missing
                    route, a red CI run. A sweep may re-run the original check
                    and clear the block if it now passes.

``OPERATOR``      — the reason IS the operator. Irreversible, consequential, or
                    judgment-bearing work. **No sweep may ever clear one.** Only
                    an explicit operator resolution (``resolve_checkpoint``)
                    moves it.

Absent or unrecognized → treated as ``OPERATOR``. Fail-closed: an untyped block
predating this module is never swept.

Loop safety
-----------
Apis has twice produced notification storms from re-assertion (131 pages from
one escalation loop; 500+ emails from unbounded notification). A sweep that
clears a block and re-blocks it next pass would be a third. Two invariants:

* ``sweep_count`` increments on every re-evaluation. Past
  ``MAX_SWEEP_ATTEMPTS`` the block is promoted to ``OPERATOR`` — a condition
  that has failed to clear N times is no longer mechanical, it is a standing
  problem that needs a human.
* Re-evaluation NEVER notifies. The original block already notified once. A
  sweep that clears a block lets the normal dispatch path speak; a sweep that
  fails to clear one stays silent.
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

# A condition that has resisted this many re-evaluations stops being treated as
# mechanical. Deliberately small: the point is to fail into human attention
# quickly, not to retry indefinitely.
MAX_SWEEP_ATTEMPTS = 3


class BlockKind(str, Enum):
    """Whether a block may ever be cleared without the operator."""

    REEVALUABLE = "reevaluable"
    OPERATOR = "operator"


# Machine-readable conditions a sweep knows how to re-check. Each names what
# would have to become true for the block to clear.
class ClearCondition(str, Enum):
    #: The gate re-runs and returns AUTO_EXECUTE (e.g. because action_type and
    #: confidence are now populated where they previously defaulted).
    GATE_REEVALUATES_CLEAN = "gate_reevaluates_clean"
    #: A route/owner can now be resolved for the task.
    OWNER_RESOLVABLE = "owner_resolvable"
    #: The task snapshot can be read (Neotoma was unreachable when blocked).
    SNAPSHOT_HYDRATES = "snapshot_hydrates"
    #: Required inputs named in the readiness assessment are now present.
    MISSING_INPUTS_SUPPLIED = "missing_inputs_supplied"
    #: Only the operator clears this. Never swept.
    OPERATOR_DECISION = "operator_decision"


#: Conditions whose block class is REEVALUABLE. Everything else is OPERATOR.
_REEVALUABLE_CONDITIONS = frozenset(
    {
        ClearCondition.GATE_REEVALUATES_CLEAN,
        ClearCondition.OWNER_RESOLVABLE,
        ClearCondition.SNAPSHOT_HYDRATES,
        ClearCondition.MISSING_INPUTS_SUPPLIED,
    }
)


def kind_for_condition(condition: ClearCondition | str | None) -> BlockKind:
    """Classify a block by the condition that would clear it.

    Unknown / absent condition → OPERATOR (fail closed).
    """
    if condition is None:
        return BlockKind.OPERATOR
    try:
        cond = ClearCondition(condition)
    except ValueError:
        return BlockKind.OPERATOR
    return (
        BlockKind.REEVALUABLE
        if cond in _REEVALUABLE_CONDITIONS
        else BlockKind.OPERATOR
    )


def classify_gate_block(
    *,
    blast_radius: str,
    confidence: float,
    threshold: float,
    action_type_was_inferred: bool,
) -> tuple[BlockKind, ClearCondition]:
    """
    Classify a block written by the execution gate.

    The distinction that matters: was the gate acting on something it
    *measured*, or on something it *guessed*?

    * A HIGH blast radius derived from an **explicit** ``action_type`` is a real
      finding about the work. The operator decides. → OPERATOR.
    * A block that rests on an **inferred** action type, or purely on the
      confidence axis being unpopulated, is a mechanical default. Once the
      creating agent declares those fields the gate may reach a different and
      better-founded answer. → REEVALUABLE.

    This is what keeps the ateles#682 fix from weakening the gate: the
    hard-dependency task, which declares a genuinely high-blast action, keeps an
    OPERATOR block and no sweep touches it. The report-only tasks, blocked on a
    guess, become re-evaluable.
    """
    is_high = str(blast_radius).strip().lower() == "high"

    if is_high and not action_type_was_inferred:
        return BlockKind.OPERATOR, ClearCondition.OPERATOR_DECISION

    # Either the blast radius was guessed from the assignee, or the block rests
    # on the confidence axis alone. Both are mechanical.
    return BlockKind.REEVALUABLE, ClearCondition.GATE_REEVALUATES_CLEAN


def may_sweep(
    *,
    block_kind: BlockKind | str | None,
    sweep_count: int = 0,
) -> bool:
    """
    Whether a re-evaluation sweep may touch this block.

    False for anything OPERATOR-classed, anything untyped (legacy blocks
    predating this module), and anything that has already been re-evaluated
    ``MAX_SWEEP_ATTEMPTS`` times without clearing.
    """
    if block_kind is None:
        return False
    try:
        kind = BlockKind(block_kind)
    except ValueError:
        return False
    if kind is not BlockKind.REEVALUABLE:
        return False
    return sweep_count < MAX_SWEEP_ATTEMPTS


def record_block_classification(
    task_entity_id: str,
    *,
    block_kind: BlockKind,
    clear_condition: ClearCondition,
    handler: str,
) -> bool:
    """
    Stamp a block's class and clearing condition onto the task, alongside the
    free-text ``blocked_reason`` the gate already writes.

    Best-effort and non-fatal: a failure here means the block stays UNTYPED,
    and ``may_sweep`` treats untyped as OPERATOR — so the degraded path is a
    block that no sweep will touch, never one that is swept unsafely.

    Idempotent by construction: the key is derived from the task and the
    classification, so a redelivered SSE event rewrites the same value rather
    than accumulating observations.
    """
    token = os.environ.get("NEOTOMA_BEARER_TOKEN", "")
    if not token:
        log.warning("[block_kind] no bearer token — block left untyped (fails closed)")
        return False

    fields = {
        "block_kind": block_kind.value,
        "block_clear_condition": clear_condition.value,
    }
    ok = True
    for field, value in fields.items():
        body = {
            "entity_id": task_entity_id,
            "entity_type": "task",
            "field": field,
            "value": value,
            "idempotency_key": (
                f"blockkind-{handler}-{task_entity_id}-{field}-{value}"
            ),
        }
        try:
            resp = httpx.post(
                f"{NEOTOMA_BASE_URL}/correct",
                headers={"Authorization": f"Bearer {token}"},
                json=body,
                timeout=15,
            )
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            log.warning(
                f"[block_kind] failed to record {field} on {task_entity_id}: {exc}"
            )
            ok = False
    return ok
