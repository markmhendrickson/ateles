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
from lib.daemon_runtime.gating import entity_record_digest
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
    # A retry requires a version on every record it compares, as Neotoma's
    # records carry; the shared store gives only the checkpoint one.
    records[task_id]["entity_version"] = "task-v1"
    # The approval binds the task revision it was shown; bind it to this one.
    revision = entity_record_digest(records[task_id])
    read_authority = apis.read_authenticated_checkpoint_authorization

    def read_versioned_authority(checkpoint_id, record):
        authority = read_authority(checkpoint_id, record)
        return None if authority is None else {**authority, "task_revision": revision}

    monkeypatch.setattr(apis, "read_authenticated_checkpoint_authorization",
                        read_versioned_authority)
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


class _Recorder:
    def __init__(self):
        self.sent = []

    def send(self, message, priority=None, handler=None, **kwargs):
        self.sent.append((message, priority))

    def blockers(self):
        return [m for m, p in self.sent if p == apis.Priority.BLOCKER]


@pytest.fixture
def escalation(released, monkeypatch):
    """Record checkpoint writes, retirements, status writes and notices."""
    briefs: list[dict] = []
    retired: list[str] = []
    statuses: list[str] = []
    state = {"brief_id": "ent_cp_exhausted"}
    notifier = _Recorder()

    def file_fresh(**kwargs):
        briefs.append(kwargs)
        return state["brief_id"]

    real_retire = apis.require_fresh_checkpoint_approval

    def retire(checkpoint_id, **kwargs):
        retired.append(checkpoint_id)
        return real_retire(checkpoint_id, **kwargs)

    real_set = apis.set_task_status

    def set_status(task_id, status, **kwargs):
        statuses.append(status.value if hasattr(status, "value") else str(status))
        return real_set(task_id, status, **kwargs)

    monkeypatch.setattr(apis, "_file_fresh_checkpoint", file_fresh)
    monkeypatch.setattr(apis, "require_fresh_checkpoint_approval", retire)
    monkeypatch.setattr(apis, "set_task_status", set_status)
    monkeypatch.setattr(apis.Notifier, "from_neotoma", lambda: notifier)
    monkeypatch.setattr(apis, "TelegramNotifier", lambda *a, **k: notifier, raising=False)
    return dict(released, briefs=briefs, retired=retired, statuses=statuses,
                state=state, notifier=notifier)


def _assert_rounds_exhausted(esc):
    task = _task(esc)
    assert task["status"] == "awaiting_approval"
    assert task["blocked_reason"].startswith("rounds_exhausted")
    assert len(esc["briefs"]) == 1
    assert esc["briefs"][0]["gate_reason"] == "rounds_exhausted"
    assert esc["briefs"][0]["context_label"] == "rounds_exhausted"
    assert esc["retired"] == [esc["brief_id"]]
    assert "failed" not in esc["statuses"]


@pytest.mark.asyncio
async def test_three_temporary_refusals_escalate_rounds_exhausted(escalation):
    escalation["script"].extend(_refusal("timeout") for _ in range(4))
    await _release(escalation)
    # The first dispatch plus three retries, then ONE rounds_exhausted
    # checkpoint. Red before the change: status 'failed', no checkpoint.
    assert len(escalation["spawned"]) == 4
    _assert_rounds_exhausted(escalation)
    reason = _task(escalation)["blocked_reason"]
    assert "Retried 3 time(s)" in reason
    assert "agent_policy unavailable" in reason


@pytest.mark.asyncio
async def test_exhausted_retry_checkpoint_unwritable_is_not_failed(escalation):
    escalation["state"]["brief_id"] = None
    escalation["script"].extend(_refusal("timeout") for _ in range(4))
    await _release(escalation)
    assert len(escalation["spawned"]) == 4
    assert "failed" not in escalation["statuses"]
    assert _task(escalation)["status"] != "failed"
    assert escalation["retired"] == []
    assert any("rounds_exhausted" in m for m in escalation["notifier"].blockers())


