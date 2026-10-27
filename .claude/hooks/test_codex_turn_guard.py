#!/usr/bin/env python3
"""Effect tests for codex_turn_guard.py.

Every case pipes a synthetic Codex hook event (the field names are the ones in
the Codex 0.157.1 hook input schemas) to the real script and asserts on its
stdout, stderr, exit code and state. The transcript is a synthetic rollout with
the same `task_started.turn_attribution.turn_trigger` shape Codex writes.

Read each test as "what did the thread do, what did the guard do". A deny is
asserted on STDERR as well as stdout: Codex shows a hook's stderr as the reason
on exit 2, so a stdout-only assertion would pass while the model saw nothing.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

HOOKS_DIR = Path(__file__).resolve().parent
HOOK = str(HOOKS_DIR / "codex_turn_guard.py")
SID = "01a1-test-thread"
HEARTBEAT = (
    "<heartbeat>\n  <automation_id>demo-follow-through</automation_id>\n"
    "  <current_time_iso>2026-10-23T12:15:09.684Z</current_time_iso>\n  <instructions>\nDo the thing.\n"
    "  </instructions>\n</heartbeat>"
)
HERMETIC_GIT = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
}


class Env:
    """One synthetic Codex thread. mode=None leaves the mode unset (defaults)."""

    def __init__(
        self,
        tmp_path: Path,
        mode: str | None = "enforce",
        opt_in_heartbeat: bool = True,
    ) -> None:
        self.state = tmp_path / "state"
        self.home = tmp_path / "codex_home"
        self.guard = self.home / "ateles" / "turn_guard"
        self.repo = tmp_path / "repo"
        self.transcript = tmp_path / "rollout.jsonl"
        for directory in (self.state, self.home, self.repo, self.guard / "heartbeats"):
            directory.mkdir(parents=True, exist_ok=True)
        self.transcript.write_text("")
        self.mode = mode
        self._git("init", "-q")
        (self.repo / "a.txt").write_text("one")
        self._git("add", "a.txt")
        self._git("commit", "-q", "-m", "init")
        if opt_in_heartbeat:
            self.write_heartbeat_spec("demo-follow-through", {"watch": []})

    def _git(self, *args: str) -> None:
        subprocess.run(
            ["git", "-c", "commit.gpgsign=false", *args],
            cwd=self.repo,
            check=True,
            capture_output=True,
            env=dict(os.environ, **HERMETIC_GIT),
        )

    def write_heartbeat_spec(self, automation_id: str, spec: dict) -> None:
        (self.guard / "heartbeats" / f"{automation_id}.json").write_text(
            json.dumps(spec)
        )

    def commit(self) -> None:
        (self.repo / "a.txt").write_text(str(time.time_ns()))
        self._git("add", "a.txt")
        self._git("commit", "-q", "-m", "next")

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
        env = dict(os.environ, **HERMETIC_GIT)
        env.pop("ATELES_CODEX_TURN_GUARD", None)
        env.update(
            ATELES_CODEX_GUARD_STATE_DIR=str(self.state), CODEX_HOME=str(self.home)
        )
        if self.mode is not None:
            env["ATELES_CODEX_TURN_GUARD"] = self.mode
        return subprocess.run(
            [sys.executable, HOOK],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )

    def state_file(self) -> Path:
        return self.state / f"codex_turn_guard.{SID}.json"

    def state_json(self) -> dict:
        return json.loads(self.state_file().read_text())

    def edit_state(self, edit) -> None:
        data = self.state_json()
        edit(data)
        self.state_file().write_text(json.dumps(data))

    def age_streak(self, name: str, seconds: float) -> None:
        def edit(data):
            data["streaks"][name]["ended_ts"] -= seconds

        self.edit_state(edit)

    def decisions(self) -> str:
        path = self.guard / "decisions.log"
        return path.read_text() if path.exists() else ""

    # Convenience turns -------------------------------------------------
    def operator_turn(self, turn_id: str, prompt: str, answer: str | None) -> None:
        self.start_turn(turn_id, "composer")
        self.fire("UserPromptSubmit", turn_id, prompt=prompt, permission_mode="default")
        if answer is not None:
            self.fire(
                "Stop", turn_id, last_assistant_message=answer, stop_hook_active=False
            )

    def goal_turn(
        self,
        turn_id: str,
        tool: str = "Bash",
        command: str = "git status",
        final: str = "status",
    ) -> subprocess.CompletedProcess:
        self.start_turn(turn_id, "goal")
        first = self.fire(
            "PreToolUse",
            turn_id,
            tool_name=tool,
            tool_input={"command": command},
            tool_use_id="t1",
        )
        self.fire("Stop", turn_id, last_assistant_message=final, stop_hook_active=False)
        return first

    def heartbeat(self, turn_id: str, final: str = "", prompt: str = HEARTBEAT):
        self.start_turn(turn_id, "automation_heartbeat_scheduled")
        first = self.fire(
            "UserPromptSubmit", turn_id, prompt=prompt, permission_mode="default"
        )
        if not first.stdout:
            self.fire(
                "Stop", turn_id, last_assistant_message=final, stop_hook_active=False
            )
        return first


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def assert_denied(done, *needles: str) -> None:
    """A deny must carry the instruction on stderr (what Codex shows) and on stdout."""
    assert done.returncode == 2
    assert done.stderr.startswith("[codex_turn_guard]")
    out = json.loads(done.stdout)["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"
    for needle in needles:
        assert needle in done.stderr and needle in out["permissionDecisionReason"]


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
    assert "data" in done.stdout and "not instructions" in done.stdout
    assert "path=answered" in env.decisions() and "decision=notice" in env.decisions()


@pytest.mark.parametrize("source", ["startup", "resume", "clear", "fork"])
def test_no_notice_outside_compaction(env, source):
    env.operator_turn("T1", "A question", "An answer")
    assert env.fire("SessionStart", source=source).stdout == ""


def test_no_notice_while_the_message_is_still_unanswered(env):
    env.operator_turn("T1", "A question", None)
    assert env.fire("SessionStart", source="compact").stdout == ""


def test_a_stop_for_a_different_real_turn_does_not_mark_the_prompt_answered(env):
    env.operator_turn("T1", "A question", None)
    # T0 is another real turn whose UserPromptSubmit was never seen by the guard.
    env.start_turn("T0", "composer")
    env.fire(
        "Stop",
        "T0",
        last_assistant_message="an answer to something else",
        stop_hook_active=False,
    )
    assert env.fire("SessionStart", source="compact").stdout == ""


def test_a_newer_unanswered_message_supersedes_the_answered_one(env):
    env.operator_turn("T1", "First question", "First answer")
    env.operator_turn("T2", "Second question", None)
    assert env.fire("SessionStart", source="compact").stdout == ""


def test_notice_stays_inside_the_context_cap(env):
    env.operator_turn("T1", "q" * 5000, "a" * 5000)
    assert len(env.fire("SessionStart", source="compact").stdout) < 1500


def test_answered_mode_off_in_the_mode_file_silences_the_notice(tmp_path):
    quiet = Env(tmp_path, mode=None)
    (quiet.guard / "mode.json").write_text(json.dumps({"answered": "off"}))
    quiet.operator_turn("T1", "A question", "An answer")
    assert quiet.fire("SessionStart", source="compact").stdout == ""


def test_notice_is_on_by_default(tmp_path):
    default = Env(tmp_path, mode=None)
    default.operator_turn("T1", "A question", "An answer")
    assert (
        "Do NOT answer it again"
        in default.fire("SessionStart", source="compact").stdout
    )


@pytest.mark.parametrize(
    "broken",
    [{"turns": [], "streaks": None}, {"turns": None}, [], "x", {"last_prompt": 3}],
)
def test_malformed_state_recovers_instead_of_disabling_the_guard(env, broken):
    env.state_file().write_text(json.dumps(broken))
    env.operator_turn("T1", "A question", "An answer")
    assert "Do NOT answer it again" in env.fire("SessionStart", source="compact").stdout


def test_state_file_is_private(env):
    env.operator_turn("T1", "A question", "An answer")
    assert stat.S_IMODE(env.state_file().stat().st_mode) == 0o600


# --------------------------------------------------------------------------
# (b) goal-turn guard
# --------------------------------------------------------------------------
def test_first_goal_turn_runs_and_a_quiet_repeat_is_denied_with_the_instruction(env):
    assert env.goal_turn("G1").returncode == 0
    second = env.goal_turn("G2", command="neotoma retrieve_entities --type task")
    assert_denied(
        second, "End this turn now", "do not call update_goal", "do not write a report"
    )


def test_every_tool_call_of_a_skipped_turn_is_denied_with_the_reason_each_time(env):
    env.goal_turn("G1")
    env.start_turn("G2", "goal")
    for n in range(4):
        done = env.fire(
            "PreToolUse",
            "G2",
            tool_name="Bash",
            tool_input={"command": f"echo {n}"},
            tool_use_id=str(n),
        )
        assert_denied(done, "End this turn now")


def test_denied_turn_does_not_slide_the_window(env):
    env.goal_turn("G1")
    before = env.state_json()["streaks"]["goal"]["ended_ts"]
    env.goal_turn("G2")
    after = env.state_json()["streaks"]["goal"]
    assert after["ended_ts"] == before
    assert after["suppressed"] == 1


def test_turn_ended_by_guard_is_visible_to_a_stop_gate(env):
    sys.path.insert(0, str(HOOKS_DIR))
    import codex_turn_guard as guard

    env.goal_turn("G1")
    env.goal_turn("G2")
    os.environ["ATELES_CODEX_GUARD_STATE_DIR"] = str(env.state)
    try:
        assert guard.turn_ended_by_guard(SID, "G2") is True
        assert guard.turn_ended_by_guard(SID, "G1") is False
        assert guard.turn_ended_by_guard(SID, "never-seen") is False
    finally:
        del os.environ["ATELES_CODEX_GUARD_STATE_DIR"]


def test_window_elapsing_allows_a_recheck_and_doubles_the_next_window(env):
    env.goal_turn("G1")
    env.age_streak("goal", 301)
    assert env.goal_turn("G2").returncode == 0
    assert env.state_json()["streaks"]["goal"]["quiet"] == 2
    env.age_streak("goal", 301)  # 301s is inside the doubled 600s window
    assert env.goal_turn("G3").returncode == 2


def test_the_window_never_exceeds_the_cap(env):
    env.goal_turn("G1")

    def many_quiet_turns(data):
        data["streaks"]["goal"]["quiet"] = 40  # 300 * 2**39 seconds uncapped

    env.edit_state(many_quiet_turns)
    env.age_streak("goal", 3000)
    assert env.goal_turn("G2").returncode == 2  # inside the 3600 s cap
    env.age_streak("goal", 700)  # 3700 s since the last quiet turn
    assert env.goal_turn("G3").returncode == 0


def test_repository_change_allows_the_turn(env):
    env.goal_turn("G1")
    env.commit()
    assert env.goal_turn("G2").returncode == 0


def test_a_change_during_the_turn_by_an_unrecognised_command_is_progress(env):
    env.goal_turn("G1")
    env.start_turn("G2", "goal")
    env.age_streak("goal", 301)
    env.fire(
        "PreToolUse",
        "G2",
        tool_name="Bash",
        tool_input={"command": "./some_script.sh"},
        tool_use_id="x",
    )
    env.commit()  # the script changed the repository; the classifier did not recognise it
    env.fire("Stop", "G2", last_assistant_message="done", stop_hook_active=False)
    assert env.state_json()["streaks"]["goal"]["quiet"] == 0


def test_new_operator_message_allows_the_turn(env):
    env.goal_turn("G1")
    env.operator_turn("T1", "New instruction", "Done")
    assert env.goal_turn("G2").returncode == 0


def test_a_goal_turn_that_wrote_something_is_progress(env):
    env.goal_turn("G1", command="git commit -m work")
    assert env.state_json()["streaks"]["goal"]["quiet"] == 0
    assert env.goal_turn("G2").returncode == 0


def test_a_long_report_is_not_a_quiet_goal_turn(env):
    env.goal_turn("G1", final="finding " * 400)
    assert env.state_json()["streaks"]["goal"]["quiet"] == 0


def test_a_goal_turn_with_no_tool_call_and_a_long_report_is_not_quiet(env):
    env.start_turn("G1", "goal")
    env.fire(
        "Stop", "G1", last_assistant_message="analysis " * 600, stop_hook_active=False
    )
    assert env.state_json()["streaks"]["goal"]["quiet"] == 0


def test_stop_firing_twice_counts_the_turn_once(env):
    env.goal_turn("G1")
    env.fire("Stop", "G1", last_assistant_message="status", stop_hook_active=True)
    assert env.state_json()["streaks"]["goal"]["quiet"] == 1


def test_mutating_mcp_call_is_progress_and_read_call_is_not(env):
    env.goal_turn("G1", tool="mcp__neotoma__store", command="x")
    assert env.state_json()["streaks"]["goal"]["quiet"] == 0
    env.goal_turn("G2", tool="mcp__neotoma__retrieve_entities", command="x")
    assert env.state_json()["streaks"]["goal"]["quiet"] == 1


def test_operator_turns_are_never_denied_even_when_the_prompt_event_was_missed(env):
    env.goal_turn("G1")
    # A composer turn the prompt hook never recorded: only the transcript says what it is.
    env.start_turn("R9", "composer")
    done = env.fire(
        "PreToolUse",
        "R9",
        tool_name="Bash",
        tool_input={"command": "ls"},
        tool_use_id="x",
    )
    assert done.returncode == 0 and done.stdout == "" and done.stderr == ""


def test_operator_turns_with_a_recorded_prompt_are_never_denied(env):
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
    (env.guard / "goal_watch.json").write_text(
        json.dumps({"watch": [{"name": "probe", "argv": ["cat", str(outside)]}]})
    )
    env.goal_turn("G1")
    assert env.goal_turn("G2").returncode == 2
    outside.write_text("v2")
    assert env.goal_turn("G3").returncode == 0


def test_a_hanging_watch_command_is_bounded_and_does_not_trap_the_hook(env):
    (env.guard / "goal_watch.json").write_text(
        json.dumps({"watch": [{"name": "slow", "argv": ["sleep", "60"]}] * 5})
    )
    started = time.monotonic()
    done = env.goal_turn("G1")
    assert done.returncode == 0
    assert time.monotonic() - started < 40


def test_default_mode_is_shadow_for_goals_and_logs_what_it_would_do(tmp_path):
    shadow = Env(tmp_path, mode=None)
    shadow.goal_turn("G1")
    assert shadow.goal_turn("G2").returncode == 0  # nothing denied by default
    log = shadow.decisions()
    assert "path=goal mode=shadow decision=suppress why=within_window" in log
    assert shadow.state_json()["streaks"]["goal"]["suppressed"] == 1


def test_mode_file_can_enforce_the_goal_path_alone(tmp_path):
    only_goal = Env(tmp_path, mode=None)
    (only_goal.guard / "mode.json").write_text(json.dumps({"goal": "enforce"}))
    only_goal.goal_turn("G1")
    assert only_goal.goal_turn("G2").returncode == 2
    only_goal.heartbeat("H1")
    assert only_goal.heartbeat("H2").stdout == ""  # heartbeat path still shadow


def test_environment_overrides_the_mode_file(tmp_path):
    forced = Env(tmp_path, mode="shadow")
    (forced.guard / "mode.json").write_text(json.dumps({"goal": "enforce"}))
    forced.goal_turn("G1")
    assert forced.goal_turn("G2").returncode == 0


def test_off_mode_does_nothing(tmp_path):
    off = Env(tmp_path, mode="off")
    off.goal_turn("G1")
    assert off.goal_turn("G2").returncode == 0
    assert not off.state_file().exists()


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
    done = env.fire(
        "PreToolUse",
        "G9",
        tool_name="Bash",
        tool_input={"command": "ls"},
        tool_use_id="x",
    )
    assert done.returncode == 0


def test_trigger_lookup_matches_the_whole_turn_id(env):
    env.start_turn("GX10", "goal")
    env.start_turn("GX1", "composer")
    sys.path.insert(0, str(HOOKS_DIR))
    import codex_turn_guard as guard

    assert guard.find_turn_trigger(str(env.transcript), "GX1") == "composer"
    assert guard.find_turn_trigger(str(env.transcript), "GX10") == "goal"
    assert guard.find_turn_trigger(str(env.transcript), "GX") is None


@pytest.mark.parametrize("raw", ["", "not json", "[]", "null"])
def test_garbage_input_fails_open(env, raw):
    done = subprocess.run(
        [sys.executable, HOOK], input=raw, capture_output=True, text=True, timeout=30
    )
    assert done.returncode == 0 and done.stdout == ""


# --------------------------------------------------------------------------
# Write classifier, on the tool shapes goal turns really use
# --------------------------------------------------------------------------
sys.path.insert(0, str(HOOKS_DIR))
import codex_turn_guard as guard_module  # noqa: E402

CLASSIFIER_CASES = [
    # (tool_name, tool_input, writes?)
    ("send_message", {"message": "hello"}, True),
    ("spawn_agent", {"task": "do it"}, True),
    ("followup_task", {"id": "x"}, True),
    ("update_goal", {"status": "complete"}, True),
    ("apply_patch", "*** Begin Patch", True),
    ("mcp__neotoma__store", {}, True),
    ("mcp__neotoma__retrieve_entities", {}, False),
    ("mcp__neotoma__list_observations", {}, False),
    ("sleep", {"seconds": 30}, False),
    ("wait", {"id": "x"}, False),
    ("wait_agent", {"id": "x"}, False),
    ("list_agents", {}, False),
    ("Bash", {"command": "git status"}, False),
    ("Bash", {"command": "git log --oneline -5 && rg foo src"}, False),
    ("Bash", {"command": "ls 2>/dev/null"}, False),
    ("Bash", {"command": "gh pr view 12 --json state"}, False),
    ("Bash", {"command": "curl -s https://example.com/health"}, False),
    ("Bash", {"command": "git commit -m x"}, True),
    ("Bash", {"command": "gh pr comment 12 -b done"}, True),
    ("Bash", {"command": "curl -X POST https://example.com/hook"}, True),
    ("Bash", {"command": "echo hi > out.txt"}, True),
    ("Bash", {"command": "printf x >> log.txt"}, True),
    ("Bash", {"command": "rm -f old.txt"}, True),
    # code-mode exec carries JavaScript: arrows and comparisons are not redirects
    (
        "exec",
        "const rows = items.filter(i => i.n >= 3).map(i => i.id); text(rows.join())",
        False,
    ),
    (
        "exec",
        "if (a > b) { text(await tools.exec_command({cmd: 'git status'})) }",
        False,
    ),
    ("exec", "await tools.mcp__neotoma__store({entities: []})", True),
    ("exec", "await tools.neotoma.store({entities: []})", True),
    ("exec", "await tools.apply_patch('*** Begin Patch')", True),
    ("exec", "text(await tools.exec_command({cmd: 'git commit -m x'}))", True),
    ("functions.send_message", {"message": "hi"}, True),
]


@pytest.mark.parametrize("tool,tool_input,writes", CLASSIFIER_CASES)
def test_write_classifier(tool, tool_input, writes):
    assert guard_module.is_mutating(tool, tool_input) is writes


def test_classifier_is_linear_on_hostile_input():
    started = time.monotonic()
    guard_module.is_mutating("exec", "curl " * 20000)
    assert time.monotonic() - started < 2


# --------------------------------------------------------------------------
# (c) heartbeat guard
# --------------------------------------------------------------------------
def test_idle_heartbeat_is_blocked_before_the_model_runs(env):
    assert env.heartbeat("H1").stdout == ""
    second = env.heartbeat("H2")
    assert second.returncode == 0
    out = json.loads(second.stdout)
    assert out["decision"] == "block"
    assert "demo-follow-through" in out["reason"]
    assert "last 1 quiet run." in out["reason"] and "quiet runs" not in out["reason"]
    assert "shadow" in out["reason"]
    assert set(out) <= {
        "decision",
        "reason",
        "continue",
        "stopReason",
        "suppressOutput",
        "systemMessage",
    }
    assert (
        "decision=suppress" in env.decisions()
        and "path=heartbeat:demo-follow-through" in env.decisions()
    )


def test_heartbeat_reason_pluralises(env):
    env.heartbeat("H1")
    env.age_streak("heartbeat:demo-follow-through", 601)
    env.heartbeat("H2")
    assert "2 quiet runs" in json.loads(env.heartbeat("H3").stdout)["reason"]


def test_heartbeat_with_real_output_resets_the_window(env):
    env.heartbeat("H1", final="x" * 400)
    assert env.heartbeat("H2").stdout == ""


def test_heartbeat_window_elapsing_allows_a_check(env):
    env.heartbeat("H1")
    env.age_streak("heartbeat:demo-follow-through", 601)
    assert env.heartbeat("H2").stdout == ""


def test_heartbeat_window_is_capped_at_an_hour(env):
    env.heartbeat("H1")
    env.edit_state(
        lambda d: d["streaks"]["heartbeat:demo-follow-through"].update(quiet=40)
    )
    env.age_streak("heartbeat:demo-follow-through", 3000)
    assert env.heartbeat("H2").stdout != ""
    env.age_streak("heartbeat:demo-follow-through", 700)
    assert env.heartbeat("H3").stdout == ""


def test_heartbeats_are_tracked_per_automation(env):
    env.heartbeat("H1")
    env.write_heartbeat_spec("other-job", {"watch": []})
    assert (
        env.heartbeat(
            "H2", prompt=HEARTBEAT.replace("demo-follow-through", "other-job")
        ).stdout
        == ""
    )


def test_a_heartbeat_without_a_watch_file_is_never_blocked(tmp_path):
    unwatched = Env(tmp_path, opt_in_heartbeat=False)
    for n in range(3):
        assert unwatched.heartbeat(f"H{n}").stdout == ""
    assert "no_watch_file_not_opted_in" in unwatched.decisions()


def test_heartbeat_watch_change_allows_the_run(env):
    outside = env.home / "pr_state.txt"
    outside.write_text("open")
    env.write_heartbeat_spec(
        "demo-follow-through",
        {"watch": [{"name": "pr", "argv": ["cat", str(outside)]}], "base_s": 60},
    )
    env.heartbeat("H1")
    assert env.heartbeat("H2").stdout != ""
    outside.write_text("merged")
    assert env.heartbeat("H3").stdout == ""


def test_heartbeat_default_mode_is_shadow(tmp_path):
    shadow = Env(tmp_path, mode=None)
    shadow.heartbeat("H1")
    assert shadow.heartbeat("H2").stdout == ""
    assert (
        shadow.state_json()["streaks"]["heartbeat:demo-follow-through"]["suppressed"]
        == 1
    )
    assert "mode=shadow decision=suppress" in shadow.decisions()


def test_a_pasted_heartbeat_inside_an_operator_message_is_not_blocked(env):
    env.heartbeat("H1")
    pasted = "Why did this fire?\n" + HEARTBEAT + "\nthanks"
    env.start_turn("R1", "composer")
    done = env.fire("UserPromptSubmit", "R1", prompt=pasted, permission_mode="default")
    assert done.stdout == ""
    assert env.state_json()["last_prompt"]["turn_id"] == "R1"


def test_a_heartbeat_followed_by_operator_text_is_not_blocked(env):
    env.heartbeat("H1")
    env.start_turn("R2", "composer")
    done = env.fire(
        "UserPromptSubmit",
        "R2",
        prompt=HEARTBEAT + "\nwhy did this run?",
        permission_mode="default",
    )
    assert done.stdout == ""
    assert env.state_json()["last_prompt"]["turn_id"] == "R2"


def test_a_heartbeat_with_an_unparseable_id_does_not_arm_the_goal_streak(env):
    bad = "<heartbeat>\n  <automation_id>bad id!</automation_id>\n</heartbeat>"
    env.heartbeat("H1", prompt=bad)
    env.heartbeat("H2", prompt=bad)
    assert "goal" not in env.state_json()["streaks"]
    assert env.goal_turn("G1").returncode == 0


def test_goal_and_heartbeat_streaks_are_independent(env):
    env.heartbeat("H1")
    assert env.goal_turn("G1").returncode == 0
