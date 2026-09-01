"""Tests for the Apis level-triggered reconciliation sweep (ateles#586).

The four properties the sweep must hold, per the issue:

  1. A task created while the SSE path was down IS dispatched by the sweep.
  2. A task already in flight is NOT double-dispatched.
  3. The per-pass cap holds.
  4. Skips are logged, with a reason.

Plus the gate contract: the sweep reaches the SAME dispatch_task the SSE path
uses, so the execution gate still applies — it cannot become a side door.
"""

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

import task_reconciler as tr  # noqa: E402


_OLD = 10_000.0  # comfortably past any default grace window


def _recorder():
    """An async dispatch_fn that records (task_id, trigger) calls."""
    calls: list[tuple[str, str]] = []

    async def dispatch_fn(task_id, snapshot, trigger):
        calls.append((task_id, trigger))

    return calls, dispatch_fn


# ── 1. the stranded task IS dispatched ───────────────────────────────────────


def test_task_stranded_by_dead_sse_path_is_dispatched(monkeypatch):
    """The ateles#586 case: a task created while the SSE subscription was dead.

    It is `pending`, nothing ever routed it, and it is far older than the grace
    window. Nothing else in the swarm looks at it — the watchdog classifies
    `pending` as NONE. The sweep must pick it up.
    """
    rec = tr.TaskReconciler()
    monkeypatch.setattr(tr, "_query_tasks", lambda limit: [
        ("ent_stranded", {"status": "pending", "title": "never dispatched",
                          "updated_at": tr._iso_ago(_OLD)}),
    ])
    calls, dispatch_fn = _recorder()

    counts = asyncio.run(rec.sweep(dispatch_fn))

    assert calls == [("ent_stranded", "reconcile")]
    assert counts["dispatched"] == 1


def test_absent_status_counts_as_pending(monkeypatch):
    """Legacy rows predate the lifecycle field; an absent status is sweepable."""
    rec = tr.TaskReconciler()
    monkeypatch.setattr(tr, "_query_tasks", lambda limit: [
        ("ent_legacy", {"title": "no status field", "updated_at": tr._iso_ago(_OLD)}),
    ])
    calls, dispatch_fn = _recorder()

    asyncio.run(rec.sweep(dispatch_fn))

    assert calls == [("ent_legacy", "reconcile")]


# ── 2. in-flight work is NOT double-dispatched ───────────────────────────────


def test_inflight_statuses_are_never_swept(monkeypatch):
    """Layer 1: anything the SSE path (or watchdog, or operator) owns is skipped.

    dispatch_task writes ROUTED before the readiness gate, the execution gate,
    and any spawn — so a task the event path has touched has already left
    `pending` and must not be selected here, however old it is.
    """
    owned = [
        "routed", "executing", "verified", "done", "failed", "blocked",
        "awaiting_approval", "awaiting_input", "declined", "superseded",
    ]
    rec = tr.TaskReconciler()
    monkeypatch.setattr(tr, "_query_tasks", lambda limit: [
        (f"ent_{s}", {"status": s, "title": s, "updated_at": tr._iso_ago(_OLD)})
        for s in owned
    ])
    calls, dispatch_fn = _recorder()

    counts = asyncio.run(rec.sweep(dispatch_fn))

    assert calls == []
    assert counts["not_pending"] == len(owned)
    assert counts["dispatched"] == 0


def test_recently_touched_pending_task_is_left_to_the_sse_path(monkeypatch):
    """Layer 2: the race. A task the SSE path picked up seconds ago is still
    `pending` until its ROUTED correction lands. The grace window means the sweep
    never races a live dispatch — the event path always wins."""
    rec = tr.TaskReconciler(grace_seconds=900)
    monkeypatch.setattr(tr, "_query_tasks", lambda limit: [
        ("ent_inflight", {"status": "pending", "title": "SSE is on it right now",
                          "updated_at": tr._iso_ago(5)}),
    ])
    calls, dispatch_fn = _recorder()

    counts = asyncio.run(rec.sweep(dispatch_fn))

    assert calls == []
    assert counts["within_grace"] == 1


def test_unparseable_age_fails_safe_toward_not_dispatching(monkeypatch):
    """No usable timestamp → treat as within grace. A missed pass costs one
    interval; a wrong dispatch costs duplicated agent work."""
    rec = tr.TaskReconciler()
    monkeypatch.setattr(tr, "_query_tasks", lambda limit: [
        ("ent_nots", {"status": "pending", "title": "no timestamp"}),
    ])
    calls, dispatch_fn = _recorder()

    counts = asyncio.run(rec.sweep(dispatch_fn))

    assert calls == []
    assert counts["within_grace"] == 1