@pytest.mark.asyncio
async def test_second_approved_release_exhaustion_creates_second_checkpoint(
        escalation, monkeypatch):
    # Red before the fix (key_suffix was the bare "rounds_exhausted"): the
    # second exhaustion replayed the first cycle's taskstatus/taskreason keys,
    # Neotoma (modelled below) deduped both writes, the read-back found the
    # task still executing, and no second checkpoint was filed: one
    # rounds_exhausted key per field, one brief, the second approval never
    # retired.
    from lib.daemon_runtime import gating as gating_module
    from lib.daemon_runtime import task_lifecycle

    records, task_id = escalation["records"], escalation["task_id"]
    original = records[escalation["brief_id"]]
    keys: list[str] = []
    revisions: dict[str, str] = {}
    authority = apis.read_authenticated_checkpoint_authorization

    def correct(entity_id, field, value, idempotency_key):
        # Neotoma dedupes a replayed idempotency key: success, nothing written.
        if idempotency_key in keys:
            return True
        keys.append(idempotency_key)
        records[entity_id]["snapshot"][field] = value
        return True

    def set_status(entity_id, status, **kwargs):
        escalation["statuses"].append(
            status.value if hasattr(status, "value") else str(status))
        return task_lifecycle.set_task_status(entity_id, status, **kwargs)

    def file_fresh(**kwargs):
        # A real replacement checkpoint: its own id, awaiting the operator,
        # bound to the task revision it was filed against.
        escalation["briefs"].append(kwargs)
        new_id = f"ent_cp_exhausted_{len(escalation['briefs'])}"
        record = copy.deepcopy(original)
        record["entity_id"] = new_id
        record["entity_version"] = "v1"
        record["snapshot"].update(status="awaiting_operator",
                                  resolved_dispatched=False)
        records[new_id] = record
        records["_observations"][new_id] = list(
            records["_observations"][escalation["brief_id"]])
        revisions[new_id] = entity_record_digest(records[task_id])
        return new_id

    def read_authority(checkpoint_id, record):
        found = authority(checkpoint_id, record)
        if found is None or checkpoint_id not in revisions:
            return found
        return {**found, "task_revision": revisions[checkpoint_id]}

    def read_approval(checkpoint_id, record, **kwargs):
        return {"principal_sub": "ateles@ateles-swarm",
                "observation_id": f"obs-approval-{checkpoint_id}"}

    monkeypatch.setattr(task_lifecycle, "_correct", correct)
    monkeypatch.setattr(apis, "set_task_status", set_status)
    monkeypatch.setattr(apis, "_file_fresh_checkpoint", file_fresh)
    monkeypatch.setattr(apis, "read_authenticated_checkpoint_authorization",
                        read_authority)
    monkeypatch.setattr(gating_module, "read_authenticated_checkpoint_resolution",
                        read_approval)
    monkeypatch.setattr(apis, "read_authenticated_checkpoint_resolution",
                        read_approval, raising=False)

    escalation["script"].extend(_refusal("timeout") for _ in range(4))
    await _release(escalation)
    assert _task(escalation)["status"] == "awaiting_approval"
    assert [b["context_label"] for b in escalation["briefs"]] == ["rounds_exhausted"]

    escalation["script"].extend(_refusal("timeout") for _ in range(4))
    result = await _resolve("ent_cp_exhausted_1", "approve")
    await _drain()
    assert result

    exhausted = [k for k in keys if "rounds_exhausted" in k]
    status_keys = [k for k in exhausted if k.startswith("taskstatus-")]
    reason_keys = [k for k in exhausted if k.startswith("taskreason-")]
    assert len(status_keys) == 2 and len(set(status_keys)) == 2
    assert len(reason_keys) == 2 and len(set(reason_keys)) == 2
    assert len(escalation["briefs"]) == 2
    assert escalation["retired"].count("ent_cp_exhausted_1") == 1
    assert escalation["retired"] == [escalation["brief_id"], "ent_cp_exhausted_1"]
    assert _task(escalation)["status"] == "awaiting_approval"
    assert "failed" not in escalation["statuses"]


def _assert_left_as_found(esc, status, reason):
    """No rounds_exhausted write, no checkpoint, and a BLOCKER notice."""
    task = _task(esc)
    assert task["status"] == status
    assert task.get("blocked_reason") == reason
    assert "awaiting_approval" not in esc["statuses"]
    assert "failed" not in esc["statuses"]
    assert esc["briefs"] == []
    assert esc["retired"] == []
    assert any("left as found" in m for m in esc["notifier"].blockers())


@pytest.mark.asyncio
async def test_no_baseline_writes_nothing_and_announces(escalation, monkeypatch):
    # Red before this fix (observed in the previous round's test, which pinned
    # it): with no baseline the escalation wrote awaiting_approval and filed a
    # rounds_exhausted checkpoint, so `"awaiting_approval" not in statuses`
    # failed.
    escalation["script"].append(_refusal("timeout"))
    monkeypatch.setattr(apis, "_released_retry_state",
                        lambda task_id, checkpoint_id, **kwargs: None)
    await _release(escalation)
    assert len(escalation["spawned"]) == 1
    _assert_left_as_found(escalation, "executing", "")


def _change_at_last_attempt(esc, monkeypatch, change):
    """Run four temporary refusals; apply ``change`` while the last attempt's
    launch runs, after its baseline was read."""
    esc["script"].extend(_refusal("timeout") for _ in range(4))
    spawn = apis._spawn_harness_skill

    async def changing_spawn(*args, **kwargs):
        if len(esc["spawned"]) == 3:
            change()
        return await spawn(*args, **kwargs)

    monkeypatch.setattr(apis, "_spawn_harness_skill", changing_spawn)


@pytest.mark.asyncio
async def test_hold_written_during_last_attempt_is_kept(escalation, monkeypatch):
    # Red before this fix (observed; cause inferred): the escalation took the
    # hold's status as from_status and overwrote it with awaiting_approval and
    # a rounds_exhausted reason, plus a new checkpoint.
    def hold():
        _task(escalation)["status"] = "blocked"
        _task(escalation)["blocked_reason"] = "held by a person"

    _change_at_last_attempt(escalation, monkeypatch, hold)
    await _release(escalation)
    assert len(escalation["spawned"]) == 4
    _assert_left_as_found(escalation, "blocked", "held by a person")


