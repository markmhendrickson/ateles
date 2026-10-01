#!/usr/bin/env python3
"""Run pytest with a MINIMAL, synthetic environment, never the host's.

Why this exists: a local test run inherits the operator's shell, which carries
real credentials. When a test that captures a child-process environment fails,
pytest's assertion introspection prints that whole environment into the
terminal (and into an agent transcript). This runner makes that impossible by
construction: the child pytest process gets only a fixed handful of
non-secret variables; everything else is dropped before pytest starts.

Usage:  python3 execution/scripts/run_pytest_hermetic.py <pytest args...>

It also forces ``-p no:cacheprovider`` and ``--tb=line`` unless the caller
passes their own ``--tb``. It never prints the environment.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

# Names copied from the host because tools strictly need them. None carries a
# credential. Everything else is dropped.
_PASSTHROUGH = ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "TERM")


def build_env(host=None) -> dict:
    host = os.environ if host is None else host
    env = {k: host[k] for k in _PASSTHROUGH if k in host}
    env.setdefault("PATH", "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin")
    env.setdefault("HOME", tempfile.gettempdir())
    env.setdefault("TMPDIR", tempfile.gettempdir())
    env.setdefault("LANG", "en_US.UTF-8")
    return env


def main(argv) -> int:
    args = list(argv)
    if not any(a == "--tb" or a.startswith("--tb=") for a in args):
        args.append("--tb=line")
    cmd = [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", *args]
    repo_root = Path(__file__).resolve().parents[2]
    # Say how many host variables were withheld (a COUNT, never names or
    # values), so a test that fails only because it needed one is diagnosable.
    dropped = sum(1 for name in os.environ if name not in build_env())
    print(
        "run_pytest_hermetic: %d host variables not passed to pytest" % dropped,
        file=sys.stderr,
    )
    return subprocess.call(cmd, env=build_env(), cwd=repo_root)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
