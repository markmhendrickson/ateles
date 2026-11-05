"""Tests for fly_jobs_in_flight.py and the redeploy script's deferral branch."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS.parent.parent
APIS_DIR = REPO_ROOT / "execution" / "daemons" / "apis"
HELPER = SCRIPTS / "fly_jobs_in_flight.py"
REDEPLOY = SCRIPTS / "redeploy_daemons_from_main.sh"
FIXTURE_NONCE = "ledger-entry-value-that-must-stay-private"

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import fly_jobs_in_flight as helper  # noqa: E402


def _ledger(path: Path, *deadlines) -> Path:
    now = time.time()
    in_flight = {
        f"run-{i}": {"run_id": f"run-{i}", "owner": "o", "started_at": now - 60,
                     "deadline": d, "max_cost_usd": 1.0, "login": False,
                     "machine_id": "m", "lease_nonce": FIXTURE_NONCE}
        for i, d in enumerate(deadlines)
    }
    path.write_text(json.dumps({"in_flight": in_flight, "owners": {}}))
    return path


def _run_helper(ledger: Path) -> subprocess.CompletedProcess:
    env = {**os.environ, "APIS_FLY_LEDGER_FILE": str(ledger)}
    return subprocess.run([sys.executable, str(HELPER)], env=env,
                          capture_output=True, text=True, timeout=60)


def test_future_deadline_counts_as_in_flight(tmp_path):
    ledger = _ledger(tmp_path / "l.json", time.time() + 600, time.time() + 900)
    assert helper.count_in_flight(ledger, time.time()) == 2
    result = _run_helper(ledger)
    assert result.returncode == 0
    assert result.stdout.strip() == "2"


def test_past_deadline_does_not_defer(tmp_path):
    ledger = _ledger(tmp_path / "l.json", time.time() - 1, time.time() - 3600)
    assert helper.count_in_flight(ledger, time.time()) == 0
    result = _run_helper(ledger)
    assert result.returncode == 1
    assert result.stdout == ""
    # Only the live entry counts when the two are mixed.
    mixed = _ledger(tmp_path / "m.json", time.time() - 1, time.time() + 600)
    assert helper.count_in_flight(mixed, time.time()) == 1


@pytest.mark.parametrize("content", [
    None,                                   # missing
    "{not json",                            # corrupt
    "[]",                                   # wrong shape
    json.dumps({"in_flight": []}),
    json.dumps({"in_flight": {"a": "x"}}),
    json.dumps({"in_flight": {"a": {}}}),  # no deadline
    json.dumps({"in_flight": {"a": {"deadline": "soon"}}}),
    json.dumps({"in_flight": {"a": {"deadline": True}}}),
])
def test_missing_or_corrupt_ledger_does_not_defer(tmp_path, content):
    ledger = tmp_path / "l.json"
    if content is not None:
        ledger.write_text(content)
    assert helper.count_in_flight(ledger, time.time()) == 0
    result = _run_helper(ledger)
    assert result.returncode == 1
    assert result.stdout == ""


def test_prints_only_the_count(tmp_path):
    ledger = _ledger(tmp_path / "l.json", time.time() + 600)
    result = _run_helper(ledger)
    assert result.returncode == 0
    assert result.stdout == "1\n"
    assert FIXTURE_NONCE not in result.stdout + result.stderr
    assert "run-0" not in result.stdout + result.stderr


def test_ledger_path_matches_the_runner(monkeypatch, tmp_path):
    for p in (str(REPO_ROOT), str(APIS_DIR)):
        if p not in sys.path:
            monkeypatch.syspath_prepend(p)
    import fly_runner

    monkeypatch.delenv("APIS_FLY_LEDGER_FILE", raising=False)
    assert helper.ledger_path() == fly_runner.FlyConfig.from_env().ledger_path
    for value in (str(tmp_path / "x.json"), "~/elsewhere/ledger.json", "  "):
        monkeypatch.setenv("APIS_FLY_LEDGER_FILE", value)
        assert helper.ledger_path() == fly_runner.FlyConfig.from_env().ledger_path, value


# --- shell-level: the redeploy script's deferral branch ---------------------

_GIT_STUB = """#!/bin/bash
echo "git $*" >> "$CALLS"
case "$1" in
  rev-parse)
    for a in "$@"; do last="$a"; done
    if [ "$last" = "HEAD" ]; then echo aaaaaaa; else echo bbbbbbb; fi ;;
esac
exit 0
"""

_LAUNCHCTL_STUB = """#!/bin/bash
echo "launchctl $*" >> "$CALLS"
exit 0
"""


def _run_redeploy(tmp_path: Path, ledger: Path) -> tuple[subprocess.CompletedProcess, str]:
    stubs = tmp_path / "bin"
    stubs.mkdir()
    for name, body in (("git", _GIT_STUB), ("launchctl", _LAUNCHCTL_STUB)):
        (stubs / name).write_text(body)
        (stubs / name).chmod(0o755)
    rc = tmp_path / "rc"
    (rc / ".venv" / "bin").mkdir(parents=True)
    (rc / ".venv" / "bin" / "python3").write_text("#!/bin/sh\nexit 0\n")
    (rc / ".venv" / "bin" / "python3").chmod(0o755)
    calls = tmp_path / "calls.log"
    calls.write_text("")
    env = {**os.environ, "PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}",
           "CALLS": str(calls), "RC_DIR": str(rc),
           "LOCK_DIR": str(tmp_path / "lock.d"), "APIS_FLY_LEDGER_FILE": str(ledger)}
    result = subprocess.run(["bash", str(REDEPLOY)], env=env, capture_output=True,
                            text=True, timeout=120)
    return result, calls.read_text()


def test_redeploy_defers_while_a_fly_job_is_in_flight(tmp_path):
    ledger = _ledger(tmp_path / "l.json", time.time() + 600)
    result, calls = _run_redeploy(tmp_path, ledger)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "DEFERRED: 1 Fly job(s) in flight" in result.stdout
    assert "merge --ff-only" not in calls
    assert "kickstart" not in calls
    assert FIXTURE_NONCE not in result.stdout + result.stderr


def test_redeploy_proceeds_when_no_fly_job_is_in_flight(tmp_path):
    ledger = _ledger(tmp_path / "l.json", time.time() - 60)
    result, calls = _run_redeploy(tmp_path, ledger)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "DEFERRED" not in result.stdout
    assert "git merge --ff-only origin/main" in calls
    assert calls.count("launchctl kickstart -k") == 3


def test_redeploy_proceeds_when_the_ledger_is_missing(tmp_path):
    result, calls = _run_redeploy(tmp_path, tmp_path / "absent.json")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "git merge --ff-only origin/main" in calls
    assert "launchctl kickstart -k" in calls