@pytest.mark.asyncio
async def test_terminal_task_at_exhaustion_stays_terminal(escalation, monkeypatch):
    # Red before this fix (observed; cause inferred): the escalation reopened
    # the done task as awaiting_approval.
    _change_at_last_attempt(
        escalation, monkeypatch, lambda: _task(escalation).update(status="done"))
    await _release(escalation)
    assert len(escalation["spawned"]) == 4
    _assert_left_as_found(escalation, "done", "")


@pytest.mark.asyncio
async def test_checkpoint_changed_at_exhaustion_writes_nothing(escalation, monkeypatch):
    # Red before this fix (observed; cause inferred): the escalation never
    # compared the checkpoint, so it wrote awaiting_approval and filed a
    # replacement checkpoint against the changed one.
    def bump():
        escalation["records"][escalation["brief_id"]]["entity_version"] = "cp-changed"

    _change_at_last_attempt(escalation, monkeypatch, bump)
    await _release(escalation)
    assert len(escalation["spawned"]) == 4
    _assert_left_as_found(escalation, "executing", "")


def _change_during_wait(esc, monkeypatch, change):
    real = apis._released_retry_state
    calls = []

    def fingerprint(task_id, checkpoint_id, **kwargs):
        calls.append(task_id)
        if len(calls) == 2:  # the read after the wait
            change()
        return real(task_id, checkpoint_id, **kwargs)

    monkeypatch.setattr(apis, "_released_retry_state", fingerprint)
    return calls


@pytest.mark.asyncio
async def test_policy_revision_changed_during_wait_no_spawn(escalation, monkeypatch):
    escalation["script"].append(_refusal("timeout"))
    real = apis.execution_policy_revision
    changed = []

    def revision(policy):
        return "changed-revision" if changed else real(policy)

    monkeypatch.setattr(apis, "execution_policy_revision", revision)
    calls = _change_during_wait(escalation, monkeypatch, lambda: changed.append(True))
    await _release(escalation)
    # Red before the change: the retry spawned under the stale approval.
    assert len(calls) == 2
    assert len(escalation["spawned"]) == 1
    assert any("no longer authorizes" in m for m in escalation["notifier"].blockers())


@pytest.mark.asyncio
async def test_authorization_absent_during_wait_no_spawn(escalation, monkeypatch):
    escalation["script"].append(_refusal("timeout"))
    real = apis.read_authenticated_checkpoint_authorization
    gone = []
    monkeypatch.setattr(apis, "read_authenticated_checkpoint_authorization",
                        lambda cid, rec: None if gone else real(cid, rec))
    calls = _change_during_wait(escalation, monkeypatch, lambda: gone.append(True))
    await _release(escalation)
    assert len(calls) == 2
    assert len(escalation["spawned"]) == 1


@pytest.mark.asyncio
async def test_action_type_changed_during_wait_no_spawn(escalation, monkeypatch):
    escalation["script"].append(_refusal("timeout"))
    real = apis._resolve_gate_action_type
    changed = []

    def resolve(skill, snapshot, policy):
        if changed:
            return SimpleNamespace(value="send_external_email")
        return real(skill, snapshot, policy)

    monkeypatch.setattr(apis, "_resolve_gate_action_type", resolve)
    calls = _change_during_wait(escalation, monkeypatch, lambda: changed.append(True))
    await _release(escalation)
    assert len(calls) == 2
    assert len(escalation["spawned"]) == 1


@pytest.mark.asyncio
async def test_gate_decision_inconsistent_during_wait_no_spawn(escalation, monkeypatch):
    escalation["script"].append(_refusal("timeout"))
    changed = []
    real = apis._decision_consistent_with_approval
    monkeypatch.setattr(
        apis, "_decision_consistent_with_approval",
        lambda decision, **kw: False if changed else real(decision, **kw))
    calls = _change_during_wait(escalation, monkeypatch, lambda: changed.append(True))
    await _release(escalation)
    assert len(calls) == 2
    assert len(escalation["spawned"]) == 1


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
    real = apis._released_retry_state
    calls = []

    def fingerprint(task_id, checkpoint_id, **kwargs):
        calls.append(task_id)
        if len(calls) == 2:  # the read after the wait
            _task(released)["status"] = "blocked"
            _task(released)["blocked_reason"] = "held by a person"
        return real(task_id, checkpoint_id, **kwargs)

    monkeypatch.setattr(apis, "_released_retry_state", fingerprint)
    await _release(released)
    # Red without the change: no retry existed, so the task went FAILED; with a
    # retry that ignored the change it would have spawned twice.
    assert len(released["spawned"]) == 1
    assert _task(released)["status"] == "blocked"
    assert _task(released)["blocked_reason"] == "held by a person"


