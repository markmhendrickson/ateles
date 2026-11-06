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
    if [ "$last" = "HEAD" ] || [ -n "${SAME_HEAD:-}" ]; then echo aaaaaaa; else echo bbbbbbb; fi ;;
  merge)
    echo "admission during merge: $(%s)" >> "$CALLS"
    [ -n "${MERGE_DELAY:-}" ] && sleep "$MERGE_DELAY" >/dev/null 2>&1
    exit "${MERGE_RC:-0}" ;;
  diff)
    exit "${DIFF_RC:-0}" ;;
  merge-base)
    exit "${MERGE_BASE_RC:-0}" ;;
esac
exit 0
""" % _PROBE

# Kills only the holder started for the given redeploy-script pid.
_KILL_HOLDER_PY = (
    "import os,signal,subprocess,sys\n"
    "want='fly_jobs_in_flight.py --hold '+sys.argv[1]\n"
    "killed=0\n"
    "for line in subprocess.run(['ps','-ax','-o','pid=,command='],capture_output=True,text=True).stdout.splitlines():\n"
    "    pid,_,cmd=line.strip().partition(' ')\n"
    "    if want in cmd: os.kill(int(pid),signal.SIGKILL); killed+=1\n"
    "sys.exit(0 if killed else 3)\n"
)

_LAUNCHCTL_STUB = """#!/bin/bash
echo "launchctl $*" >> "$CALLS"
echo "admission during kickstart: $(%s)" >> "$CALLS"
if [ -n "${KILL_HOLDER_ON_KICKSTART:-}" ]; then
  # $PPID is the redeploy script.
  "$PROBE_PY" "$KILL_HOLDER_PY" "$PPID" || echo "HOLDER NOT KILLED" >> "$CALLS"
  sleep 0.5
