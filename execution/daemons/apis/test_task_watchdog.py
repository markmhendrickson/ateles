"""Tests for the Apis stall watchdog — classification + sweep orchestration."""

from __future__ import annotations

import asyncio
import os
import sys

import pytest

# This daemon module imports siblings by bare name and `lib.*` absolutely; put
# both the daemon dir and the repo root on the path so the test runs from
# anywhere (mirrors how apis.py bootstraps at runtime).
_HERE = os.path.dirname(__file__)
_REPO_ROOT = os.path.abspath(os.path.join(_HERE, "..", "..", ".."))
for _p in (_HERE, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import task_watchdog as tw  # noqa: E402


# ── pure classification ──────────────────────────────────────────────────────


def test_failed_retries_then_escalates():
    wd = tw.TaskWatchdog(stall_seconds=3600)
    assert wd.classify("t", "failed", None) == tw.WatchdogAction.RETRY
    wd._state["t"] = tw._AttemptState(attempts=tw.MAX_ATTEMPTS)
    assert wd.classify("t", "failed", None) == tw.WatchdogAction.ESCALATE


def test_inflight_fresh_vs_stalled():
    wd = tw.TaskWatchdog(stall_seconds=3600)
    assert wd.classify("t", "executing", 60) == tw.WatchdogAction.NONE
    assert wd.classify("t", "executing", 4000) == tw.WatchdogAction.RETRY
    assert wd.classify("t", "routed", 5000) == tw.WatchdogAction.RETRY


def test_handsoff_states():
    wd = tw.TaskWatchdog(stall_seconds=3600)
    for s in ("pending", "done", "awaiting_approval", "blocked", "declined"):
        assert wd.classify("t", s, 99999) == tw.WatchdogAction.NONE


def test_verified_reconciles_instead_of_retrying():
    wd = tw.TaskWatchdog(stall_seconds=3600)
    assert wd.classify("t", "verified", 99999) == tw.WatchdogAction.RECONCILE


def test_backoff_between_retries():
    wd = tw.TaskWatchdog()
    assert wd.should_retry_now("x", now=1000.0)
    wd.record_retry("x", now=1000.0)  # attempts -> 1
    assert not wd.should_retry_now("x", now=1000.0 + 1)
    assert wd.should_retry_now("x", now=1000.0 + tw.backoff_seconds(1) + 1)


# ── sweep orchestration (mocked query + status writes + dispatch) ────────────


def test_sweep_retries_fresh_failure_and_escalates_exhausted(monkeypatch):
    wd = tw.TaskWatchdog(stall_seconds=3600)
    wd._state["ent_esc"] = tw._AttemptState(attempts=tw.MAX_ATTEMPTS)  # already exhausted

    monkeypatch.setattr(tw, "_query_tasks", lambda limit: [
        ("ent_retry", {"status": "failed", "title": "retry me"}),
        ("ent_esc", {"status": "failed", "title": "give up"}),
    ])

    status_calls: list[tuple] = []
    monkeypatch.setattr(tw, "set_task_status", lambda *a, **k: status_calls.append((a, k)))
    monkeypatch.setattr(tw, "_notify", lambda *a, **k: None)

    dispatched: list[tuple] = []

    async def dispatch_fn(task_id, snapshot, trigger):
        dispatched.append((task_id, trigger))

    notifier = object()
    counts = asyncio.run(wd.sweep(notifier, dispatch_fn))

    assert counts["retried"] == 1
    assert counts["escalated"] == 1
    assert ("ent_retry", "watchdog_retry") in dispatched
    assert ("ent_esc", "watchdog_retry") not in dispatched  # escalated, not retried
    # the retried task got bumped to attempt 1; the escalated one was forgotten
    assert wd.attempts_for("ent_retry") == 1
    assert wd.attempts_for("ent_esc") == 0


def test_sweep_skips_during_backoff(monkeypatch):
    wd = tw.TaskWatchdog(stall_seconds=3600)
    wd.record_retry("ent_x", now=10_000_000_000.0)  # very recent retry, attempts=1

    monkeypatch.setattr(tw, "_query_tasks", lambda limit: [("ent_x", {"status": "failed"})])
    monkeypatch.setattr(tw, "set_task_status", lambda *a, **k: None)
    monkeypatch.setattr(tw, "_notify", lambda *a, **k: None)

    dispatched: list = []

    async def dispatch_fn(task_id, snapshot, trigger):
        dispatched.append(task_id)

    counts = asyncio.run(wd.sweep(object(), dispatch_fn))
    assert counts["skipped_backoff"] == 1
    assert dispatched == []


def test_sweep_leaves_unresolved_verified_effect_without_dispatch(monkeypatch):
    wd = tw.TaskWatchdog(stall_seconds=3600)
    snapshot = {
        "status": "verified",
        "result": (
            "cicada completed (trigger=approved); "
            "run_session=ent_task:approved-2; provenance=unverified"
        ),
    }
    monkeypatch.setattr(tw, "_query_tasks", lambda _limit: [("ent_task", snapshot)])
    monkeypatch.setattr(tw, "_reconcile_completed_task", lambda *_args: False)

    dispatched: list[str] = []

    async def dispatch_fn(task_id, _snapshot, _trigger):
        dispatched.append(task_id)

    counts = asyncio.run(wd.sweep(object(), dispatch_fn))

    assert counts["reconciled"] == 0
    assert counts["retried"] == 0
    assert dispatched == []


def test_sweep_fail_open_on_query_error(monkeypatch):
    wd = tw.TaskWatchdog()

    def boom(limit):
        raise RuntimeError("query down")

    monkeypatch.setattr(tw, "_query_tasks", boom)

    async def dispatch_fn(*a):
        raise AssertionError("should not dispatch")

    counts = asyncio.run(wd.sweep(object(), dispatch_fn))
    assert counts["scanned"] == 0  # swallowed, returned empty counts


# ── timestamp / age parsing ──────────────────────────────────────────────────


def test_age_parsing():
    now = 1_000_000.0
    iso = tw._iso(now - 120)
    age = tw._age_seconds({"updated_at": iso}, now)
    assert age is not None and abs(age - 120) < 2
    assert tw._age_seconds({}, now) is None


def test_query_tasks_unwraps_shapes():
    assert tw._unwrap_snapshot({"snapshot": {"snapshot": {"status": "x"}}}) == {"status": "x"}
    assert tw._unwrap_snapshot({"snapshot": {"status": "y"}}) == {"status": "y"}
    assert tw._unwrap_snapshot({"status": "z"}) == {"status": "z"}


def test_stall_outlasts_a_fly_queue_wait_plus_the_run():
    """ateles#1456: with the Fly executor enabled, a login-machine queue wait
    is not counted against the dispatch timeout, so the stall threshold must
    cover it, or a run still in flight would be re-dispatched."""
    from task_watchdog import stall_seconds

    base = {"APIS_DISPATCH_TIMEOUT": "1800", "APIS_STALL_SECONDS": "3600"}
    assert stall_seconds(base) == 3600
    fly = {**base, "APIS_FLY_RUNNER_CLASSES": "carry_forward_check",
           "APIS_FLY_QUEUE_MAX_WAIT_SECONDS": "1800"}  # fmt: skip
    assert stall_seconds(fly) == 1800 + 600 + 1800 + 120
    assert stall_seconds(fly) > 1800 + 1800 + 120  # queue + run + Apis grace


# ── Fly run in flight: the watchdog writes nothing ───────────────────────────
# RETRY used to write ROUTED before dispatch_task's own in-flight check ran, and
# ESCALATE wrote BLOCKED without calling dispatch_task at all, so either could
# change the status of a task whose Fly run was live. The guard runs first.


def _guarded_sweep(monkeypatch, status, attempts, guard):
    wd = tw.TaskWatchdog(stall_seconds=3600, fly_in_flight_guard=guard)
    if attempts:
        wd._state["ent_fly"] = tw._AttemptState(attempts=attempts)
    snapshot = {"status": status, "title": "t", "updated_at": 0}
    monkeypatch.setattr(tw, "_query_tasks", lambda _limit: [("ent_fly", snapshot)])
    writes: list = []
    notes: list = []
    dispatched: list = []
    monkeypatch.setattr(tw, "set_task_status", lambda *a, **k: writes.append((a, k)))
    monkeypatch.setattr(tw, "_notify", lambda *a, **k: notes.append(a))

    async def dispatch_fn(task_id, snap, trigger):
        dispatched.append((task_id, trigger))

    counts = asyncio.run(wd.sweep(object(), dispatch_fn))
    return wd, counts, writes, notes, dispatched


def _raises(_entity_id, _snapshot):
    raise RuntimeError("ledger probe failed")


@pytest.mark.parametrize("guard", [lambda _e, _s: True, _raises], ids=["true", "raises"])
def test_stale_executing_retry_with_run_in_flight_is_untouched(monkeypatch, guard):
    wd, counts, writes, notes, dispatched = _guarded_sweep(monkeypatch, "executing", 0, guard)
    assert writes == [] and dispatched == [] and notes == []
    assert wd.attempts_for("ent_fly") == 0
    assert counts["skipped_fly_in_flight"] == 1 and counts["retried"] == 0


@pytest.mark.parametrize("guard", [lambda _e, _s: True, _raises], ids=["true", "raises"])
def test_escalation_with_run_in_flight_is_untouched(monkeypatch, guard):
    wd, counts, writes, notes, dispatched = _guarded_sweep(
        monkeypatch, "failed", tw.MAX_ATTEMPTS, guard)
    assert writes == [] and notes == [] and dispatched == []
    assert wd.attempts_for("ent_fly") == tw.MAX_ATTEMPTS  # not forgotten
    assert counts["skipped_fly_in_flight"] == 1 and counts["escalated"] == 0


def test_guard_false_retries_and_escalates_as_today(monkeypatch):
    wd, counts, writes, _, dispatched = _guarded_sweep(
        monkeypatch, "executing", 0, lambda _e, _s: False)
    assert [k["key_suffix"] for _a, k in writes] == ["watchdog-retry-1"]
    assert dispatched == [("ent_fly", "watchdog_retry")]
    assert counts["retried"] == 1 and counts["skipped_fly_in_flight"] == 0

    wd, counts, writes, notes, dispatched = _guarded_sweep(
        monkeypatch, "failed", tw.MAX_ATTEMPTS, lambda _e, _s: False)
    assert writes[0][0][1] == tw.TaskStatus.BLOCKED and len(notes) == 1
    assert dispatched == [] and counts["escalated"] == 1


def test_apis_main_gives_the_watchdog_the_fly_guard(monkeypatch, tmp_path):
    # Direct check of the construction site in apis.main, plus the guard's
    # behaviour: it is apis._fly_run_refuses_dispatch with trigger "watchdog".
    import inspect

    import apis

    assert "TaskWatchdog(fly_in_flight_guard=_watchdog_fly_guard)" in inspect.getsource(apis.main)
    seen = []
    monkeypatch.setattr(apis, "_fly_run_refuses_dispatch",
                        lambda snap, eid, trigger: seen.append((snap, eid, trigger)) or True)
    assert apis._watchdog_fly_guard("ent_x", {"a": 1}) is True
    assert seen == [({"a": 1}, "ent_x", "watchdog")]
    # An unreadable ledger refuses through the real function too.
    monkeypatch.undo()
    ledger = tmp_path / "fly-ledger.json"
    ledger.write_text("{not json")
    monkeypatch.setenv("APIS_FLY_LEDGER_FILE", str(ledger))
    assert apis._watchdog_fly_guard("ent_x", {"repo": "markmhendrickson/ateles"}) is True


def test_stale_task_with_no_repository_and_live_receipt_is_untouched(monkeypatch, tmp_path):
    # Through the real guard (apis._watchdog_fly_guard): a task whose repository
    # field is gone derives no delivery key, but a live receipt naming its id
    # still counts as its run, so neither RETRY nor ESCALATE writes anything.
    import apis
    import build_delivery
    import fly_runner

    ledger = tmp_path / "fly-ledger.json"
    monkeypatch.setenv("APIS_FLY_LEDGER_FILE", str(ledger))
    key = build_delivery.fly_delivery_key("markmhendrickson/ateles", None, "ent_fly")
    assert fly_runner.FlyLedger(ledger).reserve_delivery(
        key, {"status": "launching", "branch": "b", "run_id": "", "token": "x"})
    for status, attempts in (("executing", 0), ("failed", tw.MAX_ATTEMPTS)):
        wd, counts, writes, notes, dispatched = _guarded_sweep(
            monkeypatch, status, attempts, apis._watchdog_fly_guard)
        assert writes == [] and notes == [] and dispatched == [], status
        assert counts["skipped_fly_in_flight"] == 1, status


# ── Reconciliation obeys the in-flight refusal too ───────────────────────────
# RECONCILE was handled before the in-flight check, so _reconcile_completed_task
# could write DONE on a verified task whose Fly run was still live.


def _verified_sweep(monkeypatch, tmp_path, *, live_receipt, ledger_text=None):
    import apis
    import build_delivery
    import fly_runner

    ledger = tmp_path / "fly-ledger.json"
    monkeypatch.setenv("APIS_FLY_LEDGER_FILE", str(ledger))
    if ledger_text is not None:
        ledger.write_text(ledger_text)
    elif live_receipt:
        key = build_delivery.fly_delivery_key("markmhendrickson/ateles", None, "ent_fly")
        assert fly_runner.FlyLedger(ledger).reserve_delivery(
            key, {"status": "launching", "branch": "b", "run_id": "", "token": "x"})
    wd = tw.TaskWatchdog(stall_seconds=3600, fly_in_flight_guard=apis._watchdog_fly_guard)
    snapshot = {
        "status": "verified", "title": "t", "updated_at": 0,
        "repo": "markmhendrickson/ateles",
        "result": "cicada completed; run_session=ent_fly:approved-1; provenance=unverified",
    }
    monkeypatch.setattr(tw, "_query_tasks", lambda _limit: [("ent_fly", snapshot)])
    writes: list = []
    run_writes: list = []
    monkeypatch.setattr(tw, "set_task_status", lambda *a, **k: writes.append((a, k)) or True)
    monkeypatch.setattr(tw, "recover_run_session", lambda **k: {"run": k})
    monkeypatch.setattr(tw, "update_run_session_status",
                        lambda run, **k: run_writes.append((run, k)) or True)

    async def dispatch_fn(task_id, snap, trigger):
        raise AssertionError("verified tasks are never dispatched")

    counts = asyncio.run(wd.sweep(object(), dispatch_fn))
    return counts, writes, run_writes


def test_verified_task_with_live_receipt_is_untouched(monkeypatch, tmp_path):
    # Seen red before the fix (RECONCILE ran ahead of the in-flight check):
    #   AssertionError: assert ([(('ent_fly',...ified', ...})] == []
    #   Left contains one more item: (('ent_fly', <TaskStatus.DONE: 'done'>),
    #   {'handler': 'apis-watchdog', 'from_status': 'verified', ...
    #   'result': '...; provenance=verified', ...})
    counts, writes, run_writes = _verified_sweep(monkeypatch, tmp_path, live_receipt=True)
    assert writes == [] and run_writes == []
    assert counts["skipped_fly_in_flight"] == 1 and counts["reconciled"] == 0


def test_verified_task_without_live_receipt_still_reconciles(monkeypatch, tmp_path):
    counts, writes, run_writes = _verified_sweep(monkeypatch, tmp_path, live_receipt=False)
    assert [a[1] for a, _k in writes] == [tw.TaskStatus.DONE]
    assert len(run_writes) == 1
    assert counts["reconciled"] == 1 and counts["skipped_fly_in_flight"] == 0


def test_verified_task_with_unreadable_ledger_is_untouched(monkeypatch, tmp_path):
    counts, writes, run_writes = _verified_sweep(
        monkeypatch, tmp_path, live_receipt=False, ledger_text="{not json")
    assert writes == [] and run_writes == []
    assert counts["skipped_fly_in_flight"] == 1 and counts["reconciled"] == 0