@pytest.mark.asyncio
async def test_task_changed_during_launch_is_not_retried(released, monkeypatch):
    # Red against the old baseline (taken AFTER the refusal came back), this
    # failed with
    #   assert 2 == 1  (len(released["spawned"]))
    # the change made during the launch was in both the baseline and the read
    # after the wait, so the two agreed and the retry spawned the agent again.
    spawn = apis._spawn_harness_skill

    async def changing_spawn(*args, **kwargs):
        if not released["spawned"]:
            task = released["records"][released["task_id"]]
            task["snapshot"]["notes"] = "changed while the launch ran"
            task["entity_version"] = "task-v-changed"
            released["script"].append(_refusal("timeout"))
        return await spawn(*args, **kwargs)

    monkeypatch.setattr(apis, "_spawn_harness_skill", changing_spawn)
    await _release(released)
    assert len(released["spawned"]) == 1
    assert _task(released)["notes"] == "changed while the launch ran"
    assert _task(released)["status"] != "done"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["done", "completed", "cancelled", "verified", "blocked",
                                    "awaiting_approval", "awaiting_input",
                                    "some_unknown_status", 7, None])
async def test_task_terminal_or_held_during_wait_is_not_retried(
        released, monkeypatch, status):
    released["script"].append(_refusal("timeout"))
    real = apis._released_retry_state
    calls = []

    def fingerprint(task_id, checkpoint_id, **kwargs):
        result = real(task_id, checkpoint_id, **kwargs)
        calls.append(result)
        if len(calls) == 2:
            # Lands after the read that follows the wait, so the fingerprints
            # agree; only the status guard before resuming can see it.
            _task(released)["status"] = status
        return result

    monkeypatch.setattr(apis, "_released_retry_state", fingerprint)
    await _release(released)
    assert len(calls) == 2 and calls[0] == calls[1] and calls[0] is not None
    assert len(released["spawned"]) == 1
    assert _task(released)["status"] == status


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["awaiting_approval", "awaiting_input",
                                    "some_unknown_status", 7])
async def test_task_held_in_baseline_and_during_wait_is_not_retried(
        released, monkeypatch, status):
    # A fresh hold that lands before the pre-launch read is in the baseline
    # AND the read after the wait, so the fingerprints agree. Red before the
    # allow-list (awaiting_approval, awaiting_input, unknown) this failed with
    #   assert [1] == []  (resumed)
    # the retry resumed _finish_dispatch past the new hold's approval. A
    # non-string status already made the baseline unreadable, so 7 was green.
    released["script"].append(_refusal("timeout"))
    real = apis._released_retry_state
    calls = []
    resumed = []
    finish_retry = apis._schedule_released_retry

    def fingerprint(task_id, checkpoint_id, **kwargs):
        if not calls:
            _task(released)["status"] = status
        calls.append(task_id)
        return real(task_id, checkpoint_id, **kwargs)

    def schedule(**kwargs):
        resume = kwargs["resume"]

        async def spy(attempt):
            resumed.append(attempt)
            await resume(attempt)

        return finish_retry(**{**kwargs, "resume": spy})

    monkeypatch.setattr(apis, "_released_retry_state", fingerprint)
    monkeypatch.setattr(apis, "_schedule_released_retry", schedule)
    await _release(released)
    assert resumed == []
    assert len(released["spawned"]) == 1  # the first launch only
    assert len(calls) == 1  # the pre-launch read; no retry was scheduled


@pytest.mark.parametrize("status,allowed", [
    ("routed", True), ("executing", True), ("EXECUTING", True),
    ("awaiting_approval", False), ("awaiting_input", False), ("pending", False),
    ("blocked", False), ("done", False), ("some_unknown_status", False),
    ("", False), (None, False), (7, False), (["executing"], False),
])
def test_released_retry_dispatchable_is_an_allow_list(monkeypatch, status, allowed):
    monkeypatch.setattr(apis, "fetch_task_record",
                        lambda task_id: {"snapshot": {"status": status,
                                                      "blocked_reason": ""}})
    assert apis._released_retry_task_dispatchable("t") is allowed


@pytest.mark.parametrize("reason", ["held by a person", None, " "])
def test_released_retry_dispatchable_requires_empty_hold_reason(monkeypatch, reason):
    # Judged as _release_lifecycle_proven judges it: exactly "". Red before
    # this fix (observed): the status alone was checked, so True was returned.
    snapshot = {"status": "executing"}
    if reason is not None:
        snapshot["blocked_reason"] = reason
    monkeypatch.setattr(apis, "fetch_task_record", lambda task_id: {"snapshot": snapshot})
    assert apis._released_retry_task_dispatchable("t") is False


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["routed", "executing"])
async def test_hold_reason_at_retry_entry_is_not_retried(released, monkeypatch, status):
    # Red before this fix (observed; cause inferred): the retry checked only the
    # status, so a routed or executing task carrying a hold reason resumed and
    # the agent was spawned a second time.
    released["script"].append(_refusal("timeout"))
    real = apis._released_retry_state
    calls = []

    def fingerprint(task_id, checkpoint_id, **kwargs):
        result = real(task_id, checkpoint_id, **kwargs)
        calls.append(result)
        if len(calls) == 2:
            # After the read that follows the wait, so the fingerprints agree;
            # only the retry-entry guard can see the hold reason.
            _task(released)["status"] = status
            _task(released)["blocked_reason"] = "held by a person"
        return result

    monkeypatch.setattr(apis, "_released_retry_state", fingerprint)
    await _release(released)
    assert len(calls) == 2 and calls[0] == calls[1] and calls[0] is not None
    assert len(released["spawned"]) == 1
    assert _task(released)["status"] == status
    assert _task(released)["blocked_reason"] == "held by a person"


