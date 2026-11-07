"""A released builder dispatch survives a brief Neotoma slowdown.

Two cases left a released task needing a person: the agent_policy read timed
out (the refusal is right; only the task's fate changes), and the executing
status write timed out after it had in fact landed. A temporary failure is
retried with the same approved checkpoint after a delay, off the event stream;
anything else ends as before. A write with an unknown outcome always ends the
current pass with nothing further written; only the retry reads it back.
"""
from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace

import httpx
import pytest

import apis
import build_delivery as delivery
import skill_runner
from test_checkpoint_release import (  # noqa: F401 — fixtures
    _resolve, _cicada_delivers_a_pr, release_store,
)
from test_existing_pr_lifecycle_handoff import joined  # noqa: F401 — fixture
from test_existing_pr_owner_reader import normal, admitted_reader  # noqa: F401
from test_fly_run_job_delivery import world, existing_correction  # noqa: F401
from test_fly_delivery_dispatch import _StubNotifier
from test_policy_authority_dispatch import admission, once  # noqa: F401


def _refusal(code: str) -> skill_runner.SkillResult:
    error = (
        f"{skill_runner.POLICY_UNAVAILABLE_PREFIX}: agent_policy for 'cicada' is "
        f"unavailable (query, {code}); the agent was not run — nothing was "
        "dispatched; repair the load, the task is retried"
    )
    return skill_runner.SkillResult("cicada", False, None, "", "", error=error,
                                    provider="codex", policy_unavailable=True)


async def _drain() -> None:
    """Wait out every detached run and scheduled retry."""
    for _ in range(50):
        pending = [t for t in apis._background_dispatch_tasks if not t.done()]
        if not pending:
            return
        await asyncio.gather(*pending, return_exceptions=True)


@pytest.fixture
def no_wait(monkeypatch):
    monkeypatch.setattr(apis, "_RELEASED_RETRY_DELAYS", (0.0, 0.0, 0.0))


@pytest.fixture
def released(monkeypatch, release_store, no_wait):
    records, brief_id, task_id = release_store
    monkeypatch.setattr(apis, "DRY_RUN", False)
    monkeypatch.setattr(apis, "RUN_CONVERSATIONS", False)
    monkeypatch.setattr(apis, "RUN_EMAIL", False)
    success = _cicada_delivers_a_pr(monkeypatch, records)
    spawned: list[int] = []
    script: list = []

    async def spawn(skill, entity_id, *args, **kwargs):
        spawned.append(len(spawned))
        step = script.pop(0) if script else success()
        return step

    monkeypatch.setattr(apis, "_spawn_harness_skill", spawn)
    notifier = _StubNotifier()
    monkeypatch.setattr(apis, "TelegramNotifier", lambda *a, **k: notifier, raising=False)
    monkeypatch.setattr(apis._activity, "started", lambda *a: SimpleNamespace(
        finished=lambda *a: None, failed=lambda *a: None, escalated=lambda *a: None))
    return dict(records=records, brief_id=brief_id, task_id=task_id,
                spawned=spawned, script=script, success=success)


async def _release(state):
    result = await _resolve(state["brief_id"], "approve")
    await _drain()
    return result


def _task(state):
    return state["records"][state["task_id"]]["snapshot"]


@pytest.mark.asyncio
async def test_temporary_policy_refusal_then_good_read_dispatches_once(released):
    released["script"].append(_refusal("timeout"))
    await _release(released)
    # Red without the change: the refusal marked the task FAILED after one spawn.
    assert len(released["spawned"]) == 2
    assert _task(released)["status"] == "done"
    assert "agent_policy" not in (_task(released).get("blocked_reason") or "")


@pytest.mark.asyncio
async def test_three_temporary_refusals_end_as_today_with_retry_notice(released):
    released["script"].extend(_refusal("timeout") for _ in range(4))
    await _release(released)
    # The first dispatch plus three retries, then FAILED as before.
    assert len(released["spawned"]) == 4
    task = _task(released)
    assert task["status"] == "failed"
    assert "Retried 3 time(s)" in task["blocked_reason"]
    assert "agent_policy unavailable" in task["blocked_reason"]


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["not_found", "bad_response", "refused_status"])
async def test_permanent_refusal_fails_at_once(released, code):
    released["script"].append(_refusal(code))
    await _release(released)
    assert len(released["spawned"]) == 1
    assert _task(released)["status"] == "failed"
    assert "Retried" not in _task(released)["blocked_reason"]