def test_claimed_task_is_not_dispatched_twice_across_passes(monkeypatch):
    """Layer 3: status writes are fail-OPEN, so a lost ROUTED write leaves the
    task reading `pending` on the next pass. The claim ledger stops layers 1+2
    from re-selecting it."""
    rec = tr.TaskReconciler()
    # Same task, still `pending` on the second pass — the ROUTED write was lost.
    monkeypatch.setattr(tr, "_query_tasks", lambda limit: [
        ("ent_lostwrite", {"status": "pending", "title": "ROUTED write never landed",
                           "updated_at": tr._iso_ago(_OLD)}),
    ])
    calls, dispatch_fn = _recorder()

    first = asyncio.run(rec.sweep(dispatch_fn))
    second = asyncio.run(rec.sweep(dispatch_fn))

    assert first["dispatched"] == 1
    assert second["dispatched"] == 0
    assert second["already_claimed"] == 1
    assert calls == [("ent_lostwrite", "reconcile")]  # exactly once


def test_claim_is_held_even_when_dispatch_raises(monkeypatch):
    """A task that crashed the dispatcher must not be re-thrown at it every
    pass. Recovery from there is the watchdog's and the operator's."""
    rec = tr.TaskReconciler()
    monkeypatch.setattr(tr, "_query_tasks", lambda limit: [
        ("ent_boom", {"status": "pending", "title": "explodes",
                      "updated_at": tr._iso_ago(_OLD)}),
    ])
    attempts = []

    async def dispatch_fn(task_id, snapshot, trigger):
        attempts.append(task_id)
        raise RuntimeError("dispatch blew up")

    first = asyncio.run(rec.sweep(dispatch_fn))
    second = asyncio.run(rec.sweep(dispatch_fn))

    assert first["dispatch_failed"] == 1
    assert second["already_claimed"] == 1
    assert attempts == ["ent_boom"]  # tried once, not every pass


# ── 3. the cap holds ─────────────────────────────────────────────────────────


def test_cap_bounds_dispatches_per_pass_and_defers_the_rest(monkeypatch):
    """~100 tasks are waiting. A pass that fanned all of them out at once would
    be its own incident, so the cap bounds each pass and the remainder are
    deferred (not dropped) to the next one."""
    rec = tr.TaskReconciler(max_per_sweep=3)
    monkeypatch.setattr(tr, "_query_tasks", lambda limit: [
        (f"ent_{i}", {"status": "pending", "title": f"backlog {i}",
                      "updated_at": tr._iso_ago(_OLD)})
        for i in range(10)
    ])
    calls, dispatch_fn = _recorder()

    counts = asyncio.run(rec.sweep(dispatch_fn))

    assert counts["dispatched"] == 3
    assert len(calls) == 3
    assert counts["cap_reached"] == 7  # deferred, and visible as such
    assert counts["scanned"] == 10


def test_deferred_backlog_depth_is_reported_at_info(monkeypatch, caplog):
    """The undrained remainder is what an operator watching a drain needs, and
    it is what says whether the cap is set sensibly — so it is stated at INFO,
    not left to be counted out of per-task DEBUG lines."""
    rec = tr.TaskReconciler(max_per_sweep=2)
    monkeypatch.setattr(tr, "_query_tasks", lambda limit: [
        (f"ent_{i}", {"status": "pending", "title": f"backlog {i}",
                      "updated_at": tr._iso_ago(_OLD)})
        for i in range(9)
    ])
    _, dispatch_fn = _recorder()

    with caplog.at_level(logging.INFO, logger="apis.reconciler"):
        asyncio.run(rec.sweep(dispatch_fn))

    assert "7 eligible task(s) deferred" in caplog.text


def test_backlog_drains_across_passes_without_repeating(monkeypatch):
    """Successive passes make progress: the cap throttles, the claim ledger
    stops the already-dispatched ones from consuming the next pass's budget."""
    rec = tr.TaskReconciler(max_per_sweep=2)
    backlog = [
        (f"ent_{i}", {"status": "pending", "title": f"backlog {i}",
                      "updated_at": tr._iso_ago(_OLD)})
        for i in range(5)
    ]
    monkeypatch.setattr(tr, "_query_tasks", lambda limit: backlog)
    calls, dispatch_fn = _recorder()

    for _ in range(3):
        asyncio.run(rec.sweep(dispatch_fn))

    dispatched = [c[0] for c in calls]
    assert dispatched == [f"ent_{i}" for i in range(5)]  # each exactly once, in order


