"""A spent session window cools the provider until its stated reset.

2026-09-29: the router reported claude headroom 0.65 (weekly), while every
Claude launch printed "You've hit your session limit · resets 12:30pm" and
exited 1.  ``dispatch_role`` logged ``cooling: none`` and kept spawning the dead
CLI (fifteen lens runs and four repairs failed instantly) because the only
cooldown lived in one process's memory.  These tests pin the fix: the refusal's
reset becomes a persisted cooling window the router honours in every process.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

_DAEMON_DIR = Path(__file__).resolve().parent
if str(_DAEMON_DIR) not in sys.path:
    sys.path.insert(0, str(_DAEMON_DIR))

import harness_router  # noqa: E402
import skill_runner  # noqa: E402
from skill_runner import SkillResult  # noqa: E402

REFUSAL = "You've hit your session limit · resets 12:30pm (Europe/Madrid)"
BINARIES = {"claude": "/bin/claude", "codex": None, "cursor": None}
ALL_BINARIES = {"claude": "/bin/claude", "codex": "/bin/codex", "cursor": "/bin/cursor"}


@pytest.fixture(autouse=True)
def _router(monkeypatch, tmp_path):
    monkeypatch.setenv("APIS_HARNESS_USAGE_FILE", str(tmp_path / "usage.json"))
    monkeypatch.setenv("APIS_HARNESS_HEADROOM_FILE", str(tmp_path / "absent.json"))
    monkeypatch.delenv("APIS_HARNESS_HEADROOM", raising=False)
    monkeypatch.delenv("APIS_HARNESS_MIN_HEADROOM", raising=False)
    monkeypatch.delenv("APIS_HARNESS_COOLDOWN_SECONDS", raising=False)
    monkeypatch.setenv("APIS_HARNESS_PROVIDERS", "claude,codex,cursor")
    harness_router.reset_state()
    yield
    harness_router.reset_state()


def _spawning_attempt(stdout: str, returncode: int = 1):
    """An attempt that reaches the CLI through a mocked subprocess boundary."""
    spawn = MagicMock(name="create_subprocess_exec")

    async def fake_exec(*cmd, **kwargs):
        spawn(*cmd)
        proc = MagicMock()
        proc.returncode = returncode

        async def communicate(input=None):
            return stdout.encode(), b""

        proc.communicate = communicate
        return proc

    async def attempt(selected: str) -> SkillResult:
        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            proc = await asyncio.create_subprocess_exec(selected)
            out, err = await proc.communicate()
        return SkillResult(
            "cicada", proc.returncode == 0, proc.returncode,
            out.decode(), err.decode(), provider=selected,
        )

    return attempt, spawn


def _run(attempt, binaries):
    return asyncio.run(
        skill_runner._run_provider_attempts("cicada", attempt, binaries=binaries)
    )


def _madrid(wall: float) -> datetime:
    return datetime.fromtimestamp(wall, ZoneInfo("Europe/Madrid"))


# ── refusal -> persisted window ────────────────────────────────────────────


def test_refusal_persists_a_window_ending_at_the_stated_reset() -> None:
    attempt, spawn = _spawning_attempt(REFUSAL)
    result = _run(attempt, BINARIES)

    assert not result.ok
    assert spawn.call_count == 1  # the one launch that discovered the refusal
    cooling = harness_router.persisted_cooling("claude")
    assert cooling is not None
    assert cooling["reason"] == "session_limit"
    until = _madrid(float(cooling["until"]))
    assert (until.hour, until.minute) == (12, 30)
    assert time.time() < float(cooling["until"]) <= time.time() + 24 * 3600 + 60


def test_the_window_outlives_the_process_that_recorded_it() -> None:
    attempt, _ = _spawning_attempt(REFUSAL)
    _run(attempt, BINARIES)
    harness_router.reset_state()  # a fresh dispatch_role interpreter: no memory
    assert "claude" in harness_router.cooling_providers()
    assert harness_router.provider_candidates(ALL_BINARIES)[0] != "claude"


def test_unparseable_reset_gets_the_default_window_and_says_so(monkeypatch, caplog) -> None:
    monkeypatch.setenv("APIS_HARNESS_COOLDOWN_SECONDS", "1200")
    attempt, _ = _spawning_attempt("You've hit your session limit")  # no reset stated
    with caplog.at_level("WARNING"):
        _run(attempt, BINARIES)
    cooling = harness_router.persisted_cooling("claude")
    assert cooling is not None
    assert cooling["reason"] == "capacity_unparsed_reset"
    assert time.time() + 1100 < float(cooling["until"]) <= time.time() + 1200
    assert "could not be read" in caplog.text


# ── a cooled provider is never spawned ─────────────────────────────────────


def test_dispatch_during_the_window_does_not_spawn_the_cli() -> None:
    harness_router.record_cooling("claude", time.time() + 3600, reason="session_limit")
    attempt, spawn = _spawning_attempt(REFUSAL)

    result = _run(attempt, BINARIES)

    spawn.assert_not_called()
    assert result.attempted_providers == ()
    assert not result.ok


def test_dispatch_routes_around_a_cooled_provider_without_spawning_it() -> None:
    harness_router.record_cooling("claude", time.time() + 3600, reason="session_limit")
    attempt, spawn = _spawning_attempt("done", returncode=0)

    result = _run(attempt, ALL_BINARIES)

    assert result.ok
    assert result.provider != "claude"
    assert all(call.args != ("claude",) for call in spawn.call_args_list)


def test_every_provider_cooled_is_a_distinct_cooled_until_result() -> None:
    until = time.time() + 7200
    harness_router.record_cooling("claude", until, reason="session_limit")
    attempt, spawn = _spawning_attempt(REFUSAL)

    result = _run(attempt, BINARIES)

    spawn.assert_not_called()
    assert result.cooled_until == harness_router.render_wall(until)
    assert "cooled until" in result.error
    assert "no subscription-backed harness provider has usable headroom" not in result.error
    assert "exhausted after attempts" not in result.error


def test_cooled_result_names_the_earliest_return_across_providers() -> None:
    now = time.time()
    harness_router.record_cooling("claude", now + 9000, reason="session_limit")
    harness_router.record_cooling("codex", now + 3000, reason="session_limit")
    attempt, spawn = _spawning_attempt(REFUSAL)

    result = _run(attempt, {"claude": "/bin/claude", "codex": "/bin/codex", "cursor": None})

    spawn.assert_not_called()
    assert result.cooled_until == harness_router.render_wall(now + 3000)


def test_no_binary_is_still_generic_not_cooled() -> None:
    attempt, spawn = _spawning_attempt(REFUSAL)
    result = _run(attempt, {"claude": None, "codex": None, "cursor": None})
    spawn.assert_not_called()
    assert result.cooled_until == ""
    assert "usable headroom" in result.error


def test_pinned_cooled_provider_reports_the_window() -> None:
    harness_router.record_cooling("claude", time.time() + 3600, reason="session_limit")
    attempt, spawn = _spawning_attempt(REFUSAL)
    result = asyncio.run(
        skill_runner._run_provider_attempts(
            "cicada", attempt, binaries=ALL_BINARIES, provider="claude"
        )
    )
    spawn.assert_not_called()
    assert "cooled until" in result.error


def test_swarm_classifies_a_cooled_result_as_a_usage_limit() -> None:
    import swarm_dispatch

    result = SkillResult("pavo", False, None, "", "", error="x", cooled_until="2026-09-29T12:30+02:00")
    assert swarm_dispatch.review_failure_class(result) == "usage limit"


# ── expiry re-enables the provider ─────────────────────────────────────────


def test_expiry_re_enables_the_provider() -> None:
    now = time.time()
    harness_router.record_cooling("claude", now + 3600, reason="session_limit")
    assert harness_router.provider_candidates(BINARIES, now_wall=now) == []
    assert harness_router.provider_candidates(BINARIES, now_wall=now + 3601) == ["claude"]
    assert harness_router.usable_provider_names(BINARIES, now_wall=now + 3601) == {"claude"}
    assert harness_router.persisted_cooling("claude", now_wall=now + 3601) is None


def test_an_already_expired_window_dispatches_normally() -> None:
    harness_router.record_cooling("claude", time.time() - 5, reason="session_limit")
    attempt, spawn = _spawning_attempt("done", returncode=0)
    result = _run(attempt, BINARIES)
    assert result.ok and spawn.call_count == 1


# ── the window is its own fact beside usage/exhaustion ─────────────────────


def test_cooling_keeps_usage_windows_and_survives_a_fresh_observation(tmp_path) -> None:
    now = time.time()
    harness_router.record_usage("claude", [{"name": "weekly", "used_percent": 35}])
    harness_router.record_cooling("claude", now + 3600, reason="session_limit")
    entry = json.loads((tmp_path / "usage.json").read_text())["claude"]
    assert entry["windows"][0]["used_percent"] == 35
    assert entry["cooling"]["reason"] == "session_limit"

    harness_router.record_usage("claude", [{"name": "weekly", "used_percent": 36}])
    harness_router.record_exhausted("claude", now + 86400)
    assert harness_router.persisted_cooling("claude") is not None


def test_weekly_headroom_alone_would_have_dispatched() -> None:
    """The incident: headroom fine, session window spent."""
    harness_router.record_usage("claude", [{"name": "weekly", "used_percent": 35}])
    assert harness_router.configured_headroom()["claude"] == pytest.approx(0.65)
    assert harness_router.provider_candidates(BINARIES) == ["claude"]
    harness_router.record_cooling("claude", time.time() + 3600, reason="session_limit")
    assert harness_router.provider_candidates(BINARIES) == []


def test_auth_failure_is_not_persisted() -> None:
    attempt, _ = _spawning_attempt("Invalid authentication credentials")
    _run(attempt, BINARIES)
    assert harness_router.persisted_cooling("claude") is None
