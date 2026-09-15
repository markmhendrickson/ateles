"""Effect tests for fleet-wide plist rollback during the #515 cutover."""

from __future__ import annotations

import json
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


def _write_rewrite_failure_tools(fake_bin: Path) -> None:
    launchctl = fake_bin / "launchctl"
    launchctl.write_text("#!/usr/bin/env bash\nexit 0\n")
    launchctl.chmod(0o755)
    plutil = fake_bin / "plutil"
    plutil.write_text("""#!/usr/bin/env bash
set -eu
plist="${@: -1}"
if grep -q "$CUTOVER_TEST_RC/" "$plist"; then exit 1; fi
exit 0
""")
    plutil.chmod(0o755)


def test_reconciliation_exception_restores_earlier_state_mutation(tmp_path):
    home = tmp_path / "home"
    shared = tmp_path / "shared"
    rc = tmp_path / "rc"
    (rc / "lib/daemon_runtime").mkdir(parents=True)
    shutil.copy2(HELPER, rc / "lib/daemon_runtime/cutover_state.py")

    # Cotinga is reconciled before Piculet. Its shared-only state is therefore
    # copied into RC before the malformed list member raises TypeError.
    shared_cotinga = shared / "execution/daemons/cotinga/.cotinga_last_run"
    rc_cotinga = rc / "execution/daemons/cotinga/.cotinga_last_run"
    shared_cotinga.parent.mkdir(parents=True)
    shared_cotinga.write_text("2026-09-15T12:00:00Z\n")

    shared_seen = shared / "execution/daemons/piculet/seen_files.json"
    rc_seen = rc / "execution/daemons/piculet/seen_files.json"
    shared_seen.parent.mkdir(parents=True)
    rc_seen.parent.mkdir(parents=True)
    shared_seen.write_text(json.dumps(["meeting.wav"]) + "\n")
    rc_seen_original = json.dumps([{"malformed": "entry"}]) + "\n"
    rc_seen.write_text(rc_seen_original)

    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "ATELES_SHARED_CHECKOUT": str(shared),
            "ATELES_REPO_PATH": str(rc),
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
    assert "unhashable type" in result.stderr
    assert not rc_cotinga.exists()
    assert rc_seen.read_text() == rc_seen_original
    backups = list((home / ".config/ateles/daemon-state-backups").glob("*"))
    assert len(backups) == 1
    assert (backups[0] / "manifest.json").is_file()


def test_unreadable_existing_plist_aborts_before_mutation_and_preserves_bytes(tmp_path):
    home = tmp_path / "home"
    shared = tmp_path / "shared"
    rc = tmp_path / "rc"
    launch_agents = home / "Library/LaunchAgents"
    fake_bin = tmp_path / "bin"
    launch_agents.mkdir(parents=True)
    fake_bin.mkdir()
    (rc / "lib/daemon_runtime").mkdir(parents=True)
    shutil.copy2(HELPER, rc / "lib/daemon_runtime/cutover_state.py")

    unreadable = launch_agents / "com.ateles.cotinga.plist"
    original = _write_plist(unreadable, "com.ateles.cotinga", shared)
    unreadable.chmod(0)
    assert not os.access(unreadable, os.R_OK)

    # A later rewrite failure makes the old implementation consume the false
    # absence marker and delete the pre-existing unreadable Cotinga plist.
    _write_plist(
        launch_agents / "com.ateles.cyphorhinus.plist",
        "com.ateles.cyphorhinus",
        shared,
    )
    shared_state = shared / "execution/daemons/cotinga/.cotinga_last_run"
    shared_state.parent.mkdir(parents=True)
    shared_state.write_text("2026-09-15T13:00:00Z\n")

    _write_rewrite_failure_tools(fake_bin)

    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "PATH": f"{fake_bin}:{env['PATH']}",
            "ATELES_SHARED_CHECKOUT": str(shared),
            "ATELES_REPO_PATH": str(rc),
            "CUTOVER_TEST_RC": str(rc),
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
    assert "existing unreadable plist" in result.stderr
    assert unreadable.exists()
    unreadable.chmod(0o600)
    assert unreadable.read_bytes() == original
    assert not (home / ".config/ateles/daemon-state-backups").exists()


@pytest.mark.parametrize("invalid_kind", ["directory", "broken_symlink"])
def test_existing_nonregular_plist_aborts_before_mutation(tmp_path, invalid_kind):
    home = tmp_path / "home"
    shared = tmp_path / "shared"
    rc = tmp_path / "rc"
    launch_agents = home / "Library/LaunchAgents"
    fake_bin = tmp_path / "bin"
    launch_agents.mkdir(parents=True)
    fake_bin.mkdir()
    (rc / "lib/daemon_runtime").mkdir(parents=True)
    shutil.copy2(HELPER, rc / "lib/daemon_runtime/cutover_state.py")

    invalid = launch_agents / "com.ateles.cotinga.plist"
    if invalid_kind == "directory":
        invalid.mkdir()
    else:
        invalid.symlink_to(launch_agents / "missing-target")
    _write_plist(
        launch_agents / "com.ateles.cyphorhinus.plist",
        "com.ateles.cyphorhinus",
        shared,
    )
    shared_state = shared / "execution/daemons/cotinga/.cotinga_last_run"
    shared_state.parent.mkdir(parents=True)
    shared_state.write_text("2026-09-15T13:00:00Z\n")
    _write_rewrite_failure_tools(fake_bin)

    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "PATH": f"{fake_bin}:{env['PATH']}",
            "ATELES_SHARED_CHECKOUT": str(shared),
            "ATELES_REPO_PATH": str(rc),
            "CUTOVER_TEST_RC": str(rc),
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
    assert "existing non-regular plist" in result.stderr
    if invalid_kind == "directory":
        assert invalid.is_dir()
    else:
        assert invalid.is_symlink()
        assert invalid.readlink() == launch_agents / "missing-target"
    assert not (home / ".config/ateles/daemon-state-backups").exists()


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
