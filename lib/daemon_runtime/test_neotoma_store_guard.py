"""Pacing and transient-failure policy for a pinned daemon's stores."""

import json
import os

import pytest

from lib.daemon_runtime import neotoma_store_guard as guard
from lib.daemon_runtime.pinned_cli_identity import PIN_ENV


@pytest.fixture
def pinned(tmp_path, monkeypatch):
    monkeypatch.setenv(PIN_ENV, "piculet@ateles-swarm")
    monkeypatch.setenv("NEOTOMA_STORE_PACE_FILE", str(tmp_path / "pace.json"))
    monkeypatch.delenv("NEOTOMA_STORE_MIN_INTERVAL_SECONDS", raising=False)
    return tmp_path / "pace.json"


class Clock:
    """A fake clock whose sleep advances it, so gaps are measured exactly."""

    def __init__(self, start=1_000.0):
        self.now = start
        self.sleeps = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def _stores(n, clock, call_cost=1.0):
    """Run n paced stores; return the clock time at which each began."""
    starts = []

    def one():
        starts.append(clock.now)
        clock.now += call_cost

    for _ in range(n):
        guard.paced(one, clock=clock.time, sleep=clock.sleep)
    return starts


def test_stores_are_at_least_twelve_seconds_apart_by_default(pinned):
    clock = Clock()
    starts = _stores(4, clock)
    gaps = [b - a for a, b in zip(starts, starts[1:])]
    # Measured from the END of the previous store (cost 1s): start gap is 12+1.
    assert all(gap >= 12 for gap in gaps), gaps
    assert guard.min_interval() == 12


def test_the_interval_is_configurable(pinned, monkeypatch):
    monkeypatch.setenv("NEOTOMA_STORE_MIN_INTERVAL_SECONDS", "30")
    clock = Clock()
    starts = _stores(3, clock, call_cost=0)
    assert [b - a for a, b in zip(starts, starts[1:])] == [30, 30]


def test_the_gap_holds_across_processes_via_the_stamp_file(pinned):
    """A second 'process' (fresh Clock, same stamp file) still waits."""
    first = Clock(start=5_000.0)
    _stores(1, first, call_cost=0)
    second = Clock(start=5_003.0)  # 3 s after the first store ended
    starts = _stores(1, second, call_cost=0)
    assert starts == [5_012.0]
    assert json.loads(pinned.read_text())["last"] == 5_012.0


def test_a_failing_store_still_stamps_so_the_next_one_waits(pinned):
    clock = Clock()

    def boom():
        raise RuntimeError("store failed")

    with pytest.raises(RuntimeError):
        guard.paced(boom, clock=clock.time, sleep=clock.sleep)
    starts = _stores(1, clock, call_cost=0)
    assert starts[0] - 1_000.0 >= 12


def test_an_unpinned_run_is_not_paced(tmp_path, monkeypatch):
    monkeypatch.delenv(PIN_ENV, raising=False)
    monkeypatch.setenv("NEOTOMA_STORE_PACE_FILE", str(tmp_path / "pace.json"))
    clock = Clock()
    _stores(3, clock, call_cost=0)
    assert clock.sleeps == []
    assert not (tmp_path / "pace.json").exists()


@pytest.mark.parametrize(
    "text",
    [
        "HTTP 502 Bad Gateway",
        "503 Service Unavailable",
        "Error: request timed out",
        "store timed out",
        "TypeError: fetch failed",
        "HTTP 504 Gateway Timeout",
        "Error: connect ECONNREFUSED 127.0.0.1:3080",
        "ECONNRESET",
    ],
)
def test_transient_failures_are_recognised(text):
    assert guard.is_transient_failure(text)


@pytest.mark.parametrize(
    "text",
    [
        "ERR_IDEMPOTENCY_MISMATCH",
        "403 forbidden: capability_denied",
        "400 bad request",
        "unknown option --timeout 30",
        "audio is 503 seconds long",
        "TRANSCRIBE_TIMEOUT setting ignored",
        "fetch failed to parse the manifest",
    ],
)
def test_permanent_failures_are_not_transient(text):
    assert not guard.is_transient_failure(text)


