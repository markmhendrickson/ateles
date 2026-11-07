"""A Fly delivery task cut off by an Apis restart before launch is recovered once.

Seen on 2026-11-06: a checkpoint was approved, Apis logged the dispatch, and the
automatic redeploy restarted Apis 12 s later. The task was already EXECUTING but
its Fly delivery receipt was not yet reserved, so no run launched, the ledger
stayed empty and nothing ever picked the task up again.

``recover_unlaunched_fly_tasks`` runs at startup and re-dispatches exactly such
tasks. These tests drive the real ``_spawn_harness_skill`` → ``run_fly_delivery``
path with only ``run_skill`` (the Fly launch) faked, so a launch is counted where
it would really happen.

What these looked like RED on the head before the change (``apis`` had no
recovery sweep): every test errored or failed with

    AttributeError: module 'apis' has no attribute '_consumed_approval'

With the sweep present but its receipt check removed, four failed: the second
sweep in ``test_restart_between_dispatch_and_launch_launches_exactly_once``
dispatched the task again (``assert len(dispatched) == 1``), and the in-flight,
``launching`` and settled-receipt cases dispatched a task whose run was already
recorded (``assert launches == [] and dispatched == []``), which would move a
running task back to routed. The existing reservation still refused the second
launch itself, which is why the concurrent case passes either way.

Run: timeout 300 python3 -m pytest -q execution/daemons/apis/test_fly_restart_before_launch.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import apis  # noqa: E402
import build_delivery  # noqa: E402
import fly_runner  # noqa: E402
from skill_runner import SkillResult  # noqa: E402

REPO = "owner/repo"
TASK = "ent_task_restart_before_launch"
CHECKPOINT = "ent_checkpoint_approved"


class _Notifier:
    def __init__(self):
        self.sent: list[str] = []

    def send(self, message, *args, **kwargs):
        self.sent.append(str(message))


def _task(**over) -> dict:
    snap = {
        "title": "Build the thing",
        "status": "executing",
        "repo": REPO,
        "assigned_to": "cicada",
        "action_type": "open_or_merge_pr",
    }
    snap.update(over)
    return snap


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Fly opted in, an empty ledger, Neotoma reads faked, and a dispatch that
    goes through the real spawn path with the Fly launch counted."""
    ledger = tmp_path / "fly-ledger.json"
    monkeypatch.setenv("APIS_FLY_LEDGER_FILE", str(ledger))
    monkeypatch.setenv("APIS_FLY_RUNNER_CLASSES", "open_or_merge_pr")
    monkeypatch.setattr(apis, "_tasks_in_dispatch", {})
    monkeypatch.setattr(apis, "_detached_runs_in_progress", set())

    state = {"task": _task(), "approval": (CHECKPOINT, "")}
    launches: list[str] = []
    dispatched: list[dict] = []
    writes: list[tuple] = []

    monkeypatch.setattr(apis, "query_all_entities",
                        lambda *a, **k: [{"entity_id": TASK, "snapshot": dict(state["task"])}])
    monkeypatch.setattr(apis, "fetch_task_snapshot", lambda entity_id: dict(state["task"]))
    monkeypatch.setattr(apis, "_consumed_approval", lambda task_id: state["approval"])
    monkeypatch.setattr(apis, "set_task_status", lambda *a, **k: writes.append((a, k)) or True)

    async def _run_skill(skill, prompt, **kwargs):
        assert kwargs.get("runner") == "fly"
        launches.append(kwargs["task_entity_id"])
        await asyncio.sleep(0)
        return SkillResult(skill, False, 0, "", "", fly_delivery_status="no_changes",
                           remote_run_id=f"run-{len(launches)}")

    async def _dispatch(entity_id, snapshot, trigger, notifier, **kwargs):
        dispatched.append({"trigger": trigger, **kwargs})
        return await apis._spawn_harness_skill("cicada", entity_id, snapshot, trigger, notifier)

    monkeypatch.setattr(apis, "run_skill", _run_skill)
    monkeypatch.setattr(apis, "dispatch_task", _dispatch)
    return ledger, state, launches, dispatched, writes


def test_restart_between_dispatch_and_launch_launches_exactly_once(env):
    ledger, _, launches, dispatched, _ = env
    notifier = _Notifier()
    # The process that dispatched died before reserving: EXECUTING, empty ledger.
    asyncio.run(apis.recover_unlaunched_fly_tasks(notifier))
    # A further restart (or a replayed sweep) must not run it again.
    asyncio.run(apis.recover_unlaunched_fly_tasks(notifier))
    assert launches == [TASK], f"expected exactly one Fly run, got {launches}"
    assert len(dispatched) == 1
    assert dispatched[0]["trigger"] == "restart_recovery"
    assert dispatched[0]["gate_override"] is True
    assert dispatched[0]["release_key"].startswith(CHECKPOINT)
    receipt = build_delivery._receipt_for(
        build_delivery.fly_delivery_key(REPO, None, TASK),
        fly_runner.FlyLedger(ledger).delivery_receipts())
    assert receipt and receipt["run_id"] == "run-1" and receipt["task_id"] == TASK
    assert notifier.sent == []