@pytest.mark.asyncio
async def test_unreadable_prelaunch_state_launches_but_refusal_is_not_retried(
        released, monkeypatch):
    released["script"].append(_refusal("timeout"))
    calls = []

    def fingerprint(task_id, checkpoint_id, **kwargs):
        calls.append(task_id)
        return None

    monkeypatch.setattr(apis, "_released_retry_state", fingerprint)
    await _release(released)
    assert len(calls) == 1  # the pre-launch read only; no retry was scheduled
    assert len(released["spawned"]) == 1
    # No baseline: nothing is written (never FAILED, never awaiting_approval);
    # the task stays as found.
    assert _task(released)["status"] == "executing"
    assert _task(released)["blocked_reason"] == ""


def _change_checkpoint_during_wait(released, monkeypatch, change):
    """Apply ``change`` to the checkpoint record just before the read that
    follows the retry wait."""
    real = apis._released_retry_state
    calls = []

    def fingerprint(task_id, checkpoint_id, **kwargs):
        calls.append(task_id)
        if len(calls) == 2:  # the read after the wait
            change(released["records"][released["brief_id"]])
        return real(task_id, checkpoint_id, **kwargs)

    monkeypatch.setattr(apis, "_released_retry_state", fingerprint)
    return calls


@pytest.mark.asyncio
async def test_checkpoint_version_changed_during_wait_stops_retry_without_spawn(
        released, monkeypatch):
    # Run against the old fingerprint (f817125) this case and the resolution
    # case below PASSED: it already compared version and resolution, so they
    # pin that comparison. What it got wrong is the authorization: there the
    # parametrized test below failed for every case with, e.g.,
    #   assert [('task-v1', 'executing', '', None, 'approved')] == [None]
    # (a checkpoint with no version still produced a fingerprint, so a retry
    # was scheduled), likewise ('task-v1', 'executing', '', 'v3', None) for an
    # unapproved one and a full tuple for one naming another task; and
    # test_owner_mismatch_is_not_authorized failed with a full tuple where
    # None was expected.
    released["script"].append(_refusal("timeout"))

    def bump(record):
        record["entity_version"] = "v99"

    calls = _change_checkpoint_during_wait(released, monkeypatch, bump)
    await _release(released)
    assert len(calls) == 2  # the retry was scheduled and then declined
    assert len(released["spawned"]) == 1


@pytest.mark.asyncio
async def test_checkpoint_resolution_changed_during_wait_stops_retry_without_spawn(
        released, monkeypatch):
    released["script"].append(_refusal("timeout"))

    def reopen(record):
        record["snapshot"]["status"] = "awaiting_operator"

    calls = _change_checkpoint_during_wait(released, monkeypatch, reopen)
    await _release(released)
    assert len(calls) == 2
    assert len(released["spawned"]) == 1


def _break_checkpoint_before_baseline(released, monkeypatch, change):
    """Apply ``change`` to the checkpoint record before the baseline read, so
    the retry is never scheduled."""
    real = apis._released_retry_state
    results = []

    def fingerprint(task_id, checkpoint_id, **kwargs):
        if not results:
            change(released["records"][released["brief_id"]])
        result = real(task_id, checkpoint_id, **kwargs)
        results.append(result)
        return result

    monkeypatch.setattr(apis, "_released_retry_state", fingerprint)
    return results


def _drop_version(record):
    record.pop("entity_version", None)


def _unapprove(record):
    record["snapshot"]["status"] = "awaiting_operator"


def _other_task(record):
    record["snapshot"]["task_entity_id"] = "ent_task_other"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [_drop_version, _unapprove, _other_task],
                         ids=["no_version", "unapproved", "other_task"])
async def test_unauthorized_checkpoint_gets_no_retry_and_no_spawn(
        released, monkeypatch, change):
    released["script"].append(_refusal("timeout"))
    results = _break_checkpoint_before_baseline(released, monkeypatch, change)
    await _release(released)
    # Unreadable: no retry was scheduled, so the state read ran once.
    assert results == [None]
    assert len(released["spawned"]) == 1
    # No baseline: nothing is written (never FAILED, never awaiting_approval);
    # the task stays as found.
    assert _task(released)["status"] == "executing"
    assert _task(released)["blocked_reason"] == ""


def test_owner_mismatch_is_not_authorized(released, monkeypatch):
    records = released["records"]
    brief_id, task_id = released["brief_id"], released["task_id"]
    records[brief_id]["snapshot"]["status"] = "approved"
    assert apis._released_retry_fingerprint(task_id, brief_id) is not None
    records["_observations"][task_id][0]["user_id"] = (
        "22222222-2222-4222-8222-222222222222")
    assert apis._released_retry_fingerprint(task_id, brief_id) is None
    assert apis._released_retry_fingerprint(
        task_id, brief_id, checkpoint_only=True) is None


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


