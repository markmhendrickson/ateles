"""Tests for the confidence × blast-radius execution gate."""

from __future__ import annotations

import pytest

from lib.daemon_runtime.gating import (
    DEFAULT_CLOSED_BOUNDARIES,
    NEVER_AUTO_EXECUTE_ACTION_TYPES,
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


def test_unrecognized_action_type_never_auto_executes(caplog):
    """An action type in NEITHER blast set must not resolve to LOW.

    This test previously asserted the opposite — that an unrecognized action
    type under a LOW default auto-executes. That was the ateles#715 fail-open
    written down as expected behaviour: the next unclassified action type
    inherits "safe" from a default that was never a judgment about it.
    """
    low = ExecutionPolicy(entity_id="p", blast_radius_default=BlastRadius.LOW, loaded=True)
    with caplog.at_level("WARNING"):
        d = evaluate_gate(confidence=0.9, action_type="totally_unknown", policy=low)
    assert d.action == GateAction.CHECKPOINT
    assert d.blast_radius == BlastRadius.NEVER
    assert not d.may_auto_execute

    high = ExecutionPolicy(
        entity_id="p", blast_radius_default=BlastRadius.HIGH, loaded=True
    )
    d = evaluate_gate(confidence=0.9, action_type="totally_unknown", policy=high)
    assert d.action == GateAction.CHECKPOINT


def test_unrecognized_action_type_warns_by_name(caplog):
    """The fallthrough must be loud, naming the value, so a missing
    classification is visible rather than silently permissive."""
    pol = ExecutionPolicy(entity_id="ent_policy", loaded=True)
    with caplog.at_level("WARNING"):
        pol.blast_radius_for("open_pull_request")
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert warnings, "unrecognized action_type must log a warning"
    joined = " ".join(r.getMessage() for r in warnings)
    assert "open_pull_request" in joined
    assert "ent_policy" in joined


def test_absent_action_type_still_uses_policy_default():
    """"Nothing declared" is distinct from "declared and unclassified".

    A task with no action_type at all keeps the policy default — that is the
    case the default exists for, and tightening it would checkpoint every
    unannotated task. Only a DECLARED-but-unclassified value fails closed.
    """
    low = ExecutionPolicy(entity_id="p", blast_radius_default=BlastRadius.LOW, loaded=True)
    assert low.blast_radius_for(None) == BlastRadius.LOW
    assert low.blast_radius_for("") == BlastRadius.LOW
    d = evaluate_gate(confidence=0.9, action_type=None, policy=low)
    assert d.action == GateAction.AUTO_EXECUTE


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
    # Declared but classified by neither set → NEVER, not the LOW default
    # (ateles#715).
    assert pol.blast_radius_for("unknown") == BlastRadius.NEVER
    # Nothing declared at all → the policy default, unchanged.
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


# ── operator_only: never auto-executable (ateles#715) ────────────────────────
#
# The regression these cover: `operator_only` was in neither
# low_blast_action_types nor high_blast_action_types, so blast_radius_for()
# fell through to blast_radius_default = LOW and the gate auto-executed on
# high confidence. The observed line was:
#
#   gate: task=<id> → cicada action=operator_only blast=low conf=0.95/0.85
#     → auto_execute (high confidence, low blast radius)
#
# The operator's decision was NEVER-auto-executable rather than merely
# high-blast, because high-blast still graduates via
# auto_execute_after_n_successful_recurrences — and `operator_only` does not
# mean "risky", it means an agent structurally cannot do the work. No number of
# prior successes changes whether the swarm holds a credential it was designed
# not to hold. Hence `test_operator_only_never_graduates_via_recurrence` below,
# which is the half that a merely-high-blast fix would silently fail.


def test_operator_only_resolves_to_never_blast_radius():
    assert _default().blast_radius_for("operator_only") == BlastRadius.NEVER


@pytest.mark.parametrize("confidence", [0.0, 0.5, 0.85, 0.95, 0.99, 1.0])
def test_operator_only_never_auto_executes_at_any_confidence(confidence):
    """The observed bug scaled with confidence: the surer the authoring agent
    was that a task needed a human, the more certainly it dispatched."""
    d = evaluate_gate(
        confidence=confidence, action_type="operator_only", policy=_default()
    )
    assert d.action != GateAction.AUTO_EXECUTE
    assert not d.may_auto_execute
    assert d.blast_radius == BlastRadius.NEVER


def test_operator_only_reproduces_the_observed_case():
    """Exactly the logged case: action=operator_only, conf=0.95, threshold 0.85."""
    d = evaluate_gate(
        confidence=0.95, action_type="operator_only", policy=_default()
    )
    assert d.action == GateAction.CHECKPOINT
    assert not d.may_auto_execute
    assert d.blast_radius == BlastRadius.NEVER
    assert "never auto-executable" in d.reason


@pytest.mark.parametrize("recurrences", [0, 3, 5, 100, 10_000])
def test_operator_only_never_graduates_via_recurrence(recurrences):
    """THE operator decision: recurrence graduation must not reach it.

    A HIGH-blast action is merely gated; a proven recurring series clears it
    after N clean cycles. `operator_only` must bypass that path entirely, so
    this asserts it at counts far past the graduation threshold.
    """
    pol = ExecutionPolicy(
        entity_id="p",
        confidence_threshold=0.85,
        auto_execute_after_n_successful_recurrences=3,
        loaded=True,
    )
    d = evaluate_gate(
        confidence=0.99,
        action_type="operator_only",
        policy=pol,
        successful_recurrences=recurrences,
    )
    assert d.action == GateAction.CHECKPOINT
    assert not d.may_auto_execute
    assert d.blast_radius == BlastRadius.NEVER


def test_operator_only_graduation_contrast_with_low_blast():
    """Control: the same recurrence count DOES graduate a low-blast action.

    Without this contrast, the test above could pass because graduation is
    broken generally rather than because operator_only is excluded from it.
    """
    pol = ExecutionPolicy(
        entity_id="p",
        confidence_threshold=0.85,
        auto_execute_after_n_successful_recurrences=3,
        low_blast_action_types=frozenset({"local_edit"}),
        loaded=True,
    )
    graduated = evaluate_gate(
        confidence=0.10, action_type="local_edit", policy=pol, successful_recurrences=5
    )
    assert graduated.action == GateAction.AUTO_EXECUTE

    held = evaluate_gate(
        confidence=0.10,
        action_type="operator_only",
        policy=pol,
        successful_recurrences=5,
    )
    assert held.action == GateAction.CHECKPOINT


def test_policy_cannot_demote_operator_only_to_low_blast():
    """A policy listing operator_only as low blast must not win.

    Membership in the low set is a tuning knob; "an agent structurally cannot
    do this" is not. If a policy could demote it, the whole guarantee reduces
    to whoever last edited the policy entity.
    """
    permissive = ExecutionPolicy(
        entity_id="p",
        low_blast_action_types=frozenset({"operator_only", "local_edit"}),
        blast_radius_default=BlastRadius.LOW,
        loaded=True,
    )
    assert permissive.blast_radius_for("operator_only") == BlastRadius.NEVER
    d = evaluate_gate(
        confidence=1.0, action_type="operator_only", policy=permissive,
        successful_recurrences=999,
    )
    assert d.action == GateAction.CHECKPOINT
    assert not d.may_auto_execute


def test_operator_only_case_and_whitespace_insensitive():
    for spelling in ("OPERATOR_ONLY", " operator_only ", "Operator_Only"):
        assert _default().blast_radius_for(spelling) == BlastRadius.NEVER


def test_operator_only_in_fallback_high_blast_as_defense_in_depth():
    """Belt-and-braces: if the NEVER tier were ever lost in a refactor, the
    fallback set must degrade operator_only to HIGH, not back to LOW."""
    from lib.daemon_runtime.gating import _FALLBACK_HIGH_BLAST

    assert "operator_only" in _FALLBACK_HIGH_BLAST
    assert "operator_only" in NEVER_AUTO_EXECUTE_ACTION_TYPES


def test_may_auto_execute_rejects_a_never_tier_decision():
    """Hand-constructed AUTO_EXECUTE at NEVER radius is still not dispatchable."""
    from lib.daemon_runtime.gating import GateDecision

    forged = GateDecision(
        action=GateAction.AUTO_EXECUTE,
        blast_radius=BlastRadius.NEVER,
        confidence=1.0,
        threshold=0.85,
        policy_id="p",
        reason="forged",
    )
    assert not forged.may_auto_execute


def test_parse_policy_accepts_never_as_blast_radius_default():
    pol = _parse_policy("p", {"snapshot": {"blast_radius_default": "never"}})
    assert pol.blast_radius_default == BlastRadius.NEVER
    d = evaluate_gate(confidence=1.0, action_type=None, policy=pol)
    assert d.action == GateAction.CHECKPOINT


def test_any_action_type_vocabulary_must_contain_the_never_set():
    """Forward guard against ateles#689's `normalize_action_type` discard rule.

    PR #689 introduces `lib/daemon_runtime/action_type.py` with its own
    `KNOWN_ACTION_TYPES` vocabulary and a `normalize_action_type()` that
    DISCARDS any value outside it, returning None. That rule is right for a
    typo ("open_pull_request"), but `operator_only` is not a typo — it is
    absent from both of #689's vocabularies, so a declared `operator_only`
    would be dropped as if never declared, fall through to text inference, and
    land back on `blast_radius_default` = LOW. The gate would never see the
    value this module refuses to auto-execute.

    This test is skipped until that module exists, and fails the moment it
    lands without `operator_only` in its vocabulary — so the two changes
    cannot merge in the wrong order silently.
    """
    action_type = pytest.importorskip(
        "lib.daemon_runtime.action_type",
        reason="ateles#689 not merged yet — nothing to guard",
    )

    known = getattr(action_type, "KNOWN_ACTION_TYPES", None)
    assert known is not None, "action_type module must expose KNOWN_ACTION_TYPES"
    missing = NEVER_AUTO_EXECUTE_ACTION_TYPES - set(known)
    assert not missing, (
        f"{sorted(missing)} missing from KNOWN_ACTION_TYPES. "
        "normalize_action_type() discards anything outside that set, so these "
        "would be dropped as if undeclared and fall back to blast_radius_default "
        "(LOW) — reopening ateles#715 through a different door."
    )

    normalize = getattr(action_type, "normalize_action_type", None)
    if normalize is not None:
        for value in NEVER_AUTO_EXECUTE_ACTION_TYPES:
            assert normalize(value) == value, (
                f"normalize_action_type({value!r}) must preserve the value, not "
                "discard it — the gate cannot refuse what it never receives."
            )
