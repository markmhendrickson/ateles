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
def loop(fly_env, seam, monkeypatch):  # noqa: F811 -- pytest fixtures
    """The provider loop over real Fly-forced attempts. Returns a runner and
    what it observed: attempts, local spawns, cooldowns, Machines API."""
    spawned, fake_exec = seam
    # Claude runs per job; codex and cursor are login providers. Codex has its
    # login machine, so what refuses it is the Neotoma-tools check (the case
    # seen in production); cursor has none.
    monkeypatch.setattr(fly_runner, "LOGIN_PROVIDERS", frozenset({"codex", "cursor"}))
    monkeypatch.setenv("APIS_FLY_CODEX_MACHINE_ID", "m-codex")
    monkeypatch.delenv("APIS_FLY_CURSOR_MACHINE_ID", raising=False)
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

    def run(candidates, **kw):
        monkeypatch.setattr(skill_runner, "provider_candidates", lambda *a, **k: list(candidates))

        async def attempt(selected, **_):
            seen["attempts"].append(selected)
            return await skill_runner._run_skill_once(
                "lanius", "check the gates", provider=selected, role="lanius",
                runner="fly", repository="owner/repo", github_token="",
            )  # fmt: skip

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            return asyncio.run(skill_runner._run_provider_attempts(
                "lanius", attempt, binaries=dict(BINARIES), role="lanius",
                fly_bound=FLY_BOUND, **kw,
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
