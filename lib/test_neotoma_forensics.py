"""Tests for pre-recovery diagnostic capture.

The load-bearing property is the ordering: recovery must not be reachable
without a snapshot having been written first. Most of these tests exist to
pin that down, plus the failure posture (a collector that raises must not
abort the capture, and the capture must not abort the recovery).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lib.neotoma_forensics import (
    Collector,
    capture,
    pending_snapshots,
    recover_with_capture,
)


def _collector(name: str, value):
    return Collector(name, lambda: value)


def test_capture_writes_a_snapshot_to_disk(tmp_path: Path):
    snap = capture("test", collectors=[_collector("a", {"x": 1})], directory=tmp_path)
    assert snap.path is not None and snap.path.exists()
    written = json.loads(snap.path.read_text())
    assert written["items"]["a"] == {"x": 1}
    assert written["reason"] == "test"


def test_a_failing_collector_does_not_abort_the_capture(tmp_path: Path):
    """Partial evidence beats an exception raised mid-incident."""

    def boom():
        raise RuntimeError("flyctl exploded")

    snap = capture(
        "test",
        collectors=[Collector("bad", boom), _collector("good", "kept")],
        directory=tmp_path,
    )
    assert snap.items["good"] == "kept"
    assert "flyctl exploded" in snap.errors["bad"]
    assert snap.path is not None and snap.path.exists()


def test_collectors_run_in_priority_order(tmp_path: Path):
    """The perishable evidence (fly logs) must be collected first."""
    order: list[str] = []
    collectors = [
        Collector("slow", lambda: order.append("slow"), priority=90),
        Collector("perishable", lambda: order.append("perishable"), priority=0),
        Collector("middle", lambda: order.append("middle"), priority=50),
    ]
    capture("test", collectors=collectors, directory=tmp_path)
    assert order == ["perishable", "middle", "slow"]


def test_budget_expiry_stops_collecting_but_still_writes(tmp_path: Path):
    """An outage must never be prolonged by the diagnostic tool."""
    collectors = [
        Collector("first", lambda: "got it", priority=0),
        Collector("second", lambda: "too late", priority=10),
    ]
    snap = capture(
        "test", collectors=collectors, budget_seconds=0.0, directory=tmp_path
    )
    assert snap.budget_expired is True
    assert snap.path is not None and snap.path.exists()
    assert "budget expired" in snap.errors["first"]


def test_recovery_runs_only_after_the_snapshot_is_on_disk(tmp_path: Path):
    """The core guarantee: capture strictly precedes recovery.

    Asserted by having the recovery callable observe the filesystem: if the
    snapshot were written afterwards, or concurrently, this would see nothing.
    """
    seen: list[int] = []

    def recovery():
        seen.append(len(list(tmp_path.glob("snapshot-*.json"))))
        return "restarted"

    snap, result = recover_with_capture(
        "wedged",
        recovery,
        collectors=[_collector("a", 1)],
        directory=tmp_path,
    )
    assert result == "restarted"
    assert seen == [1], "recovery ran before the snapshot was durable"
    assert snap.path is not None


def test_recovery_still_runs_when_every_collector_fails(tmp_path: Path):
    """Availability must not depend on diagnostics succeeding."""

    def boom():
        raise RuntimeError("no flyctl here")

    ran: list[str] = []
    snap, _ = recover_with_capture(
        "wedged",
        lambda: ran.append("recovered"),
        collectors=[Collector("bad", boom)],
        directory=tmp_path,
    )
    assert ran == ["recovered"]
    assert snap.errors["bad"]


def test_snapshot_is_never_observed_half_written(tmp_path: Path):
    """Write-then-rename: no `.partial` file survives a completed capture."""
    capture("test", collectors=[_collector("a", "b")], directory=tmp_path)
    assert list(tmp_path.glob("*.partial")) == []
    assert len(list(tmp_path.glob("snapshot-*.json"))) == 1


def test_analysis_flags_a_blocked_event_loop(tmp_path: Path):
    """/health does no DB work, so a slow /health localises the fault."""
    snap = capture(
        "test",
        collectors=[
            _collector(
                "event_loop_probe",
                {"internal_health_ms": 9400, "internal_health_status": 200},
            )
        ],
        directory=tmp_path,
    )
    analysis = snap.to_dict()["analysis"]
    assert analysis["event_loop_blocked"] is True
    assert "event loop" in analysis["event_loop_note"]


def test_analysis_does_not_flag_a_responsive_event_loop(tmp_path: Path):
    snap = capture(
        "test",
        collectors=[_collector("event_loop_probe", {"internal_health_ms": 3})],
        directory=tmp_path,
    )
    assert snap.to_dict()["analysis"]["event_loop_blocked"] is False


def test_analysis_separates_memory_pressure_from_event_loop_stalls(tmp_path: Path):
    """RSS well under capacity rules OOM out, even while the server is stalled."""
    snap = capture(
        "test",
        collectors=[
            _collector("proc_status", {"VmRSS_kB": 487844, "MemTotal_kB": 8134000})
        ],
        directory=tmp_path,
    )
    analysis = snap.to_dict()["analysis"]
    assert analysis["memory_pressure"] is False
    assert analysis["rss_pct_of_total"] < 10


def test_pending_snapshots_lists_newest_first(tmp_path: Path):
    for _ in range(2):
        capture("test", collectors=[_collector("a", 1)], directory=tmp_path)
    found = pending_snapshots(tmp_path)
    assert len(found) == len(list(tmp_path.glob("snapshot-*.json")))
    assert found == sorted(found, reverse=True)


def test_pending_snapshots_is_empty_when_nothing_captured(tmp_path: Path):
    assert pending_snapshots(tmp_path / "nope") == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
