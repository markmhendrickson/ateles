"""Synthetic canonical-role admission through the real sibling review path."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
for entry in (ROOT, ROOT / "execution" / "daemons" / "apis", Path(__file__).parent):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

import harness_lens_runner as runner
from lib.daemon_runtime.agent_loader import AgentDefinition, AgentLoader


def definition() -> AgentDefinition:
    return AgentDefinition(
        entity_id="ent_synthetic_role",
        name="pavo",
        prompt_markdown="Canonical synthetic role",
        status="active",
        tool_allowlist=["Read"],
        raw={"snapshot": {"name": "pavo", "status": "active",
                          "prompt_markdown": "Canonical synthetic role",
                          "tool_allowlist": ["Read"]}},
    )


def test_same_runner_shape_uses_canonical_prompt_for_ateles_and_sibling(tmp_path, monkeypatch):
    """Real run_one; inert guard/report boundaries never launch a provider."""
    role = definition()
    monkeypatch.setattr(AgentLoader, "load_for_lens", lambda self: role, raising=False)
    monkeypatch.setattr(runner, "check_headroom", lambda provider: None)
    monkeypatch.setattr(runner.Worktree, "create", lambda wt, **kw: wt.path.mkdir(parents=True))
    monkeypatch.setattr(runner.Worktree, "remove", lambda wt: None)
    monkeypatch.setattr(runner.HarnessSandbox, "build", lambda *a, **kw: SimpleNamespace())
    monkeypatch.setattr(runner, "render_lens_task", lambda target, brief: target.repo)
    seen = []
    def report(*a, task_text, **kw):
        seen.append(task_text)
        return {"ok": True}
    monkeypatch.setattr(runner, "dry_run_report", report)
    for repo in ("example/ateles", "example/neotoma"):
        target = runner.LensTarget(repo=repo, pr=1, head="a" * 40, lens="pm", agent="pavo")
        result = asyncio.run(runner.run_one(
            target, provider="codex", post=False, dry_run=True,
            repo_worktree_name=repo.split("/")[-1], scratch_root=tmp_path,
            brief_path=tmp_path / "brief", timeout=900))
        assert result["ok"] is True
    assert len(seen) == 2
    assert all(role.prompt_markdown in task for task in seen)
