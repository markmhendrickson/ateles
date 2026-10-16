"""Runtime regression guard for ateles#657 — the live-process half of the DoD.

`test_launcher_no_credential_export.py` proves the export loop is gone from
*source*: a static content sweep of every daemon launcher file. That sweep
cannot catch a regression where credentials leak into the launched process by
a *different* mechanism than the one that was removed — e.g. a re-added
export elsewhere, or a future `subprocess.Popen(..., env=os.environ)` call
that bypasses the dotenv-scrubbing path entirely, would pass the static
check while still exposing secrets. Only an externally-observed running
process closes that gap, which is what issue #657's Definition of Done names
as the second, required half (see PR #943 body's "Missing process-env
integration test" discussion).

This test drives the REAL `execution/daemons/anthus/run_anthus_launchd.sh` —
the actual committed launcher file, unmodified — end to end, and inspects the
resulting process's environment from OUTSIDE that process (via `ps eww` on
macOS, or `/proc/<pid>/environ` on Linux), not via the launcher's own
generated command string or any in-process introspection.

Why the payload is a fixture "python" shim rather than the real anthus.py:
anthus.py's `main()` immediately does live network work (loads its
agent_definition from Neotoma, subscribes to Neotoma SSE) and has no fixture
mode — running it for real requires live credentials and a reachable Neotoma
instance, which is not available to this test and is not what this test is
about. `run_anthus_launchd.sh` reads its interpreter from `$ANTHUS_PYTHON`
(falling back to the repo venv) but hardcodes the *script* path
(`anthus.py`) as its final exec argument — that argument is not
substitutable. So the fixture shim is installed as the interpreter itself:
`ANTHUS_PYTHON=<fixture>` points the launcher's `exec "$PY" ".../anthus.py"`
at a tiny script that ignores the anthus.py path argv and instead dumps its
own environment and sleeps. This exercises the exact `exec` line under test
— same file, same env-inheritance semantics production uses — without
depending on Neotoma.
"""

from __future__ import annotations

import os
import select
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

_DAEMONS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _DAEMONS_DIR.parent.parent
_LAUNCHER = _DAEMONS_DIR / "anthus" / "run_anthus_launchd.sh"

# Placeholder-only — never real credential values, matching the convention of
# the static sibling test (test_launcher_no_credential_export.py).
_FIXTURE_SECRETS = {
    "GITHUB_TOKEN": "fake-pat",
    "TELEGRAM_BOT_TOKEN": "fake-telegram",
    "CLAUDE_CODE_OAUTH_TOKEN": "fake-oauth",
}

# A legit, non-secret var that MUST still be present in the observed
# environment — without this, "the fixture secrets are absent" could pass
# vacuously because nothing at all made it into the process's env block.
_LEGIT_MARKER_VAR = "ANTHUS_RUNTIME_TEST_MARKER"
_LEGIT_MARKER_VALUE = "present-and-not-a-secret"

# Mirrors lib/daemon_runtime/__init__.py's own bootstrap: read a dotenv file
# from disk and set it into THIS process's own os.environ, in-process, never
# exported to any parent/child shell. That in-process load is the mechanism
# the fix relies on (anthus.py imports lib.daemon_runtime, which does this at
# import time) — the shim reproduces it directly so this test does not need
# to import the real lib.daemon_runtime (which pulls in Neotoma network
# calls at import time; see module docstring).
_SHIM_SOURCE = """\
#!/usr/bin/env python3
# Fixture stand-in for a python interpreter, installed via $ANTHUS_PYTHON so
# run_anthus_launchd.sh execs THIS instead of the real venv python3. Ignores
# the anthus.py path argv (argv[1]). Reproduces lib/daemon_runtime's
# in-process dotenv load (read from disk into this process's own os.environ,
# never exported) so the test can prove that path stays invisible to `ps
# eww`/`/proc` while separately proving the launcher SCRIPT itself injects
# nothing into the exec'd process's OS-level environment block.
import os
import sys
import time
from pathlib import Path

dotenv_path = Path(os.environ["FIXTURE_DOTENV_PATH"])
for line in dotenv_path.read_text().splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, _, v = line.partition("=")
        os.environ[k.strip()] = v.strip()

sys.stdout.write("shim-ready\\n")
sys.stdout.flush()
time.sleep(5)
"""


def _write_shim(tmp_path: Path) -> Path:
    shim = tmp_path / "fixture_python_shim.py"
    shim.write_text(_SHIM_SOURCE, encoding="utf-8")
    mode = shim.stat().st_mode
    shim.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return shim


def _write_fixture_dotenv(tmp_path: Path) -> Path:
    dotenv = tmp_path / "fixture.env"
    lines = [f"{key}={value}" for key, value in _FIXTURE_SECRETS.items()]
    dotenv.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return dotenv


