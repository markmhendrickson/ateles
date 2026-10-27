#!/usr/bin/env python3
"""Effect tests for install_codex_turn_guard.py.

The installer edits a person's Codex home, so these tests check what a
config-presence test would miss: the install is reversible without clobbering
later edits, unrelated hooks and automations are left alone, a hostile or
unsupported automation file cannot escape or abort the batch, and the installed
command lines really run the guard (and do nothing when the script is gone,
because exit 2 would block every prompt and tool call in every thread).
"""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALLER = REPO_ROOT / "execution" / "scripts" / "install_codex_turn_guard.py"
HOOK = REPO_ROOT / ".claude" / "hooks" / "codex_turn_guard.py"

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


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, module)
    spec.loader.exec_module(module)
    return module


installer = load_module(INSTALLER, "install_codex_turn_guard_under_test")


def automation_toml(
    automation_id: str, kind: str, prompt: str, status: str = "PAUSED"
) -> str:
    return (
        "version = 1\n"
        f'id = "{automation_id}"\n'
        f'kind = "{kind}"\n'
        f'name = "{automation_id} job"\n'
        f"prompt = {json.dumps(prompt)}\n"
        f'status = "{status}"\n'
        'rrule = "FREQ=MINUTELY;INTERVAL=2"\n'
        'target_thread_id = "00000000-0000-7000-8000-000000000000"\n'
        "created_at = 1792677535566\n"
        "updated_at = 1792758155802\n"
    )


@pytest.fixture
def home(tmp_path):
    root = tmp_path / "codex"
    for name in ("big-job", "plain-task"):
        (root / "automations" / name).mkdir(parents=True)
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


def run(home: Path, *args: str, cwd: Path | None = None):
    argv = [sys.executable, str(INSTALLER), "--codex-home", str(home), *args]
    if not {"--uninstall", "--check"} & set(args):
        argv.append("--allow-worktree")
    return subprocess.run(argv, capture_output=True, text=True, timeout=60, cwd=cwd)


def read_hooks(home: Path) -> dict:
    return json.loads((home / "hooks.json").read_text())


def ours(groups):
    return [
        h for g in groups for h in g["hooks"] if "codex_turn_guard.py" in h["command"]
    ]


def automation(home: Path, name: str) -> Path:
    return home / "automations" / name / "automation.toml"


def guard_dir(home: Path) -> Path:
    return home / "ateles" / "turn_guard"


# --------------------------------------------------------------------------
# hooks.json
# --------------------------------------------------------------------------
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
    assert hooks["SessionStart"][0]["matcher"] == "compact"


def test_install_is_idempotent_and_check_agrees(home):
    assert run(home, "--check").returncode == 1
    run(home)
    first = (home / "hooks.json").read_text()
    again = run(home)
    assert again.returncode == 0 and "nothing to change" in again.stdout
    assert (home / "hooks.json").read_text() == first
    assert len(ours(read_hooks(home)["hooks"]["Stop"])) == 1
    checked = run(home, "--check")
    assert checked.returncode == 0 and "cannot be verified" in checked.stdout


def test_uninstall_restores_the_original_hooks_content(home):
    before = read_hooks(home)
    run(home)
    assert run(home, "--uninstall").returncode == 0
    assert read_hooks(home) == before


def test_uninstall_on_a_home_that_was_never_installed_says_so_and_writes_nothing(
    tmp_path,
):
    empty = tmp_path / "nothing-here"
    done = run(empty, "--uninstall")
    assert done.returncode == 0 and "nothing to remove" in done.stdout
    assert not (empty / "hooks.json").exists()


def test_uninstall_dry_run_previews_and_writes_nothing(home):
    run(home, "--heartbeats")
    snapshot = {p: p.read_bytes() for p in home.rglob("*") if p.is_file()}
    done = run(home, "--uninstall", "--dry-run")
    assert (
        done.returncode == 0
        and "would: hooks.json" in done.stdout
        and "restore the original prompt" in done.stdout
    )
    assert {p: p.read_bytes() for p in home.rglob("*") if p.is_file()} == snapshot


def test_dry_run_writes_nothing_and_discloses_the_heartbeat_cadence(home):
    snapshot = {p: p.read_bytes() for p in home.rglob("*") if p.is_file()}
    done = run(home, "--dry-run", "--heartbeats")
    assert done.returncode == 0 and "would: " in done.stdout
    assert (
        "watch file:" in done.stdout
        and "back-off 10 min doubling to 60 min" in done.stdout
    )
    assert "now: shadow" in done.stdout
    assert {p: p.read_bytes() for p in home.rglob("*") if p.is_file()} == snapshot


def test_config_toml_is_never_touched(home):
    (home / "config.toml").write_text("[features]\nhooks = true\n")
    run(home, "--heartbeats")
    assert (home / "config.toml").read_text() == "[features]\nhooks = true\n"


