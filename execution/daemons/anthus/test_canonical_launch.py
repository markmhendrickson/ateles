"""Exercise the real launch command with a hermetic, non-inference CLI.

The fake implements the documented CLI permission contract, not Claude itself.
It catches missing/bypassed command controls; vendor enforcement remains an
independent review concern. No provider, network, token or model is used.
"""
from __future__ import annotations

import asyncio
import json
import sys

import anthus
import pytest


@pytest.mark.parametrize(("role", "tools", "requested", "expected"), [
    ("pavo", ["Read"], "Read", True),
    ("pavo", ["Read"], "Write", False),
    ("cicada", ["Read"], "Bash", False),
    ("pavo", ["Bash"], "Read", False),
    ("pavo", ["Read"], "mcp__fixture__write", False),
    ("pavo", ["mcp__fixture__read"], "Read", False),
    ("pavo", ["mcp__fixture__read"], "mcp__fixture__read", True),
    ("cicada", ["*"], "Write", True),
])
def test_actual_cli_launch_binds_canonical_capabilities(
    monkeypatch, tmp_path, role, tools, requested, expected,
):
    receipt = tmp_path / "receipt.json"
    cli = tmp_path / "claude"
    cli.write_text(f"#!{sys.executable}\n" + r'''
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
def value(flag, default=None):
    return args[args.index(flag)+1] if flag in args else default
# CLI built-ins include intrinsically available reads even in dontAsk mode.
# --tools bounds that surface separately from approval rules.
builtins = value('--tools', 'default')
available = {'Read', 'Write', 'Bash'} if builtins == 'default' else set(builtins.split(','))
allows = set(value('--allowed-tools', '').split(','))
mode = value('--permission-mode', 'default')
bypass = '--dangerously-skip-permissions' in args or mode == 'bypassPermissions'
# Simulate a saved ambient write allow rule, unless settings sources are disabled.
ambient = value('--setting-sources', 'user,project,local') != ''
requested = os.environ['FIXTURE_REQUESTED_TOOL']
visible = requested.startswith('mcp__') or requested in available
approved = requested == 'Read' or requested in allows or '*' in allows or ambient
allowed = bypass or (visible and approved)
Path(os.environ['FIXTURE_RECEIPT']).write_text(json.dumps({
    'allowed': allowed,
    'canonical': 'Canonical launch authority' in value('--append-system-prompt', ''),
    'mode': mode,
}))
''')
    cli.chmod(0o700)
    monkeypatch.setenv("FIXTURE_RECEIPT", str(receipt))
    monkeypatch.setenv("FIXTURE_REQUESTED_TOOL", requested)
    # No local SKILL.md exists: only the canonical source authorizes launch.
    monkeypatch.setenv("ATELES_REPO_ROOT", str(tmp_path))
    monkeypatch.setattr("shutil.which", lambda _: str(cli))
    definition = anthus.AgentDefinition(
        entity_id="ent_canonical", name=role, status="active",
        prompt_markdown="Canonical launch authority", tool_allowlist=tools,
    )
    class Loader:
        def __init__(self, _):
            pass
        def load(self):
            return definition
        def render_policy_prompt(self):
            return ""
    monkeypatch.setattr(anthus, "AgentLoader", Loader)
    real_exec = asyncio.create_subprocess_exec
    children = []
    async def track_child(*args, **kwargs):
        child = await real_exec(*args, **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr(anthus.asyncio, "create_subprocess_exec", track_child)
    async def launch():
        await anthus._spawn_agent(role, "ent_work", "pm", {})
        assert len(children) == 1
        stdout, stderr = await asyncio.wait_for(children[0].communicate(), timeout=5)
        assert children[0].returncode == 0, (stdout, stderr)
    asyncio.run(launch())
    result = json.loads(receipt.read_text())
    assert result["allowed"] is expected
    assert result["canonical"] is True
    assert result["mode"] == "dontAsk"


@pytest.mark.parametrize("overrides", [
    {"status": "retired"}, {"status": "disabled"}, {"status": "undefined"},
    {"status": "unrecognized"}, {"status": None}, {"prompt_markdown": None},
    {"prompt_markdown": ""}, {"tool_allowlist": []}, {"tool_allowlist": None},
    {"tool_allowlist": "[invalid"}, {"tool_allowlist": ["default"]},
    {"tool_allowlist": ["--dangerously-skip-permissions"]}, {"is_stub": True},
])
def test_direct_launch_refuses_unadmitted_definition(monkeypatch, overrides):
    definition = anthus.AgentDefinition(
        **({"name": "pavo", "status": "active", "prompt_markdown": "Canonical role",
            "tool_allowlist": ["Read"]} | overrides)
    )
    children = []
    async def child(*args, **kwargs):
        from types import SimpleNamespace
        children.append(args)
        return SimpleNamespace(pid=123)
    monkeypatch.setattr(anthus.asyncio, "create_subprocess_exec", child)
    with pytest.raises(anthus.AgentDefinitionUnavailable):
        asyncio.run(anthus._spawn_agent("pavo", "ent_work", "pm", {}, definition))
    assert not children


@pytest.mark.parametrize("mutation", ["remove_builtin_bound", "restore_ambient_allows", "restore_bypass"])
def test_cli_permission_controls_detect_planted_regression(monkeypatch, tmp_path, mutation):
    """The actual subprocess checks must reject a launcher with a lost bound."""
    real_exec = asyncio.create_subprocess_exec
    async def mutated_exec(*args, **kwargs):
        args = list(args)
        if mutation == "remove_builtin_bound":
            index = args.index("--tools")
            del args[index:index + 2]
        elif mutation == "restore_ambient_allows":
            args[args.index("--setting-sources") + 1] = "user,project,local"
        else:
            args.insert(-1, "--dangerously-skip-permissions")
        return await real_exec(*args, **kwargs)
    monkeypatch.setattr(anthus.asyncio, "create_subprocess_exec", mutated_exec)
    # Reads are intrinsically permitted by the CLI; MCP writes need an allow
    # rule. These different probes isolate tool visibility vs ambient grants.
    tools, requested = (
        (["Read"], "mcp__fixture__write") if mutation == "restore_ambient_allows"
        else (["Bash"], "Read")
    )
    with pytest.raises(AssertionError):
        test_actual_cli_launch_binds_canonical_capabilities(
            monkeypatch, tmp_path, "cicada", tools, requested, False,
        )
