"""Tests for the Neotoma degradation watchdog.

The cases that matter most are the ones that reproduce the incident:
a 200 from /health while reads time out must NOT be reported as healthy, and
must be distinguished from a genuinely wedged instance, because the remedies
are opposite.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from execution.daemons.nyctea import nyctea as ny  # noqa: E402
from execution.daemons.nyctea import probe as pr  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("NYCTEA_STATE_DIR", str(tmp_path / "state"))


class FakeNotifier:
    def __init__(self):
        self.sent = []

    def send(self, message, priority=None, handler=""):
        self.sent.append({"message": message, "priority": priority, "handler": handler})
        return True


# ── probe verdicts ───────────────────────────────────────────────────────────


def test_healthy_when_read_is_fast(monkeypatch):
    monkeypatch.setattr(pr, "_post", lambda *a, **k: (200, 0.12))
    r = pr.probe("https://x", "tok")
    assert r.verdict is pr.Verdict.HEALTHY
    assert r.read_latency == 0.12


def test_degraded_when_read_is_slow_but_succeeds(monkeypatch):
    monkeypatch.setattr(pr, "_post", lambda *a, **k: (200, 16.0))
    r = pr.probe("https://x", "tok")
    assert r.verdict is pr.Verdict.DEGRADED
    assert "16.0s" in r.detail


def test_saturated_is_the_incident_shape(monkeypatch):
    """/health 200, read times out. The exact live measurement on 2026-09-01.

    This must NOT be HEALTHY (that is ateles#577) and must NOT be WEDGED
    (that would recommend a restart against a live instance).
    """

    def _timeout(*a, **k):
        raise TimeoutError("read timed out")

    monkeypatch.setattr(pr, "_post", _timeout)
    monkeypatch.setattr(pr, "check_liveness", lambda *a, **k: (True, 0.89))

    r = pr.probe("https://x", "tok")
    assert r.verdict is pr.Verdict.SATURATED
    assert r.liveness_ok is True
    assert "do NOT restart" in r.detail.lower() or "not restart" in r.detail.lower()


def test_wedged_when_reads_and_liveness_both_fail(monkeypatch):
    def _timeout(*a, **k):
        raise TimeoutError("read timed out")

    monkeypatch.setattr(pr, "_post", _timeout)
    monkeypatch.setattr(pr, "check_liveness", lambda *a, **k: (False, None))

    r = pr.probe("https://x", "tok")
    assert r.verdict is pr.Verdict.WEDGED


def test_health_200_alone_never_yields_healthy(monkeypatch):
    """Liveness must never be able to produce a HEALTHY verdict on its own."""

    def _timeout(*a, **k):
        raise TimeoutError("boom")

    monkeypatch.setattr(pr, "_post", _timeout)
    monkeypatch.setattr(pr, "check_liveness", lambda *a, **k: (True, 0.01))
    assert pr.probe("https://x", "tok").verdict is not pr.Verdict.HEALTHY


def test_auth_failure_is_not_reported_as_an_outage(monkeypatch):
    """A 401 is the watchdog's own fault, not Neotoma being down."""

    def _401(*a, **k):
        raise urllib.error.HTTPError("u", 401, "Unauthorized", {}, None)

    monkeypatch.setattr(pr, "_post", _401)
    monkeypatch.setattr(pr, "check_liveness", lambda *a, **k: (True, 0.1))
    r = pr.probe("https://x", "tok")
    assert r.verdict is pr.Verdict.DEGRADED
    assert "credential" in r.detail.lower()


def test_severity_ordering():
    assert pr.severity(pr.Verdict.HEALTHY) < pr.severity(pr.Verdict.DEGRADED)
    assert pr.severity(pr.Verdict.DEGRADED) < pr.severity(pr.Verdict.SATURATED)
    assert pr.severity(pr.Verdict.SATURATED) < pr.severity(pr.Verdict.WEDGED)


# ── escalation policy ────────────────────────────────────────────────────────


def _force(monkeypatch, verdict):
    monkeypatch.setattr(
        ny, "probe", lambda *a, **k: pr.ProbeResult(verdict=verdict, detail="x")
    )


def test_saturation_escalates_at_critical_so_it_bypasses_quiet_hours(monkeypatch):
    """The 34-escalations-into-a-silent-digest failure must not recur."""
    from lib.notify.notifier import Priority

    _force(monkeypatch, pr.Verdict.SATURATED)
    n = FakeNotifier()
    wd = ny.Watchdog("https://x", "tok", notifier=n)

    for _ in range(ny.SUSTAIN_CYCLES):
        wd.run_once()

    assert n.sent, "saturation must escalate"
    assert n.sent[-1]["priority"] is Priority.CRITICAL


def test_single_slow_cycle_does_not_page(monkeypatch):
    _force(monkeypatch, pr.Verdict.DEGRADED)
    n = FakeNotifier()
    wd = ny.Watchdog("https://x", "tok", notifier=n)
    wd.run_once()
    assert n.sent == [], "one bad cycle is not a sustained condition"


def test_does_not_repage_every_cycle_for_the_same_condition(monkeypatch):
    _force(monkeypatch, pr.Verdict.SATURATED)
    n = FakeNotifier()
    wd = ny.Watchdog("https://x", "tok", notifier=n)
    for _ in range(12):
        wd.run_once()
    assert len(n.sent) == 1, "a bypass that repeats every cycle becomes noise"


def test_escalates_again_when_condition_worsens(monkeypatch):
    n = FakeNotifier()
    wd = ny.Watchdog("https://x", "tok", notifier=n)
    _force(monkeypatch, pr.Verdict.DEGRADED)
    for _ in range(ny.SUSTAIN_CYCLES):
        wd.run_once()
    before = len(n.sent)
    _force(monkeypatch, pr.Verdict.WEDGED)
    for _ in range(ny.SUSTAIN_CYCLES):
        wd.run_once()
    assert len(n.sent) > before, "a worse verdict is new information"


def test_recovery_is_announced_once(monkeypatch):
    n = FakeNotifier()
    wd = ny.Watchdog("https://x", "tok", notifier=n)
    _force(monkeypatch, pr.Verdict.SATURATED)
    for _ in range(ny.SUSTAIN_CYCLES):
        wd.run_once()
    _force(monkeypatch, pr.Verdict.HEALTHY)
    wd.run_once()
    wd.run_once()
    assert sum("recovered" in s["message"] for s in n.sent) == 1


def test_healthy_from_the_start_sends_nothing(monkeypatch):
    _force(monkeypatch, pr.Verdict.HEALTHY)
    n = FakeNotifier()
    wd = ny.Watchdog("https://x", "tok", notifier=n)
    for _ in range(5):
        wd.run_once()
    assert n.sent == []


def test_notifier_failure_does_not_kill_the_watchdog(monkeypatch):
    class Broken:
        def send(self, *a, **k):
            raise RuntimeError("telegram down")

    _force(monkeypatch, pr.Verdict.SATURATED)
    wd = ny.Watchdog("https://x", "tok", notifier=Broken())
    for _ in range(ny.SUSTAIN_CYCLES):
        wd.run_once()  # must not raise
    assert ny.read_heartbeat() is not None


def test_probe_exception_does_not_kill_the_loop(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(ny, "probe", _boom)
    wd = ny.Watchdog("https://x", "tok", notifier=FakeNotifier())
    with pytest.raises(RuntimeError):
        wd.run_once()  # run_once propagates; run_forever is what swallows


# ── self-check ───────────────────────────────────────────────────────────────


def test_heartbeat_written_even_when_healthy(monkeypatch):
    """A watchdog that only writes state when unhappy cannot prove it is alive."""
    _force(monkeypatch, pr.Verdict.HEALTHY)
    ny.Watchdog("https://x", "tok", notifier=FakeNotifier()).run_once()
    hb = ny.read_heartbeat()
    assert hb is not None and hb["verdict"] == "healthy"


def test_self_check_fails_when_never_run():
    ok, detail = ny.self_check()
    assert not ok and "no heartbeat" in detail


def test_self_check_detects_a_dead_watchdog(monkeypatch):
    _force(monkeypatch, pr.Verdict.HEALTHY)
    ny.Watchdog("https://x", "tok", notifier=FakeNotifier()).run_once()
    ok, _ = ny.self_check()
    assert ok
    stale = time.time() + ny.STALE_AFTER_SECONDS + 60
    ok, detail = ny.self_check(now=stale)
    assert not ok and "not running" in detail


def test_self_check_exit_code_contract(monkeypatch, capsys):
    _force(monkeypatch, pr.Verdict.HEALTHY)
    ny.Watchdog("https://x", "tok", notifier=FakeNotifier()).run_once()
    assert ny.main(["--self-check", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_main_refuses_to_run_without_a_token(monkeypatch, capsys):
    """No token means the probe measures auth, not the DB — a fast 401 would
    look like health. Refuse rather than report a false green."""
    monkeypatch.delenv("NEOTOMA_BEARER_TOKEN", raising=False)
    assert ny.main(["--once"]) == 2


# ── load shedding ────────────────────────────────────────────────────────────


def test_read_slot_caps_concurrency(monkeypatch):
    import lib.neotoma_concurrency as nc

    monkeypatch.setenv("NEOTOMA_MAX_CONCURRENT_READS", "2")
    monkeypatch.setattr(nc, "_semaphore", None)
    monkeypatch.setattr(nc, "_semaphore_cap", None)

    with nc.neotoma_read_slot() as a, nc.neotoma_read_slot() as b:
        assert a and b
        with nc.neotoma_read_slot(timeout=0.05) as c:
            assert c is False, "third concurrent reader must be shed"


def test_read_slot_fails_open_rather_than_raising(monkeypatch):
    """A limiter that throws during an incident makes a slow system a broken one."""
    import lib.neotoma_concurrency as nc

    monkeypatch.setenv("NEOTOMA_MAX_CONCURRENT_READS", "1")
    monkeypatch.setattr(nc, "_semaphore", None)
    monkeypatch.setattr(nc, "_semaphore_cap", None)
    with nc.neotoma_read_slot():
        with nc.neotoma_read_slot(timeout=0.01) as got:
            assert got is False  # proceeds, does not raise
