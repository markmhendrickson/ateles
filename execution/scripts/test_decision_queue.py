"""Tests for the checkpoint queue classifier.

The examples are synthetic and shaped like the gate's real records. Every class
has a positive case, and the safety cases pin the ORDER of the checks: a hold a
person raised or an operator-only hold must never be read as noise, whatever
else is true of the task.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import decision_queue as dq
import pytest

NOW = datetime(2026, 11, 5, 18, 0, tzinfo=timezone.utc)
UNSCORED_LOW = "not scored by a producer — score is a mechanical estimate"
UNSCORED_HIGH = (
    "not scored by a producer (score is a mechanical estimate) and high blast radius "
    "— propose alternatives"
)
OP_ONLY = (
    "operator-only action — never auto-executable at any confidence or recurrence count"
)
PEOPLE = frozenset({"operator", "alex"})


def brief(**over):
    base = {
        "status": "awaiting_operator",
        "handler": "apis",
        "checkpoint_name": "PLAN",
        "gate_action": "checkpoint_plan_approval",
        "reason": UNSCORED_LOW,
        "blast_radius": "low",
        "plan_summary": "Assigned to cicada. Action: local_edit. Trigger: created.",
        "task_entity_id": "ent_t",
    }
    base.update(over)
    return base


def task(**over):
    base = {
        "status": "awaiting_approval",
        "blocked_reason": UNSCORED_LOW,
        "title": "Tidy a helper",
    }
    base.update(over)
    return base


def klass(b, t, newer=False):
    return dq.classify_one(b, t, has_newer_open=newer, person_owners=PEOPLE)


@pytest.mark.parametrize(
    "b,t,expected",
    [
        (brief(checkpoint_name="DESIGN"), task(), dq.RAISED_BY_PERSON_OR_SESSION),
        (brief(handler="cicada"), task(), dq.RAISED_BY_PERSON_OR_SESSION),
        (brief(handler=None), task(), dq.RAISED_BY_PERSON_OR_SESSION),
        (
            brief(gate_action="operator_decision_brief"),
            task(),
            dq.RAISED_BY_PERSON_OR_SESSION,
        ),
        (
            brief(reason="Eight decisions a design must assume."),
            task(),
            dq.RAISED_BY_PERSON_OR_SESSION,
        ),
        (
            brief(
                reason=OP_ONLY,
                blast_radius="never",
                plan_summary="Action: operator_only.",
            ),
            task(blocked_reason=OP_ONLY),
            dq.OPERATOR_ONLY,
        ),
        (
            brief(
                reason=OP_ONLY, blast_radius="never", plan_summary="Action: decision."
            ),
            task(blocked_reason=OP_ONLY),
            dq.OPERATOR_ONLY,
        ),
        (
            brief(
                reason=OP_ONLY,
                blast_radius="never",
                plan_summary="Action: code_review.",
            ),
            task(blocked_reason=OP_ONLY),
            dq.OPERATOR_ONLY_LABEL,
        ),
        (
            brief(
                reason=OP_ONLY,
                blast_radius="never",
                plan_summary="Action: code_review.",
            ),
            task(status="done"),
            dq.OPERATOR_ONLY_TASK_FINISHED,
        ),
        (brief(), task(owner="operator"), dq.OWNED_BY_PERSON),
        (brief(), task(assigned_to="Alex"), dq.OWNED_BY_PERSON),
        (brief(), task(status="done"), dq.TASK_FINISHED),
        (brief(), task(status="completed"), dq.TASK_FINISHED),
        (brief(), task(status="cancelled"), dq.TASK_FINISHED),
        (brief(), task(status="in_progress"), dq.TASK_MOVED_ON),
        (brief(), task(status="pending", blocked_reason=""), dq.TASK_MOVED_ON),
        (
            brief(),
            task(blocked_reason="Draft staged; a person reviews before any send."),
            dq.SESSION_WROTE_HOLD,
        ),
        (brief(), task(blocked_reason=""), dq.SESSION_WROTE_HOLD),
        (brief(), task(owner="codex:01a17dee"), dq.LIVE_SESSION_OWNED),
        (
            brief(),
            task(owner="coordinator: a Claude Code session"),
            dq.LIVE_SESSION_OWNED,
        ),
        (brief(), task(owner="root01a18247 sole coordinator"), dq.LIVE_SESSION_OWNED),
        (
            brief(
                reason=UNSCORED_HIGH,
                blast_radius="high",
                plan_summary="Action: payment.",
            ),
            task(blocked_reason=UNSCORED_HIGH),
            dq.LIVE_EFFECT,
        ),
        (
            brief(
                reason=UNSCORED_HIGH,
                blast_radius="high",
                plan_summary="Action: send_external_comms.",
            ),
            task(blocked_reason=UNSCORED_HIGH),
            dq.LIVE_EFFECT,
        ),
        (
            brief(),
            task(title="Decide the retention rule for call transcripts"),
            dq.LIVE_ASKS_FOR_DECISION,
        ),
        (
            brief(),
            task(title="Operator: send the guide link"),
            dq.LIVE_ASKS_FOR_DECISION,
        ),
        (
            brief(
                reason=UNSCORED_HIGH,
                blast_radius="high",
                plan_summary="Action: open_or_merge_pr.",
            ),
            task(blocked_reason=UNSCORED_HIGH),
            dq.LIVE_CODE,
        ),
        (brief(), task(), dq.LIVE_LOW),
    ],
)
def test_each_class_has_a_recognised_example(b, t, expected):
    assert klass(b, t) == expected


def test_a_task_that_cannot_be_read_is_kept_not_noise():
    assert klass(brief(), None) == dq.UNRECOGNISED
    assert dq.UNRECOGNISED in dq.REAL_DECISION_CLASSES


def test_a_duplicate_is_the_older_of_two_on_a_live_task():
    assert klass(brief(), task(), newer=True) == dq.OLDER_DUPLICATE


# ── The order of the checks is the safety property ───────────────────────────


@pytest.mark.parametrize(
    "status", ["done", "in_progress", "pending", "awaiting_approval"]
)
@pytest.mark.parametrize("newer", [True, False])
def test_a_raised_hold_is_kept_whatever_became_of_the_task(status, newer):
    held = klass(brief(checkpoint_name="DESIGN"), task(status=status), newer=newer)
    assert held == dq.RAISED_BY_PERSON_OR_SESSION
    assert held in dq.REAL_DECISION_CLASSES


@pytest.mark.parametrize("status", ["in_progress", "pending", "awaiting_approval"])
@pytest.mark.parametrize("newer", [True, False])
def test_an_operator_only_hold_is_kept_on_any_live_task(status, newer):
    held = klass(
        brief(
            reason=OP_ONLY, blast_radius="never", plan_summary="Action: operator_only."
        ),
        task(status=status),
        newer=newer,
    )
    assert held == dq.OPERATOR_ONLY


def test_an_operator_only_label_on_the_task_alone_is_still_kept():
    """A later gate pass can write the label on the task and not this brief."""
    held = klass(brief(), task(blocked_reason=OP_ONLY))
    assert held in dq.REAL_DECISION_CLASSES
    assert held == dq.OPERATOR_ONLY_LABEL


def test_a_note_after_the_automatic_reason_does_not_make_it_a_written_hold():
    noted = f"{UNSCORED_LOW}; declared local_edit, but the task text names an outward action"
    assert klass(brief(reason=noted), task(blocked_reason=noted)) == dq.LIVE_LOW


def test_noise_classes_are_exactly_the_ones_left_out_of_the_reading():
    noise = {
        dq.TASK_FINISHED,
        dq.OLDER_DUPLICATE,
        dq.TASK_MOVED_ON,
        dq.LIVE_SESSION_OWNED,
        dq.LIVE_CODE,
        dq.LIVE_LOW,
        dq.OWNED_BY_PERSON,
        dq.OPERATOR_ONLY_TASK_FINISHED,
    }
    assert not noise & dq.REAL_DECISION_CLASSES


# ── Whole-queue behaviour ────────────────────────────────────────────────────


def record(entity_id, snapshot, *, at=NOW - timedelta(hours=100), count=1):
    return {
        "entity_id": entity_id,
        "snapshot": snapshot,
        "last_observation_at": at.isoformat(),
        "observation_count": count,
    }


def test_only_open_checkpoints_are_placed():
    briefs = [
        record("ent_a", brief()),
        record("ent_b", brief(status="superseded")),
        record("ent_c", brief(status="approved")),
    ]
    placed = dq.classify_queue(
        briefs, {"ent_t": record("ent_t", task())}, now=NOW, person_owners=PEOPLE
    )
    assert [p.checkpoint_id for p in placed] == ["ent_a"]


def test_the_newest_of_several_on_one_task_keeps_its_class():
    briefs = [
        record("ent_old", brief(), at=NOW - timedelta(days=9)),
        record("ent_new", brief(), at=NOW - timedelta(days=1)),
    ]
    placed = {
        p.checkpoint_id: p.klass
        for p in dq.classify_queue(
            briefs, {"ent_t": record("ent_t", task())}, now=NOW, person_owners=PEOPLE
        )
    }
    assert placed == {"ent_old": dq.OLDER_DUPLICATE, "ent_new": dq.LIVE_LOW}


def test_the_reading_counts_only_real_decisions_and_their_age():
    briefs = [
        record(
            "ent_real", brief(checkpoint_name="DESIGN"), at=NOW - timedelta(hours=100)
        ),
        record(
            "ent_fresh",
            brief(checkpoint_name="DESIGN", task_entity_id="ent_u"),
            at=NOW - timedelta(hours=10),
        ),
        record(
            "ent_noise", brief(task_entity_id="ent_v"), at=NOW - timedelta(hours=900)
        ),
    ]
    tasks = {
        "ent_t": record("ent_t", task()),
        "ent_u": record("ent_u", task()),
        "ent_v": record("ent_v", task()),
    }
    summary = dq.summarize(
        dq.classify_queue(briefs, tasks, now=NOW, person_owners=PEOPLE)
    )
    assert summary["total_open"] == 3
    assert summary["real_decisions"] == 2
    assert summary["real_decisions_over_limit"] == 1
    assert summary["real_decisions_oldest_hours"] == 100.0


def test_a_real_decision_counts_once_per_task_in_the_reading():
    briefs = [
        record("ent_1", brief(checkpoint_name="DESIGN"), at=NOW - timedelta(hours=200)),
        record("ent_2", brief(checkpoint_name="DESIGN"), at=NOW - timedelta(hours=100)),
    ]
    summary = dq.summarize(
        dq.classify_queue(
            briefs, {"ent_t": record("ent_t", task())}, now=NOW, person_owners=PEOPLE
        )
    )
    assert summary["real_decisions"] == 1
    assert summary["real_decisions_oldest_hours"] == 200.0


# ── Could approving it start work ────────────────────────────────────────────


def signed(**over):
    envelope = {
        "task_entity_id": "ent_t",
        "task_observation_count": 3,
        "task_last_observation_at": "2026-10-01T00:00:00Z",
    }
    envelope.update(over)
    return brief(body=json.dumps(envelope))


def task_record(status="awaiting_approval", count=3, at="2026-10-01T00:00:00Z"):
    return {
        "entity_id": "ent_t",
        "snapshot": task(status=status),
        "observation_count": count,
        "last_observation_at": at,
    }


def test_a_signed_hold_on_the_same_task_revision_may_release():
    assert dq.approval_could_release(signed(), task_record()) is True


@pytest.mark.parametrize(
    "b,t",
    [
        (brief(), task_record()),  # no signed record
        (brief(body="not json"), task_record()),
        (signed(), task_record(count=4)),  # the task changed since
        (signed(), task_record(at="2026-10-02T00:00:00Z")),
        (signed(task_entity_id="ent_other"), task_record()),
        (signed(), task_record(status="done")),
        (signed(), task_record(status="in_progress")),
        (signed(), None),
        ({**signed(), "blast_radius": "never"}, task_record()),
    ],
)
def test_approval_cannot_release_in_these_cases(b, t):
    assert dq.approval_could_release(b, t) is False


def test_person_names_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("ATELES_OPERATOR_NAMES", "Sam, Sam Doe")
    monkeypatch.delenv("APIS_OPERATOR_LOGIN", raising=False)
    assert (
        dq.classify_one(brief(), task(owner="sam doe"), has_newer_open=False)
        == dq.OWNED_BY_PERSON
    )
    assert (
        dq.classify_one(brief(), task(owner="someone else"), has_newer_open=False)
        == dq.LIVE_LOW
    )


# ── copies must not drift from the code that owns them ──────────────────────
import sys as _sys  # noqa: E402
from pathlib import Path as _Path  # noqa: E402

_REPO = _Path(__file__).resolve().parents[2]

#: Each automatic reason prefix, with the module that composes it.
_PREFIX_OWNERS = {
    "not scored by a producer": "lib/daemon_runtime/gating.py",
    "below confidence threshold": "lib/daemon_runtime/gating.py",
    "low confidence and high blast radius": "lib/daemon_runtime/gating.py",
    "high blast radius — operator approval required": "lib/daemon_runtime/gating.py",
    "operator-only action — never auto-executable": "lib/daemon_runtime/gating.py",
    "producer score could not be recorded": "execution/daemons/apis/apis.py",
    "task authorization context changed": "execution/daemons/apis/apis.py",
    "re-issued checkpoint": "execution/daemons/apis/apis.py",
    "checkpoint check failed at boundary": "lib/daemon_runtime/checkpoint_posture.py",
}


def test_every_automatic_prefix_has_a_named_owner_and_nothing_extra():
    assert set(dq.AUTOMATIC_REASON_PREFIXES) == set(_PREFIX_OWNERS)


@pytest.mark.parametrize("prefix,owner", sorted(_PREFIX_OWNERS.items()))
def test_automatic_prefix_is_still_composed_by_its_owner(prefix, owner):
    """Fails when the gating code rewords a reason the classifier calls automatic."""
    assert prefix in (_REPO / owner).read_text(), (
        f"{owner} no longer composes {prefix!r}"
    )


def test_effect_action_types_match_the_gates_own_high_blast_set():
    """Bound to the gate's policy source. The one extra type is deliberate."""
    if str(_REPO) not in _sys.path:
        _sys.path.insert(0, str(_REPO))
    from lib.daemon_runtime import gating

    owned = set(gating._FALLBACK_HIGH_BLAST)
    assert dq.EFFECT_ACTION_TYPES - owned == {"correspondence"}, (
        "an effect type the gate does not list as high blast"
    )
    # safe direction: the gate's outward types must all be kept as real
    outward = {
        "payment",
        "send_external_comms",
        "publish",
        "release",
        "delete_entity_or_data",
        "external_api_write",
    }
    assert outward <= owned and outward <= dq.EFFECT_ACTION_TYPES


@pytest.mark.parametrize(
    "owner,expected",
    [
        ("codex_subagent:lens_review", dq.LIVE_SESSION_OWNED),
        ("operator_session", dq.LIVE_SESSION_OWNED),
        ("Codex coordinator0a1b2c3d-1111-2222-3333-444444444444", dq.LIVE_SESSION_OWNED),
        ("falco independent review; root verifies", dq.LIVE_LOW),
    ],
)
def test_session_owner_uses_the_shared_recognizer(owner, expected):
    assert klass(brief(), task(owner=owner)) == expected