# ── 4. skips are logged, with reasons ────────────────────────────────────────


def test_every_skip_is_logged_with_its_reason(monkeypatch, caplog):
    """A silent sweep reproduces the invisibility this fixes. Each skipped task
    must name itself and its reason in the log."""
    rec = tr.TaskReconciler(max_per_sweep=1, grace_seconds=900)
    rec.claim("ent_claimed")
    monkeypatch.setattr(tr, "_query_tasks", lambda limit: [
        ("ent_owned", {"status": "executing", "updated_at": tr._iso_ago(_OLD)}),
        ("ent_young", {"status": "pending", "updated_at": tr._iso_ago(5)}),
        ("ent_claimed", {"status": "pending", "updated_at": tr._iso_ago(_OLD)}),
        ("ent_ok", {"status": "pending", "updated_at": tr._iso_ago(_OLD)}),
        ("ent_capped", {"status": "pending", "updated_at": tr._iso_ago(_OLD)}),
    ])
    _, dispatch_fn = _recorder()

    with caplog.at_level(logging.DEBUG, logger="apis.reconciler"):
        counts = asyncio.run(rec.sweep(dispatch_fn))

    text = caplog.text
    # Each skipped task is named alongside the reason it was skipped.
    for entity_id, reason in (
        ("ent_owned", "not_pending"),
        ("ent_young", "within_grace"),
        ("ent_claimed", "already_claimed"),
        ("ent_capped", "cap_reached"),
    ):
        assert entity_id in text, f"{entity_id} was skipped silently"
        assert reason in text, f"{entity_id} skipped without naming {reason}"

    # And the pass itself reports its counts, so an empty sweep is
    # distinguishable from a sweep that never ran.
    assert "sweep:" in text
    assert counts["scanned"] == 5


def test_empty_sweep_still_logs(monkeypatch, caplog):
    """'Found nothing' and 'did not run' must not look the same in the log —
    that indistinguishability is what hid 67,450 skipped events for 88 days."""
    rec = tr.TaskReconciler()
    monkeypatch.setattr(tr, "_query_tasks", lambda limit: [])
    _, dispatch_fn = _recorder()

    with caplog.at_level(logging.INFO, logger="apis.reconciler"):
        asyncio.run(rec.sweep(dispatch_fn))

    assert "sweep:" in caplog.text


# ── the gate contract ────────────────────────────────────────────────────────


def test_sweep_routes_through_apis_dispatch_task_so_the_gate_applies():
    """The sweep must not be a side door around the execution gate.

    It never spawns an agent itself: it calls the dispatch_fn it is handed, which
    apis.py binds to `dispatch_task` WITHOUT gate_override — the same call the
    SSE path makes. So blast radius and confidence are still evaluated and
    high-blast work is still held for an operator checkpoint.
    """
    import inspect

    import apis

    # The sweep's only outward action is `await dispatch_fn(...)`.
    src = inspect.getsource(tr.TaskReconciler.sweep)
    assert "dispatch_fn(" in src
    assert "gate_override" not in src, "the sweep must not bypass the gate"

    # apis.py wires that closure to dispatch_task, which applies the gate unless
    # gate_override is passed — and the reconciler closure does not pass it.
    wiring = inspect.getsource(apis.main)
    assert "reconcile_dispatch" in wiring
    assert "TaskReconciler" in wiring

    gate_src = inspect.getsource(apis.dispatch_task)
    assert "if not gate_override:" in gate_src
    assert "GateAction.AUTO_EXECUTE" in gate_src


# ── fail-open ────────────────────────────────────────────────────────────────


def test_query_failure_is_swallowed(monkeypatch):
    """A Neotoma blip must never kill the sweep loop."""
    rec = tr.TaskReconciler()

    def boom(limit):
        raise RuntimeError("neotoma down")

    monkeypatch.setattr(tr, "_query_tasks", boom)

    async def dispatch_fn(*a):
        raise AssertionError("should not dispatch")

    counts = asyncio.run(rec.sweep(dispatch_fn))
    assert counts["scanned"] == 0
    assert counts["dispatched"] == 0


def test_run_is_a_noop_when_disabled(monkeypatch):
    """Default-off: `run` returns immediately rather than sweeping."""
    monkeypatch.setattr(tr, "ENABLED", False)
    rec = tr.TaskReconciler()

    async def dispatch_fn(*a):
        raise AssertionError("should not dispatch while disabled")

    monkeypatch.setattr(
        rec, "sweep", lambda *a: (_ for _ in ()).throw(AssertionError("swept while off"))
    )
    asyncio.run(rec.run(dispatch_fn))  # returns without sweeping


