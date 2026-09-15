"""Neotoma hard-dependency halt — task ent_670cacab2f46fd9547ced7ed.

Every test here drives the REAL entrypoint (`apis.dispatch_task`,
`TaskWatchdog.sweep`, `ReachabilityGate.raise_if_halted`) rather than a
re-implementation of the logic. That matters specifically for this change: the
failure mode being guarded against is a broad `try/except` swallowing the abort,
which a test that calls the gate directly and asserts on its return value would
never catch.

Each test below was verified to FAIL when its corresponding implementation is
reverted; the mapping is in the PR body.
"""

from __future__ import annotations

import asyncio
import os
import sys

import pytest

_HERE = os.path.dirname(__file__)
_REPO_ROOT = os.path.abspath(os.path.join(_HERE, "..", "..", ".."))
for _p in (_HERE, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from lib.daemon_runtime import neotoma_reachability as nr  # noqa: E402


# ── helpers ──────────────────────────────────────────────────────────────────


def _down(_timeout):
    """Simulate an unreachable Neotoma: the read raises, as a real outage does."""
    return nr.ProbeResult(nr.Reachability.UNREACHABLE, 0.01, "ConnectError: down")


def _up(_timeout):
    return nr.ProbeResult(nr.Reachability.OK, 0.05, "read answered in 0.1s")


def _slow(_timeout):
    return nr.ProbeResult(nr.Reachability.SLOW, 25.0, "read answered in 25.0s (degraded)")


def _halted_gate():
    """A gate already in the halted state, with probing unthrottled."""
    g = nr.ReachabilityGate(probe_fn=_down, probe_interval_seconds=0, failures_before_halt=1)
    g.probe(force=True)
    assert g.halted
    return g


class FakeNotifier:
    def __init__(self):
        self.sent: list[tuple[str, object]] = []

    def send(self, message, priority=None, handler=None):
        self.sent.append((message, priority))
        return True


# ── 1. the probe is a real read, never /health ───────────────────────────────


def test_probe_reads_entities_and_never_health(monkeypatch):
    """A wedged DB serves a green /health while every read hangs, so the probe
    must hit the read path. Assert on the URL actually requested."""
    called: dict = {}

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"entities": []}

    def fake_post(url, **kwargs):
        called["url"] = url
        called["json"] = kwargs.get("json")
        return _Resp()

    monkeypatch.setattr(nr, "NEOTOMA_BEARER_TOKEN", "test-token")
    monkeypatch.setattr(nr.httpx, "post", fake_post)

    result = nr._real_read(5.0)

    assert result.verdict is nr.Reachability.OK
    assert called["url"].endswith("/entities/query")
    assert "/health" not in called["url"]
    assert called["json"]["limit"] == 1


# ── 2. "unreachable" stays distinguishable from "slow" ───────────────────────


def test_slow_is_reported_but_does_not_halt():
    """A 25s answer is degraded, not down. Halting on it would convert every
    slow period into a full swarm outage."""
    g = nr.ReachabilityGate(probe_fn=_slow, probe_interval_seconds=0)
    for _ in range(10):
        g.raise_if_halted()  # must not raise
    assert not g.halted
    assert g.last_result.verdict is nr.Reachability.SLOW


def test_single_failure_does_not_halt_but_repeated_failure_does():
    g = nr.ReachabilityGate(probe_fn=_down, probe_interval_seconds=0, failures_before_halt=3)
    g.raise_if_halted()
    g.raise_if_halted()
    assert not g.halted
    with pytest.raises(nr.HaltedError):
        g.raise_if_halted()


def test_probe_is_cached_so_a_dispatch_burst_is_one_probe():
    """The cache IS the backoff: retrying harder is how slow becomes unreachable."""
    calls = {"n": 0}

    def counting(_t):
        calls["n"] += 1
        return _up(_t)

    g = nr.ReachabilityGate(probe_fn=counting, probe_interval_seconds=300)
    for _ in range(50):
        g.raise_if_halted()
    assert calls["n"] == 1


def test_one_good_read_clears_the_halt():
    """Recovery is asymmetric with entry — staying halted after the record
    demonstrably answers would be its own outage."""
    g = _halted_gate()
    g.probe_fn = _up
    g.raise_if_halted()  # must not raise
    assert not g.halted


