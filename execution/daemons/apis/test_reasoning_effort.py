"""Inert per-dispatch effort controls: no provider, credential or network calls."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "execution/scripts"))
import dispatch_role  # noqa: E402
import harness_lens_runner as lens  # noqa: E402
import skill_runner  # noqa: E402


def _forbidden(*args, **kwargs):
    raise AssertionError("external boundary must remain inert")


@pytest.fixture(autouse=True)
def no_external_effects(monkeypatch):
    import httpx
    import subprocess
    import urllib.request

    monkeypatch.setattr(skill_runner.asyncio, "create_subprocess_exec", _forbidden)
    monkeypatch.setattr(subprocess, "run", _forbidden)
    monkeypatch.setattr(httpx.Client, "request", _forbidden)
    monkeypatch.setattr(httpx.AsyncClient, "request", _forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", _forbidden)


@pytest.mark.parametrize("effort", ["none", "low", "medium", "high", "xhigh", "max"])
def test_requested_effort_reaches_native_codex_argv(monkeypatch, effort):
    monkeypatch.setattr(skill_runner, "_git_roots_for_sandbox", lambda cwd: [])
    command, stdin = skill_runner._provider_command(
        "codex",
        "fixture-codex",
        "CANONICAL ROLE",
        "TASK",
        cwd=None,
        model="gpt-6-sol",
        reasoning_effort=effort,
    )
    assert command[:4] == ["fixture-codex", "exec", "--model", "gpt-6-sol"]
    index = command.index("--config")
    assert command[index + 1] == f"model_reasoning_effort={json.dumps(effort)}"
    assert command[command.index("--sandbox") + 1] == "workspace-write"
    assert command[-1] == "-" and b"CANONICAL ROLE" in stdin and b"TASK" in stdin
    assert "CANONICAL ROLE" not in command and "TASK" not in command


@pytest.mark.parametrize("provider", ["codex", "cursor", "claude"])
def test_omitted_effort_preserves_command(monkeypatch, provider):
    monkeypatch.setattr(skill_runner, "_git_roots_for_sandbox", lambda cwd: [])
    original = skill_runner._provider_command(
        provider, "fixture", "ROLE", "TASK", cwd=None
    )
    explicit_none = skill_runner._provider_command(
        provider,
        "fixture",
        "ROLE",
        "TASK",
        cwd=None,
        reasoning_effort=None,
    )
    assert original == explicit_none
    assert not any("model_reasoning_effort" in arg for arg in original[0])


@pytest.mark.parametrize(
    "effort", ["", "HIGH", "high\n", 'high"\nother=true', 1, {}, "ultra"]
)
def test_invalid_effort_refuses_before_dispatch_edges(monkeypatch, effort):
    monkeypatch.setattr(skill_runner, "_provider_binaries", _forbidden)
    monkeypatch.setattr(skill_runner, "_load_agent_def", _forbidden)
    monkeypatch.setattr(skill_runner.asyncio, "create_subprocess_exec", _forbidden)
    with pytest.raises(ValueError, match="reasoning effort"):
        asyncio.run(
            skill_runner.run_skill(
                "pavo", "TASK", provider="codex", reasoning_effort=effort
            )
        )
    with pytest.raises(ValueError, match="reasoning effort"):
        asyncio.run(
            skill_runner._run_skill_once(
                "pavo", "TASK", provider="codex", reasoning_effort=effort
            )
        )


@pytest.mark.parametrize("provider", [None, "cursor", "claude", "claude-local"])
def test_explicit_effort_refuses_unsupported_or_unpinned_provider(
    monkeypatch, provider
):
    monkeypatch.setattr(skill_runner, "_provider_binaries", _forbidden)
    monkeypatch.setattr(skill_runner, "_load_agent_def", _forbidden)
    with pytest.raises(ValueError, match="codex"):
        asyncio.run(
            skill_runner.run_skill(
                "pavo", "TASK", provider=provider, reasoning_effort="high"
            )
        )
    with pytest.raises(ValueError, match="codex"):
        skill_runner._provider_command(
            provider, "fixture", "ROLE", "TASK", cwd=None, reasoning_effort="high"
        )


def test_dispatch_and_repeated_attempts_keep_request(monkeypatch):
    seen = []

    async def once(skill, prompt, **kwargs):
        seen.append(kwargs)
        return skill_runner.SkillResult(
            skill, True, 0, "done", "", provider=kwargs["provider"]
        )

    async def repeat(skill, attempt, **kwargs):
        assert kwargs["provider"] == "codex"
        await attempt("codex")
        return await attempt("codex")

    monkeypatch.setattr(skill_runner, "_run_skill_once", once)
    monkeypatch.setattr(skill_runner, "_run_provider_attempts", repeat)
    monkeypatch.setattr(
        skill_runner, "_provider_binaries", lambda: {"codex": "fixture"}
    )
    monkeypatch.setattr(
        skill_runner, "_tier_bound_binaries", lambda binaries, *a, **k: binaries
    )
    result = asyncio.run(
        dispatch_role.dispatch(
            "pavo",
            "TASK",
            provider="codex",
            model="gpt-6-sol",
            reasoning_effort="high",
            command_wrapper=["fixture-guard"],
            local_review=True,
            env_extra={"CODEX_HOME": "/fixture/isolated"},
        )
    )
    assert result.ok and len(seen) == 2
    assert all(row["reasoning_effort"] == "high" for row in seen)
    assert all(row["model"] == "gpt-6-sol" for row in seen)
    assert all(row["command_wrapper"] == ["fixture-guard"] for row in seen)
    assert all(row["env_extra"] == {"CODEX_HOME": "/fixture/isolated"} for row in seen)
    assert all(row["local_review"] for row in seen)


def test_lens_cli_forwards_explicit_effort_without_running_review(
    monkeypatch, tmp_path
):
    seen = []

    async def capture(target, **kwargs):
        seen.append(kwargs)
        return {"ok": True}

    monkeypatch.setattr(lens, "run_one", capture)
    brief = tmp_path / "brief.md"
    brief.write_text("review")
    assert (
        lens.main(
            [
                "--repo",
                "owner/repository",
                "--pr",
                "1",
                "--head",
                "a" * 40,
                "--lens",
                "arch",
                "--provider",
                "codex",
                "--model",
                "gpt-6-sol",
                "--reasoning-effort",
                "high",
                "--brief",
                str(brief),
            ]
        )
        == 0
    )
    assert seen[0]["reasoning_effort"] == "high"


def test_lens_compare_refuses_mixed_provider_before_first_review(monkeypatch, tmp_path):
    monkeypatch.setattr(lens, "run_one", _forbidden)
    target = lens.LensTarget("owner/repository", 1, "a" * 40, "arch", "waxwing")
    with pytest.raises(ValueError, match="codex"):
        asyncio.run(
            lens.run_compare(
                target,
                providers=["codex", "cursor"],
                post=False,
                dry_run=False,
                repo_worktree_name="fixture",
                scratch_root=tmp_path,
                brief_path=tmp_path / "brief",
                timeout=1,
                reasoning_effort="high",
            )
        )


@pytest.fixture
def inert_provider_boundary(monkeypatch, tmp_path):
    """Reach the real command/env builder, replacing every external edge."""
    from types import SimpleNamespace
    from lib.daemon_runtime import AgentDefinition

    skill = tmp_path / ".claude/skills/pavo/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("ROLE SKILL")
    monkeypatch.setattr(skill_runner, "ATELES_REPO", tmp_path)
    monkeypatch.setattr(
        skill_runner,
        "_load_agent_def",
        lambda role: AgentDefinition(
            name=role,
            prompt_markdown="CANONICAL FULL ROLE",
            tool_allowlist="*",
        ),
    )
    monkeypatch.setattr(
        skill_runner, "_load_active_policy_prompt", lambda role: "LIVE POLICY"
    )
    monkeypatch.setattr(
        skill_runner, "_provider_binaries", lambda: {"codex": "fixture-codex"}
    )
    monkeypatch.setattr(skill_runner, "_git_roots_for_sandbox", lambda cwd: [])
    monkeypatch.setattr(skill_runner, "_write_harness_event", lambda **kwargs: None)
    seen = []

    async def fake_exec(*argv, **kwargs):
        record = {"argv": list(argv), "env": kwargs["env"]}
        seen.append(record)

        async def communicate(input=None):
            record["stdin"] = input
            return b"done", b""

        return SimpleNamespace(returncode=0, communicate=communicate)

    monkeypatch.setattr(skill_runner.asyncio, "create_subprocess_exec", fake_exec)
    home = tmp_path / "isolated-home"
    return seen, {"CODEX_HOME": str(home), "ATELES_LOCAL_REVIEW_HOME": str(home)}


def test_real_attempt_forwards_effort_to_inert_spawn_and_keeps_role_and_home(
    inert_provider_boundary,
):
    seen, env = inert_provider_boundary
    result = asyncio.run(
        skill_runner._run_skill_once(
            "pavo",
            "TASK BODY",
            provider="codex",
            reasoning_effort="high",
            model="gpt-6-sol",
            local_review=True,
            env_extra=env,
            command_wrapper=["fixture-guard"],
        )
    )
    assert result.ok and len(seen) == 1
    argv = seen[0]["argv"]
    assert argv[:2] == ["fixture-guard", "fixture-codex"]
    assert argv[argv.index("--config") + 1] == 'model_reasoning_effort="high"'
    assert argv[argv.index("--model") + 1] == "gpt-6-sol"
    assert seen[0]["env"]["CODEX_HOME"] == env["CODEX_HOME"]
    assert seen[0]["env"]["HOME"] == env["CODEX_HOME"]
    assert (
        "GH_TOKEN" not in seen[0]["env"]
        and "NEOTOMA_BEARER_TOKEN" not in seen[0]["env"]
    )
    payload = seen[0]["stdin"].decode()
    assert payload.startswith("CANONICAL FULL ROLE")
    assert (
        "ROLE SKILL" in payload and "LIVE POLICY" in payload and "TASK BODY" in payload
    )


@pytest.mark.parametrize("contract", ["model", "wrapper", "credential"])
def test_effort_does_not_bypass_existing_refusals(inert_provider_boundary, contract):
    seen, env = inert_provider_boundary
    kwargs = {"model": "gpt-6-sol", "local_review": True, "env_extra": env}
    if contract == "model":
        kwargs["model"] = "invalid model"
        with pytest.raises(ValueError):
            asyncio.run(
                skill_runner._run_skill_once(
                    "pavo",
                    "TASK",
                    provider="codex",
                    reasoning_effort="high",
                    **kwargs,
                )
            )
    else:
        if contract == "wrapper":
            kwargs["codex_outer_sandboxed"] = True
        else:
            kwargs.update(include_github_contract=True, github_token="")
        result = asyncio.run(
            skill_runner._run_skill_once(
                "pavo",
                "TASK",
                provider="codex",
                reasoning_effort="high",
                **kwargs,
            )
        )
        assert not result.ok
    assert seen == []


def test_lens_run_one_forwards_effort_to_real_dispatch_boundary(monkeypatch, tmp_path):
    from types import SimpleNamespace

    seen = []
    monkeypatch.setattr(lens.Worktree, "create", lambda self, **k: self.path.mkdir())
    monkeypatch.setattr(lens.Worktree, "remove", lambda self: None)
    monkeypatch.setattr(lens, "check_headroom", lambda provider: None)
    monkeypatch.setattr(lens, "read_agent_prompt", lambda *args: "TARGET ROLE")
    monkeypatch.setattr(lens, "capture_stash_ref_state", lambda *args: None)
    sandbox = SimpleNamespace(
        env_extra={"CODEX_HOME": "/fixture/isolated"},
        command_wrapper=["fixture-guard"],
        codex_outer_sandbox_probed=False,
    )
    monkeypatch.setattr(lens.HarnessSandbox, "build", lambda *a, **k: sandbox)
    monkeypatch.setattr(lens, "refuse_if_guard_required", lambda value: None)

    class BoundaryObserved(Exception):
        pass

    async def capture(role, task, **kwargs):
        seen.append((role, task, kwargs))
        raise BoundaryObserved()

    monkeypatch.setattr(lens.dispatch_role, "dispatch", capture)
    # Preserve cleanup's capture without reaching a verdict reader or Git.
    monkeypatch.setattr(
        lens, "verify_stash_ref_unchanged_after_dispatch", lambda *a, **k: None
    )
    brief = tmp_path / "brief.md"
    brief.write_text("FULL REVIEW BRIEF")
    target = lens.LensTarget("owner/repository", 1, "a" * 40, "arch", "waxwing")
    with pytest.raises(BoundaryObserved):
        asyncio.run(
            lens.run_one(
                target,
                provider="codex",
                post=False,
                dry_run=False,
                repo_worktree_name="fixture",
                scratch_root=tmp_path,
                brief_path=brief,
                timeout=1,
                model="gpt-6-sol",
                reasoning_effort="high",
            )
        )
    assert len(seen) == 1
    role, task, kwargs = seen[0]
    assert role == "waxwing" and "FULL REVIEW BRIEF" in task and "TARGET ROLE" in task
    assert kwargs["reasoning_effort"] == "high" and kwargs["model"] == "gpt-6-sol"
    assert kwargs["command_wrapper"] == sandbox.command_wrapper
    assert kwargs["env_extra"] == sandbox.env_extra and kwargs["local_review"]


def test_pinned_effort_never_fails_over_to_another_provider(monkeypatch):
    seen = []
    monkeypatch.setenv("APIS_HARNESS_PROVIDERS", "codex,cursor")
    monkeypatch.setenv("APIS_HARNESS_HEADROOM", '{"codex":1,"cursor":1}')
    monkeypatch.setattr(
        skill_runner,
        "_provider_binaries",
        lambda: {"codex": "fixture", "cursor": "fixture"},
    )
    monkeypatch.setattr(
        skill_runner, "_tier_bound_binaries", lambda binaries, *a, **k: binaries
    )
    monkeypatch.setattr(skill_runner, "_cool_after_capacity_failure", lambda *a: None)
    monkeypatch.setattr(skill_runner, "cooled_until_all", lambda *a: {})
    monkeypatch.setattr(skill_runner, "notify_dispatch_failure", lambda *a, **k: None)

    async def unavailable(skill, prompt, **kwargs):
        seen.append(kwargs)
        return skill_runner.SkillResult(
            skill, False, 1, "", "usage limit reached", provider=kwargs["provider"]
        )

    monkeypatch.setattr(skill_runner, "_run_skill_once", unavailable)
    result = asyncio.run(
        skill_runner.run_skill(
            "pavo",
            "TASK",
            provider="codex",
            reasoning_effort="high",
            model="gpt-6-sol",
        )
    )
    assert not result.ok and result.attempted_providers == ("codex",)
    assert len(seen) == 1 and seen[0]["reasoning_effort"] == "high"


def test_dispatch_cli_forwards_effort(monkeypatch, tmp_path):
    seen = []
    skills = tmp_path / ".claude/skills/pavo"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text("role")
    monkeypatch.setattr(dispatch_role, "ATELES_REPO", tmp_path)
    monkeypatch.setenv("APIS_HARNESS_PROVIDERS", "codex")
    monkeypatch.setattr(dispatch_role, "_headroom_note", lambda: "fixture")
    monkeypatch.setattr(dispatch_role, "_install_signal_envelope", lambda emitter: None)
    from lib.daemon_runtime import AgentDefinition

    monkeypatch.setattr(
        dispatch_role,
        "_load_agent_def",
        lambda role: AgentDefinition(
            name=role, prompt_markdown="ROLE", tool_allowlist=[]
        ),
    )

    async def capture(role, task, **kwargs):
        seen.append(kwargs)
        return skill_runner.SkillResult(role, True, 0, "done", "", provider="codex")

    monkeypatch.setattr(dispatch_role, "dispatch", capture)
    assert (
        dispatch_role.main(
            [
                "--role",
                "pavo",
                "--task",
                "TASK",
                "--provider",
                "codex",
                "--reasoning-effort",
                "high",
                "--json",
            ]
        )
        == 0
    )
    assert seen[0]["reasoning_effort"] == "high"


def test_dry_run_reports_same_native_effort_argv(tmp_path):
    sandbox = lens.HarnessSandbox(
        provider="codex",
        root=tmp_path,
        env_extra={"CODEX_HOME": str(tmp_path)},
        credential_read_denied=True,
        credential_binding_protected=True,
        user_config_write_denied=True,
        git_stash_denied=True,
        authentication_ready=True,
        review_write_confined=True,
        command_wrapper=["fixture-guard"],
        unavailable_guards=(),
    )
    target = lens.LensTarget("owner/repository", 1, "a" * 40, "arch", "waxwing")
    report = lens.dry_run_report(
        target,
        provider="codex",
        sandbox=sandbox,
        task_text="TASK",
        worktree_path=tmp_path,
        model="gpt-6-sol",
        reasoning_effort="high",
    )
    argv = report["example_command"]
    assert report["reasoning_effort"] == "high"
    assert argv[argv.index("--config") + 1] == 'model_reasoning_effort="high"'
    assert report["no_model_call_made"]
