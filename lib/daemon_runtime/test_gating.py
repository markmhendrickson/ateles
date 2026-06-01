"""Tests for the confidence × blast-radius execution gate."""

from __future__ import annotations

from lib.daemon_runtime.gating import (
    BlastRadius,
    ExecutionPolicy,
    GateAction,
    evaluate_gate,
)


def _default() -> ExecutionPolicy:
    # loaded=False uses the conservative fallback (threshold 0.85, default low,
    # fallback high-blast set incl. open_pr/payment/release/...).
    return ExecutionPolicy(entity_id="default", loaded=False)


def test_high_conf_low_blast_auto_executes():
    d = evaluate_gate(confidence=0.9, action_type="local_edit", policy=_default())
    assert d.action == GateAction.AUTO_EXECUTE
    assert d.blast_radius == BlastRadius.LOW


def test_high_conf_high_blast_checkpoints():
    d = evaluate_gate(confidence=0.99, action_type="open_pr", policy=_default())
    assert d.action == GateAction.CHECKPOINT
    assert d.blast_radius == BlastRadius.HIGH


def test_low_conf_low_blast_checkpoints():
    d = evaluate_gate(confidence=0.3, action_type="local_edit", policy=_default())
    assert d.action == GateAction.CHECKPOINT


def test_low_conf_high_blast_proposes_alternatives():
    d = evaluate_gate(confidence=0.2, action_type="payment", policy=_default())
    assert d.action == GateAction.CHECKPOINT_WITH_ALTERNATIVES


def test_unknown_action_uses_policy_default_blast():
    low = ExecutionPolicy(entity_id="p", blast_radius_default=BlastRadius.LOW, loaded=True)
    d = evaluate_gate(confidence=0.9, action_type="totally_unknown", policy=low)
    assert d.action == GateAction.AUTO_EXECUTE

    high = ExecutionPolicy(
        entity_id="p", blast_radius_default=BlastRadius.HIGH, loaded=True
    )
    d = evaluate_gate(confidence=0.9, action_type="totally_unknown", policy=high)
    assert d.action == GateAction.CHECKPOINT


def test_recurrence_graduation_auto_executes_below_threshold():
    pol = ExecutionPolicy(
        entity_id="p",
        auto_execute_after_n_successful_recurrences=3,
        blast_radius_default=BlastRadius.LOW,
        loaded=True,
    )
    # Below threshold but graduated → auto-execute (low blast only).
    d = evaluate_gate(
        confidence=0.5,
        action_type="neotoma_internal_entity_update",
        policy=pol,
        successful_recurrences=3,
    )
    assert d.action == GateAction.AUTO_EXECUTE
    assert "recurrence-graduated" in d.reason


def test_recurrence_graduation_never_applies_to_high_blast():
    pol = ExecutionPolicy(
        entity_id="p",
        auto_execute_after_n_successful_recurrences=3,
        blast_radius_default=BlastRadius.LOW,
        loaded=True,
    )
    d = evaluate_gate(
        confidence=0.5,
        action_type="payment",  # in fallback high-blast set
        policy=pol,
        successful_recurrences=99,
    )
    assert d.action != GateAction.AUTO_EXECUTE


def test_monedula_strict_payment_never_auto_executes():
    """Financial policy: threshold 1.0, default high, graduation disabled."""
    mon = ExecutionPolicy(
        entity_id="monedula-strict",
        confidence_threshold=1.0,
        blast_radius_default=BlastRadius.HIGH,
        auto_execute_after_n_successful_recurrences=None,
        loaded=True,
    )
    d = evaluate_gate(
        confidence=1.0,
        action_type="payment",
        policy=mon,
        successful_recurrences=999,
    )
    assert d.action == GateAction.CHECKPOINT
    assert d.may_auto_execute is False


def test_missing_confidence_fails_closed_for_high_blast():
    # confidence 0.0 (e.g. agent hasn't scored yet) + high blast → checkpoint
    d = evaluate_gate(confidence=0.0, action_type="merge_pr", policy=_default())
    assert d.action == GateAction.CHECKPOINT_WITH_ALTERNATIVES


def test_blast_radius_for_classification():
    pol = ExecutionPolicy(
        entity_id="p",
        high_blast_action_types=frozenset({"payment"}),
        low_blast_action_types=frozenset({"draft"}),
        blast_radius_default=BlastRadius.LOW,
        loaded=True,
    )
    assert pol.blast_radius_for("payment") == BlastRadius.HIGH
    assert pol.blast_radius_for("draft") == BlastRadius.LOW
    assert pol.blast_radius_for("unknown") == BlastRadius.LOW
    assert pol.blast_radius_for(None) == BlastRadius.LOW