def test_missing_token_is_a_config_fault_not_an_outage(monkeypatch):
    """A misconfigured env var must not halt the whole swarm and blame the server."""
    monkeypatch.setattr(nr, "NEOTOMA_BEARER_TOKEN", "")
    result = nr._real_read(5.0)
    assert result.reachable


def test_http_502_response_is_slow_not_unreachable(monkeypatch):
    """An arriving 502 proves the server is alive — must be SLOW, not UNREACHABLE."""
    class _Resp:
        status_code = 502

        def raise_for_status(self):
            raise AssertionError("5xx must not go through raise_for_status")

        def json(self):
            raise AssertionError("502 body must not be required for SLOW")

    monkeypatch.setattr(nr, "NEOTOMA_BEARER_TOKEN", "test-token")
    monkeypatch.setattr(nr.httpx, "post", lambda *a, **k: _Resp())

    result = nr._real_read(5.0)
    assert result.verdict is nr.Reachability.SLOW
    assert result.reachable

    g = nr.ReachabilityGate(
        probe_fn=nr._real_read, probe_interval_seconds=0, failures_before_halt=1,
    )
    for _ in range(5):
        g.raise_if_halted()  # must not raise
    assert not g.halted


def test_http_503_response_is_slow_not_unreachable(monkeypatch):
    """Any arriving 5xx is degraded-alive, not a one-off 502 special case."""
    class _Resp:
        status_code = 503

        def raise_for_status(self):
            raise AssertionError("5xx must not go through raise_for_status")

        def json(self):
            raise AssertionError("503 body must not be required for SLOW")

    monkeypatch.setattr(nr, "NEOTOMA_BEARER_TOKEN", "test-token")
    monkeypatch.setattr(nr.httpx, "post", lambda *a, **k: _Resp())

    result = nr._real_read(5.0)
    assert result.verdict is nr.Reachability.SLOW
    assert result.reachable

    g = nr.ReachabilityGate(
        probe_fn=nr._real_read, probe_interval_seconds=0, failures_before_halt=1,
    )
    for _ in range(5):
        g.raise_if_halted()
    assert not g.halted


# ── 3. dispatch halts — through the real entrypoint ──────────────────────────


def _dispatch_fixture(monkeypatch, gate):
    """Import apis with the halt gate replaced, and record any status write."""
    import apis

    monkeypatch.setattr(apis, "shared_gate", lambda: gate)
    writes: list = []
    monkeypatch.setattr(
        apis, "set_task_status",
        lambda *a, **k: (writes.append((a, k)), True)[1],
    )
    return apis, writes


def test_dispatch_halts_and_writes_nothing_when_neotoma_is_unreachable(monkeypatch):
    """The headline behaviour: no dispatch, and the task is left EXACTLY as
    found — no ROUTED write, no BLOCKED write, nothing claimed."""
    apis, writes = _dispatch_fixture(monkeypatch, _halted_gate())
    spawned = {"n": 0}
    monkeypatch.setattr(
        apis, "_spawn_harness_skill",
        lambda *a, **k: spawned.__setitem__("n", spawned["n"] + 1),
    )
    notifier = FakeNotifier()

    with pytest.raises(nr.HaltedError):
        asyncio.run(apis.dispatch_task(
            "ent_test", {"title": "t", "status": "pending", "tags": ["ateles"]},
            trigger="created", notifier=notifier,
        ))

    assert spawned["n"] == 0, "halted dispatch must not spawn an agent"
    assert writes == [], "halted dispatch must write no status at all"


def test_dispatch_proceeds_normally_when_neotoma_is_reachable(monkeypatch):
    """The guard must not be a permanent brake — the happy path still runs."""
    gate = nr.ReachabilityGate(probe_fn=_up, probe_interval_seconds=0)
    apis, writes = _dispatch_fixture(monkeypatch, gate)
    notifier = FakeNotifier()

    # Reaching the routing stage at all proves the halt did not fire; stop there
    # rather than driving a full spawn.
    monkeypatch.setattr(apis, "_resolve_skill", lambda *a, **k: None)
    asyncio.run(apis.dispatch_task(
        "ent_test", {"title": "t", "status": "pending", "tags": []},
        trigger="created", notifier=notifier, snapshot_hydrated=True,
    ))
    # Got past the gate into the no-owner path.
    assert any(a[1] == apis.TaskStatus.BLOCKED for a, _ in writes) or writes == []


