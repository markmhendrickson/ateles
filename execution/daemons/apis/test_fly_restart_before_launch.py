"""A Fly delivery task cut off by an Apis restart before launch is reported once.

Seen on 2026-11-06: a checkpoint was approved, Apis logged the dispatch, and the
automatic redeploy restarted Apis 12 s later. The task was already EXECUTING but
its Fly delivery receipt was not yet reserved, so no run launched, the ledger
stayed empty and nothing ever picked the task up again.

``recover_unlaunched_fly_tasks`` runs at startup and names exactly such tasks to
the operator. It never relaunches one and never writes: the fixture's
``dispatch_task``, ``run_skill`` and ``set_task_status`` fakes record any call,
and every test asserts none happened.

What these looked like RED:

* Before the change (``apis`` had no sweep), every test failed with
  ``AttributeError`` on ``recover_unlaunched_fly_tasks`` (or on
  ``_tasks_in_dispatch`` in the fixture).
* ``test_stranded_task_is_reported_once_and_never_relaunched`` asserts no
  dispatch and one notice; #1686's sweep dispatched exactly this task with
  ``gate_override=True`` (read from its code, not run here).
* ``test_partial_task_inventory_sends_one_notice_and_reports_incomplete``,
  checked by mutation: with ``strict=True`` removed from the listing call, all
  four cases failed (the dropped-row and short-count cases reported the task
  with the per-task notice, the missing and not-a-list cases read an empty
  inventory and sent nothing; ``complete`` was True in each).
* ``test_inventory_repeating_one_id_in_place_of_another_is_incomplete``: on
  #1691's last head (row count only, no distinct-id rule) both cases read the
  inventory as complete, examined the task and sent the per-task stranded
  notice instead of the inventory notice.
* ``test_dispatch_guard_holds_the_id_while_dispatching_and_releases_it``,
  checked by mutation: with ``dispatch_task`` no longer adding the id, both
  cases failed ``assert apis._tasks_in_dispatch == {TASK: 1}`` while paused;
  with the release moved out of the ``finally``, both cases failed
  ``assert TASK not in apis._tasks_in_dispatch``.

* ``test_settled_receipt_of_an_earlier_task_on_the_same_pr_is_reported``,
  checked by mutation: with ``_fly_delivery_recorded`` restored to accepting
  any receipt on the task's key (round 1), it failed ``assert
  len(notifier.sent) == 1`` with no notice sent. ``test_own_receipt_on_the_same_pr_is_silent``
  passes either way and pins that the fix does not over-report.
* ``test_receipt_on_the_key_with_no_owner_is_unknown``, same mutation: the
  ownerless receipt read as recorded (True) instead of None.
* ``test_routing_check_that_raises_is_reported_as_unknown``, checked by
  mutation: with ``_fly_routing`` returning ``(False, "")`` on an exception
  (round 1's ``_fly_routed_delivery`` behaviour), no notice was sent and the
  task counted as ``not_fly``.
* ``test_unlaunched_fly_recovery_sweep_is_scheduled_at_startup``, checked by
  removing ``unlaunched_fly_recovery_sweep(),`` from the startup gather: the
  gather-block assertion failed.

Run: python3 -m pytest -q execution/daemons/apis/test_fly_restart_before_launch.py
"""

from __future__ import annotations

import asyncio
import inspect
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import apis  # noqa: E402
import build_delivery  # noqa: E402
import fly_runner  # noqa: E402
from lib.daemon_runtime import gating  # noqa: E402

REPO = "owner/repo"
TASK = "ent_task_restart_before_launch"