def test_symlinked_hooks_json_stays_a_symlink_and_keeps_its_mode(tmp_path):
    real = tmp_path / "dotfiles" / "hooks.json"
    real.parent.mkdir()
    real.write_text(json.dumps(UNRELATED_HOOKS))
    real.chmod(0o644)
    home = tmp_path / "codex"
    home.mkdir()
    (home / "hooks.json").symlink_to(real)
    assert run(home).returncode == 0
    assert (home / "hooks.json").is_symlink()
    assert "codex_turn_guard.py" in real.read_text()
    assert stat.S_IMODE(real.stat().st_mode) == 0o644


def test_malformed_hooks_json_is_an_error_naming_the_file_not_a_rewrite(home):
    (home / "hooks.json").write_text("{not json")
    done = run(home)
    assert done.returncode == 2 and "hooks.json" in done.stderr
    assert (home / "hooks.json").read_text() == "{not json"
    (home / "hooks.json").write_text("[1, 2]")
    assert run(home).returncode == 2
    assert (home / "hooks.json").read_text() == "[1, 2]"


def test_relative_codex_home_is_resolved(home, tmp_path):
    done = run(Path("codex"), "--heartbeats", cwd=tmp_path)
    assert done.returncode == 0
    brief = guard_dir(home) / "heartbeats" / "big-job" / "brief.md"
    assert brief.exists()
    assert (
        str(brief) in tomllib.loads(automation(home, "big-job").read_text())["prompt"]
    )


# --------------------------------------------------------------------------
# The installed command must run the guard, and be harmless when the script is gone
# --------------------------------------------------------------------------
def installed_command(home: Path, event: str) -> str:
    return ours(read_hooks(home)["hooks"][event])[0]["command"]


def run_shell(
    command: str, payload: dict, extra_env: dict
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["sh", "-c", command],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=dict(os.environ, **extra_env),
        timeout=60,
    )


@pytest.mark.parametrize(
    "event", ["SessionStart", "UserPromptSubmit", "PreToolUse", "Stop"]
)
def test_a_missing_hook_script_is_a_no_op_not_a_block(home, event):
    run(home)
    command = installed_command(home, event).replace(
        str(HOOK), "/nonexistent/checkout/codex_turn_guard.py"
    )
    assert "/nonexistent/checkout/codex_turn_guard.py" in command
    done = run_shell(command, {"hook_event_name": event, "session_id": "s"}, {})
    assert done.returncode == 0 and done.stdout == ""


def test_check_flags_an_installed_script_that_no_longer_exists(home):
    run(home)
    document = read_hooks(home)
    for groups in document["hooks"].values():
        for group in groups:
            for handler in group["hooks"]:
                handler["command"] = handler["command"].replace(
                    str(HOOK), "/nonexistent/codex_turn_guard.py"
                )
    (home / "hooks.json").write_text(json.dumps(document))
    done = run(home, "--check")
    assert done.returncode == 1 and "missing" in done.stdout


def test_installed_commands_deliver_the_notice_and_block_an_idle_heartbeat(
    home, tmp_path
):
    run(home, "--heartbeats", "--enforce", "heartbeat")
    state = tmp_path / "state"
    state.mkdir()
    env = {"ATELES_CODEX_GUARD_STATE_DIR": str(state), "CODEX_HOME": str(home)}
    heartbeat = "<heartbeat>\n<automation_id>wired-job</automation_id>\n</heartbeat>"
    (guard_dir(home) / "heartbeats" / "wired-job.json").write_text('{"watch": []}')

    def fire(event: str, **fields):
        payload = dict(
            fields, hook_event_name=event, session_id="wire-test", cwd=str(tmp_path)
        )
        return run_shell(installed_command(home, event), payload, env)

    assert (
        fire("UserPromptSubmit", turn_id="T1", prompt="What is blocked?").returncode
        == 0
    )
    fire("Stop", turn_id="T1", last_assistant_message="Nothing is blocked.")
    assert "What is blocked?" in fire("SessionStart", source="compact").stdout

    assert fire("UserPromptSubmit", turn_id="H1", prompt=heartbeat).stdout == ""
    fire("Stop", turn_id="H1", last_assistant_message="")
    second = fire("UserPromptSubmit", turn_id="H2", prompt=heartbeat)
    assert json.loads(second.stdout)["decision"] == "block"


# --------------------------------------------------------------------------
# Where the checkout lives
# --------------------------------------------------------------------------
def test_a_linked_worktree_checkout_is_refused_without_the_override(tmp_path):
    fake_root = tmp_path / "wt"
    fake_root.mkdir()
    (fake_root / ".git").write_text("gitdir: /elsewhere\n")
    home = tmp_path / "codex"
    assert installer.checkout_problem(fake_root) is not None
    assert installer.do_install(home, fake_root, None, [], [], False, False) == 2
    assert not home.exists()


