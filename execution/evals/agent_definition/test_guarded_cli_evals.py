"""Offline agent-invoked guarded CLI evidence using the actual canonical loader.

Only workspace/OS guard setup and transport/process edges are synthetic. The
brief is deliberately available; it cannot substitute for the canonical role.
No provider, key, public write or network service is used.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[3]
for path in (ROOT, ROOT / "execution/scripts", ROOT / "execution/daemons/apis"):
    sys.path.insert(0, str(path))

import harness_lens_runner as runner  # noqa: E402
import skill_runner  # noqa: E402
from lib.daemon_runtime import agent_loader as loader  # noqa: E402


@pytest.mark.parametrize("outcome", [
    "timeout", "malformed", "none", "missing_prompt", "missing_tools",
    "missing_status", "wrong_role", "disabled", "changed_second_read", "real",
])
def test_guarded_cli_requires_available_canonical_definition(monkeypatch, tmp_path, capsys, outcome):
    # Both modules hold import-time path values. Use this fixture checkout,
    # never an optional developer checkout under the test process home.
    monkeypatch.setattr(skill_runner, "ATELES_REPO", ROOT)
    monkeypatch.setattr(runner.dispatch_role, "ATELES_REPO", ROOT)
    monkeypatch.setenv("ATELES_REPO_PATH", str(ROOT))
    monkeypatch.setattr(runner, "check_headroom", lambda provider: None)
    # CI readiness is an external input, independent of the role-loading
    # behavior this offline eval judges. Keep the real seating gate and supply
    # a known-green head rather than reading GitHub or the host's credentials.
    monkeypatch.setattr(runner, "current_pr_base_ref", lambda **kw: "main")

    async def green_checks(repository, head_sha, base_ref):
        assert repository == "example/project" and head_sha == "a" * 40
        return runner.panel_check_gate.CheckGate(head=head_sha, green=True)

    monkeypatch.setattr(runner.panel_check_gate, "read_check_gate", green_checks)
    monkeypatch.setattr(runner.Worktree, "create", lambda self, **kw: self.path.mkdir(parents=True))
    monkeypatch.setattr(runner.Worktree, "remove", lambda self: None)
    monkeypatch.setattr(runner, "refuse_if_credential_files_in_worktree", lambda path: None)
    monkeypatch.setattr(runner.HarnessSandbox, "build", lambda *a, **k: SimpleNamespace(
        env_extra={"ATELES_LOCAL_REVIEW_HOME": str(tmp_path), "CODEX_HOME": str(tmp_path)},
        command_wrapper=[], codex_outer_sandbox_probed=False))
    monkeypatch.setattr(runner, "refuse_if_guard_required", lambda sandbox: None)
    monkeypatch.setattr(runner, "capture_stash_ref_state", lambda path: None)
    monkeypatch.setattr(runner, "verify_stash_ref_unchanged_after_dispatch", lambda *a: None)
    monkeypatch.setattr(loader.ns, "via_cli_enabled", lambda: False)
    monkeypatch.setattr(loader, "_sleep", lambda seconds: None, raising=False)
    monkeypatch.setattr(skill_runner, "_provider_binaries", lambda: {"codex": "/usr/bin/true"})
    monkeypatch.setenv("APIS_HARNESS_PROVIDERS", "codex,cursor")
    monkeypatch.setenv("APIS_USAGE_GATED_PROVIDERS", "claude")
    monkeypatch.setenv("APIS_HARNESS_HEADROOM", '{"codex":1.0,"cursor":1.0}')
    monkeypatch.setenv("APIS_HARNESS_HEADROOM_FILE", str(tmp_path / "headroom.json"))
    monkeypatch.setenv("APIS_HARNESS_USAGE_FILE", str(tmp_path / "usage.json"))
    monkeypatch.setenv("APIS_USAGE_PROBE_ON_DISPATCH", "0")
    skill_runner._agent_def_cache.clear()
    monkeypatch.setattr(skill_runner, "_write_harness_event", lambda **k: None)
    monkeypatch.setattr(skill_runner.model_tiering, "record_dispatch", lambda **k: "synthetic")
    monkeypatch.setattr(skill_runner.model_tiering, "record_dispatch_usage", lambda **k: None)
    requests, policy_reads, spawns, inputs = [], [], [], []

    def post(*a, **k):
        if k.get("json", {}).get("entity_type") == "agent_policy":
            policy_reads.append(True)
            return httpx.Response(200, json={"entities": [], "total": 0},
                                  request=httpx.Request("POST", "https://fixture.invalid"))
        requests.append(True)
        if outcome == "timeout":
            raise httpx.ReadTimeout("synthetic")
        payload = {"error": "synthetic"} if outcome == "malformed" else {"entities": []}
        if outcome in {"real", "wrong_role", "disabled", "changed_second_read"} or outcome.startswith("missing_"):
            snap = {"name": "pavo", "status": "active", "prompt_markdown": "Canonical role", "tool_allowlist": ["Read"]}
            if outcome.startswith("missing_"):
                snap.pop({"missing_prompt": "prompt_markdown", "missing_tools": "tool_allowlist", "missing_status": "status"}[outcome])
            elif outcome == "wrong_role":
                snap["name"] = "other_role"
            elif outcome == "disabled":
                snap["status"] = "disabled"
            elif outcome == "changed_second_read" and len(requests) == 2:
                snap["prompt_markdown"] = "Changed canonical role"
            payload = {"entities": [{"entity_id": "ent_synthetic", "snapshot": snap}]}
        return httpx.Response(200, json=payload, request=httpx.Request("POST", "https://fixture.invalid"))

    async def spawn(*a, **k):
        spawns.append(a)
        if outcome != "real":
            raise AssertionError("an unavailable canonical role reached a provider")
        async def communicate(input=None):
            inputs.append(input)
            return b"Synthetic authorized child result", b""
        return SimpleNamespace(returncode=0, communicate=communicate)

    monkeypatch.setattr(loader.httpx, "post", post)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    brief = tmp_path / "brief.md"
    brief.write_text("Review a synthetic change.\n")
    runner.main(["--repo", "example/project", "--pr", "1", "--head", "a" * 40,
                 "--lens", "pm", "--provider", "codex", "--brief", str(brief), "--json"])
    report = json.loads(capsys.readouterr().out)
    if outcome == "real":
        assert spawns, report
        assert inputs and inputs[0].startswith(b"Canonical role")
        assert len(requests) == 2  # wrapper and child independently read the role
        assert len(policy_reads) == 1
        assert report["role_entity_id"] == "ent_synthetic"
        assert len(report["role_fingerprint"]) == 64
        assert "pavo" not in skill_runner._agent_def_cache
        assert report["posted"] is False
        return
    assert report["ok"] is False and report["posted"] is False
    assert requests, report
    assert not policy_reads
    assert len(requests) == (3 if outcome == "timeout" else 2 if outcome == "changed_second_read" else 1)
    if outcome == "changed_second_read":
        assert "agent_definition unavailable:" in report["reason"], report
    else:
        assert "canonical reviewer role 'pavo' is unavailable:" in report["reason"], report
    assert "agent_definition for 'pavo' is unavailable" in report["reason"], report
    assert not spawns
    assert "pavo" not in skill_runner._agent_def_cache


def test_guarded_cli_refuses_a_reinstated_failed_load_stub(monkeypatch, tmp_path, capsys):
    real_load = loader.AgentLoader.load

    def old_fallback(self, **kwargs):
        try:
            return real_load(self, **kwargs)
        except loader.AgentDefinitionUnavailable as exc:
            return self._undefined_definition(exc.code)

    monkeypatch.setattr(loader.AgentLoader, "load", old_fallback)
    test_guarded_cli_requires_available_canonical_definition(
        monkeypatch, tmp_path, capsys, "timeout")


@pytest.mark.parametrize("outcome", ["real", "none", "missing_tools"])
def test_guarded_cli_uses_checkout_without_ambient_skills(monkeypatch, tmp_path, capsys, outcome):
    """An imported home-checkout default must not decide this offline eval."""
    absent = tmp_path / "absent-ambient-checkout"
    assert not absent.exists()
    monkeypatch.setenv("ATELES_REPO_PATH", str(absent))
    monkeypatch.setattr(skill_runner, "ATELES_REPO", absent)
    # dispatch_role imports the path by value, separately from skill_runner.
    monkeypatch.setattr(runner.dispatch_role, "ATELES_REPO", absent)
    test_guarded_cli_requires_available_canonical_definition(
        monkeypatch, tmp_path, capsys, outcome,
    )