# ── A policy slowdown spanning the prelaunch read and the agent's own ────────


def _policy_slowdown(esc, monkeypatch, *, recover_after_first_spawn: bool):
    """From the moment the task enters EXECUTING (just before the prelaunch
    read), ``resolve_policy_for_agent`` returns a policy that did not load, as
    it does when Neotoma is slow. The agent's own first read fails the same
    way (a scripted temporary refusal). With ``recover_after_first_spawn`` the
    policy loads again once that refusal has returned. Each spawn records
    whether the policy was loadable when it ran."""
    import dataclasses

    slow = {"on": False}
    real_resolve = apis.resolve_policy_for_agent

    def resolve(skill, *args, **kwargs):
        policy = real_resolve(skill, *args, **kwargs)
        return dataclasses.replace(policy, loaded=False) if slow["on"] else policy

    monkeypatch.setattr(apis, "resolve_policy_for_agent", resolve)
    real_set = apis.set_task_status

    def set_status(task_id, status, **kwargs):
        value = status.value if hasattr(status, "value") else str(status)
        if value == "executing" and not esc["launches"]:
            slow["on"] = True
        return real_set(task_id, status, **kwargs)

    monkeypatch.setattr(apis, "set_task_status", set_status)
    spawn = apis._spawn_harness_skill

    async def watched_spawn(*args, **kwargs):
        esc["launches"].append(not slow["on"])
        if slow["on"]:
            result = _refusal("timeout")  # the runner refuses: no agent starts
            if recover_after_first_spawn:
                slow["on"] = False
            return result
        return await spawn(*args, **kwargs)

    monkeypatch.setattr(apis, "_spawn_harness_skill", watched_spawn)
    scheduled = []
    real_schedule = apis._schedule_released_retry

    def schedule(**kwargs):
        ok = real_schedule(**kwargs)
        scheduled.append(ok)
        return ok

    monkeypatch.setattr(apis, "_schedule_released_retry", schedule)
    return slow, scheduled


@pytest.fixture
def slowdown(escalation):
    return dict(escalation, launches=[])


@pytest.mark.asyncio
async def test_policy_slowdown_spanning_prelaunch_and_agent_read_retries_automatically(
        slowdown, monkeypatch):
    # Red before this fix (observed against the previous head, bbd2bc8):
    #   assert ([False] and False is True)
    # The prelaunch baseline was _released_retry_fingerprint, which returned
    # None while the policy was not loaded, so _schedule_released_retry
    # refused (no baseline) and the task needed a person. That the
    # escalation also refused is inferred from the code.
    slow, scheduled = _policy_slowdown(slowdown, monkeypatch,
                                       recover_after_first_spawn=True)
    await _release(slowdown)
    assert scheduled and scheduled[0] is True
    # The first launch ran while the policy was unavailable and the runner
    # refused it; the retry started the agent exactly once, after it loaded.
    assert slowdown["launches"] == [False, True]
    assert _task(slowdown)["status"] == "done"
    assert "failed" not in slowdown["statuses"]
    assert slowdown["briefs"] == []
    assert slowdown["notifier"].blockers() == []


@pytest.mark.asyncio
async def test_policy_unavailable_through_every_attempt_escalates_once(
        slowdown, monkeypatch):
    # Red before this fix (observed against the previous head, bbd2bc8):
    #   AssertionError: assert 'executing' == 'awaiting_approval'
    # No baseline was read, so nothing was scheduled or escalated. (Inferred,
    # not run: with a baseline but no authorization check in the wait, each
    # retry would have resumed into the runner.)
    _policy_slowdown(slowdown, monkeypatch, recover_after_first_spawn=False)
    await _release(slowdown)
    # Only the first launch ran (and the runner refused it); every retry
    # found the policy unavailable and started nothing.
    assert slowdown["launches"] == [False]
    assert True not in slowdown["launches"]
    _assert_rounds_exhausted(slowdown)
    assert "Retried 3 time(s)" in _task(slowdown)["blocked_reason"]


@pytest.mark.asyncio
async def test_denied_authorization_at_retry_is_left_as_found(slowdown, monkeypatch):
    # Not red before this fix: run against the previous head (bbd2bc8) it
    # passed, since the old fingerprint also refused a changed revision with
    # the same BLOCKER. It pins that the new three-way check keeps "denied"
    # apart from "policy unavailable": a denied approval neither resumes nor
    # escalates. (Inferred red state: treating denied as unavailable would
    # have rescheduled and then filed a rounds_exhausted checkpoint.)
    slowdown["script"].append(_refusal("timeout"))
    real = apis.execution_policy_revision
    changed = []
    monkeypatch.setattr(apis, "execution_policy_revision",
                        lambda policy: "changed-revision" if changed else real(policy))
    spawn = apis._spawn_harness_skill
    statuses_at_refusal = []

    async def changing_spawn(*args, **kwargs):
        result = await spawn(*args, **kwargs)
        changed.append(True)  # the policy loads, at a revision the approval did not name
        statuses_at_refusal.append(list(slowdown["statuses"]))
        return result

    monkeypatch.setattr(apis, "_spawn_harness_skill", changing_spawn)
    await _release(slowdown)
    assert len(slowdown["spawned"]) == 1  # the refused first launch only
    assert slowdown["statuses"] == statuses_at_refusal[0]  # no status write
    assert slowdown["briefs"] == []
    assert slowdown["retired"] == []
    assert _task(slowdown)["status"] == "executing"
    assert _task(slowdown)["blocked_reason"] == ""
    assert any("no longer authorizes" in m for m in slowdown["notifier"].blockers())


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