def test_a_temporary_checkout_path_is_flagged(tmp_path):
    assert installer.checkout_problem(tmp_path) is not None
    assert installer.checkout_problem(Path("/Users/someone/repos/ateles")) is None


# --------------------------------------------------------------------------
# Modes
# --------------------------------------------------------------------------
def test_modes_start_as_shadow_for_goal_and_heartbeat(home):
    run(home)
    assert json.loads((guard_dir(home) / "mode.json").read_text()) == {
        "answered": "enforce",
        "goal": "shadow",
        "heartbeat": "shadow",
    }


def test_enforce_and_shadow_flags_flip_a_single_path(home):
    run(home)
    run(home, "--enforce", "goal")
    modes = json.loads((guard_dir(home) / "mode.json").read_text())
    assert modes["goal"] == "enforce" and modes["heartbeat"] == "shadow"
    run(home, "--shadow", "goal")
    assert json.loads((guard_dir(home) / "mode.json").read_text())["goal"] == "shadow"


def test_uninstall_removes_an_untouched_mode_file_but_keeps_an_edited_one(
    home, tmp_path
):
    run(home)
    run(home, "--uninstall")
    assert not (guard_dir(home) / "mode.json").exists()
    other = tmp_path / "second"
    other.mkdir()
    run(other)
    run(other, "--enforce", "goal")
    run(other, "--uninstall")
    assert (guard_dir(other) / "mode.json").exists()


def test_skeleton_watch_matches_the_hooks_own_heartbeat_defaults():
    guard = load_module(HOOK, "codex_turn_guard_under_test")
    assert installer.SKELETON_WATCH["base_s"] == guard.HEARTBEAT_BASE_S
    assert installer.SKELETON_WATCH["cap_s"] == guard.HEARTBEAT_CAP_S
    assert installer.DEFAULT_MODES == guard.DEFAULT_MODES


# --------------------------------------------------------------------------
# Heartbeat conversion
# --------------------------------------------------------------------------
def test_heartbeat_conversion_keeps_the_brief_and_every_other_field(home):
    path = automation(home, "big-job")
    original = path.read_bytes()
    original_data = tomllib.loads(original.decode())
    assert run(home, "--heartbeats").returncode == 0
    data = tomllib.loads(path.read_text())
    assert len(data["prompt"]) < 600 < len(original_data["prompt"])
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
    brief = guard_dir(home) / "heartbeats" / "big-job" / "brief.md"
    assert brief.read_text() == original_data["prompt"]
    assert str(brief) in data["prompt"]
    backups = list((guard_dir(home) / "backup" / "automations" / "big-job").iterdir())
    assert len(backups) == 1 and backups[0].read_bytes() == original


def test_the_guard_stub_does_not_contradict_the_brief_or_make_claims_it_cannot_act_on(
    home,
):
    run(home, "--heartbeats")
    prompt = tomllib.loads(automation(home, "big-job").read_text())["prompt"]
    assert "AGENTS.md" not in prompt and "Neotoma" not in prompt
    assert "hook" not in prompt.lower() and "binding" not in prompt.lower()
    assert "follow it exactly" in prompt


def test_non_heartbeat_automations_are_left_alone(home):
    path = automation(home, "plain-task")
    before = path.read_bytes()
    run(home, "--heartbeats")
    assert path.read_bytes() == before


def test_heartbeats_are_opt_in(home):
    path = automation(home, "big-job")
    before = path.read_bytes()
    run(home)
    assert path.read_bytes() == before


def test_named_heartbeat_only_and_unknown_names_are_an_error(home):
    other = home / "automations" / "second"
    other.mkdir()
    (other / "automation.toml").write_text(
        automation_toml("second", "heartbeat", "x" * 3000)
    )
    assert run(home, "--heartbeats", "second").returncode == 0
    assert tomllib.loads((other / "automation.toml").read_text())["prompt"].startswith(
        "<!-- ateles-turn-guard:"
    )
    assert not tomllib.loads(automation(home, "big-job").read_text())[
        "prompt"
    ].startswith("<!--")
    typo = run(home, "--heartbeats", "nosuch")
    assert typo.returncode == 2 and "nosuch" in typo.stderr and "second" in typo.stderr


def test_conversion_is_idempotent(home):
    run(home, "--heartbeats")
    path = automation(home, "big-job")
    once = path.read_bytes()
    assert run(home, "--heartbeats").returncode == 0
    assert path.read_bytes() == once
    assert (
        len(list((guard_dir(home) / "backup" / "automations" / "big-job").iterdir()))
        == 1
    )


