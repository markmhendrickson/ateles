"""`_resolve_confidence` — populating confidence ahead of the gate (ateles#902).

The action gate reads `confidence` off a task snapshot at `_read_confidence`,
but until this fix NO producer anywhere in `execution/` or `lib/` ever wrote
that field onto a task entity: `float(None)` raised, the reader returned 0.0,
and the gate stamped every unscored task with a reason that read as a
judgment ("low confidence and high blast radius") nobody made — 36 of 49
pending checkpoints carried it.

`_resolve_confidence` is the single choke point (immediately ahead of
`dispatch_task`'s gate read) that now scores an unscored task mechanically,
per the confidence_rubric named in the Cicada skill prompt, and reports
whether it did so. These tests exercise it exactly as `dispatch_task` does,
mirroring `test_operator_only_never_dispatches.py`'s pattern.

Run: pytest execution/daemons/apis/test_confidence_gate_resolution.py -v
"""

from __future__ import annotations

import apis
from lib.daemon_runtime.gating import ExecutionPolicy, GateAction, evaluate_gate


def _fallback_policy() -> ExecutionPolicy:
    return ExecutionPolicy(entity_id="fallback", loaded=False)


def test_explicit_confidence_is_used_verbatim_and_not_unscored():
    """A producer that DID score the task (e.g. Cicada's full-rubric
    self-score) must win over the mechanical approximation — the mechanical
    scorer only fills a genuine gap, it never overrides a real judgment.
    """
    confidence, unscored = apis._resolve_confidence(
        {"confidence": 0.42},
        has_owner=True,
        action_type_recognized=True,
        relationship_count=0,
    )
    assert confidence == 0.42
    assert unscored is False


def test_absent_confidence_is_scored_not_defaulted_to_zero():
    """The core of the bug: a snapshot with no confidence/confidence_score
    field at all must not silently resolve to 0.0 from an absent-field crash.
    It gets a real (if conservative) mechanical score, and is flagged unscored.
    """
    confidence, unscored = apis._resolve_confidence(
        {"title": "Well-specified task", "body": "b" * 60},
        has_owner=True,
        action_type_recognized=True,
        relationship_count=3,
    )
    assert unscored is True
    assert isinstance(confidence, float)
    assert 0.0 <= confidence <= 1.0


def test_unparseable_confidence_is_also_treated_as_unscored():
    """A garbage value (e.g. a stray string) is the same 'nobody scored this'
    state as a fully absent field — `_confidence_is_explicit` must reject it.
    """
    confidence, unscored = apis._resolve_confidence(
        {"confidence": "not-a-number"},
        has_owner=True,
        action_type_recognized=True,
        relationship_count=0,
    )
    assert unscored is True


def test_end_to_end_unscored_task_reason_says_never_scored():
    """Full path: an unscored task's gate decision must say "never scored",
    not "low confidence" — the exact operator-facing symptom in ateles#902.
    """
    snapshot = {"title": "Publish the release notes", "body": "b" * 10}
    confidence, unscored = apis._resolve_confidence(
        snapshot, has_owner=True, action_type_recognized=True, relationship_count=0,
    )
    decision = evaluate_gate(
        confidence=confidence,
        action_type="payment",  # high-blast under the fallback policy
        policy=_fallback_policy(),
        confidence_unscored=unscored,
    )
    assert decision.action != GateAction.AUTO_EXECUTE
    assert "never scored" in decision.reason.lower()
    assert "low confidence" not in decision.reason.lower()


def test_end_to_end_scored_low_task_reason_still_says_low_confidence():
    """Control: a task an agent genuinely scored low must keep the original,
    accurate reason — this fix must not blur a real low score into "unscored".
    """
    snapshot = {"title": "Risky migration", "confidence": 0.1}
    confidence, unscored = apis._resolve_confidence(
        snapshot, has_owner=True, action_type_recognized=True, relationship_count=0,
    )
    decision = evaluate_gate(
        confidence=confidence,
        action_type="payment",
        policy=_fallback_policy(),
        confidence_unscored=unscored,
    )
    assert decision.action != GateAction.AUTO_EXECUTE
    assert "low confidence" in decision.reason.lower()
    assert "never scored" not in decision.reason.lower()


def test_unscored_high_blast_task_still_checkpoints_end_to_end():
    """Fail-closed, proven at the dispatch layer: an unscored task assigned a
    high-blast action still checkpoints — mechanical scoring must never turn
    into an accidental auto-execute path for work nobody vouched for.
    """
    snapshot = {"title": "Merge the release branch"}  # no confidence at all
    confidence, unscored = apis._resolve_confidence(
        snapshot, has_owner=True, action_type_recognized=True, relationship_count=0,
    )
    decision = evaluate_gate(
        confidence=confidence,
        action_type="open_or_merge_pr",  # high-blast
        policy=_fallback_policy(),
        confidence_unscored=unscored,
    )
    assert unscored is True
    assert decision.action != GateAction.AUTO_EXECUTE
    assert not decision.may_auto_execute


def test_unscored_task_with_no_owner_hits_required_inputs_floor():
    """No resolved owner is a missing required input under the rubric — the
    mechanical score must reflect that with its own hard floor, same as a
    genuinely low agent score would.
    """
    confidence, unscored = apis._resolve_confidence(
        {"title": "Ambiguous task"},
        has_owner=False,
        action_type_recognized=True,
        relationship_count=0,
    )
    assert unscored is True
    assert confidence <= 0.4


def test_unscored_well_specified_low_blast_task_auto_executes():
    """Part 2 intentionally lets the drain path move: a well-specified,
    NEVER-scored task assigned a low-blast action_type mechanically scores
    high enough to clear the fallback threshold and AUTO_EXECUTEs — same as
    if an agent had scored it itself. `confidence_unscored` only rewrites the
    CHECKPOINT* reason text (see Part 1); it does not gate the action axis,
    so it cannot turn this task's mechanical score into a checkpoint.

    This is the positive case existing fail-closed tests never covered —
    those all use confidence <= 0.2 or a high-blast action_type, so they
    stay green even if the #902 low-blast drain path regresses. Without this
    test, a change that accidentally checkpoints every unscored task (or one
    that lets mechanical scoring auto-execute a high-blast task) would both
    pass CI.
    """
    snapshot = {
        "title": "Well-specified task",
        "body": "b" * 60,
    }  # no confidence/confidence_score field at all
    confidence, unscored = apis._resolve_confidence(
        snapshot, has_owner=True, action_type_recognized=True, relationship_count=3,
    )
    decision = evaluate_gate(
        confidence=confidence,
        action_type="local_edit",  # low-blast under the fallback policy
        policy=_fallback_policy(),
        confidence_unscored=unscored,
    )
    assert unscored is True
    assert confidence >= 0.85  # >= _FALLBACK_THRESHOLD
    assert decision.action == GateAction.AUTO_EXECUTE
    assert decision.may_auto_execute is True
