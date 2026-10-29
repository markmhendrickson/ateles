"""Tests for the Apis stall watchdog — classification + sweep orchestration."""

from __future__ import annotations

import asyncio
import inspect
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
from lib.daemon_runtime import session_finalize  # noqa: E402


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

    async def dispatch_fn(task_id, snapshot, trigger, run_key):
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

    async def dispatch_fn(task_id, snapshot, trigger, run_key):
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

    async def dispatch_fn(task_id, _snapshot, _trigger, _run_key):
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


@pytest.mark.parametrize("status,stamp_key,nested", [
    ("routed", "last_observation_at", False),
    ("executing", "computed_at", False),
    ("routed", "last_observation_at", True),
    ("executing", "computed_at", True),
])
def test_row_stamped_stalled_task_reaches_retry_from_second_page(
    monkeypatch, status, stamp_key, nested,
):
    old = tw._iso(tw.time.time() - 7200)
    task = {"status": status}
    wrapped = {"snapshot": task} if nested else task
    row = {"entity_id": "ent_stalled", "snapshot": wrapped}
    (wrapped if nested else row)[stamp_key] = old
    pages = [
        {"entities": [{"entity_id": "ent_done", "snapshot": {"status": "done"}}],
         "limit": tw.QUERY_LIMIT, "offset": 0, "total": 2, "next_cursor": "second"},
        {"entities": [row], "limit": tw.QUERY_LIMIT, "offset": 0,
         "total": 2, "next_cursor": None},
    ]
    requests = []

    class Response:
        status_code = 200

        def __init__(self, body):
            self.body = body

        def raise_for_status(self):
            pass

        def json(self):
            return self.body

    def post(_url, **kwargs):
        requests.append(kwargs["json"])
        return Response(pages[len(requests) - 1])

    monkeypatch.setattr(tw, "NEOTOMA_BEARER_TOKEN", "test-token")
    monkeypatch.setattr(tw.httpx, "post", post)
    state = {"status": status}
    fresh_reads = []

    def fresh(_id):
        fresh_reads.append(state["status"])
        return {"snapshot": {"status": state["status"]}, stamp_key: old}

    def write(_id, new_status, **_kwargs):
        state["status"] = new_status.value
        return True

    monkeypatch.setattr(tw, "fetch_task_record", fresh)
    monkeypatch.setattr(tw, "set_task_status", write)
    dispatched = []

    async def dispatch(task_id, snapshot, trigger, run_key):
        dispatched.append((task_id, snapshot["status"], trigger, run_key))

    counts = asyncio.run(tw.TaskWatchdog(stall_seconds=3600).sweep(object(), dispatch))
    assert [r.get("cursor") for r in requests] == [None, "second"]
    assert counts["scan_complete"] is True
    assert counts["scanned"] == 2
    assert counts["retried"] == 1
    assert fresh_reads == [status, "routed"]
    assert dispatched[0][:3] == ("ent_stalled", "routed", "watchdog_retry")


