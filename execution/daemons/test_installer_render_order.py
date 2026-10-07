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
# Stub: records every call and models ONE job as a `launchctl list` row
# (PID, status, label) in $STUB_DIR/loaded. Flags are files in $STUB_DIR:
#   refuse_unload  unload exits 1 and the job is left as it is
#   unload_noop    unload exits 0 but the job stays listed
#   refuse_new     load exits 1 for a plist carrying NEWMARK
#   refuse_all     every load exits 1
#   run_at_load    a successful load leaves a running process (pid 4242)
#   list_fails     `list` exits 1
echo "$*" >> "$STUB_DIR/calls.log"
case "$1" in
  list)
    if [ -f "$STUB_DIR/list_fails" ]; then exit 1; fi
    cat "$STUB_DIR/loaded" 2>/dev/null; exit 0 ;;
  unload)
    if [ -f "$STUB_DIR/refuse_unload" ]; then exit 1; fi
    if [ -f "$STUB_DIR/unload_noop" ]; then exit 0; fi
    : > "$STUB_DIR/loaded"; exit 0 ;;
  load)
    if [ -f "$STUB_DIR/refuse_new" ] && grep -q NEWMARK "$2"; then exit 1; fi
    if [ -f "$STUB_DIR/refuse_all" ]; then exit 1; fi
    if [ -f "$STUB_DIR/run_at_load" ]; then
      printf '4242\t0\t%s\n' "$LABEL" > "$STUB_DIR/loaded"
    else
      printf -- '-\t0\t%s\n' "$LABEL" > "$STUB_DIR/loaded"
    fi
    exit 0 ;;
