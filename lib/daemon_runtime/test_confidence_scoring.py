"""Tests for the mechanical confidence scorer (ateles#902 Part 2).

`score_confidence` populates confidence for a task no producer ever scored,
so `apis._read_confidence` stops reading an absent field. These tests pin the
pure scorer's contract: it is deterministic, it honors the confidence_rubric's
hard floors (`required_inputs_present` <= 0.4, `decision_consistency` <= 0.5),
and it stays inside [0, 1].
"""

from __future__ import annotations

from lib.daemon_runtime.confidence_scoring import ConfidenceScore, score_confidence


def test_well_specified_task_scores_high():
    result = score_confidence(
        {"title": "Fix the retry backoff", "body": "Widen jitter on 429s."},
        has_owner=True,
        action_type_recognized=True,
        relationship_count=3,
        successful_recurrences=0,
    )
    assert isinstance(result, ConfidenceScore)
    assert result.value > 0.7


def test_no_owner_hits_required_inputs_hard_floor():
    """Rubric: 'hard floor 0.4 if a required input/credential/target is
    missing'. No resolved owner/skill is exactly that — dispatch itself
    lacks a required input.
    """
    result = score_confidence(
        {"title": "Do the thing"},
        has_owner=False,
        action_type_recognized=True,
        relationship_count=5,
        successful_recurrences=5,
    )
    assert result.value <= 0.4


def test_unresolved_conflict_marker_hits_decision_consistency_floor():
    """Rubric: 'hard floor 0.5 on unresolved conflict'."""
    result = score_confidence(
        {"title": "Reconcile the numbers",
         "body": "Note: unresolved conflict between the two sources."},
        has_owner=True,
        action_type_recognized=True,
        relationship_count=3,
        successful_recurrences=5,
    )
    assert result.value <= 0.5


def test_unclassified_action_type_lowers_score():
    recognized = score_confidence(
        {"title": "x", "body": "y" * 50},
        has_owner=True, action_type_recognized=True, relationship_count=2,
    )
    unrecognized = score_confidence(
        {"title": "x", "body": "y" * 50},
        has_owner=True, action_type_recognized=False, relationship_count=2,
    )
    assert unrecognized.value < recognized.value


def test_score_always_in_unit_interval():
    for has_owner in (True, False):
        for recognized in (True, False):
            for rel in (0, 1, 2, 10):
                for recur in (0, 1, 3, 100):
                    result = score_confidence(
                        {"title": "t", "body": "b"},
                        has_owner=has_owner,
                        action_type_recognized=recognized,
                        relationship_count=rel,
                        successful_recurrences=recur,
                    )
                    assert 0.0 <= result.value <= 1.0


def test_bare_task_scores_low_but_not_zero():
    """A task with nothing going for it lands low — but the scorer never
    silently defaults to the same 0.0 the absent-field bug produced; it
    always reflects the (poor) axes it was given.
    """
    result = score_confidence(
        {},
        has_owner=False,
        action_type_recognized=False,
        relationship_count=0,
        successful_recurrences=0,
    )
    assert result.value <= 0.4
    assert result.axes  # axes are populated, not empty


def test_deterministic_same_input_same_output():
    kwargs = dict(
        has_owner=True, action_type_recognized=True,
        relationship_count=2, successful_recurrences=1,
    )
    a = score_confidence({"title": "t", "body": "b"}, **kwargs)
    b = score_confidence({"title": "t", "body": "b"}, **kwargs)
    assert a.value == b.value
    assert a.axes == b.axes