def _status_error(code: int, write: dict) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://inert.invalid/correct")
    response = httpx.Response(code, json={"error": "server"}, request=request)
    return httpx.HTTPStatusError(f"{code}", request=request, response=response)


@pytest.mark.asyncio
async def test_executing_write_5xx_is_retried_once_then_spawns_once(joined, no_wait, monkeypatch):
    failed = []

    def before(write):
        if write["field"] == "status" and write["value"] == "executing" and not failed:
            failed.append(True)
            raise _status_error(503, write)
        return None

    joined["before_write"] = before
    seen = _watch_retry_start(joined, monkeypatch)
    await _release_correction(joined)
    # FieldWrite.transient counts a 5xx as temporary: one retry, which read the
    # task back before anything spawned, then exactly one spawn.
    assert failed
    assert len(seen) == 1 and seen[0]["calls"] == []
    assert len(_executing_writes(joined)) == 1
    assert joined["calls"] == ["owner", "job"]


@pytest.mark.asyncio
async def test_executing_write_4xx_is_not_retried(joined, no_wait, monkeypatch):
    def before(write):
        if write["field"] == "status" and write["value"] == "executing":
            raise _status_error(400, write)
        return None

    joined["before_write"] = before
    seen = _watch_retry_start(joined, monkeypatch)
    await _release_correction(joined)
    assert seen == []
    assert _executing_writes(joined) == []
    assert joined["calls"] == []
    assert not delivery._delivery_ledger().delivery_receipts()


@pytest.mark.asyncio
async def test_executing_write_exhausted_escalates_one_checkpoint(joined, no_wait, monkeypatch):
    briefs = []
    monkeypatch.setattr(apis, "_escalate_released_retry_exhausted",
                        lambda **kwargs: briefs.append(kwargs) or True)

    def before(write):
        if write["field"] == "status" and write["value"] == "executing":
            raise httpx.ConnectTimeout("slow")
        return None

    joined["before_write"] = before
    await _release_correction(joined)
    # The first pass and three retries all timed out: one rounds_exhausted
    # escalation, nothing spawned. Red before the change: briefs == [].
    assert len(briefs) == 1
    assert "Retried 3 time(s)" in briefs[0]["reason"]
    assert joined["calls"] == []
    assert not delivery._delivery_ledger().delivery_receipts()


# ── The retry verifies the authenticated approver ─────────────────────────────


RECORDED_APPROVER = "ateles@ateles-swarm"


def _approver_at_retry(monkeypatch, approval_for):
    """Answer the authenticated resolution read with the fixture's approval
    until the retry wait has passed its state and dispatchability checks,
    then with ``approval_for(**kwargs)``: so the first release is proven and
    only the retry-time check sees the change. Returns the retry-time calls."""
    real_read = apis.read_authenticated_checkpoint_resolution
    real_dispatchable = apis._released_retry_task_dispatchable
    at_retry: list[dict] = []
    retrying: list[bool] = []

    def dispatchable(task_id):
        retrying.append(True)  # only the retry wait (and escalation) asks
        return real_dispatchable(task_id)

    def read(checkpoint_id, record, **kwargs):
        if not retrying:
            return real_read(checkpoint_id, record, **kwargs)
        at_retry.append(kwargs)
        return approval_for(**kwargs)

    monkeypatch.setattr(apis, "_released_retry_task_dispatchable", dispatchable)
    monkeypatch.setattr(apis, "read_authenticated_checkpoint_resolution", read)
    return at_retry


async def _refused_then_retried(esc, monkeypatch):
    """Release, refuse the first launch temporarily, and record the status
    writes made by the moment of the refusal."""
    esc["script"].append(_refusal("timeout"))
    spawn = apis._spawn_harness_skill
    statuses_at_refusal = []

    async def recording_spawn(*args, **kwargs):
        result = await spawn(*args, **kwargs)
        statuses_at_refusal.append(list(esc["statuses"]))
        return result

    monkeypatch.setattr(apis, "_spawn_harness_skill", recording_spawn)
    await _release(esc)
    return statuses_at_refusal


def _assert_retry_denied(esc, statuses_at_refusal, at_retry):
    assert at_retry, "the retry never read the authenticated approval"
    assert len(esc["spawned"]) == 1  # the refused first launch only
    assert esc["statuses"] == statuses_at_refusal[0]  # no status write
    assert esc["briefs"] == []
    assert esc["retired"] == []
    assert _task(esc)["status"] == "executing"
    assert _task(esc)["blocked_reason"] == ""
    assert any("no longer authorizes" in m for m in esc["notifier"].blockers())


