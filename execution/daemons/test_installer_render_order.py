"""Daemon installers must render and validate BEFORE they touch a running agent.

A launchd installer that unloads the running agent and then renders can leave
the agent stopped when the renderer refuses the template. The installers now
share ``_install_plist.sh``: render into a scratch file, and only a clean
render unloads, swaps and reloads. These tests run each installer against a
stub ``launchctl`` in a throwaway tree (fake HOME, fake LaunchAgents, no real
service touched) and assert the order, the continuity of the running agent
when a template is refused, and the recovery message when a load fails.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DAEMONS = REPO_ROOT / "execution" / "daemons"
RENDERER = REPO_ROOT / "execution" / "scripts" / "render_daemon_plist.py"

# installer directory -> (plist file name, launchd label, extra installer args)
SIMPLE_INSTALLERS = {
    "aquila": ("com.ateles.aquila.plist", "com.ateles.aquila", []),
    "monedula": (
        "com.markmhendrickson.monedula.plist",
        "com.markmhendrickson.monedula",
        [],
    ),
    "morning-brief": (
        "com.ateles.morning-brief.plist",
        "com.ateles.morning-brief",
        [],
    ),
    "neotoma-agent": (
        "com.ateles.neotoma-agent.plist",
        "com.ateles.neotoma-agent",
        [],
    ),
    "strix": ("com.ateles.strix.plist", "com.ateles.strix", []),
}
ALL_INSTALLERS = [*SIMPLE_INSTALLERS, "phoenicurus-release"]

OLD_PLIST = "<!-- previous installed plist -->\n"
GOOD_TEMPLATE = (
    '<?xml version="1.0" encoding="UTF-8"?><plist version="1.0"><dict>'
    "<key>Label</key><string>{label}</string>"
    "<key>ProgramArguments</key><array><string>/bin/true</string></array>"
    "<key>NEWMARK</key><string>yes</string>"
    "</dict></plist>"
)
# Refused by the renderer: key-shaped opaque value under a non-credential name.
BAD_TEMPLATE = (
    '<?xml version="1.0" encoding="UTF-8"?><plist version="1.0"><dict>'
    "<key>Label</key><string>{label}</string>"
    "<key>EnvironmentVariables</key><dict>"
    "<key>SOME_SETTING</key><string>" + "Ab3" * 12 + "</string></dict>"
    "</dict></plist>"
)

STUB_LAUNCHCTL = """#!/usr/bin/env bash
# Stub: records every call, models a single loaded label, can refuse a load.
echo "$*" >> "$STUB_DIR/calls.log"
case "$1" in
  list) cat "$STUB_DIR/loaded" 2>/dev/null; exit 0 ;;
  unload) : > "$STUB_DIR/loaded"; exit 0 ;;
  load)
    if [ -f "$STUB_DIR/refuse_new" ] && grep -q NEWMARK "$2"; then exit 1; fi
    if [ -f "$STUB_DIR/refuse_all" ]; then exit 1; fi
    echo "$LABEL" > "$STUB_DIR/loaded"; exit 0 ;;
