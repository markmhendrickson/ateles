"""
Regression tests for the two Anthus dispatch bugs fixed in ateles#172:

1. handle_event must hydrate the snapshot for issue/pull_request events before
   routing to orchestration — the SSE stream carries only metadata, so without
   hydration _project_from_repo sees "" and every event is silently dropped.

2. _spawn_agent must read the GitHub issue number from `github_number` (the
   field Neotoma `issue` entities actually store), not only `number` /
   `issue_number`, or the spawned agent gets `#` and cannot locate the issue.

Run with: pytest execution/daemons/anthus/test_dispatch_wiring.py -v
"""

from __future__ import annotations

import asyncio

import anthus


# ── Bug 1: issue/PR events are hydrated and reach orchestration ──────────────


def test_issue_event_is_hydrated_then_orchestrated(monkeypatch):
    """An issue event with an empty snapshot must be hydrated and forwarded to
    _orchestrate_workflow_for — not dropped."""
    hydrated: list[str] = []
    orchestrated: list[str] = []

    async def fake_hydrate(event):
        # Simulate the entity fetch populating the snapshot.
        event.snapshot = {"repo": "markmhendrickson/swarm-smoke", "github_number": 31}
        hydrated.append(event.entity_id)
        return event

    async def fake_orchestrate(event):
        orchestrated.append(event.entity_id)

    monkeypatch.setattr(anthus, "hydrate_snapshot", fake_hydrate)
    monkeypatch.setattr(anthus, "_orchestrate_workflow_for", fake_orchestrate)

    ev = anthus.NeotomaEvent(
        entity_type="issue", entity_id="ent_issue31", action="created", snapshot={}
    )
    asyncio.run(anthus.handle_event(ev))

    assert hydrated == ["ent_issue31"], "issue event must be hydrated"
    assert orchestrated == ["ent_issue31"], "hydrated issue event must reach orchestration"


def test_pull_request_event_is_hydrated_then_orchestrated(monkeypatch):
    hydrated: list[str] = []
    orchestrated: list[str] = []

    async def fake_hydrate(event):
        event.snapshot = {"repo": "markmhendrickson/ateles"}
        hydrated.append(event.entity_id)
        return event

    async def fake_orchestrate(event):
        orchestrated.append(event.entity_id)

    monkeypatch.setattr(anthus, "hydrate_snapshot", fake_hydrate)
    monkeypatch.setattr(anthus, "_orchestrate_workflow_for", fake_orchestrate)

    ev = anthus.NeotomaEvent(
        entity_type="pull_request", entity_id="ent_pr7", action="updated", snapshot={}
    )
    asyncio.run(anthus.handle_event(ev))

    assert hydrated == ["ent_pr7"]
    assert orchestrated == ["ent_pr7"]


def test_non_workflow_events_are_not_hydrated(monkeypatch):
    """Escalation/daemon_report/etc. must NOT trigger the issue/PR hydrate path."""
    hydrated: list[str] = []

    async def fake_hydrate(event):
        hydrated.append(event.entity_id)
        return event

    async def noop(event):
        return None

    monkeypatch.setattr(anthus, "hydrate_snapshot", fake_hydrate)
    monkeypatch.setattr(anthus, "_handle_daemon_report", noop)

    ev = anthus.NeotomaEvent(
        entity_type="daemon_report", entity_id="ent_dr", action="created", snapshot={}
    )
    asyncio.run(anthus.handle_event(ev))

    assert hydrated == [], "daemon_report must not go through the issue/PR hydrate path"


# ── Bug 2: _spawn_agent resolves the GitHub number from github_number ────────


def _capture_spawn_number(monkeypatch, snapshot: dict) -> str:
    """Drive _spawn_agent far enough to capture the issue number it derives,
    stubbing out the actual subprocess launch and SKILL.md read."""
    captured: dict = {}

    # Stub the claude binary discovery and SKILL.md read so we reach the
    # prompt-construction step deterministically.
    import shutil

    monkeypatch.setattr(shutil, "which", lambda _bin: "/usr/bin/true")

    class _FakeSkillPath:
        def exists(self):
            return True

        def read_text(self, encoding="utf-8"):
            return "SKILL"

    # Patch Path so the SKILL.md lookup resolves to our fake.
    import anthus as _a

    monkeypatch.setattr(_a, "AgentLoader", _FakeLoader)

    async def fake_exec(*args, **kwargs):
        # The prompt is the last positional arg; extract the "#<number>" token.
        prompt = args[-1]
        captured["prompt"] = prompt
        return _FakeProc()

    monkeypatch.setattr(anthus.asyncio, "create_subprocess_exec", fake_exec)

    # Path(...).exists()/read_text() for the SKILL.md — patch pathlib.Path used
    # inside _spawn_agent via a module-level shim.
    import pathlib

    real_path = pathlib.Path

    def _path_shim(*a, **k):
        p = real_path(*a, **k)
        return p

    monkeypatch.setattr(pathlib, "Path", _path_shim)

    asyncio.run(
        anthus._spawn_agent(
            owner_agent="pavo",
            work_entity_id="ent_issue31",
            gate_name="pm",
            snapshot=snapshot,
        )
    )
    return captured.get("prompt", "")


class _FakeLoader:
    def __init__(self, name):
        self.name = name

    def load(self):
        class _D:
            entity_id = "ent_def"
            last_observation_id = "obs"

        return _D()

    def render_policy_prompt(self):
        return ""