# ── The parse layer: _unwrap_snapshot / _query_tasks (ateles#598 qa lens) ────
#
# These sit UPSTREAM of all three double-dispatch layers and had no tests: 13
# of the 15 cases above monkeypatch `_query_tasks` away, and `_unwrap_snapshot`
# never appeared in this file. A wrong unwrap defeats layer 1 at parse time —
# `status` reads absent -> "" is in _SWEEPABLE -> a done task becomes eligible.
#
# The rows below are the real `/entities/query` shape: `EntitySnapshot` declares
# `computed_at` and `last_observation_at` as SIBLINGS of `snapshot`, not inside
# it, so the pm lens's finding was that the unwrap discarded exactly the stamps
# the age calculation needs — every real stranded task skipped `within_grace`
# forever, with no expiry, because nothing would ever give the row an age.


def _row(snapshot: dict, **row_level) -> dict:
    """A row shaped like the real /entities/query EntitySnapshot response."""
    return {"entity_id": "ent_task", "entity_type": "task", "snapshot": snapshot, **row_level}


def test_unwrap_carries_row_level_stamps_into_the_snapshot():
    """The two stamps the age calculation needs survive the unwrap.

    FAILS before the fix: the row-level stamps were dropped, `_age_seconds`
    returned None, and should_dispatch mapped that to WITHIN_GRACE on every
    pass.
    """
    row = _row({"status": "pending"}, last_observation_at="2026-01-01T00:00:00Z")
    snap = tr._unwrap_snapshot(row)

    assert snap["status"] == "pending"
    assert snap["last_observation_at"] == "2026-01-01T00:00:00Z"
    assert tr._age_seconds(snap, now=time.time()) is not None


def test_a_stranded_task_is_dispatchable_rather_than_ageless():
    """The end-to-end effect #586 exists to produce, through the real parse.

    A pending task last touched well beyond the grace window must come out of
    the parse with an age, and should_dispatch must not skip it as
    `within_grace`.
    """
    old = tr._iso_ago(tr.GRACE_SECONDS + 3600)
    snap = tr._unwrap_snapshot(_row({"status": "pending"}, last_observation_at=old))
    age = tr._age_seconds(snap, now=time.time())

    assert age is not None and age > tr.GRACE_SECONDS
    skip = tr.TaskReconciler().should_dispatch("ent_task", "pending", age)
    assert skip is not tr.SkipReason.WITHIN_GRACE, "a real stranded task is not ageless"
    assert skip is None, f"nothing should skip this task, got {skip}"


def test_a_snapshot_stamp_is_not_shadowed_by_a_row_level_one():
    """`updated_at` inside the snapshot stays authoritative.

    It moves when the SSE path writes ROUTED, so it means "untouched by
    dispatch"; the row-level stamps move on ANY observation write and are only
    the fallback. Carrying them must not change that precedence.
    """
    fresh = tr._iso_ago(5)
    stale = tr._iso_ago(tr.GRACE_SECONDS + 9999)
    snap = tr._unwrap_snapshot(
        _row({"status": "pending", "updated_at": fresh}, last_observation_at=stale)
    )

    assert snap["updated_at"] == fresh
    age = tr._age_seconds(snap, now=time.time())
    assert age is not None and age < tr.GRACE_SECONDS, "the fresh stamp must win"


def test_done_task_is_not_swept_through_the_real_parse_path():
    """Layer 1 must hold at PARSE time, not only in should_dispatch.

    If the unwrap loses `status`, it reads absent -> "" is in _SWEEPABLE -> a
    completed task is re-dispatched. This asserts through the parse rather than
    handing should_dispatch a hand-built dict.
    """
    snap = tr._unwrap_snapshot(_row({"status": "done"}, last_observation_at=tr._iso_ago(99999)))

    assert snap["status"] == "done"
    skip = tr.TaskReconciler().should_dispatch(
        "ent_task", snap.get("status", ""), tr._age_seconds(snap, now=time.time())
    )
    assert skip is tr.SkipReason.NOT_PENDING


def test_unwrap_tolerates_the_doubly_nested_and_bare_shapes():
    """The nesting tolerance the docstring promises, pinned."""
    assert tr._unwrap_snapshot(_row({"snapshot": {"status": "pending"}}))["status"] == "pending"
    assert tr._unwrap_snapshot({"status": "pending"})["status"] == "pending"
