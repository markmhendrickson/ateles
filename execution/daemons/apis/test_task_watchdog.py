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


# ── lease RELEASE path (ClaimStore wired into sweep) ─────────────────────────


class _FakeClaims:
    """Minimal ClaimStore stand-in for sweep() RELEASE / escalate tests."""

    def __init__(self, *, live: bool = False, holder: str = "r1"):
        self.live = live
        self.holder = holder
        self.released: list[str] = []

    def inspect(self, task_id: str):
        return {"live": self.live, "holder": self.holder, "task_id": task_id}

    def release_expired(self, task_id: str) -> bool:
        self.released.append(task_id)
        return True


def test_sweep_releases_lapsed_claim_back_to_pending(monkeypatch):
    """With a ClaimStore injected, a dead lease returns the task to PENDING.

    Catches the production miss where TaskWatchdog() was constructed without
    `_claims=` — classify never saw a lease, RELEASE never ran, and
    release_expired was unreachable.
    """
    claims = _FakeClaims(live=False, holder="r1")
    wd = tw.TaskWatchdog(stall_seconds=3600, _claims=claims)

    monkeypatch.setattr(
        tw, "_query_tasks",
        lambda limit: [("ent_dead", {"status": "executing", "title": "stranded"})],
    )
    status_calls: list[tuple] = []
    monkeypatch.setattr(tw, "set_task_status", lambda *a, **k: status_calls.append((a, k)))
    monkeypatch.setattr(tw, "_notify", lambda *a, **k: None)

    dispatched: list = []

    async def dispatch_fn(task_id, snapshot, trigger):
        dispatched.append((task_id, trigger))

    counts = asyncio.run(wd.sweep(object(), dispatch_fn))

    assert counts["released"] == 1
    assert counts["retried"] == 0
    assert counts["escalated"] == 0
    assert dispatched == []
    assert claims.released == ["ent_dead"]
    assert wd.attempts_for("ent_dead") == 1
    assert status_calls, "expected a PENDING status write"
    args, kwargs = status_calls[0]
    assert args[0] == "ent_dead"
    assert args[1] == tw.TaskStatus.PENDING
    reason = kwargs.get("reason") or ""
    assert "lease" in reason.lower() or "lapsed" in reason.lower()


def test_sweep_escalates_after_max_lapsed_lease_releases(monkeypatch):
    """N real sweep() RELEASE cycles must escalate — no manual record_retry.

    The prior false-green handed record_retry between classify-only loops while
    production RELEASE never incremented attempts, so a dead lease looped
    forever at attempts=0.
    """
    claims = _FakeClaims(live=False, holder="r-dead")
    wd = tw.TaskWatchdog(stall_seconds=3600, _claims=claims)

    monkeypatch.setattr(
        tw, "_query_tasks",
        lambda limit: [("ent_loop", {"status": "executing", "title": "keeps dying"})],
    )
    status_calls: list[tuple] = []
    monkeypatch.setattr(tw, "set_task_status", lambda *a, **k: status_calls.append((a, k)))
    notifies: list = []
    monkeypatch.setattr(tw, "_notify", lambda *a, **k: notifies.append(a))

    async def dispatch_fn(*a):
        raise AssertionError("RELEASE/ESCALATE must not re-dispatch")

    released = 0
    escalated = 0
    for _ in range(tw.MAX_ATTEMPTS + 1):
        counts = asyncio.run(wd.sweep(object(), dispatch_fn))
        released += counts["released"]
        escalated += counts["escalated"]
        if counts["escalated"]:
            break

    assert released == tw.MAX_ATTEMPTS
    assert escalated == 1
    assert any(c[0][1] == tw.TaskStatus.BLOCKED for c in status_calls)
    assert notifies, "operator must be paged on escalate"
    assert wd.attempts_for("ent_loop") == 0  # forgotten after escalate


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