def _read_environ_external(pid: int) -> str:
    """Read a process's environment block from OUTSIDE that process.

    Two branches, deliberately not unified — they rely on different
    mechanisms and one does not substitute for the other:

    - Linux: /proc/<pid>/environ is directly readable (own-uid process,
      no elevated privileges needed) and is the portable, CI-friendly path.
    - macOS: has no /proc. `ps eww <pid>` is the reliable external-observer
      path there; psutil.Process(pid).environ() would require elevated
      privileges to inspect another process on macOS, which is exactly why
      this codebase is not adding a psutil dependency for one test.
    """
    if sys.platform.startswith("linux"):
        environ_path = Path(f"/proc/{pid}/environ")
        raw = environ_path.read_bytes()
        return raw.replace(b"\x00", b"\n").decode("utf-8", errors="replace")
    else:
        # macOS (and other non-Linux platforms): `ps eww` is the reliable
        # external-observer path here. Guarded by the module-level
        # skipif below on non-Darwin/non-Linux platforms.
        result = subprocess.run(
            ["ps", "eww", str(pid)],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout


@pytest.mark.skipif(
    not sys.platform.startswith(("darwin", "linux")),
    reason="external process-env observation only implemented for macOS (ps eww) and Linux (/proc/<pid>/environ)",
)
@pytest.mark.timeout(30)
def test_launcher_exec_does_not_leak_dotenv_secrets_into_spawned_process_env():
    """Spawn the REAL run_anthus_launchd.sh the way launchd actually does —
    with NO dotenv secrets in the parent-supplied environment (production's
    plist carries none; see com.ateles.anthus.plist.tmpl's own header
    comment) — and a fixture dotenv file on disk that only the fixture
    interpreter shim loads IN-PROCESS, reproducing lib/daemon_runtime's
    bootstrap (see module docstring). Then reads the resulting process's
    environment block from OUTSIDE that process and asserts the fixture
    secrets are absent.

    This isolates exactly the ateles#657 defect surface: whether
    run_anthus_launchd.sh ITSELF injects the dotenv into the exec'd
    process's OS-level environment block (the old `export "$_key=$_val"`
    loop did; the fix removes it), as distinct from the in-process load
    that legitimately populates that env AFTER exec, invisibly to `ps eww`.

    This is the strong outcome assertion the DoD calls for: a negative
    existence check against externally-observed live-process state, not
    against the launcher's generated command string (that's what the static
    sweep test already covers) and not against any in-process introspection.
    """
    assert _LAUNCHER.exists(), f"expected launcher at {_LAUNCHER}"

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        shim = _write_shim(tmp_path)
        fixture_dotenv = _write_fixture_dotenv(tmp_path)

        # HOME is pinned to an isolated fixture directory — NEVER the real
        # operator HOME. A pre-fix (or regressed) launcher reads
        # $HOME/.config/neotoma/.env by default; inheriting the real HOME
        # here would make this test read the operator's actual materialized
        # secrets file. (Caught in review: an earlier draft of this test
        # fell back to `os.environ.get("HOME", ...)` and, run against a
        # reverted pre-fix launcher, was observed reading the real
        # ~/.config/neotoma/.env — never again.)
        fixture_home = tmp_path / "fixture_home"
        fixture_home.mkdir()
        assert str(fixture_home) != os.environ.get("HOME"), (
            "fixture HOME collided with the real HOME — refusing to proceed "
            "rather than risk reading the operator's real materialized dotenv"
        )

        env = {
            # Minimal PATH so `bash`/`ps`/the shim's `#!/usr/bin/env python3`
            # resolve; deliberately NOT inheriting the test runner's full
            # os.environ, so we control exactly what's "materialized" here.
            # No fixture secrets here — this mirrors launchd, which sources
            # no dotenv and passes the script only what the plist declares.
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(fixture_home),
            "ANTHUS_PYTHON": str(shim),
            "FIXTURE_DOTENV_PATH": str(fixture_dotenv),
            _LEGIT_MARKER_VAR: _LEGIT_MARKER_VALUE,
        }

        proc = subprocess.Popen(
            ["bash", str(_LAUNCHER)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            # Wait for the shim's ready line so we know exec has completed
            # and the target process is the one actually running (not bash
            # still mid-exec). Bounded with select() rather than a bare
            # proc.stdout.readline() inside a time-checked loop: readline()
            # blocks until a full line arrives, so if the launcher/shim never
            # writes anything (hung exec, stalled interpreter resolution) the
            # deadline check above it would never be reached again and the
            # test would hang instead of failing with a clear message.
            deadline = time.monotonic() + 10
            ready_line = ""
            while time.monotonic() < deadline:
                remaining = max(0.0, deadline - time.monotonic())
                readable, _, _ = select.select([proc.stdout], [], [], remaining)
                if not readable:
                    break
                line = proc.stdout.readline()
                if line:
                    ready_line = line
                    break
                if proc.poll() is not None:
                    break
            assert "shim-ready" in ready_line, (
                f"fixture shim did not report ready within 10s (launcher "
                f"exec failed or hung?); stdout so far={ready_line!r} "
                f"stderr={proc.stderr.read() if proc.stderr else ''!r}"
            )

            # `exec` in bash replaces the shell process image in place, so
            # proc.pid IS the shim's pid, not a child of it — this is the
            # property under test (no export loop running as a persistent
            # parent shell around the real payload).
            observed = _read_environ_external(proc.pid)

            for key, fake_value in _FIXTURE_SECRETS.items():
                assert key not in observed, (
                    f"{key} leaked into the externally-observed environment "
                    f"of the launched process (pid={proc.pid}) — ateles#657 "
                    f"regression: a fixture secret placed in the launcher's "
                    f"environment reached the spawned process's OS-level "
                    f"environment block"
                )
                assert fake_value not in observed, (
                    f"fake value for {key} leaked into externally-observed "
                    f"process env (pid={proc.pid})"
                )

            # Non-vacuousness: prove the observation mechanism actually sees
            # SOMETHING from this process's real environment, so an empty or
            # broken `ps`/`/proc` read doesn't masquerade as a clean result.
            assert _LEGIT_MARKER_VAR in observed and _LEGIT_MARKER_VALUE in observed, (
                "external environment observation did not find the legitimate "
                "marker variable that was set on this exact process — the "
                "observation mechanism itself is not working, so the absence "
                "of fixture secrets above is not meaningful evidence"
            )
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
            if proc.stdout:
                proc.stdout.close()
            if proc.stderr:
                proc.stderr.close()