class _FakeProc:
    pid = 999


def test_spawn_agent_reads_github_number(monkeypatch, tmp_path):
    """github_number (the canonical issue-entity field) must appear in the
    prompt as the issue number."""
    # Point ATELES_REPO_ROOT at a tree with a real pavo SKILL.md so the read
    # succeeds; fall back to skip if the skill is absent in this checkout.
    import os
    from pathlib import Path

    skill = Path(anthus._REPO_ROOT) / ".claude" / "skills" / "pavo" / "SKILL.md"
    if not skill.exists():
        import pytest

        pytest.skip("pavo SKILL.md not present in this checkout")
    os.environ["ATELES_REPO_ROOT"] = str(anthus._REPO_ROOT)

    prompt = _capture_spawn_number(
        monkeypatch,
        {"repo": "markmhendrickson/swarm-smoke", "title": "T", "github_number": 31},
    )
    assert "#31" in prompt, f"expected issue #31 in prompt, got: {prompt[:200]}"


def test_spawn_agent_number_fallback_order(monkeypatch):
    """When github_number is absent, fall back to number then issue_number."""
    from pathlib import Path

    skill = Path(anthus._REPO_ROOT) / ".claude" / "skills" / "pavo" / "SKILL.md"
    if not skill.exists():
        import pytest

        pytest.skip("pavo SKILL.md not present in this checkout")
    import os

    os.environ["ATELES_REPO_ROOT"] = str(anthus._REPO_ROOT)

    prompt = _capture_spawn_number(
        monkeypatch,
        {"repo": "markmhendrickson/swarm-smoke", "title": "T", "number": 42},
    )
    assert "#42" in prompt


# ── tool_allowlist call-site effect (ateles#696 review round 1, pavo lens) ────
#
# Anthus loads the agent_definition to pin agent_definition_ref for
# provenance but, before this PR, never read `.tools` at all — every gate
# dispatch ran with whatever the ambient CLI allowed. These tests pin the
# actual argv _spawn_agent produces, not just that plan_enforcement() returns
# the right ToolPlan in isolation (that is covered by
# lib/daemon_runtime/test_tool_allowlist.py already).


class _FakeToolLoader:
    """AgentLoader stand-in whose .load().tools is a restricted allowlist —
    distinct from _FakeLoader above, which never sets .tools at all."""

    def __init__(self, name):
        self.name = name

    def load(self):
        class _D:
            entity_id = "ent_def"
            last_observation_id = "obs"
            tools = ["Bash", "Read", "Write"]

        return _D()

    def render_policy_prompt(self):
        return ""


def _capture_spawn_argv(monkeypatch, *, owner_agent: str, enforce: bool) -> list[str]:
    """Drive _spawn_agent far enough to capture the full argv passed to
    create_subprocess_exec, with a real (restricted) tool_allowlist."""
    import shutil
    from pathlib import Path

    monkeypatch.setattr(shutil, "which", lambda _bin: "/usr/bin/true")
    monkeypatch.setattr(anthus, "AgentLoader", _FakeToolLoader)
    if enforce:
        monkeypatch.setenv("ATELES_ENFORCE_TOOL_ALLOWLIST", "1")
    else:
        monkeypatch.delenv("ATELES_ENFORCE_TOOL_ALLOWLIST", raising=False)

    captured: dict = {}

    async def fake_exec(*args, **kwargs):
        captured["cmd"] = list(args)
        return _FakeProc()

    monkeypatch.setattr(anthus.asyncio, "create_subprocess_exec", fake_exec)

    skill = Path(anthus._REPO_ROOT) / ".claude" / "skills" / owner_agent / "SKILL.md"
    if not skill.exists():
        import pytest

        pytest.skip(f"{owner_agent} SKILL.md not present in this checkout")
    import os

    os.environ["ATELES_REPO_ROOT"] = str(anthus._REPO_ROOT)

    asyncio.run(
        anthus._spawn_agent(
            owner_agent=owner_agent,
            work_entity_id="ent_issue31",
            gate_name="pm",
            snapshot={"repo": "markmhendrickson/swarm-smoke", "title": "T", "github_number": 31},
        )
    )
    return captured.get("cmd", [])


def test_spawn_agent_binds_allowlist_for_no_bypass_agent_under_enforce(monkeypatch):
    """pavo never gets --dangerously-skip-permissions (not in
    _AGENTS_NEEDING_SKIP_PERMISSIONS), so under ENFORCE its restricted
    allowlist must actually reach argv as --allowed-tools."""
    cmd = _capture_spawn_argv(monkeypatch, owner_agent="pavo", enforce=True)
    assert "--allowed-tools" in cmd, f"expected --allowed-tools in argv, got: {cmd}"
    assert "--dangerously-skip-permissions" not in cmd


def test_spawn_agent_defeated_for_bypass_agent_even_under_enforce(monkeypatch):
    """cicada IS in _AGENTS_NEEDING_SKIP_PERMISSIONS, so
    --dangerously-skip-permissions defeats the allowlist at the CLI's
    permission-flow stage 4 — --allowed-tools must NOT appear even under
    ENFORCE, and the skip-permissions flag must still be present."""
    cmd = _capture_spawn_argv(monkeypatch, owner_agent="cicada", enforce=True)
    assert "--allowed-tools" not in cmd, (
        f"expected no --allowed-tools under bypass (defeated), got: {cmd}"
    )
    assert "--dangerously-skip-permissions" in cmd
