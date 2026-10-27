"""Offline effect eval for a standalone QA review dispatch.

The real admission, standalone entrypoint, role dispatcher, and child command
builder run. Only the worktree, guard probe, role fetch, and model process are
synthetic; no provider, GitHub, or credential endpoint is contacted.
"""

from __future__ import annotations

import asyncio
import plistlib
import sys
import time
from pathlib import Path

import pytest

from lib.daemon_runtime import AgentDefinition

REPO = Path(__file__).resolve().parents[3]
for directory in (
    REPO / "execution" / "daemons" / "apis",
    REPO / "execution" / "scripts",
):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

import dispatch_role  # noqa: E402
import harness_lens_runner as lens_runner  # noqa: E402
import harness_router  # noqa: E402
import skill_runner  # noqa: E402

HEAD = "a" * 40
MODEL = "qa-bound-model"
VERDICT = (
    f"<!-- review:qa commit={HEAD} -->\n"
    "**🤖 Phoenicurus — Ateles swarm, qa lens panelist**\n"
    "**SIGNED_OFF**\n\n"
    "Summary: the synthetic review completed.\n"
)


def _run_qa_dispatch(
    tmp_path, monkeypatch, *, historical_zero_gate=False, effects=None
):
    if effects is None:
        effects = {"worktree_creates": 0, "child_calls": []}
    policy = tmp_path / "apis.plist"
    with policy.open("wb") as output:
        plistlib.dump(
            {
                "EnvironmentVariables": {
                    "APIS_HARNESS_PROVIDERS": "codex,cursor",
                    "APIS_USAGE_GATED_PROVIDERS": "claude",
                }
            },
            output,
        )
    monkeypatch.delenv("APIS_HARNESS_PROVIDERS", raising=False)
    monkeypatch.delenv("APIS_USAGE_GATED_PROVIDERS", raising=False)
    monkeypatch.delenv("APIS_HARNESS_HEADROOM", raising=False)
    monkeypatch.setenv("APIS_PROVIDER_POLICY_PLIST", str(policy))
    monkeypatch.setenv("APIS_HARNESS_USAGE_FILE", str(tmp_path / "usage.json"))
    monkeypatch.setenv("APIS_HARNESS_HEADROOM_FILE", str(tmp_path / "headroom.json"))
    action_policy = tmp_path / "action-policy.json"
    action_policy.write_text('{"lens_review:qa": "mid"}', encoding="utf-8")
    binding = tmp_path / "vendor-binding.json"
    binding.write_text('{"codex": {"mid": "qa-bound-model"}}', encoding="utf-8")
    monkeypatch.setenv("APIS_ACTION_POLICY_FILE", str(action_policy))
    monkeypatch.setenv("APIS_VENDOR_BINDING_FILE", str(binding))
    harness_router.reset_state()
    now = time.time()
    harness_router.record_usage(
        "codex",
        [
            {
                "name": "weekly_all",
                "used_percent": 100,
                "resets_at": harness_router._iso_from_wall(now + 6 * 86400),
            }
        ],
        observed_at=now,
    )
    assert harness_router.headroom_resolution(now_wall=now)["codex"][0] == 0.0

    if historical_zero_gate:
        # This reinstates the unconditional entrance that run_one had before
        # resolver admission. At zero headroom it must refuse before any
        # worktree or child process is reached.
        resolve_provider_admission = harness_router.resolve_provider_admission

        def resolve_after_historical_zero_gate(provider, **kwargs):
            lens_runner.check_headroom(provider)
            return resolve_provider_admission(provider, **kwargs)

        monkeypatch.setattr(
            harness_router,
            "resolve_provider_admission",
            resolve_after_historical_zero_gate,
        )

    repo = tmp_path / "repo"
    skill = repo / ".claude" / "skills" / "phoenicurus" / "SKILL.md"
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_text("# synthetic QA role\n", encoding="utf-8")
    monkeypatch.setattr(skill_runner, "ATELES_REPO", repo)
    monkeypatch.setattr(dispatch_role, "ATELES_REPO", repo)
    role = AgentDefinition(
        name="phoenicurus",
        aauth_sub="phoenicurus@ateles-swarm",
        tier="T4",
        prompt_markdown="# synthetic QA role",
        tool_allowlist=["Read"],
    )
    monkeypatch.setattr(lens_runner, "_load_agent_def", lambda *a, **k: role)
    monkeypatch.setattr(skill_runner, "_load_agent_def", lambda *a, **k: role)
    monkeypatch.setattr(skill_runner, "_load_active_policy_prompt", lambda *a, **k: "")
    monkeypatch.setattr(dispatch_role, "_load_agent_def", lambda *a, **k: role)
    monkeypatch.setattr(
        skill_runner,
        "_provider_binaries",
        lambda: {
            "claude": None,
            "codex": "/bin/codex",
            "cursor": None,
        },
    )
    monkeypatch.setattr(skill_runner, "_write_harness_event", lambda **k: None)

    wrapper = tmp_path / "trusted-sandbox-exec"
    wrapper.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    wrapper.chmod(0o755)
    profile = tmp_path / "profile.sb"
    profile.write_text("synthetic profile\n", encoding="utf-8")
    monkeypatch.setattr(lens_runner, "TRUSTED_MACOS_SANDBOX_EXEC", wrapper)
    monkeypatch.setattr(skill_runner, "TRUSTED_MACOS_SANDBOX_EXEC", wrapper)
    sandbox = lens_runner.HarnessSandbox(
        provider="codex",
        root=tmp_path,
        env_extra={
            "CODEX_HOME": str(tmp_path),
            "ATELES_LOCAL_REVIEW_HOME": str(tmp_path),
        },
        command_wrapper=[str(wrapper), "-f", str(profile)],
        credential_read_denied=True,
        credential_binding_protected=True,
        user_config_write_denied=True,
        git_stash_denied=True,
        review_write_confined=True,
        authentication_ready=True,
        unavailable_guards=(),
    )
    monkeypatch.setattr(
        lens_runner.HarnessSandbox, "build", classmethod(lambda cls, *a, **k: sandbox)
    )

    def create_worktree(self, *, head):
        assert head == HEAD
        effects["worktree_creates"] += 1
        self._created = True
        self.path.mkdir(parents=True)

    monkeypatch.setattr(lens_runner.Worktree, "create", create_worktree)
    monkeypatch.setattr(lens_runner.Worktree, "remove", lambda self: None)
    monkeypatch.setattr(
        lens_runner,
        "capture_stash_ref_state",
        lambda path: lens_runner.StashRefState(resolved_oid=None, packed_oid=None),
    )
    monkeypatch.setattr(
        lens_runner, "verify_stash_ref_unchanged_after_dispatch", lambda *a, **k: None
    )
    monkeypatch.setattr(lens_runner, "current_pr_head", lambda **k: HEAD)
    child_calls = []

    async def child(*argv, **kwargs):
        child_calls.append(list(argv))
        effects["child_calls"].append(list(argv))

        class Process:
            returncode = 0

            async def communicate(self, input=None):
                return VERDICT.encode(), b""

        return Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", child)
    brief = tmp_path / "brief.md"
    brief.write_text("Review the synthetic head.\n", encoding="utf-8")
    target = lens_runner.LensTarget(
        repo="example/ateles", pr=42, head=HEAD, lens="qa", agent="phoenicurus"
    )
    try:
        report = asyncio.run(
            lens_runner.run_one(
                target,
                provider="codex",
                post=False,
                dry_run=False,
                repo_worktree_name="ateles",
                scratch_root=tmp_path,
                brief_path=brief,
                timeout=None,
            )
        )
        return report, child_calls, wrapper
    finally:
        harness_router.reset_state()


