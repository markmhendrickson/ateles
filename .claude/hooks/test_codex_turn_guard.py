#!/usr/bin/env python3
"""Effect tests for codex_turn_guard.py.

Every case pipes a synthetic Codex hook event (the field names are the ones in
the Codex 0.157.1 hook input schemas) to the real script and asserts on its
stdout, exit code and state file. The transcript is a synthetic rollout with
the same `task_started.turn_attribution.turn_trigger` shape Codex writes.

Read each test as "what did the thread do, what did the guard do": a test that
only checks the script exits 0 would pass on an empty file.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

HOOK = str(Path(__file__).with_name("codex_turn_guard.py"))
SID = "01a1-test-thread"


class Env:
    def __init__(self, tmp_path: Path, mode: str = "enforce") -> None:
        self.state = tmp_path / "state"
        self.home = tmp_path / "codex_home"
        self.repo = tmp_path / "repo"
        self.transcript = tmp_path / "rollout.jsonl"
        self.state.mkdir()
        self.home.mkdir()
        self.repo.mkdir()
        self.transcript.write_text("")
        self.mode = mode
        self._git("init", "-q")
        (self.repo / "a.txt").write_text("one")
        self._git("add", "a.txt")
        self._git(
            "-c",
            "user.email=t@example.com",
            "-c",
            "user.name=t",
            "commit",
            "-q",
            "-m",
            "init",
        )

    def _git(self, *args: str) -> None:
        subprocess.run(["git", *args], cwd=self.repo, check=True, capture_output=True)

    def commit(self) -> None:
        (self.repo / "a.txt").write_text(str(time.time()))
        self._git("add", "a.txt")
        self._git(
            "-c",
            "user.email=t@example.com",
            "-c",
            "user.name=t",
            "commit",
            "-q",
            "-m",
            "next",
        )

    def start_turn(self, turn_id: str, trigger: str) -> None:
        record = {
            "type": "event_msg",
            "payload": {
                "type": "task_started",
                "turn_attribution": {"turn_id": turn_id, "turn_trigger": trigger},
            },
        }
        with self.transcript.open("a") as handle:
            handle.write(json.dumps(record) + "\n")

    def fire(self, event: str, turn_id: str = "", **fields):
        payload = {
            "session_id": SID,
            "hook_event_name": event,
            "cwd": str(self.repo),
            "transcript_path": str(self.transcript),
            "model": "m",
        }
        if turn_id:
            payload["turn_id"] = turn_id
        payload.update(fields)
        env = dict(os.environ)
        env.update(
            ATELES_CODEX_GUARD_STATE_DIR=str(self.state),
            CODEX_HOME=str(self.home),
            ATELES_CODEX_TURN_GUARD=self.mode,
        )
        done = subprocess.run(
            [sys.executable, HOOK],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )
        return done

    def state_json(self) -> dict:
        return json.loads((self.state / f"codex_turn_guard.{SID}.json").read_text())

    def age_streak(self, name: str, seconds: float) -> None:
        path = self.state / f"codex_turn_guard.{SID}.json"
        data = json.loads(path.read_text())
        data["streaks"][name]["ended_ts"] -= seconds
        path.write_text(json.dumps(data))

    # Convenience turns -------------------------------------------------
    def operator_turn(self, turn_id: str, prompt: str, answer: str | None) -> None:
        self.start_turn(turn_id, "composer")
        self.fire("UserPromptSubmit", turn_id, prompt=prompt, permission_mode="default")
        if answer is not None:
            self.fire(
                "Stop", turn_id, last_assistant_message=answer, stop_hook_active=False
            )

    def goal_turn(
        self, turn_id: str, tool: str = "Bash", command: str = "git status"
    ) -> subprocess.CompletedProcess:
        self.start_turn(turn_id, "goal")
        first = self.fire(
            "PreToolUse",
            turn_id,
            tool_name=tool,
            tool_input={"command": command},
            tool_use_id="t1",
        )
        if first.returncode == 0:
            self.fire(
                "Stop", turn_id, last_assistant_message="status", stop_hook_active=False
            )
        return first


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def deny_reason(done) -> str:
    out = json.loads(done.stdout)["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"
    return out["permissionDecisionReason"]


# --------------------------------------------------------------------------
# (a) answered marker
# --------------------------------------------------------------------------
def test_compaction_notice_names_the_answered_message(env):
    env.operator_turn(
        "T1",
        "Why do I need to submit a report?",
        "You do not have to; I overstated that.",
    )
    done = env.fire("SessionStart", source="compact")
    assert done.returncode == 0
    assert "Why do I need to submit a report?" in done.stdout
    assert "Do NOT answer it again" in done.stdout
    assert "I overstated that" in done.stdout


@pytest.mark.parametrize("source", ["startup", "resume", "clear", "fork"])
def test_no_notice_outside_compaction(env, source):
    env.operator_turn("T1", "A question", "An answer")
    assert env.fire("SessionStart", source=source).stdout == ""


def test_no_notice_while_the_message_is_still_unanswered(env):
    env.operator_turn("T1", "A question", None)
    assert env.fire("SessionStart", source="compact").stdout == ""


def test_other_turns_stop_does_not_mark_the_prompt_answered(env):
    env.operator_turn("T1", "A question", None)
    env.start_turn("T0", "goal")
    env.fire(
        "Stop",
        "T0",
        last_assistant_message="unrelated goal report",
        stop_hook_active=False,
    )
    assert env.fire("SessionStart", source="compact").stdout == ""


def test_a_newer_unanswered_message_supersedes_the_answered_one(env):
    env.operator_turn("T1", "First question", "First answer")
    env.operator_turn("T2", "Second question", None)
    assert env.fire("SessionStart", source="compact").stdout == ""


def test_notice_uses_no_more_than_the_context_cap(env):
    env.operator_turn("T1", "q" * 5000, "a" * 5000)
    assert len(env.fire("SessionStart", source="compact").stdout) < 1500


# --------------------------------------------------------------------------
# (b) goal-turn guard
# --------------------------------------------------------------------------
def test_first_goal_turn_runs_and_a_quiet_repeat_is_denied(env):
    assert env.goal_turn("G1").returncode == 0
    second = env.goal_turn("G2", command="neotoma retrieve_entities --type task")
    assert second.returncode == 2
    reason = deny_reason(second)
    assert "End this turn now" in reason and "do not call update_goal" in reason


def test_every_tool_call_of_a_skipped_turn_is_denied(env):
    env.goal_turn("G1")
    env.start_turn("G2", "goal")
    for n in range(3):
        done = env.fire(
            "PreToolUse",
            "G2",
            tool_name="Bash",
            tool_input={"command": f"echo {n}"},
            tool_use_id=str(n),
        )
        assert done.returncode == 2


def test_denied_turn_does_not_slide_the_window(env):
    env.goal_turn("G1")
    before = env.state_json()["streaks"]["goal"]["ended_ts"]
    env.goal_turn("G2")
    after = env.state_json()["streaks"]["goal"]
    assert after["ended_ts"] == before
    assert after["suppressed"] == 1


def test_window_elapsing_allows_a_recheck_and_doubles_the_next_window(env):
    env.goal_turn("G1")
    env.age_streak("goal", 301)
    assert env.goal_turn("G2").returncode == 0
    assert env.state_json()["streaks"]["goal"]["quiet"] == 2
    env.age_streak("goal", 301)  # 301s is inside the doubled 600s window
    assert env.goal_turn("G3").returncode == 2


def test_repository_change_allows_the_turn(env):
    env.goal_turn("G1")
    env.commit()
    assert env.goal_turn("G2").returncode == 0


def test_new_operator_message_allows_the_turn(env):
    env.goal_turn("G1")
    env.operator_turn("T1", "New instruction", "Done")
    assert env.goal_turn("G2").returncode == 0


def test_a_goal_turn_that_wrote_something_is_progress(env):
    env.goal_turn("G1", command="git commit -m work")
    assert env.state_json()["streaks"]["goal"]["quiet"] == 0
    assert env.goal_turn("G2").returncode == 0


def test_mutating_mcp_call_is_progress_and_read_call_is_not(env):
    env.goal_turn("G1", tool="mcp__neotoma__store", command="x")
    assert env.state_json()["streaks"]["goal"]["quiet"] == 0
    env.goal_turn("G2", tool="mcp__neotoma__retrieve_entities", command="x")
    assert env.state_json()["streaks"]["goal"]["quiet"] == 1


def test_operator_turns_are_never_denied(env):
    env.goal_turn("G1")
    env.start_turn("R1", "composer")
    env.fire("UserPromptSubmit", "R1", prompt="do the thing", permission_mode="default")
    done = env.fire(
        "PreToolUse",
        "R1",
        tool_name="Bash",
        tool_input={"command": "ls"},
        tool_use_id="x",
    )
    assert done.returncode == 0 and done.stdout == ""


def test_watch_command_change_allows_the_turn(env):
    outside = env.home / "probe.txt"
    outside.write_text("v1")
    watch = env.home / "ateles" / "turn_guard"
    watch.mkdir(parents=True)
    (watch / "goal_watch.json").write_text(
        json.dumps({"watch": [{"name": "probe", "argv": ["cat", str(outside)]}]})
    )
    env.goal_turn("G1")
    assert env.goal_turn("G2").returncode == 2
    outside.write_text("v2")
    assert env.goal_turn("G3").returncode == 0


def test_shadow_mode_decides_but_never_denies(tmp_path):
    shadow = Env(tmp_path, mode="shadow")
    shadow.goal_turn("G1")
    assert shadow.goal_turn("G2").returncode == 0
    assert shadow.state_json()["streaks"]["goal"]["suppressed"] == 1


def test_off_mode_does_nothing(tmp_path):
    off = Env(tmp_path, mode="off")
    off.goal_turn("G1")
    assert off.goal_turn("G2").returncode == 0
    assert not (off.state / f"codex_turn_guard.{SID}.json").exists()


def test_subagent_events_are_ignored(env):
    env.goal_turn("G1")
    env.start_turn("G2", "goal")
    done = env.fire(
        "PreToolUse",
        "G2",
        tool_name="Bash",
        tool_input={"command": "ls"},
        tool_use_id="x",
        agent_id="sub1",
    )
    assert done.returncode == 0 and done.stdout == ""


def test_unknown_turn_kind_is_never_guarded(env):
    env.goal_turn("G1")
    # No task_started record for G9: the guard cannot tell what started it.
    done = env.fire(
        "PreToolUse",
        "G9",
        tool_name="Bash",
        tool_input={"command": "ls"},
        tool_use_id="x",
    )
    assert done.returncode == 0


@pytest.mark.parametrize("raw", ["", "not json", "[]", "null"])
def test_garbage_input_fails_open(env, raw):
    done = subprocess.run(
        [sys.executable, HOOK], input=raw, capture_output=True, text=True, timeout=30
    )
    assert done.returncode == 0 and done.stdout == ""


# --------------------------------------------------------------------------
# (c) heartbeat guard
# --------------------------------------------------------------------------
HEARTBEAT = (
    "<heartbeat>\n  <automation_id>demo-follow-through</automation_id>\n"
    "  <current_time_iso>2026-10-23T12:15:09.684Z</current_time_iso>\n  <instructions>\nDo the thing.\n"
    "  </instructions>\n</heartbeat>"
)


def heartbeat(env: Env, turn_id: str, final: str = ""):
    env.start_turn(turn_id, "automation_heartbeat_scheduled")
    first = env.fire(
        "UserPromptSubmit", turn_id, prompt=HEARTBEAT, permission_mode="default"
    )
    if not first.stdout:
        env.fire("Stop", turn_id, last_assistant_message=final, stop_hook_active=False)
    return first


def test_idle_heartbeat_is_blocked_before_the_model_runs(env):
    assert heartbeat(env, "H1").stdout == ""
    second = heartbeat(env, "H2")
    assert second.returncode == 0
    out = json.loads(second.stdout)
    assert out["decision"] == "block"
    assert "demo-follow-through" in out["reason"]
    assert set(out) <= {
        "decision",
        "reason",
        "continue",
        "stopReason",
        "suppressOutput",
        "systemMessage",
    }


def test_heartbeat_with_real_output_resets_the_window(env):
    heartbeat(env, "H1", final="x" * 400)
    assert heartbeat(env, "H2").stdout == ""


def test_heartbeat_window_elapsing_allows_a_check(env):
    heartbeat(env, "H1")
    env.age_streak("heartbeat:demo-follow-through", 601)
    assert heartbeat(env, "H2").stdout == ""


def test_heartbeats_are_tracked_per_automation(env):
    heartbeat(env, "H1")
    other = HEARTBEAT.replace("demo-follow-through", "other-job")
    env.start_turn("H2", "automation_heartbeat_scheduled")
    assert (
        env.fire(
            "UserPromptSubmit", "H2", prompt=other, permission_mode="default"
        ).stdout
        == ""
    )


def test_heartbeat_watch_change_allows_the_run(env):
    outside = env.home / "pr_state.txt"
    outside.write_text("open")
    spec_dir = env.home / "ateles" / "turn_guard" / "heartbeats"
    spec_dir.mkdir(parents=True)
    (spec_dir / "demo-follow-through.json").write_text(
        json.dumps(
            {"watch": [{"name": "pr", "argv": ["cat", str(outside)]}], "base_s": 60}
        )
    )
    heartbeat(env, "H1")
    assert heartbeat(env, "H2").stdout != ""
    outside.write_text("merged")
    assert heartbeat(env, "H3").stdout == ""


def test_heartbeat_shadow_mode_never_blocks(tmp_path):
    shadow = Env(tmp_path, mode="shadow")
    heartbeat(shadow, "H1")
    assert heartbeat(shadow, "H2").stdout == ""
    assert (
        shadow.state_json()["streaks"]["heartbeat:demo-follow-through"]["suppressed"]
        == 1
    )


def test_goal_and_heartbeat_streaks_are_independent(env):
    heartbeat(env, "H1")
    assert env.goal_turn("G1").returncode == 0