@pytest.mark.asyncio
async def test_retry_with_unattributed_approval_is_denied(escalation, monkeypatch):
    # Red before this fix, observed against 44cc0d2: at_retry == [], the retry
    # judged only the reducer status, which still says approved, and never
    # read the authenticated approval. Inferred, not observed (the first
    # assertion stops the test): the agent then started a second time.
    at_retry = _approver_at_retry(monkeypatch, lambda **kwargs: None)
    statuses_at_refusal = await _refused_then_retried(escalation, monkeypatch)
    # The checkpoint snapshot still reads approved:
    assert apis.read_checkpoint_resolution(
        escalation["records"][escalation["brief_id"]]["snapshot"]) == "approved"
    _assert_retry_denied(escalation, statuses_at_refusal, at_retry)


@pytest.mark.asyncio
async def test_retry_with_other_approver_is_denied(escalation, monkeypatch):
    # Red before this fix, observed against 44cc0d2: at_retry == [], so an
    # approval recorded by a different principal was never compared at retry.
    # Inferred, not observed: the agent then started a second time and no
    # BLOCKER was sent.
    recorded = {"sub": RECORDED_APPROVER}

    def approval_for(*, required_approver_sub, **kwargs):
        if required_approver_sub != recorded["sub"]:
            return None
        return {"principal_sub": recorded["sub"], "observation_id": "obs-approval"}

    at_retry = _approver_at_retry(monkeypatch, approval_for)
    # The approval observation the retry finds was supplied by someone else.
    recorded["sub"] = "someone-else@elsewhere"
    statuses_at_refusal = await _refused_then_retried(escalation, monkeypatch)
    assert at_retry[0]["required_approver_sub"] == RECORDED_APPROVER
    _assert_retry_denied(escalation, statuses_at_refusal, at_retry)


@pytest.mark.asyncio
async def test_retry_whose_approver_read_raises_is_denied(escalation, monkeypatch):
    # Red before this fix, observed against 44cc0d2: at_retry == [], no read
    # was made. Inferred, not observed: the retry started the agent again.
    def broken(**kwargs):
        raise RuntimeError("attribution read failed")

    at_retry = _approver_at_retry(monkeypatch, broken)
    statuses_at_refusal = await _refused_then_retried(escalation, monkeypatch)
    _assert_retry_denied(escalation, statuses_at_refusal, at_retry)


@pytest.mark.asyncio
async def test_retry_with_attributed_approval_resumes(escalation, monkeypatch):
    # Red before this fix, observed against 44cc0d2, only on at_retry == []:
    # the retry resumed without reading the approval. It pins that the new
    # check passes the authorization's approver, key and tenant and lets a
    # proven approval start the agent exactly once more.
    at_retry = _approver_at_retry(monkeypatch, lambda **kwargs: {
        "principal_sub": RECORDED_APPROVER, "observation_id": "obs-approval"})
    await _refused_then_retried(escalation, monkeypatch)
    assert at_retry
    assert at_retry[0]["required_approver_sub"] == RECORDED_APPROVER
    assert at_retry[0]["required_approver_jkt"]
    assert at_retry[0]["expected_user_id"]
    assert len(escalation["spawned"]) == 2  # the refusal, then one retry
    assert _task(escalation)["status"] == "done"


@pytest.mark.asyncio
async def test_checkpoint_only_retry_checks_approver(joined, no_wait, monkeypatch):
    # Red before this fix, observed against 44cc0d2: at_retry == [], the
    # checkpoint_only retry also judged only the reducer status. Inferred, not
    # observed: it then wrote executing again and spawned
    # (calls == ["owner", "job"]), as
    # test_timed_out_executing_write_that_did_not_land_is_retried shows for an
    # attributed approval.
    at_retry = _approver_at_retry(monkeypatch, lambda **kwargs: None)
    briefs = []
    monkeypatch.setattr(apis, "_file_fresh_checkpoint",
                        lambda **kwargs: briefs.append(kwargs) or "ent_cp_new")
    writes_at_refusal = []

    def before(write):
        if write["field"] == "status" and write["value"] == "executing" \
                and not writes_at_refusal:
            writes_at_refusal.append(list(joined["writes"]))
            raise httpx.ConnectTimeout("slow")
        return None

    joined["before_write"] = before
    notifier = _Recorder()
    await apis.handle_checkpoint_brief(
        joined["checkpoint_id"], copy.deepcopy(joined["checkpoint"]["snapshot"]), notifier)
    await _drain()
    assert writes_at_refusal
    assert at_retry, "the checkpoint_only retry never read the authenticated approval"
    assert joined["calls"] == []  # no agent started
    assert joined["writes"] == writes_at_refusal[0]  # no status write
    assert _executing_writes(joined) == []
    assert briefs == []  # no checkpoint
    assert not delivery._delivery_ledger().delivery_receipts()
    assert any("no longer authorizes" in m for m in notifier.blockers())
