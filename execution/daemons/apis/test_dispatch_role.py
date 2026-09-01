"""Tests for the named-role dispatch entrypoint (dispatch_role).

The entrypoint is deliberately thin — routing, identity, and audit all live in
skill_runner/harness_router and are covered by their own suites. What is tested
here is the surface this module actually owns: role resolution against the
SKILL.md set, refusal before any dispatch is attempted, forwarding of the
provider override, and the headroom-source reporting that makes the
file-beats-env precedence visible.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_DAEMON_DIR = Path(__file__).resolve().parent
if str(_DAEMON_DIR) not in sys.path:
    sys.path.insert(0, str(_DAEMON_DIR))

import dispatch_role  # noqa: E402
from skill_runner import SkillResult  # noqa: E402


@pytest.fixture
def fake_repo(tmp_path, monkeypatch):
    """An ATELES_REPO whose .claude/skills holds two roles."""
    skills = tmp_path / ".claude" / "skills"
    for role in ("cicada", "pavo"):
        (skills / role).mkdir(parents=True)
        (skills / role / "SKILL.md").write_text(f"# {role}\n", encoding="utf-8")
    # A directory without a SKILL.md must NOT count as dispatchable.
    (skills / "not-a-role").mkdir(parents=True)
    monkeypatch.setattr(dispatch_role, "ATELES_REPO", tmp_path)
    return tmp_path


def test_available_roles_requires_a_skill_md(fake_repo) -> None:
    assert dispatch_role.available_roles() == ["cicada", "pavo"]


def test_preflight_accepts_a_known_role(fake_repo) -> None:
    assert dispatch_role._preflight("cicada", provider=None) is None


def test_preflight_refuses_an_unknown_role(fake_repo) -> None:
    refusal = dispatch_role._preflight("nosuchrole", provider=None)
    assert refusal is not None
    # The refusal must name the valid options, not just say "no".
    assert "cicada" in refusal and "pavo" in refusal


def test_preflight_refuses_a_provider_outside_the_configured_order(
    fake_repo, monkeypatch
) -> None:
    monkeypatch.setenv("APIS_HARNESS_PROVIDERS", "claude,cursor")
    refusal = dispatch_role._preflight("cicada", provider="codex")
    assert refusal is not None and "codex" in refusal


def test_preflight_allows_a_provider_inside_the_configured_order(
    fake_repo, monkeypatch
) -> None:
    monkeypatch.setenv("APIS_HARNESS_PROVIDERS", "claude,codex,cursor")
    assert dispatch_role._preflight("cicada", provider="codex") is None


def test_unknown_role_exits_nonzero_without_dispatching(
    fake_repo, monkeypatch
) -> None:
    """A refused role must never reach run_skill — a rejected request should
    leave no harness_event suggesting work was attempted."""
    called = False

    async def _boom(*a, **k):  # pragma: no cover - must not run
        nonlocal called
        called = True
        raise AssertionError("run_skill called for a refused role")

    monkeypatch.setattr(dispatch_role, "run_skill", _boom)
    rc = dispatch_role.main(["--role", "nosuchrole", "--task", "hi"])
    assert rc == 1
    assert called is False


def test_dispatch_forwards_role_provider_and_cwd(monkeypatch) -> None:
    """The role name must be passed as BOTH skill and role: skill_runner reads
    <skill>/SKILL.md and loads the <role> agent_definition, and in this codebase
    those are the same string."""
    seen: dict = {}

    async def _capture(skill, prompt, **kwargs):
        seen["skill"] = skill
        seen["prompt"] = prompt
        seen.update(kwargs)
        return SkillResult(skill, True, 0, "out", "", provider="codex")

    monkeypatch.setattr(dispatch_role, "run_skill", _capture)

    import asyncio

    result = asyncio.run(
        dispatch_role.dispatch(
            "cicada", "do the thing", provider="codex", cwd="/tmp/wt", timeout=42
        )
    )
    assert result.ok
    assert seen["skill"] == "cicada"
    assert seen["role"] == "cicada"
    assert seen["provider"] == "codex"
    assert seen["cwd"] == "/tmp/wt"
    assert seen["timeout"] == 42
    assert seen["prompt"] == "do the thing"


def test_dispatch_without_override_leaves_provider_to_the_router(
    monkeypatch,
) -> None:
    seen: dict = {}

    async def _capture(skill, prompt, **kwargs):
        seen.update(kwargs)
        return SkillResult(skill, True, 0, "", "", provider="cursor")

    monkeypatch.setattr(dispatch_role, "run_skill", _capture)

    import asyncio

    asyncio.run(dispatch_role.dispatch("cicada", "work"))
    # None, not a default string: run_skill treats None as "route normally".
    assert seen["provider"] is None


def test_failed_run_exits_nonzero(fake_repo, monkeypatch) -> None:
    async def _fail(skill, prompt, **kwargs):
        return SkillResult(
            skill, False, 1, "", "boom", error="provider exploded", provider="codex"
        )

    monkeypatch.setattr(dispatch_role, "run_skill", _fail)
    monkeypatch.setattr(dispatch_role, "_load_agent_def", lambda r: _stub_def())
    rc = dispatch_role.main(
        ["--role", "cicada", "--task", "x", "--provider", "codex"]
    )
    assert rc == 1


def test_successful_run_exits_zero(fake_repo, monkeypatch) -> None:
    async def _ok(skill, prompt, **kwargs):
        return SkillResult(skill, True, 0, "branch-name", "", provider="codex")

    monkeypatch.setattr(dispatch_role, "run_skill", _ok)
    monkeypatch.setattr(dispatch_role, "_load_agent_def", lambda r: _stub_def())
    rc = dispatch_role.main(
        ["--role", "cicada", "--task", "x", "--provider", "codex"]
    )
    assert rc == 0


def _stub_def():
    from lib.daemon_runtime import AgentDefinition

    return AgentDefinition(
        name="cicada",
        aauth_sub="cicada@ateles-swarm",
        tier="T4",
        prompt_markdown="# cicada",
        tool_allowlist=["Bash", "Read"],
    )


def test_headroom_note_names_the_file_when_one_exists(tmp_path, monkeypatch) -> None:
    """configured_headroom() takes the FIRST of (file, env) that parses, so an
    env value does NOT override an existing file. The note must therefore say
    which source actually won, or a stale file stays silently authoritative."""
    hf = tmp_path / "headroom.json"
    hf.write_text('{"claude": 0.15, "codex": 1.0, "cursor": 1.0}', encoding="utf-8")
    monkeypatch.setenv("APIS_HARNESS_HEADROOM_FILE", str(hf))
    monkeypatch.setenv("APIS_HARNESS_HEADROOM", '{"claude": 1.0}')
    note = dispatch_role._headroom_note()
    assert str(hf) in note
    # The file's values win, not the env's.
    assert "claude=0.15" in note


def test_headroom_note_falls_back_to_env_when_no_file(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(
        "APIS_HARNESS_HEADROOM_FILE", str(tmp_path / "absent.json")
    )
    monkeypatch.setenv("APIS_HARNESS_HEADROOM", '{"claude": 0.2}')
    note = dispatch_role._headroom_note()
    assert "env APIS_HARNESS_HEADROOM" in note
    assert "claude=0.2" in note


def test_inconsistent_tool_allowlist_types_all_coerce(monkeypatch) -> None:
    """tool_allowlist is inconsistently typed across agent_definition entities
    (arrays, one comma/JSON string, some nulls). Every shape must yield a usable
    list — a null must mean 'all tools', never a crash or an empty allowlist
    that would confine the agent to nothing."""
    from lib.daemon_runtime import AgentDefinition

    assert AgentDefinition(tool_allowlist=None).tools == ["*"]
    assert AgentDefinition(tool_allowlist="*").tools == ["*"]
    assert AgentDefinition(tool_allowlist="").tools == ["*"]
    assert AgentDefinition(tool_allowlist=["Bash", "Read"]).tools == ["Bash", "Read"]
    assert AgentDefinition(tool_allowlist='["Bash", "Read"]').tools == [
        "Bash",
        "Read",
    ]
    assert AgentDefinition(tool_allowlist="Bash,Read").tools == ["Bash", "Read"]
