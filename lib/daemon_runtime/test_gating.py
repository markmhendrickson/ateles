"""Tests for the confidence × blast-radius execution gate."""

from __future__ import annotations

import pytest

from lib.daemon_runtime.gating import (
    DEFAULT_CLOSED_BOUNDARIES,
    BlastRadius,
    CheckpointPosture,
    ExecutionPolicy,
    GateAction,
    InvalidCheckpointPosture,
    _parse_policy,
    evaluate_gate,
    read_checkpoint_resolution,
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


# ── Checkpoint resolution ────────────────────────────────────────────────────


def test_checkpoint_resolution_approved():
    assert read_checkpoint_resolution({"status": "approved"}) == "approved"
    assert read_checkpoint_resolution({"status": "Approved"}) == "approved"
    assert read_checkpoint_resolution({"status": "accepted"}) == "approved"


def test_checkpoint_resolution_rejected():
    assert read_checkpoint_resolution({"status": "rejected"}) == "rejected"
    assert read_checkpoint_resolution({"status": "declined"}) == "rejected"
    assert read_checkpoint_resolution({"status": "DENIED"}) == "rejected"


def test_checkpoint_resolution_pending_is_none():
    assert read_checkpoint_resolution({"status": "awaiting_operator"}) is None
    assert read_checkpoint_resolution({"status": ""}) is None
    assert read_checkpoint_resolution({}) is None
    assert read_checkpoint_resolution({"status": "something_else"}) is None


def test_checkpoint_already_dispatched():
    from lib.daemon_runtime.gating import checkpoint_already_dispatched
    assert checkpoint_already_dispatched({"resolved_dispatched": True}) is True
    assert checkpoint_already_dispatched({"resolved_dispatched": "true"}) is True
    assert checkpoint_already_dispatched({"resolved_dispatched": "1"}) is True
    assert checkpoint_already_dispatched({"resolved_dispatched": False}) is False
    assert checkpoint_already_dispatched({}) is False
    assert checkpoint_already_dispatched({"resolved_dispatched": None}) is False


# ── checkpoint posture (ateles#350) ─────────────────────────────────────────


def test_posture_absent_field_defaults_to_open_for_unlisted_boundary():
    pol = ExecutionPolicy(entity_id="p", loaded=True)
    assert pol.posture_for("issue.triaged") == CheckpointPosture.OPEN
    assert pol.posture_for(None) == CheckpointPosture.OPEN
    assert pol.posture_for("totally_unknown_boundary") == CheckpointPosture.OPEN


def test_posture_default_closed_boundaries():
    pol = ExecutionPolicy(entity_id="p", loaded=True)
    for boundary in DEFAULT_CLOSED_BOUNDARIES:
        assert pol.posture_for(boundary) == CheckpointPosture.CLOSED
    assert DEFAULT_CLOSED_BOUNDARIES == {
        "pre_payment",
        "pre_release",
        "pre_comms",
        "pre_irreversible",
        "pre_merge",
    }


def test_posture_explicit_override_wins_over_default():
    pol = ExecutionPolicy(
        entity_id="p",
        checkpoint_postures={"pre_merge": CheckpointPosture.OPEN},
        loaded=True,
    )
    assert pol.posture_for("pre_merge") == CheckpointPosture.OPEN

    pol2 = ExecutionPolicy(
        entity_id="p",
        checkpoint_postures={"issue.triaged": CheckpointPosture.CLOSED},
        loaded=True,
    )
    assert pol2.posture_for("issue.triaged") == CheckpointPosture.CLOSED


def test_posture_is_case_insensitive_on_boundary_name():
    pol = ExecutionPolicy(
        entity_id="p",
        checkpoint_postures={"pre_merge": CheckpointPosture.OPEN},
        loaded=True,
    )
    assert pol.posture_for("PRE_MERGE") == CheckpointPosture.OPEN
    assert pol.posture_for(" Pre_Merge ") == CheckpointPosture.OPEN


def test_parse_policy_reads_checkpoint_postures_from_snapshot():
    data = {
        "snapshot": {
            "checkpoint_postures": {"pre_merge": "closed", "issue.triaged": "open"}
        }
    }
    pol = _parse_policy("ent_x", data)
    assert pol.posture_for("pre_merge") == CheckpointPosture.CLOSED
    assert pol.posture_for("issue.triaged") == CheckpointPosture.OPEN


def test_parse_policy_accepts_json_string_checkpoint_postures():
    data = {"snapshot": {"checkpoint_postures": '{"pre_merge": "closed"}'}}
    pol = _parse_policy("ent_x", data)
    assert pol.posture_for("pre_merge") == CheckpointPosture.CLOSED


def test_parse_policy_hard_fails_on_invalid_posture_value():
    data = {"snapshot": {"checkpoint_postures": {"pre_merge": "fail_open"}}}
    with pytest.raises(InvalidCheckpointPosture):
        _parse_policy("ent_x", data)


def test_parse_policy_missing_checkpoint_postures_is_empty_and_additive():
    # No behavior change for policies that never set checkpoint_postures at
    # all: only the DEFAULT_CLOSED_BOUNDARIES set applies, everything else
    # resolves open exactly like before #350.
    data = {"snapshot": {}}
    pol = _parse_policy("ent_x", data)
    assert pol.checkpoint_postures == {}
    assert pol.posture_for("issue.triaged") == CheckpointPosture.OPEN
    assert pol.posture_for("pre_merge") == CheckpointPosture.CLOSED


def test_load_policy_degrades_to_fallback_on_invalid_posture(monkeypatch):
    # A live policy entity with a typo'd checkpoint_postures value must NOT
    # crash load_policy()'s callers (core task dispatch, CI-failure handling,
    # ...) — it degrades to the same conservative fallback used for an
    # unreachable policy, per load_policy()'s "never raises, fails closed"
    # contract. Regression for the #350 self-review finding: _parse_policy
    # raising InvalidCheckpointPosture was previously unguarded here.
    import lib.daemon_runtime.gating as gating_mod

    monkeypatch.setattr(
        gating_mod,
        "_fetch_entity",
        lambda pid: {"snapshot": {"checkpoint_postures": {"pre_merge": "fail_open"}}},
    )
    pol = gating_mod.load_policy("ent_bad_policy")
    assert pol.loaded is False
    assert pol.entity_id == "ent_bad_policy"
    # Still resolves postures safely via the DEFAULT_CLOSED_BOUNDARIES fallback.
    assert pol.posture_for("pre_merge") == CheckpointPosture.CLOSED
