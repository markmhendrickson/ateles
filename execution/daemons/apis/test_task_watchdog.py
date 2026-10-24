"""Tests for the Apis stall watchdog — classification + sweep orchestration."""

from __future__ import annotations

import asyncio
import inspect
import os
import sys

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


def test_page_one_ceiling_omits_recoverable_failure_on_old_query(monkeypatch):
    """A failed row after 500 ordinary rows must reach the dispatch callback."""
    paginated = len(inspect.signature(tw._query_tasks).parameters) > 1
    first_page = [(f"ent_{n}", {"status": "done"}) for n in range(500)]
    target = [("ent_target", {"status": "failed"})]

    def query(_limit, cursor=None):
        if not paginated:
            return first_page
        return (target, None) if cursor else (first_page, "next")

    monkeypatch.setattr(tw, "_query_tasks", query)
    monkeypatch.setattr(
        tw, "fetch_task_record", lambda _id: {"snapshot": {"status": "failed"}},
        raising=False,
    )
    monkeypatch.setattr(tw, "_routed_snapshot", lambda _id: {"status": "routed"}, raising=False)
    monkeypatch.setattr(tw, "set_task_status", lambda *_a, **_k: True)
    dispatched = []

    async def dispatch(task_id, *_args):
        dispatched.append(task_id)

    asyncio.run(tw.TaskWatchdog().sweep(object(), dispatch))
    assert dispatched == ["ent_target"]


def test_failed_route_write_cannot_start_worker_on_old_watchdog(monkeypatch):
    """A failed lifecycle correction must leave the worker unlaunched."""
    paginated = len(inspect.signature(tw._query_tasks).parameters) > 1

    def query(_limit, cursor=None):
        page = [("ent_failed", {"status": "failed"})]
        return (page, None) if paginated else page

    monkeypatch.setattr(tw, "_query_tasks", query)
    monkeypatch.setattr(
        tw, "fetch_task_record", lambda _id: {"snapshot": {"status": "failed"}},
        raising=False,
    )
    monkeypatch.setattr(tw, "set_task_status", lambda *_a, **_k: False)
    dispatched = []

    async def dispatch(task_id, *_args):
        dispatched.append(task_id)

    asyncio.run(tw.TaskWatchdog().sweep(object(), dispatch))
    assert dispatched == []


def test_sweep_retries_fresh_failure_and_escalates_exhausted(monkeypatch):
    wd = tw.TaskWatchdog(stall_seconds=3600)
    wd._state["ent_esc"] = tw._AttemptState(attempts=tw.MAX_ATTEMPTS)  # already exhausted

    monkeypatch.setattr(tw, "_query_tasks", lambda limit, cursor=None: ([
        ("ent_retry", {"status": "failed", "title": "retry me"}),
        ("ent_esc", {"status": "failed", "title": "give up"}),
    ], None))
    monkeypatch.setattr(tw, "fetch_task_record", lambda _id: {"snapshot": {"status": "failed"}})
    monkeypatch.setattr(tw, "_routed_snapshot", lambda _id: {"status": "routed"})

    status_calls: list[tuple] = []
    monkeypatch.setattr(tw, "set_task_status", lambda *a, **k: status_calls.append((a, k)) or True)
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

    monkeypatch.setattr(tw, "_query_tasks", lambda limit, cursor=None: ([("ent_x", {"status": "failed"})], None))
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
    monkeypatch.setattr(tw, "_query_tasks", lambda _limit, cursor=None: ([("ent_task", snapshot)], None))
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

    def boom(limit, cursor=None):
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


def test_failed_task_after_first_page_is_retried(monkeypatch):
    pages = [
        ([(f"ent_{n}", {"status": "done"}) for n in range(500)], "next"),
        ([("ent_target", {"status": "failed"})], None),
    ]
    cursors = []

    def query(_limit, cursor=None):
        cursors.append(cursor)
        return pages[len(cursors) - 1]

    monkeypatch.setattr(tw, "_query_tasks", query)
    monkeypatch.setattr(tw, "fetch_task_record", lambda _id: {"snapshot": {"status": "failed"}})
    monkeypatch.setattr(tw, "set_task_status", lambda *_a, **_k: True)
    monkeypatch.setattr(tw, "_routed_snapshot", lambda _id: {"status": "routed", "source": "readback"})
    dispatched = []

    async def dispatch(task_id, snapshot, trigger):
        dispatched.append((task_id, snapshot, trigger))

    counts = asyncio.run(tw.TaskWatchdog().sweep(object(), dispatch))
    assert cursors == [None, "next"]
    assert counts["scanned"] == 501
    assert counts["scan_complete"] is True
    assert dispatched == [(
        "ent_target", {"status": "routed", "source": "readback"}, "watchdog_retry"
    )]


