"""Tests for the Apis stall watchdog — classification + sweep orchestration."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time

# This daemon module imports siblings by bare name and `lib.*` absolutely; put
# both the daemon dir and the repo root on the path so the test runs from
# anywhere (mirrors how apis.py bootstraps at runtime).
_HERE = os.path.dirname(__file__)
_REPO_ROOT = os.path.abspath(os.path.join(_HERE, "..", "..", ".."))
for _p in (_HERE, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import task_watchdog as tw  # noqa: E402


def _recent(**fields):
    """A snapshot last written a minute ago (inside the retry window)."""
    return {"last_observation_at": tw._iso(time.time() - 60), **fields}


def _rows(*rows):
    """A `_query_tasks` fake returning exactly these rows."""
    return lambda *_a, **_k: tw.TaskQuery(rows=list(rows))


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

    monkeypatch.setattr(tw, "_query_tasks", _rows(
        ("ent_retry", _recent(status="failed", title="retry me")),
        ("ent_esc", _recent(status="failed", title="give up")),
    ))

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

    monkeypatch.setattr(tw, "_query_tasks", _rows(("ent_x", _recent(status="failed"))))
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
    monkeypatch.setattr(tw, "_query_tasks", _rows(("ent_task", snapshot)))
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

    def boom(*_a, **_k):
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
    # Row-level stamps are carried down without shadowing the snapshot's own.
    assert tw._unwrap_snapshot(
        {"snapshot": {"status": "f"}, "last_observation_at": "2026-01-01T00:00:00Z"}
    ) == {"status": "f", "last_observation_at": "2026-01-01T00:00:00Z"}
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


# ── query by state, paging, and the recency bound ────────────────────────────


class _FakeStore:
    """A task store behind `httpx.post` that honours the status filter,
    sorting and offset paging the way Neotoma does: rows in entity-id order
    by default, or by `sort_by` in `sort_order` (ties by entity id; an
    undated row sorts first descending and last ascending, as NULLs do in
    Postgres); `limit` and `offset` applied after sorting; a cursor beside
    `snapshot_filters` refused, an offset past 2000 refused."""

    def __init__(self, tasks: dict[str, dict]):
        self.tasks = tasks
        self.requests: list[dict] = []

    def post(self, url, *, headers=None, json=None, timeout=None):
        import httpx

        self.requests.append(json)
        req = httpx.Request("POST", url)
        if json.get("snapshot_filters") and json.get("cursor"):
            return httpx.Response(400, json={"code": "ERR_CURSOR_COMBINATION"}, request=req)
        if json.get("offset", 0) > 2000:
            return httpx.Response(400, json={"code": "ERR_OFFSET_TOO_DEEP"}, request=req)
        rows = sorted(self.tasks.items())
        for key, cond in (json.get("snapshot_filters") or {}).items():
            assert cond["op"] == "eq"
            rows = [(e, t) for e, t in rows if t.get(key) == cond["value"]]
        sort_by = json.get("sort_by", "entity_id")
        desc = json.get("sort_order", "asc") == "desc"
        if sort_by != "entity_id":
            assert sort_by == "last_observation_at", sort_by
            dated = [r for r in rows if r[1].get(sort_by) is not None]
            undated = [r for r in rows if r[1].get(sort_by) is None]
            dated.sort(key=lambda r: tw._parse_ts(r[1][sort_by]), reverse=desc)
            rows = undated + dated if desc else dated + undated
        elif desc:
            rows.reverse()
        offset, limit = json.get("offset", 0), json["limit"]
        page = rows[offset:offset + limit]
        return httpx.Response(200, json={
            "entities": [{"entity_id": e, "snapshot": {k: v for k, v in t.items()
                                                      if k != "last_observation_at"},
                          "last_observation_at": t.get("last_observation_at")}
                         for e, t in page],
            "total": len(rows),
        }, request=req)


def _store(monkeypatch, tasks):
    store = _FakeStore(tasks)
    monkeypatch.setattr(tw.httpx, "post", store.post)
    monkeypatch.setattr(tw, "NEOTOMA_BEARER_TOKEN", "test-token")
    return store


def _sweep_env(monkeypatch):
    status_calls: list = []
    notified: list = []
    dispatched: list = []
    monkeypatch.setattr(tw, "set_task_status", lambda *a, **k: status_calls.append((a, k)))
    monkeypatch.setattr(tw, "_notify", lambda *a, **k: notified.append(a))

    async def dispatch_fn(task_id, snapshot, trigger):
        dispatched.append(task_id)

    return status_calls, notified, dispatched, dispatch_fn


def _old_iso():
    return tw._iso(time.time() - 3 * 24 * 3600)


def test_recent_failure_beyond_first_id_page_is_retried(monkeypatch):
    """A recent failed task sorting after hundreds of other tasks is found by
    state and retried; an unfiltered first page of ids never reached it."""
    tasks = {f"ent_{i:04d}": {"status": "done"} for i in range(600)}
    tasks["ent_9999_failed"] = {
        "status": "failed", "last_observation_at": tw._iso(time.time() - 300),
    }
    store = _store(monkeypatch, tasks)
    monkeypatch.setattr(tw, "QUERY_LIMIT", 100)
    status_calls, _notified, dispatched, dispatch_fn = _sweep_env(monkeypatch)

    counts = asyncio.run(tw.TaskWatchdog(stall_seconds=3600).sweep(object(), dispatch_fn))

    assert dispatched == ["ent_9999_failed"]
    assert counts["retried"] == 1
    assert counts["matched"] == 1
    # The request really filters by state, for every watched status, and
    # never combines a cursor with the filter.
    filters = [r["snapshot_filters"] for r in store.requests]
    assert {"status": {"op": "eq", "value": "failed"}} in filters
    assert {f["status"]["value"] for f in filters} == {"failed", "verified", "routed", "executing"}
    assert all("cursor" not in r for r in store.requests)


def test_old_failure_is_left_alone_and_counted(monkeypatch):
    _store(monkeypatch, {"ent_old": {"status": "failed", "last_observation_at": _old_iso()}})
    status_calls, notified, dispatched, dispatch_fn = _sweep_env(monkeypatch)
    wd = tw.TaskWatchdog(stall_seconds=3600)
    wd._state["ent_exhausted"] = tw._AttemptState(attempts=tw.MAX_ATTEMPTS)

    counts = asyncio.run(wd.sweep(object(), dispatch_fn))

    assert counts["skipped_old"] == 1
    assert dispatched == [] and status_calls == [] and notified == []


def test_old_exhausted_failure_is_not_escalated(monkeypatch):
    _store(monkeypatch, {"ent_old": {"status": "failed", "last_observation_at": _old_iso()}})
    status_calls, notified, dispatched, dispatch_fn = _sweep_env(monkeypatch)
    wd = tw.TaskWatchdog(stall_seconds=3600)
    wd._state["ent_old"] = tw._AttemptState(attempts=tw.MAX_ATTEMPTS)

    counts = asyncio.run(wd.sweep(object(), dispatch_fn))

    assert counts["escalated"] == 0 and counts["skipped_old"] == 1
    assert status_calls == [] and notified == []


def test_failure_without_usable_age_is_not_retried(monkeypatch):
    # created_at is creation, not the failure: it is not a usable age.
    _store(monkeypatch, {
        "ent_noage": {"status": "failed"},
        "ent_created_only": {"status": "failed", "created_at": tw._iso(time.time() - 60)},
    })
    status_calls, notified, dispatched, dispatch_fn = _sweep_env(monkeypatch)

    counts = asyncio.run(tw.TaskWatchdog(stall_seconds=3600).sweep(object(), dispatch_fn))

    assert dispatched == [] and status_calls == [] and notified == []
    assert counts["skipped_old"] == 2


def test_paging_stops_at_bound_and_warns(monkeypatch, caplog):
    recent = tw._iso(time.time() - 60)
    tasks = {f"ent_{i:04d}": {"status": "failed", "last_observation_at": recent}
             for i in range(250)}
    store = _store(monkeypatch, tasks)
    monkeypatch.setattr(tw, "QUERY_LIMIT", 50)
    monkeypatch.setattr(tw, "MAX_PAGES_PER_STATUS", 2)
    _status_calls, _notified, _dispatched, dispatch_fn = _sweep_env(monkeypatch)

    with caplog.at_level(logging.WARNING, logger="apis.watchdog"):
        counts = asyncio.run(tw.TaskWatchdog(stall_seconds=3600).sweep(object(), dispatch_fn))

    failed_reqs = [r for r in store.requests
                   if r["snapshot_filters"]["status"]["value"] == "failed"]
    assert [r.get("offset", 0) for r in failed_reqs] == [0, 50]
    assert counts["scanned"] == 100
    assert counts["matched"] == 250
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("incomplete" in m and "failed" in m for m in warnings), warnings


# ── newest-first reading: the recency window, not the id prefix, is read ─────


def _ago(seconds):
    return tw._iso(time.time() - seconds)


def _failed_requests(store):
    return [r for r in store.requests
            if r["snapshot_filters"]["status"]["value"] == "failed"]


def test_recent_failure_behind_old_backlog_is_retried_first_sweep(monkeypatch):
    """2,500 old failed tasks sort ahead of one recent failure by id. Read in
    id order, the five-page bound spent every request on the backlog
    (red: matched=2501, scanned=2500, skipped_old=2500, retried=0). Read
    newest first, the recent one is on the first page."""
    tasks = {f"ent_{i:04d}": {"status": "failed", "last_observation_at": _ago(3 * 86400 + i)}
             for i in range(2500)}
    tasks["ent_9999_recent"] = {"status": "failed", "last_observation_at": _ago(300)}
    store = _store(monkeypatch, tasks)
    monkeypatch.setattr(tw, "QUERY_LIMIT", 500)
    monkeypatch.setattr(tw, "MAX_PAGES_PER_STATUS", 5)
    _status_calls, _notified, dispatched, dispatch_fn = _sweep_env(monkeypatch)

    counts = asyncio.run(tw.TaskWatchdog(stall_seconds=3600).sweep(object(), dispatch_fn))

    assert dispatched == ["ent_9999_recent"]
    assert counts["retried"] == 1
    assert counts["matched"] == 2501
    reqs = _failed_requests(store)
    assert all(r["sort_by"] == "last_observation_at" and r["sort_order"] == "desc"
               for r in store.requests)
    # The first page already ends older than the window: nothing more is read.
    assert [r.get("offset", 0) for r in reqs] == [0]


def test_paging_stops_at_first_page_ending_older_than_bound(monkeypatch):
    """120 recent and 200 old failures, 50 per page. Pages 1-2 are recent,
    page 3 ends old, so page 4 is never requested (red: offsets 0..200,
    the whole safety bound, in id order)."""
    tasks = {f"ent_r{i:03d}": {"status": "failed", "last_observation_at": _ago(60 + i)}
             for i in range(120)}
    tasks.update({f"ent_o{i:03d}": {"status": "failed", "last_observation_at": _ago(3 * 86400 + i)}
                  for i in range(200)})
    store = _store(monkeypatch, tasks)
    monkeypatch.setattr(tw, "QUERY_LIMIT", 50)
    monkeypatch.setattr(tw, "MAX_PAGES_PER_STATUS", 5)
    _status_calls, _notified, dispatched, dispatch_fn = _sweep_env(monkeypatch)

    counts = asyncio.run(tw.TaskWatchdog(stall_seconds=3600).sweep(object(), dispatch_fn))

    assert [r.get("offset", 0) for r in _failed_requests(store)] == [0, 50, 100]
    assert sorted(dispatched) == sorted(f"ent_r{i:03d}" for i in range(120))
    assert counts["scanned"] == 150 and counts["skipped_old"] == 30


def test_more_recent_rows_than_safety_limit_reports_truncated(monkeypatch, caplog):
    """250 recent failures against a 2 x 50 safety limit: the newest 100 are
    read and the status is reported truncated (red: the id-ordered read took
    the 100 OLDEST recent rows, ids being assigned oldest first)."""
    tasks = {f"ent_{i:04d}": {"status": "failed", "last_observation_at": _ago(10_000 - i * 10)}
             for i in range(250)}
    store = _store(monkeypatch, tasks)
    monkeypatch.setattr(tw, "QUERY_LIMIT", 50)
    monkeypatch.setattr(tw, "MAX_PAGES_PER_STATUS", 2)
    _status_calls, _notified, dispatched, dispatch_fn = _sweep_env(monkeypatch)

    with caplog.at_level(logging.WARNING, logger="apis.watchdog"):
        counts = asyncio.run(tw.TaskWatchdog(stall_seconds=3600).sweep(object(), dispatch_fn))

    assert [r.get("offset", 0) for r in _failed_requests(store)] == [0, 50]
    assert counts["scanned"] == 100
    assert sorted(dispatched) == sorted(f"ent_{i:04d}" for i in range(150, 250))
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("incomplete" in m and "failed" in m for m in warnings), warnings


def test_all_old_rows_retry_nothing_and_are_not_truncated(monkeypatch, caplog):
    """300 old failures against a 2 x 50 safety limit: one page is read,
    nothing is retried and no truncation is reported (red: both pages read
    and the status reported truncated, though nothing unread could qualify)."""
    tasks = {f"ent_{i:04d}": {"status": "failed", "last_observation_at": _ago(3 * 86400 + i)}
             for i in range(300)}
    store = _store(monkeypatch, tasks)
    monkeypatch.setattr(tw, "QUERY_LIMIT", 50)
    monkeypatch.setattr(tw, "MAX_PAGES_PER_STATUS", 2)
    status_calls, notified, dispatched, dispatch_fn = _sweep_env(monkeypatch)

    with caplog.at_level(logging.WARNING, logger="apis.watchdog"):
        counts = asyncio.run(tw.TaskWatchdog(stall_seconds=3600).sweep(object(), dispatch_fn))

    assert [r.get("offset", 0) for r in _failed_requests(store)] == [0]
    assert dispatched == [] and status_calls == [] and notified == []
    assert counts["skipped_old"] == 50
    assert not any("incomplete" in r.getMessage() for r in caplog.records)


def test_undated_rows_do_not_end_paging_and_are_not_retried(monkeypatch):
    """A first page of 50 failures with no `last_observation_at` (sorted
    first descending, as NULLs are) must not stop the read: the recent
    failure on page 2 is retried and the undated ones are not, as the
    recency check treats an ageless task as old. (Red: the id-ordered read
    spent its five pages on 300 old rows and never reached either.)"""
    tasks = {f"ent_a{i:03d}": {"status": "failed", "last_observation_at": _ago(3 * 86400 + i)}
             for i in range(300)}
    tasks.update({f"ent_b{i:03d}": {"status": "failed"} for i in range(50)})
    tasks["ent_c_recent"] = {"status": "failed", "last_observation_at": _ago(120)}
    store = _store(monkeypatch, tasks)
    monkeypatch.setattr(tw, "QUERY_LIMIT", 50)
    monkeypatch.setattr(tw, "MAX_PAGES_PER_STATUS", 5)
    status_calls, notified, dispatched, dispatch_fn = _sweep_env(monkeypatch)

    counts = asyncio.run(tw.TaskWatchdog(stall_seconds=3600).sweep(object(), dispatch_fn))

    assert [r.get("offset", 0) for r in _failed_requests(store)] == [0, 50]
    assert dispatched == ["ent_c_recent"]
    assert counts["retried"] == 1
    assert counts["skipped_old"] == 50 + 49
