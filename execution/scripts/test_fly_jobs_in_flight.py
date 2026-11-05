"""Tests for fly_jobs_in_flight.py and the redeploy script's deferral branch."""

from __future__ import annotations

import asyncio
import fcntl
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


GRACE = helper.SETTLE_GRACE_SECONDS


def test_past_deadline_does_not_defer(tmp_path):
    ledger = _ledger(tmp_path / "l.json", time.time() - GRACE - 1, time.time() - 3600)
    assert helper.count_in_flight(ledger, time.time()) == 0
    result = _run_helper(ledger)
    assert result.returncode == 1
    assert result.stdout == ""
    # Only the live entry counts when the two are mixed.
    mixed = _ledger(tmp_path / "m.json", time.time() - GRACE - 1, time.time() + 600)
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


def test_corrupt_numeric_deadline_preserves_live_reservation(tmp_path):
    ledger = tmp_path / "l.json"
    # Written as raw JSON: 10**400 is a valid JSON integer that float() rejects.
    ledger.write_text(
        '{"in_flight": {"bad": {"deadline": 1' + "0" * 400 + ', "lease_nonce": "%s"},'
        ' "live": {"deadline": %r, "lease_nonce": "%s"}}}'
        % (FIXTURE_NONCE, time.time() + 600, FIXTURE_NONCE))
    assert helper.count_in_flight(ledger, time.time()) == 1
    result = _run_helper(ledger)
    assert result.returncode == 0
    assert result.stdout == "1\n"
    assert result.stderr == ""
    redeploy, calls = _run_redeploy(tmp_path, ledger)
    assert "DEFERRED: 1 Fly job(s) in flight" in redeploy.stdout
    assert "merge --ff-only" not in calls
    assert "kickstart" not in calls
    assert FIXTURE_NONCE not in redeploy.stdout + redeploy.stderr


def test_expired_reservation_within_grace_still_defers(tmp_path):
    # Expired four minutes ago: inside the five-minute settling grace.
    ledger = _ledger(tmp_path / "l.json", time.time() - 240)
    assert helper.count_in_flight(ledger, time.time()) == 1
    # An unconfirmed reservation counts on the same rule.
    data = json.loads(ledger.read_text())
    data["in_flight"]["run-0"]["unconfirmed"] = True
    ledger.write_text(json.dumps(data))
    assert helper.count_in_flight(ledger, time.time()) == 1
    result = _run_helper(ledger)
    assert result.returncode == 0 and result.stdout == "1\n"


def test_reservation_past_grace_does_not_defer(tmp_path):
    # Expired six minutes ago: past the five-minute settling grace.
    ledger = _ledger(tmp_path / "l.json", time.time() - 360)
    assert helper.count_in_flight(ledger, time.time()) == 0
    result = _run_helper(ledger)
    assert result.returncode == 1 and result.stdout == ""


def _lock_is_free(lock: Path) -> bool:
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "a+") as fh:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        return True


def test_lock_path_matches_the_runner(monkeypatch, tmp_path):
    for p in (str(REPO_ROOT), str(APIS_DIR)):
        if p not in sys.path:
            monkeypatch.syspath_prepend(p)
    import fly_runner

    ledger_file = tmp_path / "ledger.json"
    monkeypatch.setenv("APIS_FLY_LEDGER_FILE", str(ledger_file))
    config = fly_runner.FlyConfig.from_env()
    runner = fly_runner.FlyExecutor(config, client=object(),
                                  ledger=fly_runner.FlyLedger(config.ledger_path))
    lock = helper.admission_lock_path(helper.ledger_path())

    async def probe() -> bool:
        async with runner._admission():
            return _lock_is_free(lock)

    # While the runner holds admission, the helper's lock file is the locked one.
    assert asyncio.run(probe()) is False
    assert _lock_is_free(lock)


def test_hold_reports_busy_while_admission_is_held(tmp_path):
    ledger = _ledger(tmp_path / "l.json", time.time() + 600)
    lock = helper.admission_lock_path(ledger)
    env = {**os.environ, "APIS_FLY_LEDGER_FILE": str(ledger)}
    with open(lock, "a+") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        result = subprocess.run(
            [sys.executable, str(HELPER), "--hold", str(os.getpid()), "--wait", "0.3"],
            env=env, capture_output=True, text=True, timeout=60)
    assert result.stdout == "busy\n"
    assert _lock_is_free(lock)


