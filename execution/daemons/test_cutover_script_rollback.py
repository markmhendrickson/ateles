"""Effect tests for fleet-wide plist rollback during the #515 cutover."""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "execution/scripts/isolate_daemons_to_rc_src.sh"
HELPER = REPO / "lib/daemon_runtime/cutover_state.py"
LABELS = (
    "com.ateles.cotinga",
    "com.ateles.cyphorhinus",
    "com.ateles.piculet",
    "com.ateles.sylvia",
    "com.ateles.phoenicurus-prepare",
)


def _write_plist(path: Path, label: str, root: Path) -> bytes:
    payload = {
        "Label": label,
        "ProgramArguments": [
            str(root / ".venv/bin/python3"),
            str(root / "execution/daemons/fake.py"),
        ],
    }
    data = plistlib.dumps(payload)
    path.write_bytes(data)
    return data


@pytest.mark.parametrize("failure_mode", ["rewrite", "reload", "readback"])
def test_cutover_failure_restores_and_reloads_entire_prior_fleet(
    tmp_path, failure_mode
):
    home = tmp_path / "home"
    shared = tmp_path / "shared"
    rc = tmp_path / "rc"
    launch_agents = home / "Library/LaunchAgents"
    fake_bin = tmp_path / "bin"
    launch_agents.mkdir(parents=True)
    fake_bin.mkdir()
    (rc / "lib/daemon_runtime").mkdir(parents=True)
    shutil.copy2(HELPER, rc / "lib/daemon_runtime/cutover_state.py")

    originals = {}
    for label in LABELS:
        path = launch_agents / f"{label}.plist"
        originals[path] = _write_plist(path, label, shared)

    log = tmp_path / "launchctl.log"
    launchctl = fake_bin / "launchctl"
    launchctl.write_text("""#!/usr/bin/env bash
set -eu
if [ "${1:-}" = list ]; then
  n=100
  for label in com.ateles.cotinga com.ateles.cyphorhinus com.ateles.piculet com.ateles.sylvia com.ateles.phoenicurus-prepare; do
    printf '%s\\t0\\t%s\\n' "$n" "$label"
    n=$((n + 1))
  done
  exit 0
fi
plist="${@: -1}"
mode=none
if [ -f "$plist" ]; then
  if grep -q "$CUTOVER_TEST_RC/" "$plist"; then mode=rc; else mode=shared; fi
fi
printf '%s|%s\\n' "$*" "$mode" >> "$CUTOVER_TEST_LOG"
if [ "$CUTOVER_TEST_FAILURE" = reload ] && { [ "${1:-}" = bootstrap ] || [ "${1:-}" = load ]; } && grep -q "$CUTOVER_TEST_RC/" "$plist" && [ "$plist" = "$HOME/Library/LaunchAgents/com.ateles.piculet.plist" ]; then
  exit 1
fi
exit 0
""")
    launchctl.chmod(0o755)
    plutil = fake_bin / "plutil"
    plutil.write_text("""#!/usr/bin/env bash
set -eu
plist="${@: -1}"
if [ "$CUTOVER_TEST_FAILURE" = rewrite ] && grep -q "$CUTOVER_TEST_RC/" "$plist"; then
  exit 1
fi
exit 0
""")
    plutil.chmod(0o755)

    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "PATH": f"{fake_bin}:{env['PATH']}",
            "ATELES_SHARED_CHECKOUT": str(shared),
            "ATELES_REPO_PATH": str(rc),
            "CUTOVER_TEST_LOG": str(log),
            "CUTOVER_TEST_RC": str(rc),
            "CUTOVER_TEST_FAILURE": failure_mode,
        }
    )
    result = subprocess.run(
        ["bash", str(SCRIPT), "--apply"],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )

    assert result.returncode != 0
    for path, original in originals.items():
        assert path.read_bytes() == original
    calls = log.read_text().splitlines()
    for label in LABELS:
        plist = launch_agents / f"{label}.plist"
        assert any(
            call.endswith(f"{plist}|shared")
            and (call.startswith("bootstrap ") or call.startswith("load "))
            for call in calls
        ), f"prior configuration for {label} was not reloaded"