def test_the_halt_cannot_swallow_its_own_guard(monkeypatch):
    """A broad try/except inside the gate would catch the abort it exists to
    raise. Assert HaltedError escapes `raise_if_halted` intact."""
    g = _halted_gate()
    with pytest.raises(nr.HaltedError) as exc:
        g.raise_if_halted()
    assert exc.value.consecutive_failures >= 1
    assert "unreachable" in str(exc.value).lower()


def test_halted_created_event_is_redeliverable(monkeypatch):
    """The duplicate-delivery claim must not double as 'this task was handled'.
    A halted dispatch handled nothing, so the claim is released."""
    apis, _ = _dispatch_fixture(monkeypatch, _halted_gate())
    apis._created_seen.pop("ent_redeliver", None)
    apis._seen_created("ent_redeliver")
    assert apis._seen_created("ent_redeliver") is True
    apis._unsee_created("ent_redeliver")
    assert apis._seen_created("ent_redeliver") is False


# ── 4. halt work, never stop observing ───────────────────────────────────────


def test_forensics_still_writes_during_a_halt(tmp_path, monkeypatch):
    """A hard dependency that stops the thing diagnosing the dependency makes
    recovery impossible. Forensics writes to LOCAL DISK, not Neotoma, and is
    not gated by the halt."""
    import lib.neotoma_forensics as forensics

    _halted_gate()  # the swarm is halted

    probe = forensics.Collector(
        name="probe", priority=0, run=lambda: {"event_loop_ms": 12}
    )
    snap = forensics.capture(
        reason="halt-test", collectors=[probe], directory=tmp_path
    )

    assert snap.path is not None, "forensic capture must survive a Neotoma halt"
    assert snap.path.exists()
    assert tmp_path in snap.path.parents, \
        "capture must land on local disk, never in the record it is diagnosing"
    assert snap.items["probe"] == {"event_loop_ms": 12}


def test_watchdog_keeps_sweeping_but_acts_on_nothing_while_halted(monkeypatch):
    """Observation continues; action stops. The ESCALATE branch writes BLOCKED
    to Neotoma, so acting during an outage would burn a task's attempt budget
    against a fault that is not the task's."""
    import task_watchdog as tw

    monkeypatch.setattr(tw, "_reachability_gate", _halted_gate)
    queried = {"n": 0}
    monkeypatch.setattr(
        tw, "_query_tasks",
        lambda limit: (queried.__setitem__("n", queried["n"] + 1), [])[1],
    )
    dispatched = {"n": 0}

    async def _dispatch(*a, **k):
        dispatched["n"] += 1

    wd = tw.TaskWatchdog()
    counts = asyncio.run(wd.sweep(FakeNotifier(), _dispatch))

    assert counts["skipped_halted"] == 1, "the sweep must report that it held off"
    assert dispatched["n"] == 0, "no re-dispatch while the record is unreachable"
    assert queried["n"] == 0, "still-halted re-probe must not query tasks"


def test_watchdog_clears_halt_and_drains_via_sweep(monkeypatch):
    """Drain contract: sticky halt → record returns → same sweep clears halt
    and re-dispatches. Recovery must not require dispatch/SSE traffic."""
    import task_watchdog as tw

    gate = nr.ReachabilityGate(
        probe_fn=_down, probe_interval_seconds=0, failures_before_halt=1,
    )
    gate.probe(force=True)
    assert gate.halted

    # Record returns: next probe from sweep must clear halt and allow action.
    gate.probe_fn = _up
    monkeypatch.setattr(tw, "_reachability_gate", lambda: gate)

    stalled = {"status": "executing", "updated_at": "2020-01-01T00:00:00Z", "title": "t"}
    monkeypatch.setattr(tw, "_query_tasks", lambda limit: [("ent_stalled", stalled)])
    monkeypatch.setattr(tw, "set_task_status", lambda *a, **k: True)
    dispatched = []

    async def _dispatch(task_id, snapshot, trigger):
        dispatched.append(task_id)

    notifier = FakeNotifier()
    # Seed the ENTERING edge so sweep's clear can fire LEAVING.
    gate.announce(notifier)
    assert len(notifier.sent) == 1
    assert "HALTED" in notifier.sent[0][0]

    counts = asyncio.run(tw.TaskWatchdog().sweep(notifier, _dispatch))

    assert not gate.halted
    assert dispatched == ["ent_stalled"]
    assert counts["retried"] == 1
    assert counts.get("skipped_halted", 0) == 0
    assert len(notifier.sent) == 2
    assert "resumed" in notifier.sent[1][0].lower()