def test_retries_after_restart_have_distinct_verified_run_sessions(monkeypatch):
    state = {"status": "failed"}
    payloads = []
    runs = []
    route_keys = []
    acknowledged = set()
    monkeypatch.setattr(tw, "_query_tasks", lambda *_a: (
        [("ent_task", {"status": "failed"})], None
    ))
    monkeypatch.setattr(tw, "fetch_task_record", lambda _id: {
        "snapshot": {"status": state["status"]}
    })

    def write(_id, status, **kwargs):
        key = kwargs["key_suffix"]
        route_keys.append(key)
        if key in acknowledged:
            return True  # /correct acknowledges replay without applying the old write
        acknowledged.add(key)
        state["status"] = status.value
        return True

    monkeypatch.setattr(tw, "set_task_status", write)
    monkeypatch.setattr(session_finalize, "NEOTOMA_BEARER_TOKEN", "test-token")
    monkeypatch.setattr(session_finalize, "_post_store", lambda body:
                        payloads.append(body) or None)  # response lost after write

    def rows(entity_type, *, identity_value, **_kwargs):
        payload = next((body for body in payloads
                        if body["entities"][1]["native_session_id"] == identity_value), None)
        if payload is None:
            return []
        if entity_type == "agent_session":
            return [{"entity_id": f"session-{identity_value}",
                     "snapshot": payload["entities"][1]}]
        return [{"entity_id": f"conversation-{identity_value}",
                 "snapshot": payload["entities"][0]}]

    def entity_readback(entity_id, *, entity_type, required_fields, **_kwargs):
        identity = entity_id.split("-", 1)[1]
        payload = next(body for body in payloads
                       if body["entities"][1]["native_session_id"] == identity)
        entity = payload["entities"][0 if entity_type == "conversation" else 1]
        return all(entity.get(key) == value for key, value in required_fields.items())

    def relationship_readback(source_id, *, target_entity_id, relationship_type, **_kwargs):
        identity = source_id.split("-", 1)[1]
        payload = next(body for body in payloads
                       if body["entities"][1]["native_session_id"] == identity)
        source_index = 0 if source_id.startswith("conversation-") else 1
        return relationship_type == "REFERS_TO" and any(
            rel.get("source_index") == source_index and (
                rel.get("target_entity_id") == target_entity_id
                or rel.get("target_index") == 1
                and target_entity_id == f"session-{identity}"
            ) for rel in payload["relationships"]
        )

    monkeypatch.setattr(session_finalize, "_query_entity_rows", rows)
    monkeypatch.setattr(session_finalize, "_entity_readback_matches", entity_readback)
    monkeypatch.setattr(session_finalize, "_relationship_readback_matches", relationship_readback)

    async def dispatch(_task_id, routed, trigger, run_key):
        assert routed["status"] == "routed"
        assert trigger == "watchdog_retry"
        run = session_finalize.create_run_session(
            task_id="ent_task", agent="cicada", run_key=run_key,
        )
        assert run is not None
        runs.append(run)

    first = asyncio.run(tw.TaskWatchdog().sweep(object(), dispatch))
    state["status"] = "failed"
    second = asyncio.run(tw.TaskWatchdog().sweep(object(), dispatch))
    assert first["retried"] == second["retried"] == 1
    assert len(route_keys) == 2 and route_keys[0] != route_keys[1]
    assert len(payloads) == len(runs) == 2
    assert runs[0].native_session_id != runs[1].native_session_id
    assert all(p["entities"][1]["native_session_id"] == run.native_session_id
               for p, run in zip(payloads, runs))
    assert len({p["idempotency_key"] for p in payloads}) == 2


def test_unconfirmed_route_resends_with_same_attempt_key(monkeypatch):
    state = {"status": "failed"}
    keys = []
    dispatched = []
    monkeypatch.setattr(tw, "_query_tasks", lambda *_a: (
        [("ent_task", {"status": "failed"})], None
    ))
    monkeypatch.setattr(tw, "fetch_task_record", lambda _id: {
        "snapshot": {"status": state["status"]}
    })

    def write(_id, status, **kwargs):
        keys.append(kwargs["key_suffix"])
        if len(keys) == 1:
            return False  # no acknowledgement; the same attempt is retried
        state["status"] = status.value
        return True

    monkeypatch.setattr(tw, "set_task_status", write)

    async def dispatch(_task_id, _snapshot, _trigger, run_key):
        dispatched.append(run_key)

    watchdog = tw.TaskWatchdog()
    first = asyncio.run(watchdog.sweep(object(), dispatch))
    second = asyncio.run(watchdog.sweep(object(), dispatch))
    assert first["retried"] == 0 and second["retried"] == 1
    assert len(keys) == 2 and keys[0] == keys[1] == dispatched[0]


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

    async def dispatch(task_id, snapshot, trigger, run_key):
        dispatched.append((task_id, snapshot, trigger, run_key))

    counts = asyncio.run(tw.TaskWatchdog().sweep(object(), dispatch))
    assert cursors == [None, "next"]
    assert counts["scanned"] == 501
    assert counts["scan_complete"] is True
    assert len(dispatched) == 1
    assert dispatched[0][:3] == (
        "ent_target", {"status": "routed", "source": "readback"}, "watchdog_retry"
    )
    assert dispatched[0][3].startswith("watchdog_retry-")


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


