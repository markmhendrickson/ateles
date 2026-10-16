"""Tests for the shared Neotoma HTTP timeout (ateles#669).

Every Neotoma client in daemon_runtime used to carry its own literal (10s, 15s,
20s). Measured live on 2026-09-01, production answered POST /entities/query in
32.3s / 19.0s / 11.2s — so every one of those budgets could expire, and the
10s in agent_loader expired essentially always, turning each dispatch into a
stub load with no prompt.
"""

import neotoma_timeout as nt
import pytest
import os
import subprocess
import sys


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv("ATELES_NEOTOMA_TIMEOUT", raising=False)


def test_default_exceeds_measured_worst_case():
    """The default must clear the slowest read actually observed in production.

    A timeout below the server's real response time is not a timeout, it is an
    outage: it converts every load into the fallback path.
    """
    MEASURED_WORST_CASE_SECONDS = 32.3
    assert nt.neotoma_timeout() > MEASURED_WORST_CASE_SECONDS


def test_default_is_bounded_well_below_dispatch_timeout():
    """A read must never hang long enough to wedge the daemon that issued it.

    skill_runner dispatches with timeout=1800s; the read budget must stay far
    beneath that so a stuck read fails fast enough to be retried on the next
    timer tick.
    """
    assert nt.neotoma_timeout() < 300


def test_env_override_is_honoured(monkeypatch):
    monkeypatch.setenv("ATELES_NEOTOMA_TIMEOUT", "90")
    assert nt.neotoma_timeout() == 90.0


def test_env_override_accepts_float(monkeypatch):
    monkeypatch.setenv("ATELES_NEOTOMA_TIMEOUT", "12.5")
    assert nt.neotoma_timeout() == 12.5


def test_env_is_read_at_call_time_not_import_time(monkeypatch):
    """Daemons load their environment after import; the value must follow."""
    monkeypatch.setenv("ATELES_NEOTOMA_TIMEOUT", "77")
    assert nt.neotoma_timeout() == 77.0
    monkeypatch.setenv("ATELES_NEOTOMA_TIMEOUT", "88")
    assert nt.neotoma_timeout() == 88.0


@pytest.mark.parametrize("bad", ["", "   ", "abc", "10s", "None", "0", "-5", "nan_x", "nan", "inf"])
def test_invalid_config_fails_loudly(monkeypatch, bad):
    """An explicit invalid timeout must not silently change the read budget."""
    monkeypatch.setenv("ATELES_NEOTOMA_TIMEOUT", bad)
    with pytest.raises(ValueError, match="ATELES_NEOTOMA_TIMEOUT"):
        nt.neotoma_timeout()


def test_explicit_default_argument_respected(monkeypatch):
    assert nt.neotoma_timeout(default=5.0) == 5.0


def test_startup_validator_names_invalid_value(monkeypatch):
    monkeypatch.setenv("ATELES_NEOTOMA_TIMEOUT", "10s")
    with pytest.raises(SystemExit, match="ATELES_NEOTOMA_TIMEOUT='10s'"):
        nt.validate_neotoma_timeout_env()


def test_package_import_fails_before_daemon_start_on_invalid_config():
    env = dict(os.environ)
    env["ATELES_SKIP_DOTENV"] = "1"
    env["ATELES_NEOTOMA_TIMEOUT"] = "10s"
    process = subprocess.run(
        [sys.executable, "-c", "import lib.daemon_runtime"],
        env=env, capture_output=True, text=True, timeout=10,
    )
    assert process.returncode != 0
    assert "ATELES_NEOTOMA_TIMEOUT='10s'" in process.stderr