esac
exit 0
"""


def _tree(tmp_path: Path, installer_source: Path, name: str, plist: str, label: str):
    """A throwaway copy of one installer with a stub launchctl and fake HOME."""
    daemons = tmp_path / "execution" / "daemons"
    scripts = tmp_path / "execution" / "scripts"
    daemon_dir = daemons / name
    daemon_dir.mkdir(parents=True)
    scripts.mkdir(parents=True)
    (scripts / "render_daemon_plist.py").symlink_to(RENDERER)
    shutil.copy(installer_source, daemon_dir / "install.sh")
    lib = DAEMONS / "_install_plist.sh"
    if lib.exists():
        shutil.copy(lib, daemons / "_install_plist.sh")

    home = tmp_path / "home"
    agents = home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    installed = agents / plist
    installed.write_text(OLD_PLIST)

    stub = tmp_path / "stub"
    stub.mkdir()
    (stub / "loaded").write_text(label + "\n")  # the agent is running
    launchctl = stub / "launchctl"
    launchctl.write_text(STUB_LAUNCHCTL)
    launchctl.chmod(launchctl.stat().st_mode | stat.S_IXUSR)
    return daemon_dir, home, installed, stub


def _run(daemon_dir, home, stub, label, args=()):
    env = {
        "PATH": f"{stub}:/usr/bin:/bin:{Path(os.sys.executable).parent}",
        "HOME": str(home),
        "STUB_DIR": str(stub),
        "LABEL": label,
        "LANG": "C",
    }
    return subprocess.run(
        ["bash", str(daemon_dir / "install.sh"), *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(daemon_dir),
        timeout=60,
    )


def _calls(stub: Path) -> list[str]:
    log = stub / "calls.log"
    return log.read_text().splitlines() if log.exists() else []


def _leftovers(agents: Path) -> list[str]:
    return sorted(p.name for p in agents.iterdir() if p.name.startswith("."))


@pytest.fixture(params=sorted(SIMPLE_INSTALLERS))
def installer(request, tmp_path):
    name = request.param
    plist, label, args = SIMPLE_INSTALLERS[name]
    source = DAEMONS / name / "install.sh"
    daemon_dir, home, installed, stub = _tree(tmp_path, source, name, plist, label)
    return name, plist, label, args, daemon_dir, home, installed, stub


def test_refused_template_leaves_the_running_agent_and_plist_untouched(installer):
    name, plist, label, args, daemon_dir, home, installed, stub = installer
    (daemon_dir / plist).write_text(BAD_TEMPLATE.format(label=label))

    result = _run(daemon_dir, home, stub, label, args)

    assert result.returncode != 0
    assert not any(call.startswith("unload") for call in _calls(stub)), (
        "the running agent was unloaded before the renderer accepted the template"
    )
    assert not any(call.startswith("load") for call in _calls(stub))
    assert (stub / "loaded").read_text().strip() == label  # still running
    assert installed.read_text() == OLD_PLIST  # installed config unchanged
    assert _leftovers(installed.parent) == []  # no scratch files left behind
    assert "left untouched and keeps running" in result.stderr
    assert "Ab3Ab3" not in result.stdout + result.stderr  # names, never values


def test_clean_render_unloads_swaps_and_loads_in_that_order(installer):
    name, plist, label, args, daemon_dir, home, installed, stub = installer
    (daemon_dir / plist).write_text(GOOD_TEMPLATE.format(label=label))

    result = _run(daemon_dir, home, stub, label, args)

    assert result.returncode == 0, result.stderr
    verbs = [call.split()[0] for call in _calls(stub) if call.split()[0] != "list"]
    assert verbs == ["unload", "load"]
    assert "NEWMARK" in installed.read_text()
    assert (stub / "loaded").read_text().strip() == label
    assert stat.S_IMODE(installed.stat().st_mode) == 0o600
    assert _leftovers(installed.parent) == []


def test_failed_load_restores_the_previous_plist_and_says_so(installer):
    name, plist, label, args, daemon_dir, home, installed, stub = installer
    (daemon_dir / plist).write_text(GOOD_TEMPLATE.format(label=label))
    (stub / "refuse_new").write_text("")

    result = _run(daemon_dir, home, stub, label, args)

    assert result.returncode != 0
    assert installed.read_text() == OLD_PLIST  # previous config back in place
    assert (stub / "loaded").read_text().strip() == label  # and running again
    assert "restored and loaded" in result.stderr
    assert _leftovers(installed.parent) == []


def test_failed_load_with_no_way_back_names_the_recovery_command(installer):
    name, plist, label, args, daemon_dir, home, installed, stub = installer
    (daemon_dir / plist).write_text(GOOD_TEMPLATE.format(label=label))
    (stub / "refuse_all").write_text("")

    result = _run(daemon_dir, home, stub, label, args)

    assert result.returncode != 0
    assert "NOT running" in result.stderr
    assert f"launchctl load {installed}" in result.stderr


def test_first_install_with_nothing_loaded_does_not_unload(installer):
    name, plist, label, args, daemon_dir, home, installed, stub = installer
    installed.unlink()
    (stub / "loaded").write_text("")
    (daemon_dir / plist).write_text(GOOD_TEMPLATE.format(label=label))

    result = _run(daemon_dir, home, stub, label, args)

    assert result.returncode == 0, result.stderr
    assert not any(call.startswith("unload") for call in _calls(stub))
    assert "NEWMARK" in installed.read_text()


@pytest.mark.parametrize("name", ALL_INSTALLERS)
def test_every_changed_installer_goes_through_the_shared_ordered_step(name):
    text = (DAEMONS / name / "install.sh").read_text()
    assert "_install_plist.sh" in text
    assert "install_rendered_plist" in text
    # No installer unloads or renders by hand any more: that is the ordering
    # defect, and the shared step is the only place allowed to do either.
    assert "launchctl unload \"$DEST\"" not in text.replace("launchctl unload $DEST && rm $DEST", "")
    assert "render_daemon_plist.py" not in text


def test_phoenicurus_release_prepare_step_refuses_before_unloading(tmp_path):
    """The one installer whose plist step sits behind a flag: drive it for real."""
    plist, label = "com.ateles.phoenicurus-prepare.plist", "com.ateles.phoenicurus-prepare"
    source = DAEMONS / "phoenicurus-release" / "install.sh"
    daemon_dir, home, installed, stub = _tree(
        tmp_path, source, "phoenicurus-release", plist, label
    )
    (daemon_dir / plist).write_text(BAD_TEMPLATE.format(label=label))

    result = _run(daemon_dir, home, stub, label, ["--load-prepare"])

    assert result.returncode != 0
    assert not any(call.startswith("unload") for call in _calls(stub))
    assert (stub / "loaded").read_text().strip() == label
    assert installed.read_text() == OLD_PLIST