fi
if [ -n "${FAIL_GUI_KILL_HOLDER:-}" ]; then
  case "$3" in gui/*)
    "$PROBE_PY" "$KILL_HOLDER_PY" "$PPID" || echo "HOLDER NOT KILLED" >> "$CALLS"
    sleep 0.5
    exit 1 ;;
  esac
fi
if [ -n "${TERM_PARENT_ON_KICKSTART:-}" ]; then
  kill -TERM $PPID; sleep 0.5
fi
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
    (rc / "execution" / "scripts").mkdir(parents=True, exist_ok=True)
    (rc / "execution" / "scripts" / "daemon-requirements.txt").write_text("x\n")
    (rc / ".venv" / "bin" / "pip").write_text(
        '#!/bin/sh\necho "pip $*" >> "$CALLS"\n[ -n "${PIP_DELAY:-}" ] && sleep "$PIP_DELAY" >/dev/null 2>&1\n[ -n "${PIP_FAIL:-}" ] && exit 1\nexit 0\n')
    (rc / ".venv" / "bin" / "pip").chmod(0o755)
    (tmp_path / "kill_holder.py").write_text(_KILL_HOLDER_PY)
    calls = tmp_path / "calls.log"
    calls.write_text("")
    env = {**os.environ, "PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}",
           "CALLS": str(calls), "RC_DIR": str(rc),
           "KILL_HOLDER_PY": str(tmp_path / "kill_holder.py"), "PROBE_PY": sys.executable,
           "ADMISSION_LOCK": str(helper.admission_lock_path(ledger)),
           "LOCK_DIR": str(tmp_path / "lock.d"), "APIS_FLY_LEDGER_FILE": str(ledger)}
    return env, calls


def _invoke(env: dict, calls: Path) -> tuple[subprocess.CompletedProcess, str]:
    calls.write_text("")
    result = subprocess.run(["/bin/bash", str(REDEPLOY)], env=env, capture_output=True,
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


# --- the holder ends the deploy before it releases the lock ----------------

def _wait_lock_free(lock: Path, seconds: float = 15) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if _lock_is_free(lock):
            return True
        time.sleep(0.1)
    return _lock_is_free(lock)


def test_redeploy_does_not_restart_after_admission_holder_expires(tmp_path):
    ledger = _ledger(tmp_path / "l.json")
    lock = helper.admission_lock_path(ledger)
    env, calls_file = _setup_redeploy(tmp_path, ledger)
    # Holder limit shortened to 2s; the dependency refresh (after the
    # fast-forward) outlasts it.
    env.update({"APIS_FLY_HOLD_LIMIT_SECONDS": "2", "DIFF_RC": "1", "PIP_DELAY": "6"})
    calls_file.write_text("")
    out = tmp_path / "out.log"
    with open(out, "w") as fh:
        proc = subprocess.Popen(["/bin/bash", str(REDEPLOY)], env=env, stdout=fh,
                                stderr=subprocess.STDOUT, text=True)
        proc.wait(timeout=60)
    exited = time.monotonic()
    stdout = out.read_text()
    # The holder ended the script (SIGTERM) before it released the lock.
    assert proc.returncode != 0, stdout
    # The script is gone but the holder is still in its settle: lock still held.
    assert not _lock_is_free(lock)
    assert _wait_lock_free(lock)
    # Released only after the settle (KICKSTART_SETTLE_SECONDS, 5s) had passed.
    assert time.monotonic() - exited >= helper.KICKSTART_SETTLE_SECONDS - 1.0
    calls = calls_file.read_text()
    assert "kickstart" not in calls
    assert "DEPLOY DID NOT FINISH" in stdout
    assert "may be on mixed builds" in stdout
    assert "still on the old build" not in stdout
    assert "DEPLOYED" not in stdout
    time.sleep(1)
    assert "kickstart" not in calls_file.read_text()


def test_hold_limit_override_can_only_shorten_the_limit():
    prod = float(helper.HOLD_LIMIT_SECONDS)
    for bad in ("0", "-1", "inf", "nan", "abc", "", str(helper.HOLD_LIMIT_SECONDS),
                str(helper.HOLD_LIMIT_SECONDS * 10)):
        assert helper.hold_limit({helper.HOLD_LIMIT_ENV: bad}) == prod, bad
    assert helper.hold_limit({}) == prod
    assert helper.hold_limit({helper.HOLD_LIMIT_ENV: "2"}) == 2.0


def test_holder_signals_only_its_own_parent(tmp_path):
    ledger = _ledger(tmp_path / "l.json")
    env = {**os.environ, "APIS_FLY_LEDGER_FILE": str(ledger),
           "APIS_FLY_HOLD_LIMIT_SECONDS": "1"}
    # A bystander that is NOT the holder's parent must never be signalled.
    bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        holder = subprocess.Popen(
            [sys.executable, str(HELPER), "--hold", str(bystander.pid)],
            env=env, stdout=subprocess.PIPE, text=True)
        assert holder.stdout.readline() == "ok 0\n"
        holder.wait(timeout=30)  # limit reached; bystander is not its parent
        assert bystander.poll() is None
    finally:
        bystander.kill()
        bystander.wait()


def test_holder_death_stops_further_restarts(tmp_path):
    ledger = _ledger(tmp_path / "l.json")
    env, calls_file = _setup_redeploy(tmp_path, ledger)
    env["KILL_HOLDER_ON_KICKSTART"] = "1"
    result, calls = _invoke(env, calls_file)
    assert result.returncode != 0, result.stdout + result.stderr
    assert calls.count("launchctl kickstart -k") == 1
    assert "HOLDER NOT KILLED" not in calls  # the injector really killed it
    assert "Fly admission holder is gone" in result.stdout
    assert "DEPLOY DID NOT FINISH" in result.stdout
    assert "may be on mixed builds" in result.stdout
    assert "still on the old build" not in result.stdout  # Apis was restarted
    assert _wait_lock_free(helper.admission_lock_path(ledger))


def test_holder_death_during_failed_gui_attempt_blocks_user_domain_kickstart(tmp_path):
    ledger = _ledger(tmp_path / "l.json")
    env, calls_file = _setup_redeploy(tmp_path, ledger)
    env["FAIL_GUI_KILL_HOLDER"] = "1"
    result, calls = _invoke(env, calls_file)
    assert result.returncode != 0, result.stdout + result.stderr
    assert "HOLDER NOT KILLED" not in calls  # the injector really killed it
    assert "kickstart -k gui/" in calls  # the failed GUI attempt happened
    assert "kickstart -k user/" not in calls  # the fallback did not restart
    assert "Fly admission holder is gone" in result.stdout
    assert _wait_lock_free(helper.admission_lock_path(ledger))


def test_end_parent_stops_signalling_once_pid_is_no_longer_its_parent(monkeypatch):
    sent = []
    states = iter([True])  # still our parent only at entry; gone after SIGTERM
    monkeypatch.setattr(helper, "_is_my_parent", lambda _p: next(states, False))
    monkeypatch.setattr(helper, "_parent_alive", lambda _p: True)  # a reused pid
    monkeypatch.setattr(helper.os, "kill", lambda pid, sig: sent.append(sig))
    monkeypatch.setattr(helper, "PARENT_TERM_WAIT_SECONDS", 0.3)
    monkeypatch.setattr(helper, "KICKSTART_SETTLE_SECONDS", 0)
    helper._end_parent(12345)
    assert sent == [helper.signal.SIGTERM]


def test_failed_merge_releases_admission_lock(tmp_path):
    ledger = _ledger(tmp_path / "l.json")
    env, calls_file = _setup_redeploy(tmp_path, ledger)
    env["MERGE_RC"] = "1"
    result, calls = _invoke(env, calls_file)
    assert result.returncode == 1
    assert "fast-forward merge failed" in result.stdout
    assert "kickstart" not in calls
    assert _wait_lock_free(helper.admission_lock_path(ledger))


def test_failed_dependency_install_releases_admission_lock(tmp_path):
    ledger = _ledger(tmp_path / "l.json")
    env, calls_file = _setup_redeploy(tmp_path, ledger)
    env.update({"DIFF_RC": "1", "PIP_FAIL": "1"})
    result, _calls = _invoke(env, calls_file)
    assert result.returncode == 1
    assert _wait_lock_free(helper.admission_lock_path(ledger))


def test_killed_redeploy_releases_admission_lock(tmp_path):
    ledger = _ledger(tmp_path / "l.json")
    env, calls_file = _setup_redeploy(tmp_path, ledger)
    env["TERM_PARENT_ON_KICKSTART"] = "1"  # the stub SIGTERMs the script
    result, calls = _invoke(env, calls_file)
    assert result.returncode != 0
    assert calls.count("launchctl kickstart -k") == 1
    assert "DEPLOY DID NOT FINISH" in result.stdout
    assert _wait_lock_free(helper.admission_lock_path(ledger))


def test_diverged_checkout_refuses_deploy_and_releases_admission_lock(tmp_path):
    ledger = _ledger(tmp_path / "l.json")
    env, calls_file = _setup_redeploy(tmp_path, ledger)
    env["MERGE_BASE_RC"] = "1"  # HEAD is not an ancestor of origin: diverged
    result, calls = _invoke(env, calls_file)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "diverged" in result.stdout
    assert "merge --ff-only" not in calls
    assert "kickstart" not in calls
    assert _wait_lock_free(helper.admission_lock_path(ledger))