class _Notifier:
    def __init__(self):
        self.sent: list[str] = []

    def send(self, message, *args, **kwargs):
        # No notice may claim a run never launched: the sweep cannot always
        # know, so every notice says unknown.
        assert "never launched" not in str(message), message
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
    """Fly opted in, an empty ledger, Neotoma reads faked, and every effect
    (dispatch, Fly launch, task write) recorded so a test can assert none."""
    ledger = tmp_path / "fly-ledger.json"
    monkeypatch.setenv("APIS_FLY_LEDGER_FILE", str(ledger))
    monkeypatch.setenv("APIS_FLY_RUNNER_CLASSES", "open_or_merge_pr")
    monkeypatch.setattr(apis, "_tasks_in_dispatch", {})
    monkeypatch.setattr(apis, "_detached_runs_in_progress", set())

    state = {"task": _task()}
    effects: list[tuple] = []

    monkeypatch.setattr(apis, "query_all_entities",
                        lambda *a, **k: [{"entity_id": TASK, "snapshot": dict(state["task"])}])
    monkeypatch.setattr(apis, "fetch_task_snapshot", lambda entity_id: dict(state["task"]))
    monkeypatch.setattr(apis, "set_task_status",
                        lambda *a, **k: effects.append(("write", a)) or True)

    async def _run_skill(*a, **k):
        effects.append(("launch", a))

    async def _dispatch(*a, **k):
        effects.append(("dispatch", a, k))

    monkeypatch.setattr(apis, "run_skill", _run_skill)
    monkeypatch.setattr(apis, "dispatch_task", _dispatch)
    return ledger, state, effects


_REAL_DISPATCH_TASK = apis.dispatch_task
_REAL_QUERY_ALL_ENTITIES = apis.query_all_entities


def test_stranded_task_is_reported_once_and_never_relaunched(env):
    _, _, effects = env
    notifier = _Notifier()
    counts = asyncio.run(apis.recover_unlaunched_fly_tasks(notifier))
    assert effects == [], "the sweep must never dispatch, launch or write"
    assert len(notifier.sent) == 1, notifier.sent
    notice = notifier.sent[0]
    assert TASK in notice and "Build the thing" in notice
    assert "whether its Fly run launched is unknown" in notice
    assert "holds no receipt" in notice
    assert "check for an earlier Fly run" in notice
    assert "file a new task with a different title for the same work" in notice
    assert "release its checkpoint" in notice and "fresh checkpoint" not in notice
    assert counts["complete"] is True and counts["unrecovered"] == 1


def _reserve(ledger, status: str, **extra) -> None:
    key = build_delivery.fly_delivery_key(REPO, None, TASK)
    assert fly_runner.FlyLedger(ledger).reserve_delivery(
        key, {"status": status, "branch": "b", "run_id": "", "token": "x", **extra})


def test_run_in_flight_is_silent(env):
    ledger, _, effects = env
    fly = fly_runner.FlyLedger(ledger)
    key = build_delivery.fly_delivery_key(REPO, None, TASK)
    assert fly.reserve_delivery(key, {"status": "launched", "branch": "b", "run_id": "run-live",
                                      "token": "x", "task_id": TASK})
    fly.record_untracked(machine_id="m-1", run_id="run-live", owner="apis",
                         config=fly_runner.FlyConfig.from_env(),
                         started_at=fly._clock(), max_run_seconds=600)
    notifier = _Notifier()
    counts = asyncio.run(apis.recover_unlaunched_fly_tasks(notifier))
    assert effects == [] and notifier.sent == []
    assert counts["recorded"] == 1


@pytest.mark.parametrize("status", ["launching", "unknown"])
def test_recorded_receipt_is_silent(env, status):
    ledger, _, effects = env
    _reserve(ledger, status, task_id=TASK)
    notifier = _Notifier()
    asyncio.run(apis.recover_unlaunched_fly_tasks(notifier))
    assert effects == [] and notifier.sent == []


def test_unreadable_ledger_is_reported_as_unknown(env, monkeypatch):
    _, _, effects = env

    def _broken():
        raise fly_runner.FlyLedgerError("ledger unreadable")

    monkeypatch.setattr(build_delivery, "_delivery_ledger", _broken)
    notifier = _Notifier()
    counts = asyncio.run(apis.recover_unlaunched_fly_tasks(notifier))
    assert effects == []
    assert len(notifier.sent) == 1 and TASK in notifier.sent[0]
    assert "Fly receipt state is unknown" in notifier.sent[0]
    assert counts["unrecovered"] == 1 and counts["recorded"] == 0