def test_concurrent_recovery_still_launches_exactly_once(env):
    _, _, launches, _, _ = env

    async def both():
        await asyncio.gather(apis.recover_unlaunched_fly_tasks(_Notifier()),
                             apis.recover_unlaunched_fly_tasks(_Notifier()))

    asyncio.run(both())
    assert launches == [TASK], f"expected exactly one Fly run, got {launches}"


def test_run_in_flight_is_not_dispatched_again(env):
    ledger, _, launches, dispatched, writes = env
    fly = fly_runner.FlyLedger(ledger)
    key = build_delivery.fly_delivery_key(REPO, None, TASK)
    assert fly.reserve_delivery(key, {"status": "launched", "branch": "b", "run_id": "run-live",
                                      "token": "x", "task_id": TASK})
    fly.record_untracked(machine_id="m-1", run_id="run-live", owner="apis",
                         config=fly_runner.FlyConfig.from_env(),
                         started_at=fly._clock(), max_run_seconds=600)
    assert "run-live" in fly.snapshot()["in_flight"]
    notifier = _Notifier()
    asyncio.run(apis.recover_unlaunched_fly_tasks(notifier))
    assert launches == [] and dispatched == [] and writes == []
    assert notifier.sent == []


def test_launching_reservation_is_not_dispatched_again(env):
    ledger, _, launches, dispatched, _ = env
    key = build_delivery.fly_delivery_key(REPO, None, TASK)
    assert fly_runner.FlyLedger(ledger).reserve_delivery(
        key, {"status": "launching", "branch": "b", "run_id": "", "token": "x"})
    asyncio.run(apis.recover_unlaunched_fly_tasks(_Notifier()))
    assert launches == [] and dispatched == []


def test_settled_receipt_is_not_dispatched_again(env):
    ledger, _, launches, dispatched, _ = env
    key = build_delivery.fly_delivery_key(REPO, None, TASK)
    assert fly_runner.FlyLedger(ledger).reserve_delivery(
        key, {"status": "unknown", "branch": "b", "run_id": "", "token": "x", "task_id": TASK})
    asyncio.run(apis.recover_unlaunched_fly_tasks(_Notifier()))
    assert launches == [] and dispatched == []


@pytest.mark.parametrize("case", ["other_agent", "fly_not_opted_in"])
def test_task_not_routed_to_fly_is_untouched(env, monkeypatch, case):
    _, state, launches, dispatched, writes = env
    if case == "other_agent":
        state["task"] = _task(assigned_to="pavo", action_type="analysis")
    else:
        monkeypatch.delenv("APIS_FLY_RUNNER_CLASSES")
    notifier = _Notifier()
    asyncio.run(apis.recover_unlaunched_fly_tasks(notifier))
    assert launches == [] and dispatched == [] and writes == []
    assert notifier.sent == [], "a task that is not Fly-routed must not be reported either"


def test_task_without_a_consumed_approval_is_named_not_released(env):
    _, state, launches, dispatched, _ = env
    state["approval"] = (None, "no approved checkpoint that released it could be verified")
    notifier = _Notifier()
    asyncio.run(apis.recover_unlaunched_fly_tasks(notifier))
    assert launches == [] and dispatched == []
    assert len(notifier.sent) == 1 and TASK in notifier.sent[0]


def test_task_being_dispatched_in_this_process_is_left_alone(env, monkeypatch):
    _, _, launches, dispatched, _ = env
    monkeypatch.setattr(apis, "_tasks_in_dispatch", {TASK: 1})
    asyncio.run(apis.recover_unlaunched_fly_tasks(_Notifier()))
    assert launches == [] and dispatched == []


def test_consumed_approval_requires_an_approved_dispatched_checkpoint(monkeypatch):
    """Only an approved checkpoint the dispatcher already consumed releases it."""
    monkeypatch.setattr(apis, "checkpoints_for_task", lambda task_id: [
        {"entity_id": "ent_pending", "snapshot": {"status": "awaiting_operator"}},
        {"entity_id": "ent_unconsumed", "snapshot": {"status": "approved"}},
    ])
    monkeypatch.setattr(apis, "fetch_checkpoint_record",
                        lambda cid: pytest.fail("an unapproved checkpoint was read for release"))
    checkpoint, why = apis._consumed_approval(TASK)
    assert checkpoint is None and why