@pytest.mark.asyncio
async def test_state_changed_during_wait_stops_retry_without_spawn(released, monkeypatch):
    released["script"].append(_refusal("timeout"))
    real = apis._released_retry_fingerprint
    calls = []

    def fingerprint(task_id, checkpoint_id, **kwargs):
        calls.append(task_id)
        if len(calls) == 2:  # the read after the wait
            _task(released)["status"] = "blocked"
            _task(released)["blocked_reason"] = "held by a person"
        return real(task_id, checkpoint_id, **kwargs)

    monkeypatch.setattr(apis, "_released_retry_fingerprint", fingerprint)
    await _release(released)
    # Red without the change: no retry existed, so the task went FAILED; with a
    # retry that ignored the change it would have spawned twice.
    assert len(released["spawned"]) == 1
    assert _task(released)["status"] == "blocked"
    assert _task(released)["blocked_reason"] == "held by a person"


def test_no_agent_runs_while_policy_is_unavailable(admission):
    """The real runner refuses before any provider or child, and that refusal is
    exactly what the retry classifies as temporary."""
    from swarm_dispatch import neotoma_read_failure_is_transient

    admission["query"] = httpx.ReadTimeout("slow")
    for _ in range(4):
        result = once()
        assert not result.ok and result.policy_unavailable
        assert neotoma_read_failure_is_transient(result)
    assert admission["effects"] == []


# ── Case 2: the executing status write ────────────────────────────────────────


async def _release_correction(state):
    await apis.handle_checkpoint_brief(
        state["checkpoint_id"], copy.deepcopy(state["checkpoint"]["snapshot"]), _StubNotifier())
    await _drain()


def _executing_writes(state):
    return [w for w in state["writes"] if w["field"] == "status" and w["value"] == "executing"]


def _watch_retry_start(state, monkeypatch):
    """Record what the first pass left behind, at the moment the retry starts."""
    seen = []
    real = apis._correction_write_landed

    def landed(binding, status):
        seen.append(dict(calls=list(state["calls"]), writes=list(state["writes"])))
        return real(binding, status)

    monkeypatch.setattr(apis, "_correction_write_landed", landed)
    return seen


@pytest.mark.asyncio
async def test_timed_out_executing_write_that_landed_continues_to_one_spawn(
        joined, no_wait, monkeypatch):
    timed_out = []

    def reply(write, version):
        if write["field"] == "status" and write["value"] == "executing" and not timed_out:
            timed_out.append(True)
            raise httpx.ReadTimeout("slow")
        return None

    joined["reply"] = reply
    seen = _watch_retry_start(joined, monkeypatch)
    await _release_correction(joined)
    assert timed_out
    # The first pass stopped at the unknown write: nothing spawned, and the
    # executing write is the last thing it wrote.
    assert len(seen) == 1
    assert seen[0]["calls"] == []
    assert seen[0]["writes"][-1]["field"] == "status"
    assert seen[0]["writes"][-1]["value"] == "executing"
    # Red with the inline read-back: seen == [] (no retry ran) because the
    # first pass itself continued to ["owner", "job"]. Red with no retry at
    # all: calls == [] with the task stranded in executing.
    # The retry found its own write landed, did not write it again, and
    # spawned exactly once.
    assert len(_executing_writes(joined)) == 1
    assert joined["calls"] == ["owner", "job"]
    assert list(delivery._delivery_ledger().delivery_receipts()) == ["owner/repo#12"]


@pytest.mark.asyncio
async def test_timed_out_executing_write_that_did_not_land_is_retried(joined, no_wait, monkeypatch):
    refused = []

    def before(write):
        if write["field"] == "status" and write["value"] == "executing" and not refused:
            refused.append(True)
            raise httpx.ConnectTimeout("slow")
        return None

    joined["before_write"] = before
    seen = _watch_retry_start(joined, monkeypatch)
    await _release_correction(joined)
    # Red without the change: one refused write, no retry, calls == [].
    assert refused
    assert len(seen) == 1 and seen[0]["calls"] == []
    # The refused write never reached the record; the retry wrote it again.
    assert len(_executing_writes(joined)) == 1
    assert joined["calls"] == ["owner", "job"]


@pytest.mark.asyncio
async def test_executing_write_changed_by_another_writer_stops_with_no_spawn(joined, no_wait):
    def before(write):
        if write["field"] == "status" and write["value"] == "executing":
            joined["record"]["snapshot"]["notes"] = "changed by someone else"
            joined["record"]["observation_count"] += 1
            joined["record"]["entity_version"] = f"task-v{joined['record']['observation_count']}"
            raise httpx.ReadTimeout("slow")
        return None

    joined["before_write"] = before
    await _release_correction(joined)
    assert _executing_writes(joined) == []
    assert joined["calls"] == []
    assert not delivery._delivery_ledger().delivery_receipts()
