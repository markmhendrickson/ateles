#!/usr/bin/env python3
"""Effect tests for install_codex_turn_guard.py.

The installer edits a person's Codex home, so these tests check three things a
config-presence test would miss: the install is reversible byte-for-byte,
unrelated hooks and automations are left alone, and the installed command
lines actually run the guard (a wired-but-broken path is the failure that
ships silently).
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALLER = REPO_ROOT / "execution" / "scripts" / "install_codex_turn_guard.py"

UNRELATED_HOOKS = {
    "description": "operator hooks",
    "hooks": {
        "Stop": [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": "python3 /opt/other/stop_hook.py",
                        "timeout": 5,
                    }
                ]
            }
        ],
        "PreToolUse": [
            {
                "matcher": "^Bash$",
                "hooks": [
                    {"type": "command", "command": "python3 /opt/other/guard.py"}
                ],
            }
        ],
    },
}


def automation_toml(automation_id: str, kind: str, prompt: str) -> str:
    return (
        "version = 1\n"
        f'id = "{automation_id}"\n'
        f'kind = "{kind}"\n'
        f'name = "{automation_id} job"\n'
        f"prompt = {json.dumps(prompt)}\n"
        'status = "PAUSED"\n'
        'rrule = "FREQ=MINUTELY;INTERVAL=2"\n'
        'target_thread_id = "00000000-0000-7000-8000-000000000000"\n'
        "created_at = 1792677535566\n"
        "updated_at = 1792758155802\n"
    )


@pytest.fixture
def home(tmp_path):
    root = tmp_path / "codex"
    (root / "automations" / "big-job").mkdir(parents=True)
    (root / "automations" / "plain-task").mkdir(parents=True)
    (root / "hooks.json").write_text(json.dumps(UNRELATED_HOOKS, indent=2) + "\n")
    big_prompt = (
        "Continue the delivery. \"quoted\" and 'single'\nsecond line \\ backslash. "
        + ("state " * 20000)
    )
    (root / "automations" / "big-job" / "automation.toml").write_text(
        automation_toml("big-job", "heartbeat", big_prompt)
    )
    (root / "automations" / "plain-task" / "automation.toml").write_text(
        automation_toml("plain-task", "cron", "run it")
    )
    return root


def run(home: Path, *args: str):
    return subprocess.run(
        [sys.executable, str(INSTALLER), "--codex-home", str(home), *args],
        capture_output=True,
        text=True,
        timeout=60,
    )


def read_hooks(home: Path) -> dict:
    return json.loads((home / "hooks.json").read_text())


def ours(groups):
    return [
        h for g in groups for h in g["hooks"] if "codex_turn_guard.py" in h["command"]
    ]


def test_install_adds_four_entries_and_keeps_unrelated_hooks(home):
    assert run(home).returncode == 0
    hooks = read_hooks(home)["hooks"]
    assert {e for e, g in hooks.items() if ours(g)} == {
        "SessionStart",
        "UserPromptSubmit",
        "PreToolUse",
        "Stop",
    }
    assert any(
        "/opt/other/stop_hook.py" in h["command"]
        for g in hooks["Stop"]
        for h in g["hooks"]
    )
    assert any(
        "/opt/other/guard.py" in h["command"]
        for g in hooks["PreToolUse"]
        for h in g["hooks"]
    )
    start = hooks["SessionStart"][0]
    assert start["matcher"] == "compact"


def test_install_is_idempotent_and_check_agrees(home):
    assert run(home, "--check").returncode == 1
    run(home)
    first = (home / "hooks.json").read_text()
    assert run(home).returncode == 0
    assert (home / "hooks.json").read_text() == first
    assert len(ours(read_hooks(home)["hooks"]["Stop"])) == 1
    assert run(home, "--check").returncode == 0


def test_uninstall_restores_the_original_hooks_exactly(home):
    before = read_hooks(home)
    run(home)
    assert run(home, "--uninstall").returncode == 0
    assert read_hooks(home) == before


def test_dry_run_writes_nothing(home):
    snapshot = {p: p.read_bytes() for p in home.rglob("*") if p.is_file()}
    done = run(home, "--dry-run", "--heartbeats")
    assert done.returncode == 0 and "would:" in done.stdout
    assert {p: p.read_bytes() for p in home.rglob("*") if p.is_file()} == snapshot


def test_config_toml_is_never_touched(home):
    (home / "config.toml").write_text("[features]\nhooks = true\n")
    run(home, "--heartbeats")
    assert (home / "config.toml").read_text() == "[features]\nhooks = true\n"


def test_heartbeat_conversion_keeps_the_brief_and_every_other_field(home):
    path = home / "automations" / "big-job" / "automation.toml"
    original = path.read_bytes()
    original_data = tomllib.loads(original.decode())
    assert run(home, "--heartbeats").returncode == 0
    data = tomllib.loads(path.read_text())
    assert len(data["prompt"]) < 1200 < len(original_data["prompt"])
    assert data["prompt"].startswith("<!-- ateles-turn-guard:big-job -->")
    for key in (
        "version",
        "id",
        "kind",
        "name",
        "status",
        "rrule",
        "target_thread_id",
        "created_at",
    ):
        assert data[key] == original_data[key]
    assert data["updated_at"] > original_data["updated_at"]
    brief = home / "ateles" / "turn_guard" / "heartbeats" / "big-job" / "brief.md"
    assert brief.read_text() == original_data["prompt"]
    assert str(brief) in data["prompt"]
    backups = list(
        (
            home / "ateles" / "turn_guard" / "backup" / "automations" / "big-job"
        ).iterdir()
    )
    assert len(backups) == 1 and backups[0].read_bytes() == original


def test_non_heartbeat_automations_are_left_alone(home):
    path = home / "automations" / "plain-task" / "automation.toml"
    before = path.read_bytes()
    run(home, "--heartbeats")
    assert path.read_bytes() == before


def test_heartbeats_are_opt_in(home):
    path = home / "automations" / "big-job" / "automation.toml"
    before = path.read_bytes()
    run(home)
    assert path.read_bytes() == before


def test_named_heartbeat_only(home):
    other = home / "automations" / "second"
    other.mkdir()
    (other / "automation.toml").write_text(
        automation_toml("second", "heartbeat", "x" * 3000)
    )
    run(home, "--heartbeats", "second")
    assert tomllib.loads((other / "automation.toml").read_text())["prompt"].startswith(
        "<!-- ateles-turn-guard:"
    )
    assert not tomllib.loads(
        (home / "automations" / "big-job" / "automation.toml").read_text()
    )["prompt"].startswith("<!--")


def test_conversion_is_idempotent(home):
    run(home, "--heartbeats")
    path = home / "automations" / "big-job" / "automation.toml"
    once = path.read_bytes()
    assert run(home, "--heartbeats").returncode == 0
    assert path.read_bytes() == once
    assert (
        len(
            list(
                (
                    home
                    / "ateles"
                    / "turn_guard"
                    / "backup"
                    / "automations"
                    / "big-job"
                ).iterdir()
            )
        )
        == 1
    )


def test_uninstall_restores_automation_bytes_exactly(home):
    path = home / "automations" / "big-job" / "automation.toml"
    original = path.read_bytes()
    run(home, "--heartbeats")
    assert path.read_bytes() != original
    assert run(home, "--uninstall").returncode == 0
    assert path.read_bytes() == original
    assert not (
        home / "ateles" / "turn_guard" / "heartbeats" / "big-job" / "brief.md"
    ).exists()


def test_uninstall_refuses_to_clobber_an_automation_edited_since(home):
    path = home / "automations" / "big-job" / "automation.toml"
    run(home, "--heartbeats")
    edited = path.read_text().replace('status = "PAUSED"', 'status = "ACTIVE"')
    edited = edited.replace(
        "<!-- ateles-turn-guard:big-job -->", "operator rewrote this"
    )
    path.write_text(edited)
    done = run(home, "--uninstall")
    assert "edited since install" in done.stdout
    assert path.read_text() == edited


def test_check_with_heartbeats_flags_an_unconverted_automation(home):
    run(home)
    assert run(home, "--check", "--heartbeats").returncode == 1
    run(home, "--heartbeats")
    assert run(home, "--check", "--heartbeats").returncode == 0


def test_malformed_hooks_json_is_an_error_not_a_rewrite(home):
    (home / "hooks.json").write_text("[1, 2]")
    done = run(home)
    assert done.returncode == 2
    assert (home / "hooks.json").read_text() == "[1, 2]"


# --------------------------------------------------------------------------
# The installed command lines must run the guard, not merely exist.
# --------------------------------------------------------------------------
def run_installed(
    home: Path, event: str, state: Path, payload: dict
) -> subprocess.CompletedProcess:
    hooks = read_hooks(home)["hooks"][event]
    command = ours(hooks)[0]["command"]
    env = dict(
        os.environ, ATELES_CODEX_GUARD_STATE_DIR=str(state), CODEX_HOME=str(home)
    )
    return subprocess.run(
        shlex.split(command),
        input=json.dumps(
            dict(
                payload,
                hook_event_name=event,
                session_id="wire-test",
                cwd=str(REPO_ROOT),
            )
        ),
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


def test_installed_commands_deliver_the_notice_and_block_an_idle_heartbeat(
    home, tmp_path
):
    run(home)
    state = tmp_path / "state"
    state.mkdir()
    heartbeat = "<heartbeat>\n<automation_id>wired-job</automation_id>\n</heartbeat>"
    prompt = run_installed(
        home, "UserPromptSubmit", state, {"turn_id": "T1", "prompt": "What is blocked?"}
    )
    assert prompt.returncode == 0
    run_installed(
        home,
        "Stop",
        state,
        {"turn_id": "T1", "last_assistant_message": "Nothing is blocked."},
    )
    notice = run_installed(home, "SessionStart", state, {"source": "compact"})
    assert "What is blocked?" in notice.stdout

    first = run_installed(
        home, "UserPromptSubmit", state, {"turn_id": "H1", "prompt": heartbeat}
    )
    assert first.stdout == ""
    run_installed(home, "Stop", state, {"turn_id": "H1", "last_assistant_message": ""})
    second = run_installed(
        home, "UserPromptSubmit", state, {"turn_id": "H2", "prompt": heartbeat}
    )
    assert json.loads(second.stdout)["decision"] == "block"
