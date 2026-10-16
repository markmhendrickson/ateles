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


@pytest.mark.parametrize("outcome", ["timeout", "malformed"])
def test_guarded_cli_requires_available_canonical_definition(monkeypatch, tmp_path, capsys, outcome):
    monkeypatch.setattr(runner, "check_headroom", lambda provider: None)
    monkeypatch.setattr(runner.Worktree, "create", lambda self, **kw: self.path.mkdir(parents=True))
    monkeypatch.setattr(runner.Worktree, "remove", lambda self: None)
    monkeypatch.setattr(runner, "refuse_if_credential_files_in_worktree", lambda path: None)
    monkeypatch.setattr(runner.HarnessSandbox, "build", lambda *a, **k: SimpleNamespace(
        env_extra={"ATELES_LOCAL_REVIEW_HOME": str(tmp_path), "CODEX_HOME": str(tmp_path)},
        command_wrapper=[], codex_outer_sandbox_probed=False))
    monkeypatch.setattr(runner, "refuse_if_guard_required", lambda sandbox: None)
    monkeypatch.setattr(runner, "read_agent_prompt", lambda *a: "Available prefetched role brief")
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
    requests, spawns = [], []

    def post(*a, **k):
        requests.append(True)
        if outcome == "timeout":
            raise httpx.ReadTimeout("synthetic")
        payload = {"error": "synthetic"} if outcome == "malformed" else {"entities": []}
        return httpx.Response(200, json=payload, request=httpx.Request("POST", "https://fixture.invalid"))

    async def spawn(*a, **k):
        spawns.append(a)
        raise AssertionError("an unavailable canonical role reached a provider")

    monkeypatch.setattr(loader.httpx, "post", post)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    brief = tmp_path / "brief.md"
    brief.write_text("Review a synthetic change.\n")
    runner.main(["--repo", "example/project", "--pr", "1", "--head", "a" * 40,
                 "--lens", "pm", "--provider", "codex", "--brief", str(brief), "--json"])
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is False and report["posted"] is False
    assert requests, report
    assert "agent_definition unavailable:" in report["reason"], report
    assert not spawns
    assert "pavo" not in skill_runner._agent_def_cache


def test_guarded_cli_eval_detects_a_reinstated_failed_load_stub(monkeypatch, tmp_path, capsys):
    real_load = loader.AgentLoader.load

    def old_fallback(self, **kwargs):
        try:
            return real_load(self, **kwargs)
        except loader.AgentDefinitionUnavailable as exc:
            return self._undefined_definition(exc.code)

    monkeypatch.setattr(loader.AgentLoader, "load", old_fallback)
    with pytest.raises(AssertionError):
        test_guarded_cli_requires_available_canonical_definition(
            monkeypatch, tmp_path, capsys, "timeout")