def test_an_unconvertible_automation_is_skipped_and_the_rest_still_revertible(home):
    bad = home / "automations" / "multi-line"
    bad.mkdir()
    bad_text = automation_toml("multi-line", "heartbeat", "x").replace(
        'prompt = "x"', 'prompt = """\nline one\nline two\n"""'
    )
    (bad / "automation.toml").write_text(bad_text)
    done = run(home, "--heartbeats")
    assert done.returncode == 0 and "multi-line: SKIPPED" in done.stdout
    assert (bad / "automation.toml").read_text() == bad_text
    assert tomllib.loads(automation(home, "big-job").read_text())["prompt"].startswith(
        "<!--"
    )
    assert run(home, "--uninstall").returncode == 0
    restored = tomllib.loads(automation(home, "big-job").read_text())
    assert not restored["prompt"].startswith("<!--")


def test_hostile_ids_cannot_write_outside_the_guard_directory(home, tmp_path):
    evil = home / "automations" / "evil"
    evil.mkdir()
    (evil / "automation.toml").write_text(
        automation_toml("../../../escaped", "heartbeat", "secret prompt")
    )
    spaced = home / "automations" / "has space"
    spaced.mkdir()
    (spaced / "automation.toml").write_text(
        automation_toml("has space", "heartbeat", "other prompt")
    )
    done = run(home, "--heartbeats")
    assert done.returncode == 0
    assert "evil: SKIPPED" in done.stdout and "has space: SKIPPED" in done.stdout
    written = {
        p
        for p in tmp_path.rglob("*")
        if p.is_file() and "secret prompt" in p.read_text(errors="ignore")
    }
    assert written == {evil / "automation.toml"}


def test_a_tampered_manifest_cannot_make_uninstall_touch_outside_the_codex_home(
    home, tmp_path
):
    run(home, "--heartbeats")
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me")
    manifest_file = guard_dir(home) / "manifest.json"
    manifest = json.loads(manifest_file.read_text())
    entry = manifest["automations"]["big-job"]
    entry["brief"] = str(victim)
    entry["watch"] = str(victim)
    manifest_file.write_text(json.dumps(manifest))
    done = run(home, "--uninstall")
    assert victim.read_text() == "keep me"
    assert "outside" in done.stdout


def test_uninstall_restores_the_original_prompt_line_exactly(home):
    path = automation(home, "big-job")
    original = path.read_text()
    run(home, "--heartbeats")
    assert path.read_text() != original
    assert run(home, "--uninstall").returncode == 0
    now = path.read_text().split("\n")
    then = original.split("\n")
    changed = [(a, b) for a, b in zip(now, then) if a != b]
    assert [a.split(" = ")[0] for a, _ in changed] == ["updated_at"]
    assert not (guard_dir(home) / "heartbeats" / "big-job" / "brief.md").exists()
    assert not (guard_dir(home) / "heartbeats" / "big-job.json").exists()


def test_uninstall_keeps_later_edits_to_other_fields(home):
    path = automation(home, "big-job")
    run(home, "--heartbeats")
    edited = (
        path.read_text()
        .replace('status = "PAUSED"', 'status = "ACTIVE"')
        .replace("INTERVAL=2", "INTERVAL=30")
    )
    path.write_text(edited)
    assert run(home, "--uninstall").returncode == 0
    data = tomllib.loads(path.read_text())
    assert data["status"] == "ACTIVE" and "INTERVAL=30" in data["rrule"]
    assert not data["prompt"].startswith("<!--")


def test_uninstall_refuses_to_clobber_a_prompt_edited_since_and_remembers_it(home):
    path = automation(home, "big-job")
    run(home, "--heartbeats")
    edited = path.read_text().replace(
        "<!-- ateles-turn-guard:big-job -->", "operator rewrote this"
    )
    path.write_text(edited)
    done = run(home, "--uninstall")
    assert "edited since install" in done.stdout
    assert path.read_text() == edited
    manifest = json.loads((guard_dir(home) / "manifest.json").read_text())
    assert "big-job" in manifest["automations"]  # kept, so a later run can retry


def test_uninstall_keeps_a_watch_file_the_operator_edited(home):
    run(home, "--heartbeats")
    watch = guard_dir(home) / "heartbeats" / "big-job.json"
    watch.write_text(
        json.dumps({"watch": [{"name": "pr", "argv": ["gh", "pr", "view", "1"]}]})
    )
    done = run(home, "--uninstall")
    assert watch.exists() and "kept" in done.stdout


def test_check_with_heartbeats_flags_an_unconverted_automation(home):
    run(home)
    assert run(home, "--check", "--heartbeats").returncode == 1
    run(home, "--heartbeats")
    assert run(home, "--check", "--heartbeats").returncode == 0
