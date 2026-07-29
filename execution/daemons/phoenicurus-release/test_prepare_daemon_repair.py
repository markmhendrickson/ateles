"""
Effect tests for prepare.py — ateles#242 (scheduled prepare daemon repair:
missing `claude` CLI on PATH, stale NEOTOMA_BASE_URL silently skipping the
in-flight-release guard).

Tests are fully synchronous / mock-based: `subprocess.Popen`/`urllib.request`
are always patched, no real process is spawned and no real network call is
made. Every test asserts an observable effect (return value, exact call
args/absence of a call, log/telegram content) — never merely "no exception
was raised."

Run with: pytest execution/daemons/phoenicurus-release/test_prepare.py -v
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_DAEMON_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _DAEMON_DIR.parent.parent.parent
for _p in (str(_REPO_ROOT), str(_DAEMON_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import prepare  # noqa: E402


# ── Helpers ──────────────────────────────────────────────────────────────────


def _proc(returncode: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    p = MagicMock(spec=subprocess.CompletedProcess)
    p.returncode = returncode
    p.stdout = stdout
    p.stderr = stderr
    return p


# ── Bug 1: missing `claude` CLI on PATH ─────────────────────────────────────
# This is the exact regression the PATH fix in this PR addresses — pin it so
# a future plist regeneration or ~/.local/bin removal fails CI instead of
# failing silently in production again (undetected since ~2026-07-13).


class TestSpawnPrepareAgentMissingClaudeCLI:
    def test_missing_claude_logs_and_returns_false_without_spawn(self, monkeypatch):
        # `spawn_prepare_agent()` does `import shutil` locally inside the
        # function body, not at module scope — patching the real `shutil`
        # module (imported here) still works because the local import binds
        # the same module object. `prepare.shutil` does NOT exist.
        monkeypatch.setattr(shutil, "which", lambda name: None)
        with patch.object(prepare, "telegram_send") as mock_telegram, patch.object(
            prepare.subprocess, "Popen"
        ) as mock_popen:
            result = prepare.spawn_prepare_agent("v0.18.8", 4, dry_run=False)

        assert result is False
        mock_popen.assert_not_called()
        mock_telegram.assert_called_once()
        assert "claude CLI not found" in mock_telegram.call_args[0][0]

    def test_present_claude_spawns_without_telegram(self, monkeypatch, tmp_path):
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/local/bin/claude")
        agent_log = tmp_path / "agent.log"
        monkeypatch.setattr(prepare, "AGENT_LOG", agent_log)
        monkeypatch.setattr(prepare, "SPAWN_STATE_FILE", tmp_path / ".spawn")
        with patch.object(prepare, "telegram_send") as mock_telegram, patch.object(
            prepare.subprocess, "Popen"
        ) as mock_popen:
            result = prepare.spawn_prepare_agent("v0.18.8", 4, dry_run=False)

        assert result is True
        mock_popen.assert_called_once()
        mock_telegram.assert_not_called()


# ── Bug 2: NEOTOMA_BASE_URL — connection failure must not silently look like
# "no release in flight" ────────────────────────────────────────────────────


class TestExistingReleaseStatusConnectionFailureDoesNotSilentlySkip:
    def test_connection_refused_is_not_treated_as_no_release_in_flight(
        self, monkeypatch, caplog
    ):
        monkeypatch.setenv("NEOTOMA_BASE_URL", "http://localhost:9999")

        def fake_urlopen(req, timeout):
            raise prepare.urllib.error.URLError("Connection refused")

        monkeypatch.setattr(prepare.urllib.request, "urlopen", fake_urlopen)

        with caplog.at_level("WARNING"):
            # A REFUSED connection stays lenient (None): the common case is a
            # laptop with Neotoma not running, and paging the operator for that
            # is noise. Only an AUTH failure (401/403 or a missing token for a
            # remote base URL) fails closed with STATUS_UNSAFE — see the
            # fail-closed tests below (ateles#330). ateles#243 tracked whether an
            # unreachable Neotoma should also fail loud; still open.
            result = prepare.existing_release_status("v0.18.9")

        assert result is None
        assert "could not check existing release_result" in caplog.text


class TestExistingReleaseStatusUsesConfiguredBaseUrl:
    def test_reads_neotoma_base_url_env_not_hardcoded_default(self, monkeypatch):
        monkeypatch.setenv("NEOTOMA_BASE_URL", "http://localhost:9180")
        captured = {}

        def fake_urlopen(req, timeout):
            captured["url"] = req.full_url
            resp = MagicMock()
            resp.read.return_value = b'{"entities": []}'
            resp.__enter__ = lambda self: resp
            resp.__exit__ = lambda self, *a: False
            return resp

        monkeypatch.setattr(prepare.urllib.request, "urlopen", fake_urlopen)

        result = prepare.existing_release_status("v0.18.9")

        assert captured["url"].startswith("http://localhost:9180")
        assert result is None


# ═══════════════════════════════════════════════════════════════════════════
# ateles#330 — supervision of the spawned prepare agent
#
# The daemon used to spawn `claude --print` fire-and-forget AND stamp its
# idempotency lock immediately. So when the agent died seconds later (empty
# credit balance, usage limit, crash) the day was burned: no RC, no
# notification, and no retry until the next day. These tests pin the four
# repairs — exit-sentinel supervision, stamp-on-success, usage-limit backoff,
# and fail-closed in-flight checks.
# ═══════════════════════════════════════════════════════════════════════════


class _Env:
    """Isolated state files + captured operator notifications."""

    def __init__(self, tmp_path: Path):
        self.tmp_path = tmp_path
        self.notified: list[str] = []


@pytest.fixture
def env(tmp_path, monkeypatch):
    e = _Env(tmp_path)
    repo = tmp_path / "neotoma"
    repo.mkdir()
    (repo / "package.json").write_text("{}")
    monkeypatch.setattr(prepare, "NEOTOMA_REPO_ROOT", repo)
    monkeypatch.setattr(prepare, "STATE_FILE", tmp_path / ".day")
    monkeypatch.setattr(prepare, "MERGE_STATE_FILE", tmp_path / ".sha")
    monkeypatch.setattr(prepare, "SPAWN_STATE_FILE", tmp_path / ".spawn")
    monkeypatch.setattr(prepare, "RETRY_STATE_FILE", tmp_path / ".retry")
    monkeypatch.setattr(prepare, "AUTH_NOTIFY_STATE_FILE", tmp_path / ".authnotify")
    monkeypatch.setattr(prepare, "AGENT_LOG", tmp_path / "agent.log")
    monkeypatch.setattr(
        prepare, "notify_operator", lambda text, **kw: e.notified.append(text)
    )
    # No real git/gh/network from any of these tests.
    monkeypatch.setattr(prepare.subprocess, "run", lambda *a, **k: _proc(0))
    return e


def _iso_ago(minutes: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


def _write_spawn(env: _Env, **over) -> dict:
    state = {
        "sha_or_date": "2026-07-29",
        "spawned_at": prepare._now_iso(),
        "on_merge": False,
        "tag": "v0.19.0",
        "head": "",
        "commit_count": 4,
        "log_offset": 0,
    }
    state.update(over)
    prepare.SPAWN_STATE_FILE.write_text(json.dumps(state))
    return state


def _write_agent_log(text: str) -> None:
    prepare.AGENT_LOG.write_text(text)


def _stub_entities(monkeypatch, entities: list[dict]) -> None:
    """Answer the release_result query with ``entities`` over a loopback base."""
    monkeypatch.setenv("NEOTOMA_BASE_URL", "http://localhost:9180")
    payload = json.dumps({"entities": entities}).encode()

    def fake_urlopen(req, timeout=20):
        resp = MagicMock()
        resp.read.return_value = payload
        resp.__enter__ = lambda self: resp
        resp.__exit__ = lambda self, *a: False
        return resp

    monkeypatch.setattr(prepare.urllib.request, "urlopen", fake_urlopen)


def _wire_ready_to_spawn(monkeypatch, *, inflight=None, ci=True, count=4):
    monkeypatch.setattr(prepare, "latest_tag", lambda: "v0.19.0")
    monkeypatch.setattr(prepare, "unreleased_commit_count", lambda tag: count)
    monkeypatch.setattr(prepare, "existing_release_status", lambda hint: inflight)
    monkeypatch.setattr(prepare, "main_ci_green", lambda: ci)
    monkeypatch.setattr(prepare, "_head_sha", lambda: "c" * 40)
    monkeypatch.setattr(prepare, "MIN_COMMITS", 1)


# ── Spawn: exit sentinel + spawn state record ───────────────────────────────


class TestSpawnRecordsOutcomeContext:
    def test_agent_is_wrapped_so_exit_status_reaches_the_log(self, env, monkeypatch):
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/local/bin/claude")
        with patch.object(prepare.subprocess, "Popen") as mock_popen:
            assert prepare.spawn_prepare_agent("v0.19.0", 4, dry_run=False) is True

        argv = mock_popen.call_args[0][0]
        assert argv[:2] == ["sh", "-c"], "the agent must run under a shell wrapper"
        script = argv[2]
        # `;` not `&&` — the sentinel must be written for FAILURES too, which is
        # the entire point (a crashed agent used to leave no trace at all).
        assert " ; echo " in script
        assert prepare.EXIT_SENTINEL_PREFIX in script
        assert str(prepare.AGENT_LOG) in script
        assert "--dangerously-skip-permissions" in script

    def test_spawn_state_written_with_outcome_check_context(self, env, monkeypatch):
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/local/bin/claude")
        with patch.object(prepare.subprocess, "Popen"):
            prepare.spawn_prepare_agent(
                "v0.19.0", 4, dry_run=False, on_merge=True, head="d" * 40
            )

        state = json.loads(prepare.SPAWN_STATE_FILE.read_text())
        assert state["sha_or_date"] == "d" * 40  # on-merge keys off the SHA
        assert state["on_merge"] is True
        assert state["tag"] == "v0.19.0"
        assert state["head"] == "d" * 40
        assert prepare._parse_iso(state["spawned_at"]) is not None

    def test_scheduled_spawn_state_keys_off_the_date(self, env, monkeypatch):
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/local/bin/claude")
        with patch.object(prepare.subprocess, "Popen"):
            prepare.spawn_prepare_agent("v0.19.0", 4, dry_run=False)

        state = json.loads(prepare.SPAWN_STATE_FILE.read_text())
        assert state["sha_or_date"] == prepare.date.today().isoformat()
        assert state["on_merge"] is False

    def test_spawn_offset_ignores_a_previous_runs_sentinel(self, env, monkeypatch):
        # A stale sentinel from yesterday's agent must not be read as this
        # spawn's outcome — the recorded byte offset is what prevents it.
        _write_agent_log(f"old run\n{prepare.EXIT_SENTINEL_PREFIX}1\n")
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/local/bin/claude")
        with patch.object(prepare.subprocess, "Popen"):
            prepare.spawn_prepare_agent("v0.19.0", 4, dry_run=False)

        state = json.loads(prepare.SPAWN_STATE_FILE.read_text())
        assert state["log_offset"] > 0
        assert prepare._agent_exit_code(state) is None, "stale sentinel must not count"

    def test_no_spawn_state_when_popen_fails(self, env, monkeypatch):
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/local/bin/claude")
        with patch.object(
            prepare.subprocess, "Popen", side_effect=OSError("fork failed")
        ):
            assert prepare.spawn_prepare_agent("v0.19.0", 4, dry_run=False) is False

        assert not prepare.SPAWN_STATE_FILE.exists()
        assert any("failed to spawn" in n for n in env.notified)


# ── Stamp-on-success: the lock is no longer written at spawn time ───────────


class TestStampOnSuccess:
    def test_spawning_run_does_not_stamp_the_daily_lock(self, env, monkeypatch):
        _wire_ready_to_spawn(monkeypatch)
        spawned = []
        monkeypatch.setattr(
            prepare,
            "spawn_prepare_agent",
            lambda *a, **k: spawned.append(k) or True,
        )

        assert prepare.run_prepare(dry_run=False, force=False) == 0
        assert spawned, "preconditions met — the agent must be spawned"
        assert not prepare._already_ran_today(), (
            "the day must stay unstamped until the agent's outcome is confirmed"
        )

    def test_spawning_run_does_not_stamp_the_sha_lock(self, env, monkeypatch):
        _wire_ready_to_spawn(monkeypatch)
        monkeypatch.setattr(prepare, "spawn_prepare_agent", lambda *a, **k: True)

        assert prepare.run_prepare(dry_run=False, force=False, on_merge=True) == 0
        assert not prepare._already_ran_for_sha("c" * 40)

    def test_spawn_receives_mode_context_for_the_outcome_check(self, env, monkeypatch):
        _wire_ready_to_spawn(monkeypatch)
        seen = {}
        monkeypatch.setattr(
            prepare,
            "spawn_prepare_agent",
            lambda *a, **k: seen.update(k) or True,
        )

        prepare.run_prepare(dry_run=False, force=False, on_merge=True)
        assert seen == {"on_merge": True, "head": "c" * 40}

    def test_below_min_commits_still_stamps(self, env, monkeypatch):
        _wire_ready_to_spawn(monkeypatch, count=0)
        monkeypatch.setattr(prepare, "spawn_prepare_agent", lambda *a, **k: True)

        assert prepare.run_prepare(dry_run=False, force=False) == 0
        assert prepare._already_ran_today(), "early exits keep their daily lock"

    def test_inflight_release_still_stamps(self, env, monkeypatch):
        _wire_ready_to_spawn(monkeypatch, inflight="pending_approval")
        spawned = []
        monkeypatch.setattr(
            prepare, "spawn_prepare_agent", lambda *a, **k: spawned.append(1) or True
        )

        assert prepare.run_prepare(dry_run=False, force=False) == 0
        assert spawned == []
        assert prepare._already_ran_today()

    def test_ci_red_still_stamps_the_day(self, env, monkeypatch):
        _wire_ready_to_spawn(monkeypatch, ci=False)
        monkeypatch.setattr(prepare, "spawn_prepare_agent", lambda *a, **k: True)

        assert prepare.run_prepare(dry_run=False, force=False) == 0
        assert prepare._already_ran_today()
        assert any("CI is RED" in n for n in env.notified)

    def test_ci_unknown_still_stamps_the_day(self, env, monkeypatch):
        _wire_ready_to_spawn(monkeypatch, ci=None)
        spawned = []
        monkeypatch.setattr(
            prepare, "spawn_prepare_agent", lambda *a, **k: spawned.append(1) or True
        )

        assert prepare.run_prepare(dry_run=False, force=False) == 0
        assert spawned == []
        assert prepare._already_ran_today()

    def test_clear_stamp_unblocks_the_daily_lock(self, env):
        prepare._mark_ran(on_merge=False, head="")
        assert prepare._already_ran_today()
        prepare._clear_stamp(on_merge=False)
        assert not prepare._already_ran_today()

    def test_clear_stamp_unblocks_the_sha_lock(self, env):
        prepare._mark_ran(on_merge=True, head="e" * 40)
        assert prepare._already_ran_for_sha("e" * 40)
        prepare._clear_stamp(on_merge=True, head="e" * 40)
        assert not prepare._already_ran_for_sha("e" * 40)

    def test_clear_stamp_is_safe_when_absent(self, env):
        prepare._clear_stamp(on_merge=False)  # no file yet — must not raise

    def test_pending_spawn_blocks_a_duplicate_agent(self, env, monkeypatch):
        # With no stamp written at spawn time, THIS is what stops a second
        # scheduled/merge run from spawning a rival agent mid-flight.
        _wire_ready_to_spawn(monkeypatch)
        _write_spawn(env)
        spawned = []
        monkeypatch.setattr(
            prepare, "spawn_prepare_agent", lambda *a, **k: spawned.append(1) or True
        )

        assert prepare.run_prepare(dry_run=False, force=False) == 0
        assert spawned == []

    def test_reconciled_spawn_does_not_block(self, env, monkeypatch):
        _wire_ready_to_spawn(monkeypatch)
        _write_spawn(env, outcome="exit_1")
        spawned = []
        monkeypatch.setattr(
            prepare, "spawn_prepare_agent", lambda *a, **k: spawned.append(1) or True
        )

        assert prepare.run_prepare(dry_run=False, force=False) == 0
        assert spawned, "a reconciled failure must not wedge the next run"


# ── --check-agent-outcome ───────────────────────────────────────────────────


class TestCheckAgentOutcome:
    def test_no_spawn_recorded_is_a_noop(self, env):
        assert prepare.check_agent_outcome() == 0
        assert env.notified == []

    def test_running_agent_inside_window_is_left_alone(self, env, monkeypatch):
        _write_spawn(env, spawned_at=_iso_ago(5))
        _write_agent_log("thinking...\n")

        assert prepare.check_agent_outcome() == 0
        assert env.notified == []
        assert not prepare._already_ran_today()
        assert "outcome" not in json.loads(prepare.SPAWN_STATE_FILE.read_text())

    def test_missing_sentinel_past_window_is_a_failure(self, env, monkeypatch):
        _write_spawn(env, spawned_at=_iso_ago(60))
        _write_agent_log("started, then went quiet\n")

        assert prepare.check_agent_outcome() == 0
        assert len(env.notified) == 1
        assert "never reported an exit" in env.notified[0]
        assert "went quiet" in env.notified[0], "the log tail must be included"
        assert json.loads(prepare.SPAWN_STATE_FILE.read_text())["outcome"] == "no_exit"

    def test_nonzero_exit_notifies_with_log_tail(self, env, monkeypatch):
        _write_spawn(env)
        _write_agent_log(
            f"Credit balance is too low\n{prepare.EXIT_SENTINEL_PREFIX}1\n"
        )
        monkeypatch.setattr(prepare, "has_new_release_result_since", lambda ts: False)

        assert prepare.check_agent_outcome() == 0
        assert "exited 1" in env.notified[0]
        assert "Credit balance is too low" in env.notified[0]
        assert not prepare._already_ran_today(), "a failed agent must not stamp"
        assert json.loads(prepare.SPAWN_STATE_FILE.read_text())["outcome"] == "exit_1"

    def test_nonzero_exit_clears_an_existing_stamp(self, env, monkeypatch):
        prepare._mark_ran(on_merge=False, head="")  # e.g. left by a --force run
        _write_spawn(env)
        _write_agent_log(f"{prepare.EXIT_SENTINEL_PREFIX}2\n")
        monkeypatch.setattr(prepare, "has_new_release_result_since", lambda ts: False)

        prepare.check_agent_outcome()
        assert not prepare._already_ran_today(), "the retry must be unblocked"

    def test_clean_exit_with_release_result_stamps_the_day(self, env, monkeypatch):
        _write_spawn(env)
        _write_agent_log(f"RC opened\n{prepare.EXIT_SENTINEL_PREFIX}0\n")
        monkeypatch.setattr(prepare, "has_new_release_result_since", lambda ts: True)

        assert prepare.check_agent_outcome() == 0
        assert env.notified == [], "success is the agent's own notification to send"
        assert prepare._already_ran_today()
        assert json.loads(prepare.SPAWN_STATE_FILE.read_text())["outcome"] == "success"

    def test_clean_exit_on_merge_stamps_the_sha_not_the_day(self, env, monkeypatch):
        _write_spawn(env, on_merge=True, head="f" * 40)
        _write_agent_log(f"{prepare.EXIT_SENTINEL_PREFIX}0\n")
        monkeypatch.setattr(prepare, "has_new_release_result_since", lambda ts: True)

        prepare.check_agent_outcome()
        assert prepare._already_ran_for_sha("f" * 40)
        assert not prepare._already_ran_today()

    def test_clean_exit_without_release_result_is_a_failure(self, env, monkeypatch):
        # The nastiest silent case: the agent exits 0 having done nothing useful.
        _write_spawn(env)
        _write_agent_log(f"nothing to do?\n{prepare.EXIT_SENTINEL_PREFIX}0\n")
        monkeypatch.setattr(prepare, "has_new_release_result_since", lambda ts: False)

        assert prepare.check_agent_outcome() == 0
        assert "no release_result was stored" in env.notified[0]
        assert not prepare._already_ran_today()
        assert (
            json.loads(prepare.SPAWN_STATE_FILE.read_text())["outcome"]
            == "exit_0_no_result"
        )

    def test_already_reconciled_spawn_is_not_renotified(self, env, monkeypatch):
        _write_spawn(env, outcome="exit_1")
        _write_agent_log(f"{prepare.EXIT_SENTINEL_PREFIX}1\n")

        assert prepare.check_agent_outcome() == 0
        assert env.notified == []

    def test_log_tail_is_bounded(self, env, monkeypatch):
        _write_agent_log("\n".join(f"line{i}" for i in range(200)))
        state = _write_spawn(env)
        tail = prepare._agent_log_tail(state, lines=30)
        assert len(tail.splitlines()) == 30
        assert "line199" in tail
        assert "line0\n" not in tail

    def test_last_sentinel_wins(self, env):
        state = _write_spawn(env)
        _write_agent_log(
            f"{prepare.EXIT_SENTINEL_PREFIX}0\nretried\n"
            f"{prepare.EXIT_SENTINEL_PREFIX}7\n"
        )
        assert prepare._agent_exit_code(state) == 7


# ── Usage-limit backoff + --retry-if-due ────────────────────────────────────


class TestUsageLimitBackoff:
    def test_parses_reset_clock_and_timezone(self):
        deadline = prepare._parse_usage_limit_reset(
            "5-hour limit reached ∙ resets 6:40pm (Europe/Madrid)"
        )
        parsed = prepare._parse_iso(deadline)
        assert parsed is not None
        assert (parsed.hour, parsed.minute) == (18, 40)
        assert "Madrid" in str(parsed.tzinfo) or parsed.utcoffset() is not None

    def test_reset_already_past_rolls_to_tomorrow(self, monkeypatch):
        deadline = prepare._parse_iso(
            prepare._parse_usage_limit_reset("resets 12:01am (UTC)")
        )
        assert deadline > datetime.now(timezone.utc), (
            "a reset clock earlier than now means tomorrow's reset"
        )

    def test_unknown_timezone_falls_back_to_utc(self):
        deadline = prepare._parse_usage_limit_reset("resets 6:40pm (Mars/Olympus)")
        assert prepare._parse_iso(deadline) is not None

    def test_ordinary_failure_text_has_no_reset(self):
        assert prepare._parse_usage_limit_reset("Credit balance is too low") is None
        assert prepare._parse_usage_limit_reset("") is None

    def test_usage_limit_failure_schedules_a_retry(self, env, monkeypatch):
        _write_spawn(env)
        _write_agent_log(
            "limit reached ∙ resets 6:40pm (Europe/Madrid)\n"
            f"{prepare.EXIT_SENTINEL_PREFIX}1\n"
        )
        monkeypatch.setattr(prepare, "has_new_release_result_since", lambda ts: False)

        prepare.check_agent_outcome()

        retry = json.loads(prepare.RETRY_STATE_FILE.read_text())
        assert retry["attempts"] == 1
        assert prepare._parse_iso(retry["retry_after"]) is not None
        assert retry["tag"] == "v0.19.0"
        assert "retry scheduled" in env.notified[0]

    def test_non_usage_limit_failure_schedules_nothing(self, env, monkeypatch):
        _write_spawn(env)
        _write_agent_log(
            f"TypeError: boom\n{prepare.EXIT_SENTINEL_PREFIX}1\n"
        )
        monkeypatch.setattr(prepare, "has_new_release_result_since", lambda ts: False)

        prepare.check_agent_outcome()

        assert not prepare.RETRY_STATE_FILE.exists(), (
            "a real error would just re-fail — notify only"
        )
        assert env.notified, "but the operator must still hear about it"

    def test_repeat_usage_limits_increment_attempts(self, env, monkeypatch):
        monkeypatch.setattr(prepare, "has_new_release_result_since", lambda ts: False)
        for _ in range(2):
            _write_spawn(env)
            _write_agent_log(
                f"resets 6:40pm (Europe/Madrid)\n{prepare.EXIT_SENTINEL_PREFIX}1\n"
            )
            prepare.check_agent_outcome()
        assert json.loads(prepare.RETRY_STATE_FILE.read_text())["attempts"] == 2

    def test_successful_outcome_clears_a_pending_retry(self, env, monkeypatch):
        prepare.RETRY_STATE_FILE.write_text(
            json.dumps({"retry_after": _iso_ago(1), "attempts": 1})
        )
        _write_spawn(env)
        _write_agent_log(f"{prepare.EXIT_SENTINEL_PREFIX}0\n")
        monkeypatch.setattr(prepare, "has_new_release_result_since", lambda ts: True)

        prepare.check_agent_outcome()
        assert not prepare.RETRY_STATE_FILE.exists()


class TestRetryIfDue:
    def test_no_retry_scheduled_is_a_noop(self, env, monkeypatch):
        ran = []
        monkeypatch.setattr(prepare, "run_prepare", lambda *a, **k: ran.append(k) or 0)
        assert prepare.retry_if_due() == 0
        assert ran == []

    def test_retry_before_deadline_is_a_noop(self, env, monkeypatch):
        prepare.RETRY_STATE_FILE.write_text(
            json.dumps({"retry_after": _iso_ago(-30), "attempts": 1})
        )
        ran = []
        monkeypatch.setattr(prepare, "run_prepare", lambda *a, **k: ran.append(k) or 0)

        assert prepare.retry_if_due() == 0
        assert ran == []
        assert prepare.RETRY_STATE_FILE.exists(), "the schedule must survive"

    def test_due_retry_forces_a_prepare_and_disarms_the_schedule(self, env, monkeypatch):
        prepare.RETRY_STATE_FILE.write_text(
            json.dumps(
                {"retry_after": _iso_ago(1), "attempts": 1, "on_merge": True,
                 "head": "a" * 40, "tag": "v0.19.0"}
            )
        )
        calls = []

        def fake_run_prepare(dry_run, force, on_merge=False):
            calls.append((dry_run, force, on_merge))
            return 0

        monkeypatch.setattr(prepare, "run_prepare", fake_run_prepare)

        assert prepare.retry_if_due() == 0
        assert calls == [(False, True, True)], "the retry must bypass the daily lock"
        # Schedule is disarmed (no retry_after) but the attempt count is kept so
        # a subsequent usage-limit failure increments rather than resetting to 1.
        breadcrumb = json.loads(prepare.RETRY_STATE_FILE.read_text())
        assert "retry_after" not in breadcrumb
        assert breadcrumb["attempts"] == 1

    def test_retry_attempt_count_survives_rerun(self, env, monkeypatch):
        # The production path that used to infinite-loop: schedule → due → clear
        # → re-run → fail again → schedule as attempt 1 forever. The breadcrumb
        # left by retry_if_due must make the next _schedule_retry land on 2.
        prepare.RETRY_STATE_FILE.write_text(
            json.dumps(
                {"retry_after": _iso_ago(1), "attempts": 1, "on_merge": False,
                 "tag": "v0.19.0"}
            )
        )
        monkeypatch.setattr(prepare, "run_prepare", lambda *a, **k: 0)
        prepare.retry_if_due()

        prepare._schedule_retry(
            _iso_ago(-60), on_merge=False, head="", tag="v0.19.0"
        )
        retry = json.loads(prepare.RETRY_STATE_FILE.read_text())
        assert retry["attempts"] == 2, (
            "clearing the whole retry file before re-running would reset the "
            "cap and retry forever on a sticky usage limit"
        )

    def test_exhausted_budget_notifies_instead_of_retrying(self, env, monkeypatch):
        prepare.RETRY_STATE_FILE.write_text(
            json.dumps(
                {
                    "retry_after": _iso_ago(1),
                    "attempts": prepare.MAX_RETRY_ATTEMPTS,
                    "tag": "v0.19.0",
                }
            )
        )
        ran = []
        monkeypatch.setattr(prepare, "run_prepare", lambda *a, **k: ran.append(k) or 0)

        assert prepare.retry_if_due() == 0
        assert ran == []
        assert any("Giving up" in n for n in env.notified)
        assert not prepare.RETRY_STATE_FILE.exists()

    def test_unarmed_breadcrumb_is_a_noop(self, env, monkeypatch):
        prepare.RETRY_STATE_FILE.write_text(json.dumps({"attempts": 2}))
        ran = []
        monkeypatch.setattr(prepare, "run_prepare", lambda *a, **k: ran.append(k) or 0)

        assert prepare.retry_if_due() == 0
        assert ran == []
        assert prepare.RETRY_STATE_FILE.exists(), "breadcrumb must survive"

    def test_unparseable_deadline_is_discarded(self, env, monkeypatch):
        prepare.RETRY_STATE_FILE.write_text(json.dumps({"retry_after": "soon"}))
        ran = []
        monkeypatch.setattr(prepare, "run_prepare", lambda *a, **k: ran.append(k) or 0)

        assert prepare.retry_if_due() == 0
        assert ran == []
        assert not prepare.RETRY_STATE_FILE.exists()


# ── Fail-closed in-flight check (401/403 and missing credentials) ───────────


class TestInFlightCheckFailsClosedOnAuth:
    def _http_error(self, code):
        def raiser(req, timeout=20):
            raise prepare.urllib.error.HTTPError(
                "https://neotoma.example/entities/query", code, "denied", {}, None
            )

        return raiser

    @pytest.mark.parametrize("code", [401, 403])
    def test_auth_rejection_returns_unsafe_sentinel(self, env, monkeypatch, code):
        monkeypatch.setenv("NEOTOMA_BASE_URL", "https://neotoma.example")
        monkeypatch.setenv("NEOTOMA_BEARER_TOKEN", "stale-token")
        monkeypatch.setattr(
            prepare.urllib.request, "urlopen", self._http_error(code)
        )

        result = prepare.existing_release_status("v0.19.0")

        assert result == prepare.STATUS_UNSAFE, (
            "None means 'no release in flight' — an auth failure must not say that"
        )
        assert any(str(code) in n for n in env.notified)

    def test_auth_notice_is_rate_limited(self, env, monkeypatch):
        monkeypatch.setenv("NEOTOMA_BASE_URL", "https://neotoma.example")
        monkeypatch.setenv("NEOTOMA_BEARER_TOKEN", "stale-token")
        monkeypatch.setattr(prepare.urllib.request, "urlopen", self._http_error(403))

        prepare.existing_release_status("v0.19.0")
        prepare.existing_release_status("v0.19.0")

        assert len(env.notified) == 1, "a broken token must not page on every run"

    def test_missing_token_for_remote_base_blocks_before_the_network(
        self, env, monkeypatch, caplog
    ):
        monkeypatch.setenv("NEOTOMA_BASE_URL", "https://neotoma.example")
        monkeypatch.delenv("NEOTOMA_BEARER_TOKEN", raising=False)
        attempted = []
        monkeypatch.setattr(
            prepare.urllib.request,
            "urlopen",
            lambda *a, **k: attempted.append(1),
        )

        with caplog.at_level("ERROR"):
            result = prepare.existing_release_status("v0.19.0")

        assert result == prepare.STATUS_UNSAFE
        assert attempted == [], "no point issuing a request that cannot be authorized"
        assert "no token configured" in caplog.text

    def test_loopback_without_token_is_still_allowed(self, env, monkeypatch):
        # A local Neotoma needs no bearer token; the preflight must not block it.
        monkeypatch.delenv("NEOTOMA_BEARER_TOKEN", raising=False)
        _stub_entities(monkeypatch, [])
        assert prepare.existing_release_status("v0.19.0") is None

    def test_non_auth_http_error_stays_lenient(self, env, monkeypatch):
        monkeypatch.setenv("NEOTOMA_BASE_URL", "http://localhost:9180")
        monkeypatch.setattr(prepare.urllib.request, "urlopen", self._http_error(500))

        assert prepare.existing_release_status("v0.19.0") is None
        assert env.notified == []

    def test_run_prepare_defers_instead_of_spawning_when_unsafe(
        self, env, monkeypatch
    ):
        _wire_ready_to_spawn(monkeypatch, inflight=prepare.STATUS_UNSAFE)
        spawned = []
        monkeypatch.setattr(
            prepare, "spawn_prepare_agent", lambda *a, **k: spawned.append(1) or True
        )

        assert prepare.run_prepare(dry_run=False, force=False) == 0
        assert spawned == [], "never prepare on top of an unknown in-flight state"

    def test_unsafe_deferral_is_transient_for_on_merge(self, env, monkeypatch):
        # Treated like CI-unknown: the SHA stays unstamped so the next webhook
        # (or a fixed token) can still prepare this head.
        _wire_ready_to_spawn(monkeypatch, inflight=prepare.STATUS_UNSAFE)
        monkeypatch.setattr(prepare, "spawn_prepare_agent", lambda *a, **k: True)

        prepare.run_prepare(dry_run=False, force=False, on_merge=True)
        assert not prepare._already_ran_for_sha("c" * 40)


# ── Stale `publishing` records must not wedge the daemon forever ────────────


class TestStalePublishingAutoRepair:
    def test_publishing_with_shipped_tag_is_repaired_and_not_inflight(
        self, env, monkeypatch
    ):
        _stub_entities(
            monkeypatch, [{"snapshot": {"status": "publishing", "version": "v0.19.0"}}]
        )
        monkeypatch.setattr(prepare, "_release_already_shipped", lambda v: True)
        corrected = []
        monkeypatch.setattr(
            prepare,
            "_correct_release_status",
            lambda v, s, **k: corrected.append((v, s)) or True,
        )

        assert prepare.existing_release_status("v0.19.0") is None, (
            "a release that already shipped is not in flight"
        )
        assert corrected == [("v0.19.0", "published")]

    def test_publishing_that_never_shipped_still_blocks(self, env, monkeypatch):
        _stub_entities(
            monkeypatch, [{"snapshot": {"status": "publishing", "version": "v0.19.0"}}]
        )
        monkeypatch.setattr(prepare, "_release_already_shipped", lambda v: False)
        corrected = []
        monkeypatch.setattr(
            prepare,
            "_correct_release_status",
            lambda v, s, **k: corrected.append((v, s)) or True,
        )

        assert prepare.existing_release_status("v0.19.0") == "publishing"
        assert corrected == [], "a genuinely mid-publish release must not be rewritten"

    def test_pending_approval_is_untouched(self, env, monkeypatch):
        _stub_entities(
            monkeypatch,
            [{"snapshot": {"status": "pending_approval", "version": "v0.19.0"}}],
        )
        assert prepare.existing_release_status("v0.19.0") == "pending_approval"

    def test_existing_git_tag_counts_as_shipped(self, env, monkeypatch):
        monkeypatch.setattr(prepare, "_git", lambda args: "v0.19.0")
        assert prepare._release_already_shipped("v0.19.0") is True

    def test_existing_github_release_counts_as_shipped(self, env, monkeypatch):
        monkeypatch.setattr(prepare, "_git", lambda args: "")
        monkeypatch.setattr(prepare.subprocess, "run", lambda *a, **k: _proc(0))
        assert prepare._release_already_shipped("v0.19.0") is True

    def test_neither_tag_nor_release_is_not_shipped(self, env, monkeypatch):
        monkeypatch.setattr(prepare, "_git", lambda args: "")
        monkeypatch.setattr(prepare.subprocess, "run", lambda *a, **k: _proc(1))
        assert prepare._release_already_shipped("v0.19.0") is False

    def test_correction_posts_a_release_result_observation(self, env, monkeypatch):
        monkeypatch.setenv("NEOTOMA_BASE_URL", "http://localhost:9180")
        captured = {}

        def fake_urlopen(req, timeout=20):
            captured["url"] = req.full_url
            captured["body"] = json.loads(req.data)
            resp = MagicMock()
            resp.read.return_value = b"{}"
            resp.__enter__ = lambda self: resp
            resp.__exit__ = lambda self, *a: False
            return resp

        monkeypatch.setattr(prepare.urllib.request, "urlopen", fake_urlopen)

        assert prepare._correct_release_status("v0.19.0", "published", reason="x")
        assert captured["url"].endswith("/store")
        rec = captured["body"]["entities"][0]
        assert rec["entity_type"] == "release_result"
        assert (rec["version"], rec["status"]) == ("v0.19.0", "published")
        assert "release-v0.19.0-published-" in captured["body"]["idempotency_key"]


# ── has_new_release_result_since: the success signal for the outcome check ──


class TestHasNewReleaseResultSince:
    def test_result_observed_after_the_spawn_counts(self, env, monkeypatch):
        _stub_entities(
            monkeypatch,
            [
                {
                    "last_observation_at": prepare._now_iso(),
                    "snapshot": {"status": "pending_approval", "version": "v0.19.0"},
                }
            ],
        )
        assert prepare.has_new_release_result_since(_iso_ago(10)) is True

    def test_result_observed_before_the_spawn_does_not_count(self, env, monkeypatch):
        _stub_entities(
            monkeypatch,
            [
                {
                    "last_observation_at": _iso_ago(600),
                    "snapshot": {"status": "published", "version": "v0.18.0"},
                }
            ],
        )
        assert prepare.has_new_release_result_since(_iso_ago(10)) is False

    def test_terminal_failed_status_does_not_count(self, env, monkeypatch):
        _stub_entities(
            monkeypatch,
            [
                {
                    "last_observation_at": prepare._now_iso(),
                    "snapshot": {"status": "failed", "version": "v0.19.0"},
                }
            ],
        )
        assert prepare.has_new_release_result_since(_iso_ago(10)) is False

    def test_no_results_at_all(self, env, monkeypatch):
        _stub_entities(monkeypatch, [])
        assert prepare.has_new_release_result_since(_iso_ago(10)) is False

    def test_unreadable_neotoma_is_treated_as_no_result(self, env, monkeypatch):
        monkeypatch.setenv("NEOTOMA_BASE_URL", "http://localhost:9999")

        def refused(req, timeout=20):
            raise prepare.urllib.error.URLError("Connection refused")

        monkeypatch.setattr(prepare.urllib.request, "urlopen", refused)
        assert prepare.has_new_release_result_since(_iso_ago(10)) is False


# ── launchd wiring: supervision only works if it is actually scheduled ─────


class TestLaunchdCompanionAgents:
    def test_check_outcome_plist_template_exists(self):
        tmpl = _DAEMON_DIR / "com.ateles.phoenicurus-prepare-check.plist.tmpl"
        body = tmpl.read_text()
        assert "--check-agent-outcome" in body
        assert "<key>StartInterval</key>" in body
        assert "com.ateles.phoenicurus-prepare-check" in body

    def test_retry_plist_template_exists(self):
        tmpl = _DAEMON_DIR / "com.ateles.phoenicurus-prepare-retry.plist.tmpl"
        body = tmpl.read_text()
        assert "--retry-if-due" in body
        assert "<key>StartInterval</key>" in body
        assert "com.ateles.phoenicurus-prepare-retry" in body

    def test_install_script_loads_all_three_agents(self):
        body = (_DAEMON_DIR / "install.sh").read_text()
        for plist in (
            "com.ateles.phoenicurus-prepare.plist",
            "com.ateles.phoenicurus-prepare-check.plist",
            "com.ateles.phoenicurus-prepare-retry.plist",
        ):
            assert plist in body, f"install.sh --load-prepare must install {plist}"

    def test_state_files_are_gitignored(self):
        body = (_DAEMON_DIR / ".gitignore").read_text()
        assert ".phoenicurus_*" in body
        for name in (
            ".phoenicurus_prepare_last_spawn",
            ".phoenicurus_prepare_retry_after",
            ".phoenicurus_prepare_auth_notify",
        ):
            assert name in body, f"{name} must be documented in .gitignore"