esac
exit 0
"""


def _set_job(stub: Path, label: str, pid: str | None) -> None:
    """Model the job as listed: a pid (running), "-" (registered, idle) or None (absent)."""
    row = "" if pid is None else f"{pid}\t0\t{label}\n"
    (stub / "loaded").write_text(row)


def _job(stub: Path) -> str | None:
    """The first column of the modelled listing, or None when the job is absent."""
    text = (stub / "loaded").read_text().strip()
    return text.split("\t")[0] if text else None


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
    _set_job(stub, label, "123")  # the existing job is running (pid 123)
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


def _verbs(stub):
    return [c.split()[0] for c in _calls(stub) if c.split()[0] != "list"]


def test_refused_template_leaves_the_running_agent_and_plist_untouched(installer):
    name, plist, label, args, daemon_dir, home, installed, stub = installer
    (daemon_dir / plist).write_text(BAD_TEMPLATE.format(label=label))

    result = _run(daemon_dir, home, stub, label, args)

    assert result.returncode != 0
    assert _verbs(stub) == [], "launchd was touched before the renderer accepted the template"
    assert _job(stub) == "123"  # still running
    assert installed.read_text() == OLD_PLIST  # installed config unchanged
    assert _leftovers(installed.parent) == []  # no scratch files left behind
    assert "left untouched" in result.stderr
    assert "Ab3Ab3" not in result.stdout + result.stderr  # names, never values


def test_clean_render_of_a_scheduled_template_is_reported_as_registered_not_running(installer):
    name, plist, label, args, daemon_dir, home, installed, stub = installer
    (daemon_dir / plist).write_text(GOOD_TEMPLATE.format(label=label))

    result = _run(daemon_dir, home, stub, label, args)

    assert result.returncode == 0, result.stderr
    assert _verbs(stub) == ["unload", "load"]
    assert "NEWMARK" in installed.read_text()
    assert _job(stub) == "-"  # registered, idle: no process claimed
    assert "registered (scheduled; runs at its next trigger)" in result.stdout
    assert f"{name} job: running" not in result.stdout  # no process is claimed after the load
    assert stat.S_IMODE(installed.stat().st_mode) == 0o600
    assert _leftovers(installed.parent) == []


def test_clean_render_that_leaves_a_live_process_is_reported_as_running(installer):
    name, plist, label, args, daemon_dir, home, installed, stub = installer
    (daemon_dir / plist).write_text(GOOD_TEMPLATE.format(label=label))
    (stub / "run_at_load").write_text("")

    result = _run(daemon_dir, home, stub, label, args)

    assert result.returncode == 0, result.stderr
    assert "running (pid 4242)" in result.stdout


def test_unload_failure_while_the_job_is_still_loaded_stops_and_reports_the_observed_state(
    installer,
):
    name, plist, label, args, daemon_dir, home, installed, stub = installer
    (daemon_dir / plist).write_text(GOOD_TEMPLATE.format(label=label))
    (stub / "refuse_unload").write_text("")
    # The old behavior: unload and both loads fail and the job keeps running.
    (stub / "refuse_all").write_text("")

    result = _run(daemon_dir, home, stub, label, args)

    assert result.returncode != 0
    assert _verbs(stub) == ["unload"], "nothing may be loaded after a failed unload"
    assert installed.read_text() == OLD_PLIST  # configuration not replaced
    assert _job(stub) == "123"  # the existing job is untouched
    assert "could not unload" in result.stderr
    assert "NOT replaced" in result.stderr
    assert "running (pid 123)" in result.stderr  # the observed state, not an inference
    assert "NOT running" not in result.stderr
    assert "launchctl load" not in result.stderr  # no reload prescribed for a live job
    assert _leftovers(installed.parent) == []


def test_unload_that_exits_zero_but_leaves_the_job_listed_also_stops(installer):
    name, plist, label, args, daemon_dir, home, installed, stub = installer
    (daemon_dir / plist).write_text(GOOD_TEMPLATE.format(label=label))
    (stub / "unload_noop").write_text("")

    result = _run(daemon_dir, home, stub, label, args)

    assert result.returncode != 0
    assert _verbs(stub) == ["unload"]
    assert installed.read_text() == OLD_PLIST
    assert "running (pid 123)" in result.stderr


def test_load_failure_after_unload_restores_the_previous_plist_and_reports_the_observed_state(
    installer,
):
    name, plist, label, args, daemon_dir, home, installed, stub = installer
    (daemon_dir / plist).write_text(GOOD_TEMPLATE.format(label=label))
    (stub / "refuse_new").write_text("")

    result = _run(daemon_dir, home, stub, label, args)

    assert result.returncode != 0
    assert installed.read_text() == OLD_PLIST  # previous configuration back in place
    assert _job(stub) == "-"  # and registered again, as observed
    assert "previous plist was put back" in result.stderr
    assert "registered (scheduled; runs at its next trigger)" in result.stderr
    assert "running as before" not in result.stderr + result.stdout
    assert "NOT running" not in result.stderr
    assert _leftovers(installed.parent) == []


def test_load_failure_that_cannot_be_restored_names_the_observed_state_and_the_command(
    installer,
):
    name, plist, label, args, daemon_dir, home, installed, stub = installer
    (daemon_dir / plist).write_text(GOOD_TEMPLATE.format(label=label))
    (stub / "refuse_all").write_text("")

    result = _run(daemon_dir, home, stub, label, args)

    assert result.returncode != 0
    assert installed.read_text() == OLD_PLIST
    assert _job(stub) is None
    assert "Job state now observed: not registered" in result.stderr
    assert f"launchctl load {installed}" in result.stderr


def test_unknown_launchd_state_changes_nothing_and_says_so(installer):
    name, plist, label, args, daemon_dir, home, installed, stub = installer
    (daemon_dir / plist).write_text(GOOD_TEMPLATE.format(label=label))
    (stub / "list_fails").write_text("")

    result = _run(daemon_dir, home, stub, label, args)

    assert result.returncode != 0
    assert [v for v in _verbs(stub)] == []
    assert installed.read_text() == OLD_PLIST
    assert "state of the existing job is unknown" in result.stderr
    assert _leftovers(installed.parent) == []


def test_first_install_with_nothing_registered_does_not_unload(installer):
    name, plist, label, args, daemon_dir, home, installed, stub = installer
    installed.unlink()
    _set_job(stub, label, None)
    (daemon_dir / plist).write_text(GOOD_TEMPLATE.format(label=label))

    result = _run(daemon_dir, home, stub, label, args)

    assert result.returncode == 0, result.stderr
    assert _verbs(stub) == ["load"]
    assert "NEWMARK" in installed.read_text()


def test_first_install_load_failure_removes_the_new_plist_and_says_there_was_no_previous_one(
    installer,
):
    name, plist, label, args, daemon_dir, home, installed, stub = installer
    installed.unlink()
    _set_job(stub, label, None)
    (daemon_dir / plist).write_text(GOOD_TEMPLATE.format(label=label))
    (stub / "refuse_all").write_text("")

    result = _run(daemon_dir, home, stub, label, args)

    assert result.returncode != 0
    assert not installed.exists()
    assert "there was no previous plist" in result.stderr
    assert "not registered" in result.stderr


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
    assert _verbs(stub) == []
    assert _job(stub) == "123"
    assert installed.read_text() == OLD_PLIST