def test_failed_route_write_never_launches(monkeypatch):
    monkeypatch.setattr(tw, "_query_tasks", lambda _limit, _cursor=None: (
        [("ent_failed", {"status": "failed"})], None
    ))
    monkeypatch.setattr(tw, "fetch_task_record", lambda _id: {"snapshot": {"status": "failed"}})
    monkeypatch.setattr(tw, "set_task_status", lambda *_a, **_k: False)
    dispatched = []

    async def dispatch(*args):
        dispatched.append(args)

    watchdog = tw.TaskWatchdog()
    counts = asyncio.run(watchdog.sweep(object(), dispatch))
    assert dispatched == []
    assert counts["retried"] == 0
    assert watchdog.attempts_for("ent_failed") == 0


def test_unverified_routed_readback_never_launches(monkeypatch):
    monkeypatch.setattr(tw, "_query_tasks", lambda _limit, _cursor=None: (
        [("ent_failed", {"status": "failed"})], None
    ))
    monkeypatch.setattr(tw, "fetch_task_record", lambda _id: {"snapshot": {"status": "failed"}})
    monkeypatch.setattr(tw, "set_task_status", lambda *_a, **_k: True)
    dispatched = []

    async def dispatch(*args):
        dispatched.append(args)

    watchdog = tw.TaskWatchdog()
    counts = asyncio.run(watchdog.sweep(object(), dispatch))
    assert dispatched == []
    assert counts["retry_unproven"] == 1
    assert watchdog.attempts_for("ent_failed") == 0


def test_retry_rechecks_current_task_and_holds_approval(monkeypatch):
    monkeypatch.setattr(tw, "_query_tasks", lambda _limit, _cursor=None: (
        [("ent_held", {"status": "failed"})], None
    ))
    monkeypatch.setattr(tw, "fetch_task_record", lambda _id: {"snapshot": {"status": "awaiting_approval"}})
    writes = []
    monkeypatch.setattr(tw, "set_task_status", lambda *_a, **_k: writes.append(1))

    async def dispatch(*_args):
        raise AssertionError("approval hold must not launch a worker")

    counts = asyncio.run(tw.TaskWatchdog().sweep(object(), dispatch))
    assert counts["retried"] == 0
    assert writes == []


def test_query_error_preserves_cursor_and_refuses_dispatch(monkeypatch):
    def query(_limit, cursor=None):
        if cursor is None:
            return ([("ent_done", {"status": "done"})], "next")
        raise RuntimeError("query failed")

    monkeypatch.setattr(tw, "_query_tasks", query)

    async def dispatch(*_args):
        raise AssertionError("query error must not launch a worker")

    watchdog = tw.TaskWatchdog()
    counts = asyncio.run(watchdog.sweep(object(), dispatch))
    assert counts["query_errors"] == 1
    assert counts["scan_complete"] is False
    assert watchdog._cursor == "next"


def test_bounded_cursor_resumes_on_next_sweep(monkeypatch):
    seen = []

    def query(_limit, cursor=None):
        seen.append(cursor)
        if cursor is None:
            return ([("ent_done", {"status": "done"})], "next")
        return ([("ent_failed", {"status": "failed"})], None)

    monkeypatch.setattr(tw, "_query_tasks", query)
    monkeypatch.setattr(tw, "fetch_task_record", lambda _id: {"snapshot": {"status": "failed"}})
    monkeypatch.setattr(tw, "set_task_status", lambda *_a, **_k: True)
    monkeypatch.setattr(tw, "_routed_snapshot", lambda _id: {"status": "routed"})
    dispatched = []

    async def dispatch(task_id, *_args):
        dispatched.append(task_id)

    watchdog = tw.TaskWatchdog(pages_per_sweep=1)
    first = asyncio.run(watchdog.sweep(object(), dispatch))
    assert first["scan_complete"] is False
    assert watchdog._cursor == "next"
    assert dispatched == []
    second = asyncio.run(watchdog.sweep(object(), dispatch))
    assert second["scan_complete"] is True
    assert watchdog._cursor is None
    assert seen == [None, "next"]
    assert dispatched == ["ent_failed"]