def _judge(report, child_calls, wrapper):
    if not report.get("ok") or report.get("lens_verdict") != "signed_off":
        return False
    if report.get("delivery_status") != "not_requested" or report.get("posted"):
        return False
    if len(child_calls) != 1:
        return False
    command = child_calls[0]
    return (
        command[:3] == [str(wrapper), "-f", str(wrapper.parent / "profile.sb")]
        and "--model" in command
        and command[command.index("--model") + 1] == MODEL
    )


def test_standalone_qa_dispatch_produces_a_validated_review_at_zero_usage_headroom(
    tmp_path, monkeypatch
):
    historical_effects = {"worktree_creates": 0, "child_calls": []}
    # The temporary historical entrance is the counterfactual RED: the same
    # zero-headroom fixture refuses before setup reaches either side effect.
    with monkeypatch.context() as historical_patch:
        with pytest.raises(
            lens_runner.HeadroomExhausted,
            match=r"provider 'codex' has headroom=0",
        ):
            _run_qa_dispatch(
                tmp_path,
                historical_patch,
                historical_zero_gate=True,
                effects=historical_effects,
            )
    assert historical_effects == {"worktree_creates": 0, "child_calls": []}

    # The context restores the current resolver path. With the same semantic
    # fixture, explicitly ungated codex reaches the bound child and validated
    # QA verdict instead of the historical zero-headroom refusal.
    report, child_calls, wrapper = _run_qa_dispatch(tmp_path, monkeypatch)
    assert _judge(report, child_calls, wrapper), report
