"""Tests for the Apis stall watchdog — classification + sweep orchestration."""

from __future__ import annotations

import asyncio
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
    for s in ("pending", "done", "verified", "awaiting_approval", "blocked", "declined"):
        assert wd.classify("t", s, 99999) == tw.WatchdogAction.NONE


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
