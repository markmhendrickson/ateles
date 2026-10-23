"""Run the Fly runner's real entrypoint as a subprocess (ateles#1456).

The in-machine half of the Fly runner is ``deploy/fly-runner/run_job.py``.
This module starts its ``entrypoint`` in a real child process, under a
small hard deadline, with one condition that could hold the process up, and
reports what happened: the exit code and time, whether the condition was
actually reached, whether the cleanup ran, and (on Linux) whether the
process the job left behind was alive before and gone after.

Offline: the record writer is replaced by a local file write, and the
credential probe looks at no host path. The child gets an explicit
writable temporary directory, so nothing depends on the host's.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
RUN_JOB = REPO / "deploy" / "fly-runner" / "run_job.py"

# What each mode does once the CLI has run:
#   saturated-output   stdout and stderr are full (nobody reads), record write stalls
#   output-held        the same, and the main thread is inside a stdout write
#   stalled-cleanup    record write stalls; the cleanup does its work, then never returns
#   exceptional-exit   the run raises while a non-daemon thread blocks forever
#   system-exit        the run asks to exit while a non-daemon thread blocks forever
MODES = ("saturated-output", "output-held", "stalled-cleanup", "exceptional-exit",
         "system-exit")  # fmt: skip

DRIVER = r'''
import fcntl, importlib.util, json, os, sys, threading, time
from pathlib import Path
run_job_path, job_dir, work, mode = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4]
pid_file = work / "descendant.pid"
spec = importlib.util.spec_from_file_location("run_job_entry", run_job_path)
m = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = m
spec.loader.exec_module(m)
m.probe_paths_default = lambda: ()

def mark(name):
    (work / name).write_text(str(time.time()))

def mark_condition():
    """Record, before anything is terminated, that the condition is reached
    and that the process the job left behind is running at that moment."""
    pid, alive = 0, False
    for _ in range(250):  # the left-behind process writes its pid at start
        if pid_file.exists() and pid_file.read_text():
            pid = int(pid_file.read_text())
            break
        time.sleep(0.02)
    if pid:
        try:
            os.kill(pid, 0)
            alive = True
        except OSError:
            alive = False
    (work / "condition-reached").write_text(json.dumps({"descendant": pid, "alive": alive}))

real_kill = m.kill_job_processes
def kill_and_mark(*a, **k):
    result = real_kill(*a, **k)
    mark("cleanup-reached")
    if mode == "stalled-cleanup":
        time.sleep(600)  # the cleanup never returns
    return result
m.kill_job_processes = kill_and_mark

def fill_output():
    for fd in (1, 2):
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        try:
            while True:
                os.write(fd, b"x" * 4096)
        except BlockingIOError:
            pass
        fcntl.fcntl(fd, fcntl.F_SETFL, flags)

def block_forever_in_a_thread():
    threading.Thread(target=threading.Event().wait, daemon=False).start()

def writer(base_url, auth, record):
    (work / "record.json").write_text(json.dumps(record))
    if mode == "saturated-output":
        fill_output()
        mark_condition()
        time.sleep(60)
    elif mode == "output-held":
        fill_output()
        mark_condition()
        sys.stdout.write("y" * 1_000_000)  # blocks inside the stdout buffer
        sys.stdout.flush()
    elif mode == "stalled-cleanup":
        mark_condition()
        time.sleep(60)
    elif mode == "exceptional-exit":
        block_forever_in_a_thread()
        mark_condition()
        raise RuntimeError("the run failed part way")
    elif mode == "system-exit":
        block_forever_in_a_thread()
        mark_condition()
        raise SystemExit(3)
    return True

m.entrypoint(job_dir, writer=writer)
'''

# A CLI that leaves a long-running process behind (it records its own pid)
# and exits at once.
CLI = r'''
import subprocess, sys
subprocess.Popen(
    [sys.executable, "-c",
     "import os, sys, time; open(sys.argv[1], 'w').write(str(os.getpid())); time.sleep(60)",
     sys.argv[1]],
    start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
print("cli started", flush=True)
'''


@dataclass
class EntrypointTrace:
    mode: str
    exit_code: int | None
    elapsed: float
    condition_reached: bool
    exit_after_condition: float | None
    cleanup_reached: bool
    descendant_pid: int | None
    descendant_alive_before: bool | None
    descendant_gone_after: bool | None  # None where it cannot be checked
    envelope: dict


def _deadline_in(seconds: float) -> str:
    return datetime.fromtimestamp(time.time() + seconds, tz=timezone.utc).isoformat()


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    status = Path(f"/proc/{pid}/status")
    if status.exists():
        try:
            return "\tZ" not in status.read_text().split("State:", 1)[-1][:6]
        except OSError:
            return False
    return True


def write_job(job_dir: Path, cli_pid_file: Path) -> None:
    (job_dir / "secrets").mkdir(parents=True)
    spec = {
        "run_id": "fly-eval-entrypoint", "role": "lanius", "provider": "claude",
        "repository": "owner/repo", "timeout_seconds": 30,
        "credential_names": ["LANIUS_NEOTOMA_TOKEN"], "child_credential_names": [],
        "neotoma_token_name": "LANIUS_NEOTOMA_TOKEN", "neotoma_jwk_name": "",
        "neotoma_base_url": "https://neotoma.example.test", "probe_env_names": [],
        "probe_paths": [], "child_user": "",
    }  # fmt: skip
    (job_dir / "job.json").write_text(json.dumps(spec))
    (job_dir / "argv.json").write_text(json.dumps([sys.executable, "-c", CLI, str(cli_pid_file)]))
    (job_dir / "stdin").write_bytes(b"the task")
    (job_dir / "secrets" / "LANIUS_NEOTOMA_TOKEN").write_text("eval-own-token-123")


def run_entrypoint(mode: str, work: Path, *, budget: float = 3.0,
                   run_job: Path = RUN_JOB) -> EntrypointTrace:  # fmt: skip
    """Run the real entrypoint once in *mode*; every process is cleaned up."""
    assert mode in MODES, mode
    work.mkdir(parents=True, exist_ok=True)
    tmp = work / "tmp"
    tmp.mkdir(exist_ok=True)
    job_dir = work / "job"
    pid_file = work / "descendant.pid"
    write_job(job_dir, pid_file)
    driver = work / "driver.py"
    driver.write_text(DRIVER)
    child_vars = {
        "PATH": os.environ["PATH"], "TMPDIR": str(tmp), "HOME": str(work),
        "ATELES_RUN_DEADLINE": _deadline_in(budget),
    }  # fmt: skip
    started = time.monotonic()
    entry = subprocess.Popen(
        [sys.executable, str(driver), str(run_job), str(job_dir), str(work), mode],
        env=child_vars, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,  # never read: a full pipe is part of the test
    )  # fmt: skip
    condition_at = None
    alive_before = None
    descendant = None
    try:
        limit = time.monotonic() + budget + 10
        marker = work / "condition-reached"
        while entry.poll() is None and time.monotonic() < limit:
            if condition_at is None and marker.exists():
                condition_at = time.monotonic()
            time.sleep(0.01)
        rc = entry.poll()
        ended = time.monotonic()
        if condition_at is None and marker.exists():
            # Reached, and the exit followed within one poll.
            condition_at = ended
        if marker.exists():
            try:
                seen = json.loads(marker.read_text())
                descendant = int(seen["descendant"]) or None
                alive_before = bool(seen["alive"]) if descendant else None
            except (ValueError, KeyError, TypeError):
                pass
        gone = None
        if descendant and sys.platform.startswith("linux"):
            for _ in range(40):
                if not alive(descendant):
                    break
                time.sleep(0.05)
            gone = not alive(descendant)
        return EntrypointTrace(
            mode=mode, exit_code=rc, elapsed=ended - started,
            condition_reached=condition_at is not None,
            exit_after_condition=(ended - condition_at) if condition_at is not None else None,
            cleanup_reached=(work / "cleanup-reached").exists(),
            descendant_pid=descendant, descendant_alive_before=alive_before,
            descendant_gone_after=gone,
            envelope={"mode": mode, "exit": rc, "elapsed": round(ended - started, 2)},
        )  # fmt: skip
    finally:
        if entry.poll() is None:
            entry.kill()
            entry.wait()
        for stream in (entry.stdout, entry.stderr):
            stream.close()
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text() or 0), signal.SIGKILL)
            except (OSError, ValueError):
                pass
