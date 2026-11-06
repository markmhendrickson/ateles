"""A replayed task event leaves a task alone while its Fly run is in flight.

Seen on 2026-11-06: after an Apis restart a replayed task event moved a task
from executing to routed while its Fly run was still in the ledger, and the
gate then held it again; releasing it could build the same change twice.

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


def test_due_today_auto_execute_with_run_in_flight_reminds_but_does_not_route(env, monkeypatch):
    # Settles security question 1: does a due_today event with APIS_AUTO_EXECUTE=1
    # re-route a task whose Fly run is in flight? Before this change that branch
    # called dispatch_task unguarded (routed == [TASK]), so the task could be
    # moved to routed and launched again. It now uses the same guard as created
    # and updated: the operator reminder is still sent, dispatch is not.
    ledger, routed = env
    monkeypatch.setattr(apis, "AUTO_EXECUTE", True)
    _launching(ledger)
    notifier = _Notifier()
    asyncio.run(apis.handle_event(_event("due_today"), notifier))
    assert routed == [], f"due_today routed a task whose Fly run is in flight: {routed}"
    assert len(notifier.sent) == 1


def test_due_today_auto_execute_with_unreadable_ledger_does_not_route(env, monkeypatch):
    ledger, routed = env
    monkeypatch.setattr(apis, "AUTO_EXECUTE", True)
    ledger.write_text("{not json")
    asyncio.run(apis.handle_event(_event("due_today"), _Notifier()))
    assert routed == []


def test_due_today_auto_execute_with_no_run_routes_as_today(env, monkeypatch):
    _, routed = env
    monkeypatch.setattr(apis, "AUTO_EXECUTE", True)
    asyncio.run(apis.handle_event(_event("due_today"), _Notifier()))
    assert routed == [TASK]


def test_update_moving_task_to_another_repo_mid_run_does_not_route(env):
    # Settles security question 2: an update that changes the repository (and so
    # the receipt key) while the run is in flight does reach the guard, but the
    # new key misses the earlier receipt, so the guard reports no run in flight.
    # Nothing is routed anyway, because the updated branch never dispatches: it
    # only logs. A second run would need a later created replay, due_today or
    # approval against the changed key; see "Open question for security" in the PR.
    ledger, routed = env
    _launching(ledger)
    moved = _event("updated")
    moved.snapshot = {**moved.snapshot, "repo": "markmhendrickson/other-repo"}
    assert apis._fly_run_in_flight(moved.snapshot, TASK) is False  # key no longer matches
    asyncio.run(apis.handle_event(moved, _Notifier()))
    assert routed == []
