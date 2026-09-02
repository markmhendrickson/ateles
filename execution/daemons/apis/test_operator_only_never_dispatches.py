"""An `operator_only` task must never reach an agent (ateles#715).

Observed live 2026-09-02. A task explicitly marked `action_type: operator_only`
— credential extraction needing a live Fly session and a machine-local age key
the swarm deliberately does not hold — was dispatched to an agent 3 seconds
after creation:

    gate: task=<id> → cicada action=operator_only blast=low conf=0.95/0.85
      → auto_execute (high confidence, low blast radius)

`operator_only` was in neither `low_blast_action_types` nor
`high_blast_action_types`, so `blast_radius_for()` fell through to
`blast_radius_default` = LOW. Confidence was tested against the threshold while
the operator-only marking contributed nothing — so the *more* confident an
authoring agent was that a task needed a human, the more certainly it ran.

These tests assert the EFFECT at the dispatch layer (the gate refuses, so the
daemon holds the task) rather than merely that `operator_only` is an accepted
value. The unit-level guarantees live in `lib/daemon_runtime/test_gating.py`.

Run: pytest execution/daemons/apis/test_operator_only_never_dispatches.py -v
"""

from __future__ import annotations

import pytest

import apis
from lib.daemon_runtime.gating import (
    BlastRadius,
    ExecutionPolicy,
    GateAction,
    evaluate_gate,
)


def _gate_for(snapshot: dict, *, skill: str = "cicada", recurrences: int = 0):
    """Run the dispatcher's own action_type + confidence resolution through the
    gate, exactly as `apis._handle_task` does at its `# ── Execution gate` block.

    Uses the fallback policy so the test does not depend on Neotoma being
    reachable — the fallback is also the strictly more permissive of the two
    (blast_radius_default = LOW), so a pass here is the harder case.
    """
    return evaluate_gate(
        confidence=apis._read_confidence(snapshot),
        action_type=apis._infer_action_type(skill, snapshot),
        policy=ExecutionPolicy(entity_id="fallback", loaded=False),
        successful_recurrences=recurrences,
    )


def test_declared_operator_only_survives_dispatcher_inference():
    """`_infer_action_type` must pass the declared value through untouched.

    If the dispatcher swallowed an unrecognized declared value, the task would
    fall to the per-agent map (`cicada` → `open_or_merge_pr`) and the gate
    would never see `operator_only` at all.
    """
    assert (
        apis._infer_action_type("cicada", {"action_type": "operator_only"})
        == "operator_only"
    )
    # Case and whitespace as an authoring agent might actually write it.
    assert (
        apis._infer_action_type("cicada", {"action_type": " Operator_Only "})
        == "operator_only"
    )


def test_the_observed_dispatch_is_now_held():
    """The exact logged case: cicada, operator_only, confidence 0.95."""
    decision = _gate_for({"action_type": "operator_only", "confidence": 0.95})
    assert decision.action == GateAction.CHECKPOINT
    assert not decision.may_auto_execute, (
        "regression: the ateles#715 dispatch — an operator-only credential task "
        "sent to an agent 3 seconds after creation"
    )
    assert decision.blast_radius == BlastRadius.NEVER


@pytest.mark.parametrize("confidence", [0.0, 0.5, 0.85, 0.95, 0.99, 1.0])
def test_never_dispatched_at_any_confidence(confidence):
    decision = _gate_for({"action_type": "operator_only", "confidence": confidence})
    assert not decision.may_auto_execute


@pytest.mark.parametrize("recurrences", [0, 3, 4, 50, 1000])
def test_never_dispatched_after_n_successful_recurrences(recurrences):
    """The operator's actual decision, and the half a merely-high-blast fix misses.

    A HIGH-blast action still auto-executes once a recurring series clears
    `auto_execute_after_n_successful_recurrences` (3 on the default policy).
    `operator_only` does not mean "risky" — it means an agent structurally
    cannot do this, and no number of prior successes hands the swarm a
    credential it was designed not to hold. So the recurrence path must be
    bypassed, not merely outscored.
    """
    pol = ExecutionPolicy(
        entity_id="p",
        confidence_threshold=0.85,
        auto_execute_after_n_successful_recurrences=3,
        loaded=True,
    )
    decision = evaluate_gate(
        confidence=0.99,
        action_type="operator_only",
        policy=pol,
        successful_recurrences=recurrences,
    )
    assert not decision.may_auto_execute
    assert decision.blast_radius == BlastRadius.NEVER


@pytest.mark.parametrize(
    "skill", ["cicada", "vanellus", "struthio", "monedula", "fringilla", "corvus"]
)
def test_operator_only_beats_every_per_agent_prior(skill):
    """Including fringilla, whose prior (`compute_only_analysis`) is LOW.

    The declared field must win over the agent map for every routed agent, or
    the guarantee holds only for whoever happens to be assigned.
    """
    decision = _gate_for(
        {"action_type": "operator_only", "confidence": 0.99}, skill=skill
    )
    assert not decision.may_auto_execute


def test_unclassified_action_type_is_also_held():
    """The class, not only the instance: the next new action type must fail safe."""
    decision = _gate_for({"action_type": "rotate_credential", "confidence": 0.99})
    assert not decision.may_auto_execute
    assert decision.blast_radius == BlastRadius.NEVER


def test_ordinary_low_blast_work_still_auto_executes():
    """Control. Without this, every assertion above could pass because the gate
    is refusing everything — which would be a far worse regression than the bug.
    """
    decision = _gate_for({"action_type": "local_edit", "confidence": 0.95})
    assert decision.action == GateAction.AUTO_EXECUTE
    assert decision.may_auto_execute
