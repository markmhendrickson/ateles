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
  inventory as complete and sent the per-task stranded notice twice, once per
  repeated row, instead of the single inventory notice (``assert 2 == 1``).
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
  mutation: with ``_fly_routing`` (now ``_fly_routed_delivery``) returning ``(False, "")`` on an exception
  (round 1's ``_fly_routed_delivery`` behaviour), no notice was sent and the
  task counted as ``not_fly``.
* Round 2 (silent only on proof; one id identity), each checked by mutating
  ``apis.py``/``gating.py`` and restoring it:
  ``test_reservation_without_launch_evidence_is_reported`` (all three
  statuses) and ``test_settled_own_receipt_without_a_run_id_is_reported``
  failed with any own receipt accepted as proof (the previous head's
  behaviour: ``launching``/``unknown`` were silent);
  ``test_row_with_missing_or_malformed_routing_field_is_reported`` failed
  (no notice, counted ``not_fly``) with a missing or malformed repository
  read as not-Fly, as the previous head did;
  ``test_unreadable_receipt_is_reported`` failed with uninterpretable
  entries skipped; ``test_receipt_with_a_padded_task_id_is_not_this_tasks``
  failed with the receipt's ``task_id`` stripped before comparing;
  ``test_run_in_flight_is_silent`` failed with the live-run proof removed and
  the dispatch-guard test with the in-process proof removed; the padded and
  whitespace-only cases of
  ``test_inventory_repeating_one_id_in_place_of_another_is_incomplete`` (and
  of ``test_gating``'s strict-listing test) failed with the
  ``row_id != row_id.strip()`` rule removed: the padded copy was judged as a
  second task, the whitespace-only row as an unreadable task row.
* Round 3 (every field a silent proof relies on is exact), each run against
  the previous head's ``apis.py`` (9f3017a) and restored:
  ``test_settled_receipt_with_a_run_id_of_the_wrong_shape_is_reported``
  failed (no notice) for ``spaces``, ``surrounding_whitespace``,
  ``wrong_prefix``, ``fifteen_hex`` and ``uppercase_hex``, since any non-empty
  string run id was proof; ``test_live_run_recorded_under_a_different_run_id_is_reported``
  failed (no notice) with in-flight membership by key alone;
  ``test_routing_field_with_surrounding_whitespace_is_reported`` failed (no
  notice, counted ``not_fly``) for all four fields, since padded values were
  stripped or not checked; the ``not_a_string`` status case failed because the
  unhashable status raised inside the proof and the task was reported only by
  the per-task crash path, without the receipt reason. The ``not_a_string`` run
  id, padded receipt ``task_id``, ``unknown``/``padded`` status and padded
  in-process id cases passed on the previous head too (those checks were
  already exact) and pin that they stay so.
  ``test_generated_run_id_with_every_field_exact_is_silent`` pins that the fix
  does not over-report the one valid shape.
* ``test_unlaunched_fly_recovery_sweep_is_scheduled_at_startup``, checked by
  removing ``unlaunched_fly_recovery_sweep(),`` from the startup gather: the
  gather-block assertion failed.

Run: python3 -m pytest -q execution/daemons/apis/test_fly_restart_before_launch.py
"""

from __future__ import annotations

import asyncio
import inspect
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import apis  # noqa: E402
import build_delivery  # noqa: E402
import fly_runner  # noqa: E402
from lib.daemon_runtime import gating  # noqa: E402

REPO = "owner/repo"
TASK = "ent_task_restart_before_launch"
# Run ids of the exact shape ``fly_runner.build_job`` makes.
LIVE_RUN = "fly-0123456789abcdef"
DONE_RUN = "fly-fedcba9876543210"
NEW_RUN = "fly-00112233445566ff"
OLD_RUN = "fly-aabbccddeeff0011"


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
    assert fly.reserve_delivery(key, {"status": "launched", "branch": "b", "run_id": LIVE_RUN,
                                      "token": "x", "task_id": TASK})
    fly.record_untracked(machine_id="m-1", run_id=LIVE_RUN, owner="apis",
                         config=fly_runner.FlyConfig.from_env(),
                         started_at=fly._clock(), max_run_seconds=600)
    notifier = _Notifier()
    counts = asyncio.run(apis.recover_unlaunched_fly_tasks(notifier))
    assert effects == [] and notifier.sent == []
    assert counts["recorded"] == 1


@pytest.mark.parametrize("status", ["opened", "updated", "no_changes", "push_failed"])
def test_settled_own_receipt_with_a_launched_run_is_silent(env, status):
    """Allow-list (c): this task's receipt, a settled outcome, a run id."""
    ledger, _, effects = env
    _reserve(ledger, status, task_id=TASK, run_id=DONE_RUN)
    notifier = _Notifier()
    counts = asyncio.run(apis.recover_unlaunched_fly_tasks(notifier))
    assert effects == [] and notifier.sent == []
    assert counts["recorded"] == 1


@pytest.mark.parametrize("status", ["launching", "unknown", "unreported"])
def test_reservation_without_launch_evidence_is_reported(env, status):
    """A ``launching`` (or other unsettled) receipt with no run id is a
    reservation, not proof a run launched: the restart can cut off exactly
    between the reservation and the launch."""
    ledger, _, effects = env
    _reserve(ledger, status, task_id=TASK)
    notifier = _Notifier()
    counts = asyncio.run(apis.recover_unlaunched_fly_tasks(notifier))
    assert effects == []
    assert len(notifier.sent) == 1, notifier.sent
    assert TASK in notifier.sent[0]
    assert f"its Fly receipt ({status}) records no run that reached launch" in notifier.sent[0]
    assert counts["unrecovered"] == 1 and counts["recorded"] == 0


def test_settled_own_receipt_without_a_run_id_is_reported(env):
    ledger, _, effects = env
    _reserve(ledger, "opened", task_id=TASK)
    notifier = _Notifier()
    asyncio.run(apis.recover_unlaunched_fly_tasks(notifier))
    assert effects == []
    assert len(notifier.sent) == 1 and "records no run that reached launch" in notifier.sent[0]


def test_unreadable_ledger_is_reported(env, monkeypatch):
    _, _, effects = env

    def _broken():
        raise fly_runner.FlyLedgerError("ledger unreadable")

    monkeypatch.setattr(build_delivery, "_delivery_ledger", _broken)
    notifier = _Notifier()
    counts = asyncio.run(apis.recover_unlaunched_fly_tasks(notifier))
    assert effects == []
    assert len(notifier.sent) == 1 and TASK in notifier.sent[0]
    assert "the Fly ledger could not be read" in notifier.sent[0]
    assert counts["unrecovered"] == 1 and counts["recorded"] == 0


@pytest.mark.parametrize("bad", ["not-an-object", None, ["a", "list"]])
def test_unreadable_receipt_is_reported(env, monkeypatch, bad):
    """One receipt the reader cannot interpret could be this task's, even
    beside this task's own settled receipt: the task is reported."""
    _, _, effects = env

    class _Ledger:
        def snapshot(self):
            return {"in_flight": {}, "deliveries": {
                "some-key": bad,
                f"{REPO}#task:{TASK}": {"status": "opened", "run_id": DONE_RUN,
                                         "task_id": TASK}}}

    monkeypatch.setattr(build_delivery, "_delivery_ledger", lambda: _Ledger())
    notifier = _Notifier()
    counts = asyncio.run(apis.recover_unlaunched_fly_tasks(notifier))
    assert effects == []
    assert len(notifier.sent) == 1, notifier.sent
    assert "a Fly ledger receipt could not be read" in notifier.sent[0]
    assert counts["unrecovered"] == 1 and counts["recorded"] == 0


@pytest.mark.parametrize("case", ["other_agent", "other_action", "fly_not_opted_in"])
def test_task_positively_routed_away_from_fly_is_silent(env, monkeypatch, case):
    """Allow-list (a): the row's routing fields, all present and well formed,
    name a target ``dispatch_task`` would not send to Fly."""
    _, state, effects = env
    if case == "other_agent":
        state["task"] = _task(assigned_to="pavo", action_type="analysis")
    elif case == "other_action":
        state["task"] = _task(action_type="analysis")
    else:
        monkeypatch.delenv("APIS_FLY_RUNNER_CLASSES")
    notifier = _Notifier()
    counts = asyncio.run(apis.recover_unlaunched_fly_tasks(notifier))
    assert effects == [] and notifier.sent == []
    assert counts["unrecovered"] == 0


@pytest.mark.parametrize("drop,over,phrase", [
    ("repo", {}, "no repository field"),
    (None, {"repo": ""}, "its repo field is empty or malformed"),
    (None, {"repo": 7}, "its repo field is empty or malformed"),
    (None, {"repo": "no-slash"}, "its repo field is empty or malformed"),
    ("assigned_to", {}, "assigned_to field is missing, empty or malformed"),
    (None, {"assigned_to": "  "}, "assigned_to field is missing, empty or malformed"),
], ids=["repo_missing", "repo_empty", "repo_not_a_string", "repo_malformed",
        "assignee_missing", "assignee_blank"])
def test_row_with_missing_or_malformed_routing_field_is_reported(env, drop, over, phrase):
    """A routing field that is absent, empty or malformed proves nothing about
    where the task routes, so the task is reported, never read as not-Fly."""
    _, state, effects = env
    task = _task(**over)
    if drop:
        task.pop(drop)
    state["task"] = task
    notifier = _Notifier()
    counts = asyncio.run(apis.recover_unlaunched_fly_tasks(notifier))
    assert effects == []
    assert len(notifier.sent) == 1, notifier.sent
    assert TASK in notifier.sent[0] and phrase in notifier.sent[0]
    assert counts["unrecovered"] == 1 and counts["not_fly"] == 0


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
    assert ("query Neotoma for entities of type task with status executing assigned "
            "to the builder (cicada)") in notifier.sent[0]
    assert counts["complete"] is False and counts["examined"] == 0


@pytest.mark.parametrize("second_id", [TASK, f" {TASK} ", "   "],
                         ids=["repeated", "padded_copy", "whitespace_only"])
@pytest.mark.parametrize("page_size", [10, 1], ids=["same_page", "next_page"])
def test_inventory_repeating_one_id_in_place_of_another_is_incomplete(
        env, monkeypatch, page_size, second_id):
    """Two executing tasks exist and the listing reports ``total: 2``, but the
    second row repeats the first id (exactly, or padded so a strip would merge
    the two), or is whitespace only. The count matches, so only the one-identity
    rule catches it: the sweep must send the single inventory notice and judge
    no task, not name one as though the inventory were whole."""
    _, _, effects = env
    monkeypatch.setattr(apis, "query_all_entities", _REAL_QUERY_ALL_ENTITIES)
    monkeypatch.setattr(gating, "QUERY_PAGE_SIZE", page_size)
    row = {"entity_id": TASK, "snapshot": _task()}

    def _page(entity_type, *, snapshot_filters, offset=0, limit=None):
        rows = [row, dict(row, entity_id=second_id)][offset:offset + (limit or page_size)]
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
        key, {"status": "updated", "branch": "b", "run_id": OLD_RUN, "token": "x",
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
        key, {"status": "updated", "branch": "b", "run_id": NEW_RUN, "token": "x",
              "task_id": TASK, "commit": "b" * 40})
    notifier = _Notifier()
    counts = asyncio.run(apis.recover_unlaunched_fly_tasks(notifier))
    assert effects == [] and notifier.sent == []
    assert counts["recorded"] == 1


def test_receipt_on_the_key_with_no_owner_is_reported(env, monkeypatch):
    """A settled receipt on the PR key that names no task carries no task id,
    so it is no proof for this task: the task is reported."""
    ledger, _, effects = env
    key = _pr_keyed(monkeypatch)
    assert fly_runner.FlyLedger(ledger).reserve_delivery(
        key, {"status": "updated", "branch": "b", "run_id": OLD_RUN, "token": "x"})
    notifier = _Notifier()
    counts = asyncio.run(apis.recover_unlaunched_fly_tasks(notifier))
    assert effects == []
    assert len(notifier.sent) == 1 and "holds no receipt for it" in notifier.sent[0]
    assert counts["recorded"] == 0


def test_receipt_with_a_padded_task_id_is_not_this_tasks(env):
    """Receipt ownership compares the id exactly, the listing's identity:
    " <id> " is not this task's id, so it proves nothing for it."""
    ledger, _, effects = env
    _reserve(ledger, "opened", task_id=f" {TASK} ", run_id=DONE_RUN)
    notifier = _Notifier()
    asyncio.run(apis.recover_unlaunched_fly_tasks(notifier))
    assert effects == [] and len(notifier.sent) == 1


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


# ── Round 3: every silent-case proof validates each field it relies on ────────


def test_generated_run_id_with_every_field_exact_is_silent(env):
    """A run id made exactly as ``build_job`` makes it, on this task's exact id,
    with a settled status and exact routing fields: silent."""
    ledger, _, effects = env
    generated = f"fly-{uuid.uuid4().hex[:16]}"
    assert fly_runner.is_fly_run_id(generated)
    _reserve(ledger, "opened", task_id=TASK, run_id=generated)
    notifier = _Notifier()
    counts = asyncio.run(apis.recover_unlaunched_fly_tasks(notifier))
    assert effects == [] and notifier.sent == []
    assert counts["recorded"] == 1


def _one_receipt(monkeypatch, *, in_flight=None, **entry):
    """A ledger holding one receipt on this task's key, read back as written so
    a value the real ledger's validator would refuse still reaches the sweep."""
    receipt = {"status": "opened", "run_id": DONE_RUN, "task_id": TASK, **entry}

    class _Ledger:
        def snapshot(self):
            return {"in_flight": dict(in_flight or {}),
                    "deliveries": {f"{REPO}#task:{TASK}": receipt}}

    monkeypatch.setattr(build_delivery, "_delivery_ledger", lambda: _Ledger())


def _assert_reported(effects, notifier, counts, phrase="records no run that reached launch"):
    assert effects == []
    assert len(notifier.sent) == 1, notifier.sent
    assert TASK in notifier.sent[0] and phrase in notifier.sent[0]
    assert counts["unrecovered"] == 1 and counts["recorded"] == 0


@pytest.mark.parametrize("run_id", [
    "                    ",
    f" {DONE_RUN} ",
    "run-fedcba9876543210",
    "fly-fedcba987654321",
    "fly-FEDCBA9876543210",
    12345678,
], ids=["spaces", "surrounding_whitespace", "wrong_prefix", "fifteen_hex",
        "uppercase_hex", "not_a_string"])
def test_settled_receipt_with_a_run_id_of_the_wrong_shape_is_reported(env, monkeypatch, run_id):
    """Only a run id of exactly ``build_job``'s shape is launch evidence."""
    _, _, effects = env
    _one_receipt(monkeypatch, run_id=run_id)
    notifier = _Notifier()
    counts = asyncio.run(apis.recover_unlaunched_fly_tasks(notifier))
    _assert_reported(effects, notifier, counts)


def test_receipt_task_id_with_surrounding_whitespace_is_reported(env, monkeypatch):
    """A valid run id and a settled status do not rescue a receipt whose task id
    is this task's only after stripping."""
    _, _, effects = env
    _one_receipt(monkeypatch, task_id=f"  {TASK}\t")
    notifier = _Notifier()
    counts = asyncio.run(apis.recover_unlaunched_fly_tasks(notifier))
    _assert_reported(effects, notifier, counts, phrase="holds no receipt for it")


@pytest.mark.parametrize("status", ["delivered", " opened", ["opened"]],
                         ids=["unknown", "padded", "not_a_string"])
def test_receipt_with_an_unknown_status_is_reported(env, monkeypatch, status):
    """Only a settled status the allow-list names is proof; an unknown one,
    even with a valid run id, is not (and a non-string one does not raise)."""
    _, _, effects = env
    _one_receipt(monkeypatch, status=status)
    notifier = _Notifier()
    counts = asyncio.run(apis.recover_unlaunched_fly_tasks(notifier))
    _assert_reported(effects, notifier, counts)


def test_live_run_recorded_under_a_different_run_id_is_reported(env, monkeypatch):
    """Case (b): the in-flight entry must be the receipt's exact run."""
    _, _, effects = env
    _one_receipt(monkeypatch, status="launching", run_id=LIVE_RUN,
                 in_flight={LIVE_RUN: {"run_id": DONE_RUN}})
    notifier = _Notifier()
    counts = asyncio.run(apis.recover_unlaunched_fly_tasks(notifier))
    _assert_reported(effects, notifier, counts)


def test_in_process_dispatch_of_a_padded_id_does_not_cover_this_task(env, monkeypatch):
    """Case (b) in process: only a dispatch of this exact id is proof."""
    _, _, effects = env
    monkeypatch.setattr(apis, "_tasks_in_dispatch", {f" {TASK} ": 1})
    monkeypatch.setattr(apis, "_detached_runs_in_progress", {f"{TASK} "})
    notifier = _Notifier()
    counts = asyncio.run(apis.recover_unlaunched_fly_tasks(notifier))
    _assert_reported(effects, notifier, counts, phrase="holds no receipt")


@pytest.mark.parametrize("field,value", [
    ("assigned_to", " pavo "),
    ("repo", f" {REPO}"),
    ("owner", "agent "),
    ("action_type", "analysis\n"),
])
def test_routing_field_with_surrounding_whitespace_is_reported(env, field, value):
    """Case (a): every routing field the not-Fly proof reads must be a string
    equal to its own strip. A padded one, on a task that would otherwise route
    away from Fly, proves nothing, so the task is reported."""
    _, state, effects = env
    state["task"] = _task(**{"assigned_to": "pavo", "action_type": "analysis", field: value})
    notifier = _Notifier()
    counts = asyncio.run(apis.recover_unlaunched_fly_tasks(notifier))
    assert effects == []
    assert len(notifier.sent) == 1, notifier.sent
    assert TASK in notifier.sent[0] and f"its {field} field" in notifier.sent[0]
    assert counts["unrecovered"] == 1 and counts["not_fly"] == 0