def test_uninterpretable_ledger_entry_makes_the_receipt_state_unknown(monkeypatch):
    class _Ledger:
        def snapshot(self):
            return {"deliveries": {"some-key": "not-an-object"}}

    monkeypatch.setattr(apis, "_fly_run_state", lambda snap, eid: (False, None))
    monkeypatch.setattr(build_delivery, "_delivery_ledger", lambda: _Ledger())
    assert apis._fly_delivery_recorded(_task(), TASK) is None


@pytest.mark.parametrize("case", ["other_agent", "fly_not_opted_in"])
def test_task_not_routed_to_fly_is_untouched(env, monkeypatch, case):
    _, state, effects = env
    if case == "other_agent":
        state["task"] = _task(assigned_to="pavo", action_type="analysis")
    else:
        monkeypatch.delenv("APIS_FLY_RUNNER_CLASSES")
    notifier = _Notifier()
    asyncio.run(apis.recover_unlaunched_fly_tasks(notifier))
    assert effects == [] and notifier.sent == []


def test_task_that_moved_on_since_the_listing_is_not_reported(env, monkeypatch):
    _, _, effects = env
    monkeypatch.setattr(apis, "fetch_task_snapshot",
                        lambda entity_id: _task(status="completed"))
    notifier = _Notifier()
    asyncio.run(apis.recover_unlaunched_fly_tasks(notifier))
    assert effects == [] and notifier.sent == []


def test_failed_reread_is_reported(env, monkeypatch):
    _, _, effects = env
    monkeypatch.setattr(apis, "fetch_task_snapshot", lambda entity_id: None)
    notifier = _Notifier()
    asyncio.run(apis.recover_unlaunched_fly_tasks(notifier))
    assert effects == []
    assert len(notifier.sent) == 1 and "could not be re-read" in notifier.sent[0]


def test_row_without_an_entity_id_is_named_not_skipped(env, monkeypatch):
    _, _, effects = env
    monkeypatch.setattr(apis, "query_all_entities",
                        lambda *a, **k: [{"snapshot": {"status": "executing"}}])
    notifier = _Notifier()
    counts = asyncio.run(apis.recover_unlaunched_fly_tasks(notifier))
    assert effects == []
    assert len(notifier.sent) == 1 and "launched is unknown" in notifier.sent[0]
    assert counts["unrecovered"] == 1


@pytest.mark.parametrize("pages", [
    [{"entities": [{"entity_id": TASK}, "not-an-object"]}],
    [{"total": 1}],
    [{"entities": {"entity_id": TASK}}],
    [{"entities": [{"entity_id": TASK}], "total": 3}],
    [{"entities": [{"entity_id": TASK}]}],
], ids=["non_object_row", "entities_missing", "entities_not_a_list", "paging_cut_short",
        "short_page_without_total"])
def test_partial_task_inventory_sends_one_notice_and_reports_incomplete(env, monkeypatch, pages):
    """Through the real ``query_all_entities``: any row the listing could not
    keep makes the whole inventory unknown, never silently smaller."""
    _, _, effects = env
    monkeypatch.setattr(apis, "query_all_entities", _REAL_QUERY_ALL_ENTITIES)
    seen: list[dict] = []

    def _page(entity_type, *, snapshot_filters, offset=0, limit=None):
        seen.append({"type": entity_type, "filters": snapshot_filters})
        return pages[min(len(seen), len(pages)) - 1]

    monkeypatch.setattr(gating, "query_entities", _page)
    notifier = _Notifier()
    counts = asyncio.run(apis.recover_unlaunched_fly_tasks(notifier))
    assert seen and seen[0]["type"] == "task"
    assert seen[0]["filters"] == {"status": {"op": "eq", "value": "executing"}}
    assert effects == []
    assert len(notifier.sent) == 1, notifier.sent
    assert "could not fully read the executing-task inventory" in notifier.sent[0]
    assert counts["complete"] is False and counts["examined"] == 0