def test_retry_delay_defaults_to_two_minutes_and_is_configurable(monkeypatch):
    monkeypatch.delenv("NEOTOMA_STORE_RETRY_DELAY_SECONDS", raising=False)
    assert guard.retry_delay() == 120
    monkeypatch.setenv("NEOTOMA_STORE_RETRY_DELAY_SECONDS", "7")
    assert guard.retry_delay() == 7


# ── State-file hardening ────────────────────────────────────────────────────


def test_state_is_private_and_atomic(tmp_path, monkeypatch):
    import stat

    monkeypatch.setenv(PIN_ENV, "piculet@ateles-swarm")
    stamp = tmp_path / "state" / "pace.json"
    monkeypatch.setenv("NEOTOMA_STORE_PACE_FILE", str(stamp))
    clock = Clock()
    _stores(2, clock, call_cost=0)
    assert stat.S_IMODE(stamp.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(stamp.stat().st_mode) == 0o600
    lock = stamp.with_suffix(".json.lock")
    assert stat.S_IMODE(lock.stat().st_mode) == 0o600
    assert sorted(p.name for p in stamp.parent.iterdir()) == ["pace.json", "pace.json.lock"]  # no temp left
    assert json.loads(stamp.read_text())["last"] > 0


def test_a_symlinked_stamp_file_is_refused_not_followed(tmp_path, monkeypatch):
    monkeypatch.setenv(PIN_ENV, "piculet@ateles-swarm")
    victim = tmp_path / "victim.txt"
    victim.write_text("untouched")
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    (state / "pace.json").symlink_to(victim)
    monkeypatch.setenv("NEOTOMA_STORE_PACE_FILE", str(state / "pace.json"))
    ran = []
    with pytest.raises(guard.StoreDeferred):
        guard.paced(lambda: ran.append(1), clock=Clock().time, sleep=lambda s: None)
    assert ran == [] and victim.read_text() == "untouched"


def test_a_symlinked_lock_file_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv(PIN_ENV, "piculet@ateles-swarm")
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    (tmp_path / "victim").write_text("x")
    (state / "pace.json.lock").symlink_to(tmp_path / "victim")
    monkeypatch.setenv("NEOTOMA_STORE_PACE_FILE", str(state / "pace.json"))
    with pytest.raises(guard.StoreDeferred):
        guard.paced(lambda: None, clock=Clock().time, sleep=lambda s: None)


def test_a_symlinked_state_directory_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv(PIN_ENV, "piculet@ateles-swarm")
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "link").symlink_to(real)
    monkeypatch.setenv("NEOTOMA_STORE_PACE_FILE", str(tmp_path / "link" / "pace.json"))
    with pytest.raises(guard.StoreDeferred):
        guard.paced(lambda: None, clock=Clock().time, sleep=lambda s: None)


def test_a_loose_state_directory_is_tightened(tmp_path, monkeypatch):
    import stat

    monkeypatch.setenv(PIN_ENV, "piculet@ateles-swarm")
    state = tmp_path / "state"
    state.mkdir(mode=0o755)
    monkeypatch.setenv("NEOTOMA_STORE_PACE_FILE", str(state / "pace.json"))
    guard.paced(lambda: None, clock=Clock().time, sleep=lambda s: None)
    assert stat.S_IMODE(state.stat().st_mode) == 0o700


def test_the_stamp_is_replaced_atomically_never_written_in_place(tmp_path, monkeypatch):
    monkeypatch.setenv(PIN_ENV, "piculet@ateles-swarm")
    stamp = tmp_path / "state" / "pace.json"
    monkeypatch.setenv("NEOTOMA_STORE_PACE_FILE", str(stamp))
    replaced = []
    real = os.replace
    monkeypatch.setattr(guard.os, "replace", lambda a, b: (replaced.append((os.path.basename(a), os.path.basename(b))), real(a, b)))
    guard.paced(lambda: None, clock=Clock().time, sleep=lambda s: None)
    assert replaced and all(dst == "pace.json" and src != "pace.json" for src, dst in replaced)
