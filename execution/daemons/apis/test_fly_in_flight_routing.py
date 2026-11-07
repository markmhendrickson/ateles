"""A replayed task event leaves a task alone while its Fly run is in flight.

Seen on 2026-11-06: after an Apis restart a replayed task event moved a task
from executing to routed while its Fly run was still in the ledger, and the
gate then held it again; releasing it could build the same change twice.

The check lives at the top of ``dispatch_task``, so every entrance meets it;
``handle_event`` keeps an earlier copy for task.created only, so a replay is
neither announced again nor claims the created dedupe.

What these looked like RED, before the guard in ``handle_event``:

    test_created_event_with_run_in_flight_does_not_route
        AssertionError: routed a task whose Fly run is in flight: ['ent_task_inflight']
    test_unreadable_ledger_does_not_route
        AssertionError: routed with an unreadable ledger: ['ent_task_inflight']

The updated-event and no-run tests passed before too: today an updated event
never routes, and a task with no run routes as before.

Run: pytest execution/daemons/apis/test_fly_in_flight_routing.py -q
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

REPO = "markmhendrickson/ateles"
TASK = "ent_task_inflight"


class _Notifier:
    def __init__(self):
        self.sent = []

    def send(self, *args, **kwargs):
        self.sent.append((args, kwargs))


def _event(action: str) -> apis.NeotomaEvent:
    event = apis.NeotomaEvent(
        entity_type="task", entity_id=TASK, action=action, raw={},
        snapshot={"title": "t", "status": "executing", "repo": REPO},
    )
    event.hydrated = True
    return event


@pytest.fixture
def env(tmp_path, monkeypatch):
    ledger = tmp_path / "fly-ledger.json"
    monkeypatch.setenv("APIS_FLY_LEDGER_FILE", str(ledger))
    routed: list[str] = []

    async def _hydrate(event):
        return None

    async def _dispatch(entity_id, snapshot, **kwargs):
        routed.append(entity_id)

    monkeypatch.setattr(apis, "hydrate_snapshot", _hydrate)
    monkeypatch.setattr(apis, "dispatch_task", _dispatch)
    monkeypatch.setattr(apis, "_seen_created", lambda entity_id: False)
    return ledger, routed


def _launching(ledger: Path) -> None:
    key = build_delivery.fly_delivery_key(REPO, None, TASK)
    assert fly_runner.FlyLedger(ledger).reserve_delivery(
        key, {"status": "launching", "branch": "b", "run_id": "", "token": "x"})


def test_created_event_with_run_in_flight_does_not_route(env):
    ledger, routed = env
    _launching(ledger)
    asyncio.run(apis.handle_event(_event("created"), _Notifier()))
    assert routed == [], f"routed a task whose Fly run is in flight: {routed}"


def test_updated_event_with_run_in_flight_does_not_route(env):
    ledger, routed = env
    _launching(ledger)
    asyncio.run(apis.handle_event(_event("updated"), _Notifier()))
    assert routed == []


def test_task_with_no_run_in_flight_routes_as_today(env):
    _, routed = env
    asyncio.run(apis.handle_event(_event("created"), _Notifier()))
    assert routed == [TASK]


def test_unreadable_ledger_does_not_route(env):
    ledger, routed = env
    ledger.write_text("{not json")
    asyncio.run(apis.handle_event(_event("created"), _Notifier()))
    assert routed == [], f"routed with an unreadable ledger: {routed}"


def test_launched_receipt_naming_a_run_still_in_flight_does_not_route(env):
    ledger, routed = env
    fly = fly_runner.FlyLedger(ledger)
    key = build_delivery.fly_delivery_key(REPO, None, TASK)
    assert fly.reserve_delivery(
        key, {"status": "launched", "branch": "b", "run_id": "run-1", "token": "x"})
    fly.record_untracked(machine_id="m-1", run_id="run-1", owner="apis",
                         config=fly_runner.FlyConfig.from_env(),
                         started_at=fly._clock(), max_run_seconds=600)
    assert "run-1" in fly.snapshot()["in_flight"]
    asyncio.run(apis.handle_event(_event("created"), _Notifier()))
    assert routed == [], f"routed a task whose receipt names an in-flight run: {routed}"


@pytest.fixture
def real_dispatch(tmp_path, monkeypatch):
    """The real dispatch_task, with every state write and spawn recorded, and a
    sentinel on the first step after the in-flight check (``_stored_tags``) so a
    test can see dispatch went past it without running the rest."""
    ledger = tmp_path / "fly-ledger.json"
    monkeypatch.setenv("APIS_FLY_LEDGER_FILE", str(ledger))
    writes: list = []
    spawns: list = []

    class _Proceeded(Exception):
        pass

    def _proceeded(snapshot):
        raise _Proceeded()

    async def _hydrate(event):
        return None

    async def _spawn(*args, **kwargs):
        spawns.append((args, kwargs))

    monkeypatch.setattr(apis, "hydrate_snapshot", _hydrate)
    monkeypatch.setattr(apis, "set_task_status", lambda *a, **k: writes.append((a, k)))
    monkeypatch.setattr(apis, "_spawn_harness_skill", _spawn)
    monkeypatch.setattr(apis, "run_skill", _spawn)
    monkeypatch.setattr(apis, "_stored_tags", _proceeded)
    return ledger, writes, spawns, _Proceeded


def _snapshot(**extra) -> dict:
    return {"title": "t", "status": "executing", "repo": REPO, **extra}


def test_created_event_with_no_repository_reaches_dispatch(env):
    # QA: a task with no repository has no receipt key, so the guard returns
    # False without reading the ledger and dispatch_task receives the task.
    _, routed = env
    event = _event("created")
    event.snapshot = {"title": "t", "status": "pending"}
    assert apis._fly_receipt_key(event.snapshot, TASK) == ""
    assert apis._fly_run_in_flight(event.snapshot, TASK) is False
    asyncio.run(apis.handle_event(event, _Notifier()))
    assert routed == [TASK]


# ── Security question: other entrances ──────────────────────────────────────
# Settles "checkpoint release, deferred recovery, the watchdog and the
# reconciler call dispatch_task directly, skipping the handle_event guard, so
# dispatch_task could write ROUTED before the launch-time receipt check". The
# check now runs at the top of dispatch_task itself, before any state write.
# These call dispatch_task directly, as those entrances do, with each of their
# triggers: an in-flight receipt or an unreadable ledger returns with no state
# write and no spawn; with no receipt, dispatch proceeds past the check.

@pytest.mark.parametrize("trigger,kwargs", [
    ("approved", {"gate_override": True}),      # checkpoint release
    ("build_released", {}),                     # deferred recovery
    ("watchdog", {}),                           # watchdog re-dispatch
    ("reconcile", {}),                          # reconciler
    ("due_today", {}),                          # due_today with AUTO_EXECUTE
])
def test_dispatch_task_with_run_in_flight_writes_and_spawns_nothing(real_dispatch, trigger, kwargs):
    ledger, writes, spawns, _ = real_dispatch
    _launching(ledger)
    result = asyncio.run(apis.dispatch_task(TASK, _snapshot(), trigger, _Notifier(), **kwargs))
    assert result is None
    assert writes == [], f"{trigger}: wrote state for a task whose Fly run is in flight"
    assert spawns == []


def test_dispatch_task_with_unreadable_ledger_writes_and_spawns_nothing(real_dispatch):
    ledger, writes, spawns, _ = real_dispatch
    ledger.write_text("{not json")
    asyncio.run(apis.dispatch_task(TASK, _snapshot(), "approved", _Notifier(), gate_override=True))
    assert writes == [] and spawns == []


def test_dispatch_task_with_no_receipt_proceeds(real_dispatch):
    _, writes, _, proceeded = real_dispatch
    with pytest.raises(proceeded):
        asyncio.run(apis.dispatch_task(TASK, _snapshot(), "reconcile", _Notifier()))
    assert writes == []  # stopped at the sentinel, before any write


def test_due_today_auto_execute_with_run_in_flight_reminds_but_does_not_route(real_dispatch, monkeypatch):
    # The due_today branch no longer carries its own copy of the check; the one
    # in dispatch_task refuses, and the operator reminder is still sent.
    ledger, writes, spawns, _ = real_dispatch
    monkeypatch.setattr(apis, "AUTO_EXECUTE", True)
    _launching(ledger)
    notifier = _Notifier()
    asyncio.run(apis.handle_event(_event("due_today"), notifier))
    assert writes == [] and spawns == []
    assert len(notifier.sent) == 1


def test_due_today_auto_execute_with_no_run_routes_as_today(env, monkeypatch):
    _, routed = env
    monkeypatch.setattr(apis, "AUTO_EXECUTE", True)
    asyncio.run(apis.handle_event(_event("due_today"), _Notifier()))
    assert routed == [TASK]


# ── Security question: a key that changes ───────────────────────────────────
# Settles "_fly_receipt_key is derived from the current task, so a task whose
# repository changes during a run misses its own in-flight receipt". The
# reservation now records the task's id on the receipt (``task_id``), and a
# task-keyed receipt ("owner/name#task:<id>") names it in its key, so the check
# treats any live receipt naming this task's id as in flight whatever key the
# task derives now. A receipt written before the id was recorded falls back to
# the key match alone (the gap is stated in ``_fly_run_in_flight``).

def test_task_keyed_run_is_still_in_flight_after_repository_change(real_dispatch):
    ledger, writes, spawns, _ = real_dispatch
    _launching(ledger)  # receipt "markmhendrickson/ateles#task:<TASK>"
    moved = _snapshot(repo="markmhendrickson/other-repo")
    assert apis._fly_receipt_key(moved, TASK) != apis._fly_receipt_key(_snapshot(), TASK)
    assert apis._fly_run_in_flight(moved, TASK) is True
    asyncio.run(apis.dispatch_task(TASK, moved, "approved", _Notifier(), gate_override=True))
    assert writes == [] and spawns == []


def test_task_keyed_receipt_for_another_task_is_not_this_tasks_run(real_dispatch):
    ledger, *_ = real_dispatch
    other = build_delivery.fly_delivery_key(REPO, None, "ent_other_task")
    assert fly_runner.FlyLedger(ledger).reserve_delivery(
        other, {"status": "launching", "branch": "b", "run_id": "", "token": "x"})
    assert apis._fly_run_in_flight(_snapshot(), TASK) is False


def test_issue_keyed_run_is_still_in_flight_after_repository_change(real_dispatch):
    # Red before the task id was recorded: the moved task derived a different
    # key, _fly_run_in_flight returned False and dispatch went on past the
    # check toward its ROUTED write (the fixture's sentinel raised).
    ledger, writes, spawns, _ = real_dispatch
    plan = build_delivery.FlyDeliveryPlan(
        repository=REPO, branch="b", base_branch="main",
        key=build_delivery.fly_delivery_key(REPO, 42, TASK), issue_number=42, task_id=TASK)
    assert build_delivery._reserve_delivery(plan)
    receipt = fly_runner.FlyLedger(ledger).snapshot()["deliveries"][build_delivery.canonical_key(plan.key)]
    assert receipt["task_id"] == TASK
    moved = _snapshot(issue_number=42, repo="markmhendrickson/other-repo")
    assert apis._fly_receipt_key(moved, TASK) != plan.key
    assert apis._fly_run_in_flight(moved, TASK) is True
    asyncio.run(apis.dispatch_task(TASK, moved, "approved", _Notifier(), gate_override=True))
    assert writes == [] and spawns == []


def test_settling_a_reservation_keeps_its_task_id(real_dispatch):
    # Red without the settle-side carry: a launched receipt lost task_id, so a
    # moved task's run that was still in flight after launch went unseen.
    ledger, *_ = real_dispatch
    plan = build_delivery.FlyDeliveryPlan(
        repository=REPO, branch="b", base_branch="main",
        key=build_delivery.fly_delivery_key(REPO, 42, TASK), issue_number=42, task_id=TASK)
    token = build_delivery._reserve_delivery(plan)
    assert build_delivery._settle_delivery(plan, token, {"status": "launched", "branch": "b", "run_id": "run-1"})
    receipt = fly_runner.FlyLedger(ledger).snapshot()["deliveries"][build_delivery.canonical_key(plan.key)]
    assert receipt["status"] == "launched" and receipt["task_id"] == TASK


def test_receipt_with_no_task_id_still_matches_by_key(real_dispatch):
    # A receipt written before the id was recorded: the key match alone finds
    # it, and dispatch refuses with no state write and no spawn.
    ledger, writes, spawns, _ = real_dispatch
    key = build_delivery.fly_delivery_key(REPO, 42, TASK)
    assert fly_runner.FlyLedger(ledger).reserve_delivery(
        key, {"status": "launching", "branch": "b", "run_id": "", "token": "x"})
    task = _snapshot(issue_number=42)
    assert apis._fly_run_in_flight(task, TASK) is True
    asyncio.run(apis.dispatch_task(TASK, task, "approved", _Notifier(), gate_override=True))
    assert writes == [] and spawns == []


# ── Another task's run on the same key ──────────────────────────────────────
# Two cases share one delivery key and the check must tell them apart. (1) The
# run in flight belongs to ANOTHER task that holds this task's issue in the
# build registry (a correction holding its source issue): this task is
# independent work, so it must reach the existing deferral path, which records
# the deferral on the task so it resumes when that run ends, and spawns
# nothing. Before this, the in-flight check returned first, no deferral was
# written and the source work never resumed. (2) The run is this task's own:
# nothing is written and nothing is spawned, as before. Neither case may start
# a second run on the key.

def test_other_tasks_run_defers_and_own_run_writes_nothing(real_dispatch, monkeypatch):
    ledger, writes, spawns, _ = real_dispatch
    monkeypatch.setattr(apis, "_stored_tags", lambda snapshot: [])
    deferred: list = []

    async def _record(entity_id, snapshot, claim, notifier):
        deferred.append((entity_id, claim.holder))
        return type("Outcome", (), {"sweep_owns_it": False})()

    monkeypatch.setattr(apis, "_record_deferral", _record)
    key = build_delivery.fly_delivery_key(REPO, 42, TASK)
    assert fly_runner.FlyLedger(ledger).reserve_delivery(
        key, {"status": "launching", "branch": "b", "run_id": "", "token": "x"})
    task = _snapshot(status="pending", issue_number=42, assigned_to="cicada",
                     action_type="open_or_merge_pr", confidence=0.95)
    assert apis._fly_run_in_flight(task, TASK) is True

    with build_delivery.claim_build(REPO, 42, "task:ent_other_task"):
        asyncio.run(apis.dispatch_task(TASK, task, "created", _Notifier()))
    assert deferred == [(TASK, "task:ent_other_task")]
    assert writes == [] and spawns == []

    deferred.clear()
    with build_delivery.claim_build(REPO, 42, f"task:{TASK}"):
        asyncio.run(apis.dispatch_task(TASK, task, "created", _Notifier()))
    assert deferred == [] and writes == [] and spawns == []