@pytest.mark.parametrize("metadata", [
    {"total": 2}, {"total": 0}, {"total": -1}, {"total": True},
    {"total": "1"}, {"truncated": "false"}, {"has_more": 0},
    {"next_cursor": 4}, {"limit": 0}, {"offset": 1},
    {"next_cursor": "later", "has_more": False},
    {"next_offset": 1},
])
def test_query_refuses_inconsistent_page_metadata(monkeypatch, metadata):
    monkeypatch.setattr(tw, "NEOTOMA_BEARER_TOKEN", "test-token")

    class Response:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"entities": [{"entity_id": "ent_x", "snapshot": {
                "status": "failed",
            }}], "limit": 500, "offset": 0, **metadata}

    monkeypatch.setattr(tw.httpx, "post", lambda *_a, **_kw: Response())
    with pytest.raises(tw.QueryUnavailableError):
        tw._query_tasks(500)


def test_incomplete_query_does_not_dispatch_or_complete_scan(monkeypatch):
    monkeypatch.setattr(tw, "NEOTOMA_BEARER_TOKEN", "test-token")

    class Response:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"entities": [{"entity_id": "ent_x", "snapshot": {
                "status": "failed",
            }}], "total": 2, "limit": 500, "offset": 0}

    monkeypatch.setattr(tw.httpx, "post", lambda *_a, **_kw: Response())

    async def dispatch(*_args):
        raise AssertionError("incomplete query must not dispatch")

    counts = asyncio.run(tw.TaskWatchdog().sweep(object(), dispatch))
    assert counts["query_unavailable"] == 1
    assert counts["scanned"] == 0
    assert counts["scan_complete"] is False


def test_query_accepts_counted_continuation_and_final_page(monkeypatch):
    monkeypatch.setattr(tw, "NEOTOMA_BEARER_TOKEN", "test-token")
    payloads = iter([
        {"entities": [{"entity_id": "ent_a", "snapshot": {"status": "done"}}],
         "total": 2, "limit": 1, "offset": 0, "next_cursor": "later",
         "has_more": True},
        {"entities": [{"entity_id": "ent_b", "snapshot": {"status": "done"}}],
         "total": 2, "limit": 1, "offset": 0, "next_cursor": "last-page"},
        {"entities": [], "total": 2, "limit": 1, "offset": 0},
    ])

    class Response:
        status_code = 200

        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self.payload

    monkeypatch.setattr(tw.httpx, "post", lambda *_a, **_kw: Response(next(payloads)))
    assert tw._query_tasks(1) == ([("ent_a", {"status": "done"})], "later")
    assert tw._query_tasks(1, "later") == ([("ent_b", {"status": "done"})], "last-page")
    assert tw._query_tasks(1, "last-page") == ([], None)


@pytest.mark.parametrize("payload", [
    {}, {"entities": None}, {"entities": {}}, {"entities": "bad"},
    {"entities": [None]}, {"entities": [{"entity_id": "ent_x", "snapshot": None}]},
    {"entities": [{"entity_id": "ent_x", "snapshot": {"snapshot": None}}]},
    {"entities": [{"entity_id": "ent_x", "snapshot": {}}]},
])
def test_query_rejects_malformed_rows_envelope(monkeypatch, payload):
    monkeypatch.setattr(tw, "NEOTOMA_BEARER_TOKEN", "test-token")

    class Response:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return payload

    monkeypatch.setattr(tw.httpx, "post", lambda *_a, **_k: Response())
    with pytest.raises(tw.QueryUnavailableError):
        tw._query_tasks(500, "next")


def test_query_accepts_canonical_and_legacy_empty_pages(monkeypatch):
    monkeypatch.setattr(tw, "NEOTOMA_BEARER_TOKEN", "test-token")

    class Response:
        status_code = 200

        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self.payload

    for payload in ({"entities": [], "next_cursor": None}, {"results": []}):
        monkeypatch.setattr(tw.httpx, "post", lambda *_a, **_k: Response(payload))
        assert tw._query_tasks(500) == ([], None)


def test_malformed_query_does_not_complete_watchdog_scan(monkeypatch):
    monkeypatch.setattr(tw, "NEOTOMA_BEARER_TOKEN", "test-token")

    class Response:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {}

    monkeypatch.setattr(tw.httpx, "post", lambda *_a, **_k: Response())

    async def dispatch(*_args):
        raise AssertionError("malformed page must not dispatch")

    watchdog = tw.TaskWatchdog()
    watchdog._cursor = "next"
    counts = asyncio.run(watchdog.sweep(object(), dispatch))
    assert counts["query_unavailable"] == 1
    assert counts["scan_complete"] is False
    assert watchdog._cursor == "next"


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