@pytest.mark.parametrize("page_size", [10, 1], ids=["same_page", "next_page"])
def test_inventory_repeating_one_id_in_place_of_another_is_incomplete(
        env, monkeypatch, page_size):
    """Two executing tasks exist and the listing reports ``total: 2``, but the
    second row repeats the first id. The count matches, so only the distinct-id
    rule catches it: the sweep must send the single inventory notice and report
    nothing, not name the repeated task as though the inventory were whole."""
    _, _, effects = env
    monkeypatch.setattr(apis, "query_all_entities", _REAL_QUERY_ALL_ENTITIES)
    monkeypatch.setattr(gating, "QUERY_PAGE_SIZE", page_size)
    row = {"entity_id": TASK, "snapshot": _task()}

    def _page(entity_type, *, snapshot_filters, offset=0, limit=None):
        rows = [row, dict(row)][offset:offset + (limit or page_size)]
        return {"entities": rows, "total": 2}

    monkeypatch.setattr(gating, "query_entities", _page)
    notifier = _Notifier()
    counts = asyncio.run(apis.recover_unlaunched_fly_tasks(notifier))
    assert effects == []
    assert len(notifier.sent) == 1, notifier.sent
    assert "could not fully read the executing-task inventory" in notifier.sent[0]
    assert counts["complete"] is False and counts["examined"] == 0


def test_well_formed_inventory_through_the_real_listing_is_complete(env, monkeypatch):
    _, _, effects = env
    monkeypatch.setattr(apis, "query_all_entities", _REAL_QUERY_ALL_ENTITIES)
    monkeypatch.setattr(
        gating, "query_entities",
        lambda *a, **k: {"entities": [{"entity_id": TASK, "snapshot": _task()}], "total": 1})
    notifier = _Notifier()
    counts = asyncio.run(apis.recover_unlaunched_fly_tasks(notifier))
    assert counts["complete"] is True and counts["examined"] == 1
    assert len(notifier.sent) == 1 and TASK in notifier.sent[0]
    assert effects == []


@pytest.mark.parametrize("outcome", ["return", "raise"])
def test_dispatch_guard_holds_the_id_while_dispatching_and_releases_it(env, monkeypatch,
                                                                        outcome):
    """Through the real ``dispatch_task``, with only ``_dispatch_task`` faked and
    paused: the id is guarded while it runs, the concurrent sweep is silent
    about it, and the guard is released however the dispatch ends."""
    _, _, effects = env
    monkeypatch.setattr(apis, "dispatch_task", _REAL_DISPATCH_TASK)

    async def scenario():
        entered = asyncio.Event()
        release = asyncio.Event()

        async def _paused(entity_id, *a, **k):
            entered.set()
            await release.wait()
            if outcome == "raise":
                raise RuntimeError("dispatch failed")
            return True

        monkeypatch.setattr(apis, "_dispatch_task", _paused)
        running = asyncio.ensure_future(
            apis.dispatch_task(TASK, _task(), "approved", _Notifier()))
        await entered.wait()
        assert apis._tasks_in_dispatch == {TASK: 1}
        notifier = _Notifier()
        counts = await apis.recover_unlaunched_fly_tasks(notifier)
        assert notifier.sent == [], "a task dispatching in this process must not be reported"
        assert counts["recorded"] == 1
        release.set()
        if outcome == "raise":
            with pytest.raises(RuntimeError):
                await running
        else:
            assert await running is True
        assert TASK not in apis._tasks_in_dispatch

    asyncio.run(scenario())
    assert effects == []


PR = 1691
OTHER_TASK = "ent_earlier_correction_task"


def _pr_keyed(monkeypatch):
    """This task is an existing-PR correction: its receipt key is the PR's."""
    key = build_delivery.fly_delivery_key(REPO, PR, TASK)
    monkeypatch.setattr(apis, "_fly_receipt_key", lambda snap, eid: key)
    return key


