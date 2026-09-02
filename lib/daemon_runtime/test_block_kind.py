"""
Tests for typed blocks (ateles#682, and the operator's question about whether
the swarm should be able to unblock its own tasks).

The rule these encode: re-evaluate blocks whose condition the swarm can
verify; never clear blocks whose reason is "an operator must decide".
"""

from __future__ import annotations

import pytest

from lib.daemon_runtime.block_kind import (
    MAX_SWEEP_ATTEMPTS,
    BlockKind,
    ClearCondition,
    classify_gate_block,
    kind_for_condition,
    may_sweep,
)


# ── The load-bearing distinction ─────────────────────────────────────────────


def test_declared_high_blast_is_an_operator_block():
    """A HIGH blast radius the task DECLARED is a real finding, not a guess.

    This is the control case: the config-migration task that makes Neotoma a
    startup dependency must stay operator-gated no matter what any sweep does.
    """
    kind, condition = classify_gate_block(
        blast_radius="high",
        confidence=0.0,
        threshold=0.85,
        action_type_was_inferred=False,
    )
    assert kind is BlockKind.OPERATOR
    assert condition is ClearCondition.OPERATOR_DECISION
    assert not may_sweep(block_kind=kind)


def test_inferred_high_blast_is_reevaluable():
    """A HIGH radius guessed from the assignee is mechanical — sweepable.

    This is the ateles#682 population: report-only tasks that scored HIGH
    because Cicada would have handled them.
    """
    kind, condition = classify_gate_block(
        blast_radius="high",
        confidence=0.0,
        threshold=0.85,
        action_type_was_inferred=True,
    )
    assert kind is BlockKind.REEVALUABLE
    assert condition is ClearCondition.GATE_REEVALUATES_CLEAN
    assert may_sweep(block_kind=kind)


def test_low_blast_below_threshold_is_reevaluable():
    """Blocked on the confidence axis alone — the axis nothing populated."""
    kind, _ = classify_gate_block(
        blast_radius="low",
        confidence=0.0,
        threshold=0.85,
        action_type_was_inferred=True,
    )
    assert kind is BlockKind.REEVALUABLE


def test_low_blast_with_declared_action_is_still_reevaluable():
    """A declared LOW action blocked only by confidence stays mechanical.

    Declaring "this is analysis" is not an operator judgment about risk; the
    block rests entirely on the unscored confidence axis.
    """
    kind, condition = classify_gate_block(
        blast_radius="low",
        confidence=0.2,
        threshold=0.85,
        action_type_was_inferred=False,
    )
    assert kind is BlockKind.REEVALUABLE
    assert condition is ClearCondition.GATE_REEVALUATES_CLEAN


# ── Fail-closed behaviour ────────────────────────────────────────────────────


@pytest.mark.parametrize("value", [None, "", "unknown", "operator", "OPERATOR", 7, []])
def test_untyped_and_unknown_blocks_are_never_swept(value):
    """Legacy blocks predating this module must not be swept.

    Every one of the ~27 tasks currently parked was blocked before block
    typing existed. None of them carries a block_kind, so none is sweepable
    until something re-evaluates and re-types it.
    """
    assert may_sweep(block_kind=value) is False


@pytest.mark.parametrize("value", [None, "nonsense", "", 42])
def test_unknown_condition_classifies_as_operator(value):
    assert kind_for_condition(value) is BlockKind.OPERATOR


def test_reevaluable_conditions_map_to_reevaluable():
    for condition in (
        ClearCondition.GATE_REEVALUATES_CLEAN,
        ClearCondition.OWNER_RESOLVABLE,
        ClearCondition.SNAPSHOT_HYDRATES,
        ClearCondition.MISSING_INPUTS_SUPPLIED,
    ):
        assert kind_for_condition(condition) is BlockKind.REEVALUABLE
        assert kind_for_condition(condition.value) is BlockKind.REEVALUABLE


def test_operator_decision_maps_to_operator():
    assert kind_for_condition(ClearCondition.OPERATOR_DECISION) is BlockKind.OPERATOR


# ── Loop safety ──────────────────────────────────────────────────────────────


def test_sweep_budget_is_bounded():
    """A condition that repeatedly fails to clear stops being swept.

    Apis has twice produced notification storms from unbounded re-assertion
    (131 pages; 500+ emails). A sweep that clears and re-blocks forever would
    be a third.
    """
    for n in range(MAX_SWEEP_ATTEMPTS):
        assert may_sweep(block_kind=BlockKind.REEVALUABLE, sweep_count=n)
    assert not may_sweep(
        block_kind=BlockKind.REEVALUABLE, sweep_count=MAX_SWEEP_ATTEMPTS
    )
    assert not may_sweep(
        block_kind=BlockKind.REEVALUABLE, sweep_count=MAX_SWEEP_ATTEMPTS + 50
    )


def test_operator_block_is_unsweepable_at_any_count():
    for n in (0, 1, MAX_SWEEP_ATTEMPTS, 1000):
        assert not may_sweep(block_kind=BlockKind.OPERATOR, sweep_count=n)


def test_blast_radius_string_is_case_and_space_tolerant():
    for raw in ("HIGH", " high ", "High"):
        kind, _ = classify_gate_block(
            blast_radius=raw,
            confidence=0.0,
            threshold=0.85,
            action_type_was_inferred=False,
        )
        assert kind is BlockKind.OPERATOR
