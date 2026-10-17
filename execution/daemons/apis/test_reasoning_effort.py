"""Inert per-dispatch effort controls: no provider, credential or network calls."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "execution/scripts"))
import dispatch_role
import harness_lens_runner as lens
import skill_runner


def _forbidden(*args, **kwargs):
    raise AssertionError("external boundary must remain inert")


@pytest.mark.parametrize("effort", ["none", "low", "medium", "high", "xhigh", "max"])
def test_requested_effort_reaches_native_codex_argv(monkeypatch, effort):
    monkeypatch.setattr(skill_runner, "_git_roots_for_sandbox", lambda cwd: [])
    command, stdin = skill_runner._provider_command(
        "codex", "fixture-codex", "CANONICAL ROLE", "TASK", cwd=None,
        model="gpt-6-sol", reasoning_effort=effort,
    )
    assert command[:4] == ["fixture-codex", "exec", "--model", "gpt-6-sol"]
    index = command.index("--config")
    assert command[index + 1] == f'model_reasoning_effort={json.dumps(effort)}'
    assert command[command.index("--sandbox") + 1] == "workspace-write"
    assert command[-1] == "-" and b"CANONICAL ROLE" in stdin and b"TASK" in stdin
    assert "CANONICAL ROLE" not in command and "TASK" not in command


@pytest.mark.parametrize("provider", ["codex", "cursor", "claude"])
def test_omitted_effort_preserves_command(monkeypatch, provider):
    monkeypatch.setattr(skill_runner, "_git_roots_for_sandbox", lambda cwd: [])
    original = skill_runner._provider_command(provider, "fixture", "ROLE", "TASK", cwd=None)
    explicit_none = skill_runner._provider_command(
        provider, "fixture", "ROLE", "TASK", cwd=None, reasoning_effort=None,
    )
    assert original == explicit_none
    assert not any("model_reasoning_effort" in arg for arg in original[0])


@pytest.mark.parametrize("effort", ["", "HIGH", "high\n", 'high"\nother=true', 1, {}, "ultra"])
def test_invalid_effort_refuses_before_dispatch_edges(monkeypatch, effort):
    monkeypatch.setattr(skill_runner, "_provider_binaries", _forbidden)
    monkeypatch.setattr(skill_runner, "_load_agent_def", _forbidden)
    monkeypatch.setattr(skill_runner.asyncio, "create_subprocess_exec", _forbidden)
    with pytest.raises(ValueError, match="reasoning effort"):
        asyncio.run(skill_runner.run_skill("pavo", "TASK", provider="codex", reasoning_effort=effort))
    with pytest.raises(ValueError, match="reasoning effort"):
        asyncio.run(skill_runner._run_skill_once("pavo", "TASK", provider="codex", reasoning_effort=effort))


@pytest.mark.parametrize("provider", [None, "cursor", "claude", "claude-local"])
def test_explicit_effort_refuses_unsupported_or_unpinned_provider(monkeypatch, provider):
    monkeypatch.setattr(skill_runner, "_provider_binaries", _forbidden)
    monkeypatch.setattr(skill_runner, "_load_agent_def", _forbidden)
    with pytest.raises(ValueError, match="codex"):
        asyncio.run(skill_runner.run_skill("pavo", "TASK", provider=provider, reasoning_effort="high"))
    with pytest.raises(ValueError, match="codex"):
        skill_runner._provider_command(provider, "fixture", "ROLE", "TASK", cwd=None, reasoning_effort="high")


def test_dispatch_and_repeated_attempts_keep_request(monkeypatch):
    seen = []
    async def once(skill, prompt, **kwargs):
        seen.append(kwargs)
        return skill_runner.SkillResult(skill, True, 0, "done", "", provider=kwargs["provider"])
    async def repeat(skill, attempt, **kwargs):
        assert kwargs["provider"] == "codex"
        await attempt("codex")
        return await attempt("codex")
    monkeypatch.setattr(skill_runner, "_run_skill_once", once)
    monkeypatch.setattr(skill_runner, "_run_provider_attempts", repeat)
    monkeypatch.setattr(skill_runner, "_provider_binaries", lambda: {"codex": "fixture"})
    monkeypatch.setattr(skill_runner, "_tier_bound_binaries", lambda binaries, *a, **k: binaries)
    result = asyncio.run(dispatch_role.dispatch(
        "pavo", "TASK", provider="codex", model="gpt-6-sol", reasoning_effort="high",
        command_wrapper=["fixture-guard"], local_review=True,
        env_extra={"CODEX_HOME": "/fixture/isolated"},
    ))
    assert result.ok and len(seen) == 2
    assert all(row["reasoning_effort"] == "high" for row in seen)
    assert all(row["model"] == "gpt-6-sol" for row in seen)
    assert all(row["command_wrapper"] == ["fixture-guard"] for row in seen)
    assert all(row["env_extra"] == {"CODEX_HOME": "/fixture/isolated"} for row in seen)
    assert all(row["local_review"] for row in seen)


def test_lens_cli_forwards_explicit_effort_without_running_review(monkeypatch, tmp_path):
    seen = []
    async def capture(target, **kwargs):
        seen.append(kwargs)
        return {"ok": True}
    monkeypatch.setattr(lens, "run_one", capture)
    brief = tmp_path / "brief.md"
    brief.write_text("review")
    assert lens.main([
        "--repo", "owner/repository", "--pr", "1", "--head", "a" * 40,
        "--lens", "arch", "--provider", "codex", "--model", "gpt-6-sol",
        "--reasoning-effort", "high", "--brief", str(brief),
    ]) == 0
    assert seen[0]["reasoning_effort"] == "high"


def test_lens_compare_refuses_mixed_provider_before_first_review(monkeypatch, tmp_path):
    monkeypatch.setattr(lens, "run_one", _forbidden)
    target = lens.LensTarget("owner/repository", 1, "a" * 40, "arch", "waxwing")
    with pytest.raises(ValueError, match="codex"):
        asyncio.run(lens.run_compare(
            target, providers=["codex", "cursor"], post=False, dry_run=False,
            repo_worktree_name="fixture", scratch_root=tmp_path,
            brief_path=tmp_path / "brief", timeout=1, reasoning_effort="high",
        ))
