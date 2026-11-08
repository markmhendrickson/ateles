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

* ``test_routing_check_that_raises_is_reported_as_unknown``, checked by
  mutation: with ``_fly_routed_delivery`` returning ``(False, "")`` on an
  exception, no notice was sent and the task counted as ``not_fly``.
* ``test_row_with_missing_or_malformed_routing_field_is_reported`` failed (no
  notice, counted ``not_fly``) with a missing or malformed repository read as
  not-Fly; ``test_routing_field_with_surrounding_whitespace_is_reported``
  failed (no notice, counted ``not_fly``) for all four fields with padded
  values stripped or not checked. The padded and whitespace-only cases of
  ``test_inventory_repeating_one_id_in_place_of_another_is_incomplete`` (and
  of ``test_gating``'s strict-listing test) failed with the
  ``row_id != row_id.strip()`` rule removed.
* No receipt is taken as proof a run happened (the rebuild of #1692 cut that
  case). ``test_settled_own_receipt_without_a_live_run_is_reported`` and
  ``test_receipt_for_this_task_on_its_pr_is_reported`` were silent (no notice,
  counted ``recorded``) on #1692's last head, which treated this task's settled
  receipt with a launched run id as proof; they pass only with that case cut.
* ``test_in_process_dispatch_of_a_padded_id_does_not_cover_this_task`` pins
  that the in-process proof matches the exact id only.
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
# Run ids of the shape ``fly_runner.build_job`` makes.
DONE_RUN = "fly-fedcba9876543210"
NEW_RUN = "fly-00112233445566ff"


class _Notifier:
    def __init__(self):
        self.sent: list[str] = []

    def send(self, message, *args, **kwargs):
        # No notice may claim a run never launched: the sweep does not know,
        # so every notice says launch or finish is not confirmed.
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
    assert "whether its Fly run launched or finished is not confirmed" in notice
    assert "no Fly run of it is live in this process" in notice
    assert "check for an earlier Fly run of this task and its pull request" in notice
    assert "file a new task with a different title for the same work" in notice
    assert "release its checkpoint" in notice and "fresh checkpoint" not in notice
    assert counts["complete"] is True and counts["unrecovered"] == 1


def _assert_reported(effects, notifier, counts,
                     phrase="no Fly run of it is live in this process"):
    assert effects == []
    assert len(notifier.sent) == 1, notifier.sent
    assert TASK in notifier.sent[0] and phrase in notifier.sent[0]
    assert counts["unrecovered"] == 1 and counts["recorded"] == 0


def _reserve(ledger, status: str, **extra) -> None:
    key = build_delivery.fly_delivery_key(REPO, None, TASK)
    assert fly_runner.FlyLedger(ledger).reserve_delivery(
        key, {"status": status, "branch": "b", "run_id": "", "token": "x", **extra})


@pytest.mark.parametrize("status", ["opened", "updated", "no_changes", "push_failed"])
def test_settled_own_receipt_without_a_live_run_is_reported(env, status):
    """This task's own receipt, a settled outcome and a launched run id, with
    no run of it live in this process: still reported. A receipt is never
    taken as proof the run happened, so the notice says launch or finish is
    not confirmed and asks for a check before refiling."""
    ledger, _, effects = env
    _reserve(ledger, status, task_id=TASK, run_id=DONE_RUN)
    notifier = _Notifier()
    counts = asyncio.run(apis.recover_unlaunched_fly_tasks(notifier))
    _assert_reported(effects, notifier, counts)
    assert "launched or finished is not confirmed" in notifier.sent[0]


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
    assert len(notifier.sent) == 1 and "launched or finished is not confirmed" in notifier.sent[0]
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


def _pr_keyed(monkeypatch):
    """This task is an existing-PR correction: its receipt key is the PR's."""
    key = build_delivery.fly_delivery_key(REPO, PR, TASK)
    monkeypatch.setattr(apis, "_fly_receipt_key", lambda snap, eid: key)
    return key


def test_receipt_for_this_task_on_its_pr_is_reported(env, monkeypatch):
    """An existing-PR correction whose own settled receipt sits on the PR key:
    reported, since no receipt proves the run happened."""
    ledger, _, effects = env
    key = _pr_keyed(monkeypatch)
    assert fly_runner.FlyLedger(ledger).reserve_delivery(
        key, {"status": "updated", "branch": "b", "run_id": NEW_RUN, "token": "x",
              "task_id": TASK, "commit": "b" * 40})
    notifier = _Notifier()
    counts = asyncio.run(apis.recover_unlaunched_fly_tasks(notifier))
    _assert_reported(effects, notifier, counts)


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


def test_in_process_dispatch_of_a_padded_id_does_not_cover_this_task(env, monkeypatch):
    """Case (b) in process: only a dispatch of this exact id is proof."""
    _, _, effects = env
    monkeypatch.setattr(apis, "_tasks_in_dispatch", {f" {TASK} ": 1})
    monkeypatch.setattr(apis, "_detached_runs_in_progress", {f"{TASK} "})
    notifier = _Notifier()
    counts = asyncio.run(apis.recover_unlaunched_fly_tasks(notifier))
    _assert_reported(effects, notifier, counts)


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
