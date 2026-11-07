"""A Fly-bound dispatch whose first provider Fly cannot run tries the next one.

Provider rotation once sent a builder to codex first; the Fly runner refused it
before launch ("cannot yet give a codex job its Neotoma tools") and the task
ended blocked although claude runs on Fly. These tests drive the real
``_run_skill_once`` (and the real ``fly_runner.build_job``) through the real
provider loop, with only the Machines API and the local spawn faked.

Offline: no Fly, GitHub, token mint, or Neotoma network calls.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fly_runner
import skill_runner
from test_fly_runner import FakeFly, fly_env, make_executor, seam  # noqa: F401 -- fixtures

FLY_BOUND = frozenset({"claude", "codex", "cursor"})
BINARIES = {"claude": "/usr/bin/claude-test", "codex": "/usr/bin/codex", "cursor": "/usr/bin/cursor"}


@pytest.fixture
def loop(fly_env, seam, monkeypatch, tmp_path):  # noqa: F811 -- pytest fixtures
    """The provider loop over real Fly-forced attempts. Returns a runner and
    what it observed: attempts, local spawns, cooldowns, Machines API."""
    spawned, fake_exec = seam
    # Claude runs per job; codex and cursor are login providers. Codex has its
    # login machine, so what refuses it is the Neotoma-tools check (the case
    # seen in production); cursor has none.
    monkeypatch.setattr(fly_runner, "LOGIN_PROVIDERS", frozenset({"codex", "cursor"}))
    monkeypatch.setenv("APIS_FLY_CODEX_MACHINE_ID", "m-codex")
    monkeypatch.delenv("APIS_FLY_CURSOR_MACHINE_ID", raising=False)
    # _run_skill_once looks up the provider binary before the Fly path, so a
    # host without codex or cursor would end on "binary unavailable" rather
    # than the Fly refusal. The *_BIN constants are resolved at import, so set
    # the constant as well as the variable.
    for name in ("codex", "cursor"):
        stub = tmp_path / name
        stub.write_text("#!/bin/sh\nexit 0\n")
        stub.chmod(0o755)
        monkeypatch.setenv(f"APIS_{name.upper()}_BIN", str(stub))
        monkeypatch.setattr(skill_runner, f"{name.upper()}_BIN", str(stub))
    fake = FakeFly()
    executor, minted, _ = make_executor(
        fake, record={"exit_code": 0, "stdout": "GATE_INHERITANCE: clear\n"}
    )
    monkeypatch.setattr(fly_runner, "get_executor", lambda: executor)
    monkeypatch.setattr(skill_runner, "_refresh_usage_snapshot", lambda *a, **k: None)
    cooled = []
    monkeypatch.setattr(skill_runner, "cool_down", lambda name, *a, **k: cooled.append(name))
    monkeypatch.setattr(
        skill_runner, "_cool_after_capacity_failure",
        lambda name, result: cooled.append(name),
    )  # fmt: skip
    seen = {"attempts": [], "spawned": spawned, "cooled": cooled, "fake": fake,
            "minted": minted}  # fmt: skip

    def run(candidates, *, binaries=BINARIES, fly_bound=FLY_BOUND, **kw):
        monkeypatch.setattr(skill_runner, "provider_candidates", lambda *a, **k: list(candidates))

        async def attempt(selected, **_):
            seen["attempts"].append(selected)
            return await skill_runner._run_skill_once(
                "lanius", "check the gates", provider=selected, role="lanius",
                runner="fly", repository="owner/repo", github_token="",
            )  # fmt: skip

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            return asyncio.run(skill_runner._run_provider_attempts(
                "lanius", attempt, binaries=dict(binaries), role="lanius",
                fly_bound=fly_bound, **kw,
            ))  # fmt: skip

    return run, seen


def test_codex_refused_by_fly_launches_once_on_claude_with_no_local_run(loop):
    run, seen = loop
    result = run(["codex", "claude"])
    assert result.ok, result.error
    assert result.provider == "claude"
    assert seen["attempts"] == ["codex", "claude"]
    assert result.attempted_providers == ("codex", "claude")
    assert seen["minted"] == ["owner/repo"]  # exactly one Fly launch, for claude
    assert seen["spawned"] == []  # nothing ran on the Mac
    assert seen["cooled"] == []  # codex is healthy; Fly just cannot run it here



def test_cursor_with_no_login_machine_fails_over_to_claude(loop):
    # Red, seen with provider_specific=True temporarily removed from the
    # "no ... login machine configured" refusal in fly_runner.build_job:
    # the loop stopped after the first attempt, claude never ran, and
    # `assert result.ok` failed with "fly runner refused: not eligible for
    # Fly: no cursor login machine configured (APIS_FLY_CURSOR_MACHINE_ID)".
    run, seen = loop
    result = run(["cursor", "claude"])
    assert result.ok, result.error
    assert result.provider == "claude"
    assert seen["attempts"] == ["cursor", "claude"]
    assert result.attempted_providers == ("cursor", "claude")
    assert seen["minted"] == ["owner/repo"]  # exactly one Fly launch, for claude
    assert seen["spawned"] == []  # nothing ran on the Mac
    assert seen["cooled"] == []  # cursor is healthy; Fly just has no login machine for it


def test_refusal_not_specific_to_the_provider_stops_with_no_failover(loop, monkeypatch):
    # A non-https NEOTOMA_BASE_URL refuses every provider alike, so trying
    # the next one would only repeat the refusal.
    monkeypatch.setenv("NEOTOMA_BASE_URL", "http://neotoma.example.test")
    run, seen = loop
    result = run(["claude", "cursor"])
    assert not result.ok
    assert result.error.startswith("fly runner refused: not eligible for Fly")
    assert "NEOTOMA_BASE_URL must be an https URL" in result.error
    assert not result.fly_provider_ineligible
    assert seen["attempts"] == ["claude"]
    assert seen["minted"] == [] and seen["spawned"] == [] and seen["cooled"] == []


@pytest.mark.parametrize("pin", ["provider", "preferred_provider"])
def test_pinned_codex_is_refused_with_no_failover(loop, pin):
    run, seen = loop
    result = run(["codex", "claude"], **{pin: "codex"})
    assert not result.ok
    assert "the Fly runner cannot yet give a codex job its Neotoma tools" in result.error
    assert seen["attempts"] == ["codex"]
    assert seen["minted"] == [] and seen["spawned"] == [] and seen["cooled"] == []


def test_no_eligible_provider_ends_with_the_refusal_as_before(loop):
    run, seen = loop
    result = run(["codex", "cursor"])
    assert not result.ok
    assert result.error.startswith("fly runner refused: not eligible for Fly")
    assert "no cursor login machine configured" in result.error
    # The builder reads this to decide its local-delivery path, as before.
    assert result.fly_ineligible_before_launch
    assert result.attempted_providers == ("codex", "cursor")
    assert seen["minted"] == [] and seen["spawned"] == [] and seen["cooled"] == []


def test_fly_launch_failure_neither_fails_over_nor_cools(loop):
    run, seen = loop
    seen["fake"].create_mode = "refused"  # 422: the machine was never created
    result = run(["claude", "codex"])
    assert not result.ok
    assert result.error.startswith("fly runner refused: Fly launch failed")
    assert not result.fly_provider_ineligible
    assert seen["attempts"] == ["claude"]
    assert seen["spawned"] == [] and seen["cooled"] == []


# A provider the loop may select that has no binary on the Fly runner image.
NO_RUNNER = "gemini"


@pytest.fixture
def no_runner_provider(monkeypatch, tmp_path):
    """Make NO_RUNNER Fly-bound with a local binary, so its attempt reaches
    ``fly_runner.build_job`` and the ``RUNNER_BINARIES`` check is what stops it."""
    assert NO_RUNNER not in fly_runner.RUNNER_BINARIES
    stub = tmp_path / NO_RUNNER
    stub.write_text("#!/bin/sh\nexit 0\n")
    stub.chmod(0o755)
    real_binaries = skill_runner._provider_binaries
    monkeypatch.setattr(
        skill_runner, "_provider_binaries", lambda: {**real_binaries(), NO_RUNNER: str(stub)}
    )
    # The local command builder knows only real harnesses; give NO_RUNNER a
    # plain command so the attempt gets as far as the Fly check.
    real_command = skill_runner._provider_command

    def command(provider, binary, system_prompt, work_prompt, **kw):
        if provider == NO_RUNNER:
            return [binary, "--print"], work_prompt.encode()
        return real_command(provider, binary, system_prompt, work_prompt, **kw)

    monkeypatch.setattr(skill_runner, "_provider_command", command)
    return {"fly_bound": FLY_BOUND | {NO_RUNNER}, "binaries": {**BINARIES, NO_RUNNER: str(stub)}}


def test_provider_with_no_fly_runner_fails_over_to_claude(loop, no_runner_provider):
    # Red, seen with provider_specific=True temporarily removed from the
    # RUNNER_BINARIES refusal in fly_runner.build_job: the loop stopped after
    # the first attempt, claude never ran, and `assert result.ok` failed with
    # "fly runner refused: not eligible for Fly: provider 'gemini' has no Fly
    # runner".
    run, seen = loop
    result = run([NO_RUNNER, "claude"], **no_runner_provider)
    assert result.ok, result.error
    assert result.provider == "claude"
    assert seen["attempts"] == [NO_RUNNER, "claude"]
    assert result.attempted_providers == (NO_RUNNER, "claude")
    assert seen["minted"] == ["owner/repo"]  # exactly one Fly launch, for claude
    assert seen["spawned"] == []
    assert seen["cooled"] == []


def test_only_provider_with_no_fly_runner_ends_with_the_refusal(loop, no_runner_provider):
    run, seen = loop
    result = run([NO_RUNNER], **no_runner_provider)
    assert not result.ok
    assert result.error.startswith("fly runner refused: not eligible for Fly")
    assert f"provider {NO_RUNNER!r} has no Fly runner" in result.error
    assert result.fly_ineligible_before_launch
    assert seen["attempts"] == [NO_RUNNER]
    assert seen["minted"] == [] and seen["spawned"] == [] and seen["cooled"] == []