def test_hold_releases_when_its_parent_dies(tmp_path):
    ledger = _ledger(tmp_path / "l.json")
    lock = helper.admission_lock_path(ledger)
    env = {**os.environ, "APIS_FLY_LEDGER_FILE": str(ledger)}
    parent = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"],
                              stdin=subprocess.PIPE)
    holder = subprocess.Popen([sys.executable, str(HELPER), "--hold", str(parent.pid)],
                              env=env, stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline() == "ok 0\n"
        assert not _lock_is_free(lock)
        parent.stdin.close()
        parent.wait(timeout=30)
        holder.wait(timeout=30)
        assert _lock_is_free(lock)
    finally:
        for proc in (parent, holder):
            if proc.poll() is None:
                proc.kill()


# --- shell-level: the redeploy script's deferral branch ---------------------

# The merge and kickstart stubs record whether a non-blocking flock on the
# admission lock is refused at that moment, i.e. whether a Fly launch could
# reserve while the checkout is updated and the daemons restart.
_PROBE = (
    '"$PROBE_PY" -c \'import fcntl,sys\n'
    'f=open(sys.argv[1],"a+")\n'
    'try:\n fcntl.flock(f.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB); print("acquired")\n'
    'except BlockingIOError:\n print("refused")\' "$ADMISSION_LOCK"'
)

_GIT_STUB = """#!/bin/bash
echo "git $*" >> "$CALLS"
case "$1" in
  rev-parse)
    for a in "$@"; do last="$a"; done
    if [ "$last" = "HEAD" ]; then echo aaaaaaa; else echo bbbbbbb; fi ;;
  merge)
    echo "admission during merge: $(%s)" >> "$CALLS" ;;
esac
exit 0
""" % _PROBE

_LAUNCHCTL_STUB = """#!/bin/bash
echo "launchctl $*" >> "$CALLS"
echo "admission during kickstart: $(%s)" >> "$CALLS"
exit 0
""" % _PROBE


def _setup_redeploy(tmp_path: Path, ledger: Path, *, extra_stubs=()) -> tuple[dict, Path]:
    stubs = tmp_path / "bin"
    stubs.mkdir(exist_ok=True)
    for name, body in (("git", _GIT_STUB), ("launchctl", _LAUNCHCTL_STUB), *extra_stubs):
        (stubs / name).write_text(body)
        (stubs / name).chmod(0o755)
    rc = tmp_path / "rc"
    (rc / ".venv" / "bin").mkdir(parents=True, exist_ok=True)
    (rc / ".venv" / "bin" / "python3").write_text("#!/bin/sh\nexit 0\n")
    (rc / ".venv" / "bin" / "python3").chmod(0o755)
    calls = tmp_path / "calls.log"
    calls.write_text("")
    env = {**os.environ, "PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}",
           "CALLS": str(calls), "RC_DIR": str(rc), "PROBE_PY": sys.executable,
           "ADMISSION_LOCK": str(helper.admission_lock_path(ledger)),
           "LOCK_DIR": str(tmp_path / "lock.d"), "APIS_FLY_LEDGER_FILE": str(ledger)}
    return env, calls


def _invoke(env: dict, calls: Path) -> tuple[subprocess.CompletedProcess, str]:
    calls.write_text("")
    result = subprocess.run(["bash", str(REDEPLOY)], env=env, capture_output=True,
                            text=True, timeout=120)
    return result, calls.read_text()


def _run_redeploy(tmp_path: Path, ledger: Path, **kw) -> tuple[subprocess.CompletedProcess, str]:
    return _invoke(*_setup_redeploy(tmp_path, ledger, **kw))


def test_redeploy_defers_while_a_fly_job_is_in_flight(tmp_path):
    ledger = _ledger(tmp_path / "l.json", time.time() + 600)
    result, calls = _run_redeploy(tmp_path, ledger)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "DEFERRED: 1 Fly job(s) in flight" in result.stdout
    assert "merge --ff-only" not in calls
    assert "kickstart" not in calls
    assert FIXTURE_NONCE not in result.stdout + result.stderr


def test_redeploy_proceeds_when_no_fly_job_is_in_flight(tmp_path):
    ledger = _ledger(tmp_path / "l.json", time.time() - 3600)
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


def test_redeploy_prevents_new_fly_admission_after_empty_snapshot(tmp_path):
    ledger = _ledger(tmp_path / "l.json")
    result, calls = _run_redeploy(tmp_path, ledger)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "git merge --ff-only origin/main" in calls
    assert "admission during merge: refused" in calls
    assert calls.count("admission during kickstart: refused") == 3
    assert "acquired" not in calls
    assert _lock_is_free(helper.admission_lock_path(ledger))


def test_deferred_run_releases_the_admission_lock(tmp_path):
    ledger = _ledger(tmp_path / "l.json", time.time() + 600)
    result, calls = _run_redeploy(tmp_path, ledger)
    assert "DEFERRED: 1 Fly job(s) in flight" in result.stdout
    assert "merge" not in calls
    assert _lock_is_free(helper.admission_lock_path(ledger))


def test_redeploy_retries_after_deferred_job_finishes(tmp_path):
    ledger = _ledger(tmp_path / "l.json", time.time() + 600)
    env, calls_file = _setup_redeploy(tmp_path, ledger)
    first, first_calls = _invoke(env, calls_file)
    assert "DEFERRED: 1 Fly job(s) in flight" in first.stdout
    assert "merge --ff-only" not in first_calls and "kickstart" not in first_calls
    _ledger(ledger)  # the job finished: its reservation is released
    second, second_calls = _invoke(env, calls_file)
    assert second.returncode == 0, second.stdout + second.stderr
    assert "DEFERRED" not in second.stdout
    assert "git merge --ff-only origin/main" in second_calls
    assert second_calls.count("launchctl kickstart -k") == 3


def test_redeploy_fails_open_when_the_holder_cannot_start(tmp_path):
    ledger = _ledger(tmp_path / "l.json", time.time() + 600)
    broken = ("python3", "#!/bin/sh\nexit 1\n")
    result, calls = _run_redeploy(tmp_path, ledger, extra_stubs=(broken,))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "WARNING: Fly in-flight check unavailable; deploying without it." in result.stdout
    assert "git merge --ff-only origin/main" in calls
    assert FIXTURE_NONCE not in result.stdout + result.stderr