def test_expired_cursor_resets_without_dispatch(monkeypatch):
    def query(_limit, cursor=None):
        raise tw.CursorExpiredError("HTTP 410")

    monkeypatch.setattr(tw, "_query_tasks", query)

    async def dispatch(*_args):
        raise AssertionError("expired page must not launch a worker")

    watchdog = tw.TaskWatchdog()
    watchdog._cursor = "expired"
    counts = asyncio.run(watchdog.sweep(object(), dispatch))
    assert counts["cursor_expired"] == 1
    assert counts["scan_complete"] is False
    assert watchdog._cursor is None


def test_repeated_cursor_fails_closed_without_reprocessing_page(monkeypatch):
    monkeypatch.setattr(tw, "_query_tasks", lambda _limit, _cursor=None: (
        [("ent_failed", {"status": "failed"})], "repeat"
    ))

    async def dispatch(*_args):
        raise AssertionError("non-advancing cursor must not launch a worker")

    watchdog = tw.TaskWatchdog()
    watchdog._cursor = "repeat"
    counts = asyncio.run(watchdog.sweep(object(), dispatch))
    assert counts["query_errors"] == 1
    assert counts["scan_complete"] is False
    assert counts["scanned"] == 0
    assert watchdog._cursor is None


def test_query_requests_canonical_cursor_and_rejects_missing_token(monkeypatch):
    monkeypatch.setattr(tw, "NEOTOMA_BEARER_TOKEN", "")
    try:
        tw._query_tasks(500, "next")
    except tw.QueryUnavailableError:
        pass
    else:
        raise AssertionError("missing token must be unavailable")

    monkeypatch.setattr(tw, "NEOTOMA_BEARER_TOKEN", "test-token")
    request_bodies = []

    class Response:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"entities": [], "next_cursor": "later"}

    def post(_url, *, json, **_kwargs):
        request_bodies.append(json)
        return Response()

    monkeypatch.setattr(tw.httpx, "post", post)
    assert tw._query_tasks(500, "next") == ([], "later")
    assert request_bodies == [{"entity_type": "task", "limit": 500, "cursor": "next"}]


def test_query_refuses_incomplete_page_without_cursor(monkeypatch):
    monkeypatch.setattr(tw, "NEOTOMA_BEARER_TOKEN", "test-token")

    class Response:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"entities": [], "has_more": True}

    monkeypatch.setattr(tw.httpx, "post", lambda *_a, **_k: Response())
    try:
        tw._query_tasks(500)
    except tw.QueryUnavailableError:
        pass
    else:
        raise AssertionError("partial page without continuation must not look complete")


def test_cursor_error_is_distinct_from_other_bad_requests(monkeypatch):
    monkeypatch.setattr(tw, "NEOTOMA_BEARER_TOKEN", "test-token")

    class Response:
        status_code = 400

        def __init__(self, code):
            self.code = code

        def json(self):
            return {"error": {"code": self.code}}

        def raise_for_status(self):
            raise RuntimeError("bad request")

    monkeypatch.setattr(tw.httpx, "post", lambda *_a, **_k: Response("ERR_INVALID_CURSOR"))
    try:
        tw._query_tasks(500, "expired")
    except tw.CursorExpiredError:
        pass
    else:
        raise AssertionError("invalid cursor must be classified for reset")

    monkeypatch.setattr(tw.httpx, "post", lambda *_a, **_k: Response("ERR_INVALID_QUERY"))
    try:
        tw._query_tasks(500, "next")
    except RuntimeError as exc:
        assert str(exc) == "bad request"
    else:
        raise AssertionError("unrelated bad request must preserve cursor")


def test_watchdog_retains_background_run_entrypoint():
    assert callable(tw.TaskWatchdog.run)


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