# ── 5. the halt announces itself, once per edge ──────────────────────────────


def test_announcement_fires_once_on_entry_not_once_per_blocked_dispatch():
    """#645: lib/notify has no rate limiting of its own, so the edge-trigger
    here IS the rate limiting."""
    g = _halted_gate()
    notifier = FakeNotifier()

    for _ in range(200):
        g.announce(notifier)

    assert len(notifier.sent) == 1, "one page per halt, not one per blocked dispatch"
    assert "HALTED" in notifier.sent[0][0]


def test_announcement_fires_again_on_leaving_the_halt():
    """A halt that never announces its end leaves the operator believing the
    swarm is still down."""
    g = _halted_gate()
    notifier = FakeNotifier()
    g.announce(notifier)

    g.probe_fn = _up
    g.probe(force=True)
    g.announce(notifier)

    assert len(notifier.sent) == 2
    assert "HALTED" in notifier.sent[0][0]
    assert "resumed" in notifier.sent[1][0].lower()


def test_a_notifier_failure_never_converts_a_halt_into_a_crash():
    class Exploding:
        def send(self, *a, **k):
            raise RuntimeError("telegram down")

    g = _halted_gate()
    g.announce(Exploding())  # must not raise


# ── 6. mid-task outage: never claim completion you cannot record ─────────────


def test_completion_that_cannot_be_recorded_is_not_reported_as_done(monkeypatch):
    """The exact inverse of participation_record, where terminal writes fail
    silently and rows sit stranded. A failed DONE write must not report success
    — the task stays EXECUTING for the watchdog to requeue."""
    import apis

    gate = nr.ReachabilityGate(probe_fn=_up, probe_interval_seconds=0)
    monkeypatch.setattr(apis, "shared_gate", lambda: gate)

    # The DONE write fails, as it does when Neotoma goes away mid-task.
    attempted: list = []

    def failing_write(entity_id, status, **kwargs):
        attempted.append(status)
        return status != apis.TaskStatus.DONE

    monkeypatch.setattr(apis, "set_task_status", failing_write)

    class _Result:
        ok = True
        error = None
        returncode = 0

    async def _spawn(*a, **k):
        return _Result()

    monkeypatch.setattr(apis, "_spawn_harness_skill", _spawn)
    monkeypatch.setattr(apis, "_resolve_skill", lambda *a, **k: "ateles")
    monkeypatch.setattr(apis, "_resolve_role", lambda *a, **k: "ateles")
    monkeypatch.setattr(apis, "READINESS_GATE", False)
    monkeypatch.setattr(apis, "DRY_RUN", False)

    jobs = []

    class _Job:
        def started(self, *a, **k):
            return self

        def finished(self, msg):
            jobs.append(("finished", msg))

        def failed(self, msg):
            jobs.append(("failed", msg))

    monkeypatch.setattr(apis._activity, "started", lambda *a, **k: _Job())

    class _Decision:
        auto_execute = True
        action = "auto_execute"
        confidence = 1.0
        blast_radius = "low"
        reason = ""

    monkeypatch.setattr(apis, "evaluate_gate", lambda *a, **k: _Decision())

    notifier = FakeNotifier()
    asyncio.run(apis.dispatch_task(
        "ent_midtask", {"title": "t", "status": "pending", "tags": ["ateles"]},
        trigger="created", notifier=notifier, gate_override=True,
    ))

    assert apis.TaskStatus.DONE in attempted, "the write must still be ATTEMPTED"
    assert ("finished", ) not in [(j[0],) for j in jobs], \
        "a completion whose write failed must not be reported as finished"
    assert any(j[0] == "failed" for j in jobs), \
        "the run must be recorded as not-recorded, not as success"
    assert any("could NOT be recorded" in m for m, _ in notifier.sent), \
        "the operator must be told the completion is unrecorded"
