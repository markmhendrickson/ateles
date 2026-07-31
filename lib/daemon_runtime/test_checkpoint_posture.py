"""Tests for the on_check_failure posture enforcement (ateles#350)."""

from __future__ import annotations

import asyncio

import pytest

from lib.daemon_runtime.checkpoint_posture import (
    CheckFailure,
    PostureOutcome,
    evaluate_with_posture,
)
from lib.daemon_runtime.gating import CheckpointPosture, ExecutionPolicy


def _policy(**postures) -> ExecutionPolicy:
    return ExecutionPolicy(
        entity_id="p",
        checkpoint_postures={k: CheckpointPosture(v) for k, v in postures.items()},
        loaded=True,
    )


async def _ok_check():
    return "the-real-result"


async def _failing_check():
    raise CheckFailure("Neotoma unreachable")


def test_check_success_returns_result_unchanged():
    result = asyncio.run(
        evaluate_with_posture(
            _ok_check,
            boundary="issue.triaged",
            policy=_policy(),
            task_entity_id="ent_task",
            title="t",
            handler="test",
        )
    )
    assert result == "the-real-result"


def test_open_posture_logs_and_proceeds_on_check_failure(caplog):
    pol = _policy(**{"pre_impl": "open"})
    with caplog.at_level("WARNING"):
        result = asyncio.run(
            evaluate_with_posture(
                _failing_check,
                boundary="pre_impl",
                policy=pol,
                task_entity_id="ent_task",
                title="t",
                handler="test",
            )
        )
    assert isinstance(result, PostureOutcome)
    assert result.proceeded is True
    assert result.posture == CheckpointPosture.OPEN
    assert "on_check_failure=open" in caplog.text
    assert "boundary=pre_impl" in caplog.text


def test_closed_posture_blocks_and_never_proceeds_on_check_failure(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "lib.daemon_runtime.checkpoint_posture.write_checkpoint_brief",
        lambda **kw: (calls.append(kw), "ent_brief_123")[1],
    )
    notified = []

    def _notify(msg, **kw):
        notified.append((msg, kw))

    pol = _policy(**{"pre_merge": "closed"})
    result = asyncio.run(
        evaluate_with_posture(
            _failing_check,
            boundary="pre_merge",
            policy=pol,
            task_entity_id="ent_task",
            title="Merge PR #1",
            handler="apis",
            notify=_notify,
        )
    )
    assert isinstance(result, PostureOutcome)
    assert result.proceeded is False
    assert result.posture == CheckpointPosture.CLOSED
    assert result.checkpoint_brief_id == "ent_brief_123"
    assert len(calls) == 1
    assert calls[0]["task_entity_id"] == "ent_task"
    assert len(notified) == 1
    assert "posture=closed" in notified[0][0]


def test_closed_posture_still_blocks_when_brief_persist_fails(monkeypatch):
    # write_checkpoint_brief returns None when Neotoma is unreachable — the
    # caller must STILL not proceed (fail closed, never a silent hang OR a
    # silent proceed).
    monkeypatch.setattr(
        "lib.daemon_runtime.checkpoint_posture.write_checkpoint_brief",
        lambda **kw: None,
    )
    result = asyncio.run(
        evaluate_with_posture(
            _failing_check,
            boundary="pre_payment",
            policy=_policy(**{"pre_payment": "closed"}),
            task_entity_id="ent_task",
            title="t",
            handler="test",
        )
    )
    assert result.proceeded is False
    assert result.checkpoint_brief_id is None


def test_default_absent_boundary_behaves_as_open_no_behavior_change():
    # A boundary with no checkpoint_postures entry and not in
    # DEFAULT_CLOSED_BOUNDARIES resolves open — matches pre-#350 behavior.
    result = asyncio.run(
        evaluate_with_posture(
            _failing_check,
            boundary="some_new_boundary_nobody_configured",
            policy=_policy(),
            task_entity_id="ent_task",
            title="t",
            handler="test",
        )
    )
    assert result.proceeded is True


def test_file_brief_override_replaces_default_write_checkpoint_brief(monkeypatch):
    # Callers whose checkpoints aren't keyed by a Neotoma task entity (e.g.
    # a GitHub-event dispatcher keyed by owner/repo#n) supply their own
    # escalation via file_brief — the default write_checkpoint_brief must
    # NOT be called in that case.
    default_calls = []
    monkeypatch.setattr(
        "lib.daemon_runtime.checkpoint_posture.write_checkpoint_brief",
        lambda **kw: default_calls.append(kw) or "should-not-be-used",
    )
    override_calls = []

    async def _file_brief(boundary, error, decision):
        override_calls.append((boundary, error, decision))
        return "ent_custom_brief"

    result = asyncio.run(
        evaluate_with_posture(
            _failing_check,
            boundary="pre_merge",
            policy=_policy(**{"pre_merge": "closed"}),
            title="t",
            handler="apis",
            file_brief=_file_brief,
        )
    )
    assert default_calls == []
    assert len(override_calls) == 1
    assert override_calls[0][0] == "pre_merge"
    assert result.checkpoint_brief_id == "ent_custom_brief"
    assert result.proceeded is False


def test_non_check_failure_exception_also_resolved_by_posture():
    # evaluate_with_posture treats ANY exception from check() as a check
    # failure, not just the CheckFailure marker type — a bare ValueError
    # (e.g. malformed response) must still route through posture logic.
    async def _raises_value_error():
        raise ValueError("malformed response")

    result = asyncio.run(
        evaluate_with_posture(
            _raises_value_error,
            boundary="pre_release",
            policy=_policy(),
            task_entity_id="ent_task",
            title="t",
            handler="test",
        )
    )
    assert result.proceeded is False  # pre_release defaults closed
