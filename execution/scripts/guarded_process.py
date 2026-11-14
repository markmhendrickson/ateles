"""Run child processes so they cannot outlive the process that started them.

Tyto kills a transcription at its outer deadline with SIGKILL, which cannot be
handled: whatever the transcription had running (whisper-cli, ffmpeg, FluidAudio)
would keep burning CPU for minutes. Two tools:

* ``run_guarded`` runs one command in its own session; a watcher outside the
  parent notices the parent is gone and kills it, and a timeout kills the whole
  process group.
* ``guard_pattern`` guards a child this code does not start itself (FluidAudio is
  started deep inside ``local_fluid``): a watcher kills any process whose command
  line contains a unique token (a temporary file path) once the parent is gone.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess

# The watcher's output is detached so it never holds the caller's pipes open.
_PARENT_GUARD = (
    'parent=$1; shift; "$@" & child=$!; '
    '( while kill -0 "$parent" 2>/dev/null; do sleep 1; done; kill -9 "$child" 2>/dev/null ) '
    ">/dev/null 2>&1 & watcher=$!; "
    'wait "$child"; status=$?; kill "$watcher" 2>/dev/null; exit "$status"'
)
_PATTERN_GUARD = (
    'while kill -0 "$1" 2>/dev/null; do sleep 1; done; pkill -9 -f -- "$2" 2>/dev/null'
)


def run_guarded(cmd: list[str], *, timeout: float) -> subprocess.CompletedProcess:
    """Like ``subprocess.run(capture_output=True, text=True)``, but the command dies
    with its parent and the whole process group is killed on timeout."""
    proc = subprocess.Popen(
        ["/bin/sh", "-c", _PARENT_GUARD, "sh", str(os.getpid()), *cmd],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True,
    )
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
        raise
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


def check_guarded(cmd: list[str], *, timeout: float) -> subprocess.CompletedProcess:
    """``run_guarded`` that raises ``CalledProcessError`` on a non-zero exit."""
    done = run_guarded(cmd, timeout=timeout)
    if done.returncode != 0:
        raise subprocess.CalledProcessError(done.returncode, cmd, done.stdout, done.stderr)
    return done


@contextlib.contextmanager
def guard_pattern(token: str):
    """While active, kill any process whose command line contains ``token`` if this
    process disappears. ``token`` must be unique to the guarded child."""
    watcher = subprocess.Popen(
        ["/bin/sh", "-c", _PATTERN_GUARD, "sh", str(os.getpid()), token],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        yield
    finally:
        with contextlib.suppress(OSError):
            os.killpg(watcher.pid, signal.SIGKILL)
        watcher.wait()