def test_settled_receipt_of_an_earlier_task_on_the_same_pr_is_reported(env, monkeypatch):
    """An earlier correction's settled receipt sits on the PR key; the newer
    correction is EXECUTING with no run (restart before its run superseded
    that receipt). The earlier task's receipt does not clear this one."""
    ledger, _, effects = env
    key = _pr_keyed(monkeypatch)
    assert fly_runner.FlyLedger(ledger).reserve_delivery(
        key, {"status": "updated", "branch": "b", "run_id": "run-old", "token": "x",
              "task_id": OTHER_TASK, "commit": "a" * 40})
    notifier = _Notifier()
    counts = asyncio.run(apis.recover_unlaunched_fly_tasks(notifier))
    assert effects == []
    assert len(notifier.sent) == 1, notifier.sent
    assert TASK in notifier.sent[0] and "holds no receipt" in notifier.sent[0]
    assert counts["unrecovered"] == 1 and counts["recorded"] == 0


def test_own_receipt_on_the_same_pr_is_silent(env, monkeypatch):
    ledger, _, effects = env
    key = _pr_keyed(monkeypatch)
    assert fly_runner.FlyLedger(ledger).reserve_delivery(
        key, {"status": "updated", "branch": "b", "run_id": "run-new", "token": "x",
              "task_id": TASK, "commit": "b" * 40})
    notifier = _Notifier()
    counts = asyncio.run(apis.recover_unlaunched_fly_tasks(notifier))
    assert effects == [] and notifier.sent == []
    assert counts["recorded"] == 1


def test_receipt_on_the_key_with_no_owner_is_unknown(env, monkeypatch):
    """A settled receipt on the PR key that names no task: whose it is cannot
    be read, so the receipt state is unknown and the task is reported."""
    ledger, _, effects = env
    key = _pr_keyed(monkeypatch)
    assert fly_runner.FlyLedger(ledger).reserve_delivery(
        key, {"status": "updated", "branch": "b", "run_id": "run-old", "token": "x"})
    assert apis._fly_delivery_recorded(_task(), TASK) is None
    notifier = _Notifier()
    asyncio.run(apis.recover_unlaunched_fly_tasks(notifier))
    assert effects == []
    assert len(notifier.sent) == 1 and "Fly receipt state is unknown" in notifier.sent[0]


def test_routing_check_that_raises_is_reported_as_unknown(env, monkeypatch):
    """Fault injection: the routing check raises for an executing task. That
    is unknown routing, never "not Fly": the task is named, with the error
    type and never the error's message."""
    _, _, effects = env

    def _boom(*a, **k):
        raise KeyError("value-that-must-not-appear")

    monkeypatch.setattr(apis, "_resolve_skill", _boom)
    notifier = _Notifier()
    counts = asyncio.run(apis.recover_unlaunched_fly_tasks(notifier))
    assert effects == []
    assert len(notifier.sent) == 1, notifier.sent
    notice = notifier.sent[0]
    assert TASK in notice
    assert "whether it routes to Fly could not be determined" in notice
    assert "KeyError" in notice and "value-that-must-not-appear" not in notice
    assert counts["unrecovered"] == 1 and counts["not_fly"] == 0


def test_unlaunched_fly_recovery_sweep_is_scheduled_at_startup():
    """Startup wiring: the sweep is defined in ``main``, calls the recovery,
    and is a sibling in the startup gather (the pattern of
    test_stale_inflight_markers.test_startup_gather_includes_marker_sweep_coroutine)."""
    src = inspect.getsource(apis.main)
    start = src.index("async def unlaunched_fly_recovery_sweep()")
    body = src[start:src.index("async def ", start + 1)]
    assert "await recover_unlaunched_fly_tasks(notifier)" in body
    gather_block = src[src.rfind("await asyncio.gather("):]
    assert "unlaunched_fly_recovery_sweep()," in gather_block
