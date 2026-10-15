"""Local Codex and Cursor swarm children run in an isolated harness home.

Finding 2026-10-15: a swarm Codex child on the Mac used the operator's
personal ~/.codex/config.toml, whose MCP servers include personal tools, a
dev instance and a client's hosted instance, and could not reach production
Neotoma. Every local Codex and Cursor dispatch now runs with a generated
home whose MCP servers are exactly those the role's allowlist grants: by
default only production Neotoma, over its remote endpoint with the role's
own token. These tests read the generated configuration, and run a stand-in
CLI as a real process to show what a child actually sees.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tomllib
from pathlib import Path

import pytest

_DAEMON_DIR = Path(__file__).resolve().parent
if str(_DAEMON_DIR) not in sys.path:
    sys.path.insert(0, str(_DAEMON_DIR))

import skill_runner  # noqa: E402
from lib.daemon_runtime import AgentDefinition  # noqa: E402

ROLE_TOKEN = "lanius-own-token-canary-1234"
SHARED_BEARER = "operator-shared-bearer-canary-5678"
PERSONAL_SERVERS = ("neotoma-dev", "neotoma", "client-hosted", "typefully", "node_repl",
                    "computer-use", "ateles")  # fmt: skip


@pytest.fixture
def operator_home(monkeypatch, tmp_path):
    """A stand-in for the operator's home with personal harness configs."""
    home = tmp_path / "operator"
    codex = home / ".codex"
    codex.mkdir(parents=True)
    servers = "\n".join(
        f'[mcp_servers.{name}]\nurl = "https://{name}.personal.example.test/mcp"\n'
        if name in ("client-hosted", "typefully")
        else f'[mcp_servers.{name}]\ncommand = "/usr/local/bin/{name}-shim"\n'
        for name in PERSONAL_SERVERS
    )
    (codex / "config.toml").write_text(
        'model = "gpt-operator-default"\nmodel_reasoning_effort = "high"\n'
        'approval_policy = "never"\n' + servers, encoding="utf-8")
    (codex / "auth.json").write_text("{}", encoding="utf-8")
    os.chmod(codex / "auth.json", 0o600)
    (codex / "AGENTS.md").write_text("operator's personal instructions", encoding="utf-8")
    cursor = home / ".cursor"
    cursor.mkdir()
    (cursor / "mcp.json").write_text(json.dumps({"mcpServers": {
        name: {"url": f"https://{name}.personal.example.test/mcp"} for name in PERSONAL_SERVERS}}))
    (home / "Library" / "Keychains").mkdir(parents=True)
    (home / ".gitconfig").write_text("[user]\n\tname = Operator\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setenv("NEOTOMA_BASE_URL", "https://neotoma.example.test")
    monkeypatch.setenv("LANIUS_NEOTOMA_TOKEN", ROLE_TOKEN)
    monkeypatch.setenv("NEOTOMA_BEARER_TOKEN", SHARED_BEARER)
    return home


def _prepare(provider, tools, *, cwd=None):
    return skill_runner.prepare_isolated_harness_home(
        provider, role="lanius", agent_tools=tools, cwd=cwd, env_extra=None)


def _cleanup(isolated):
    import shutil

    shutil.rmtree(isolated.home, ignore_errors=True)


# ── The generated configuration ─────────────────────────────────────────────


@pytest.mark.parametrize("provider", ["codex", "cursor"])
def test_a_wildcard_role_gets_only_production_neotoma(operator_home, provider):
    isolated = _prepare(provider, ["*"])
    try:
        declared = skill_runner.effective_mcp_servers(provider, isolated.config_path)
        assert declared == {"mcpsrv_neotoma"}
        text = isolated.config_path.read_text()
        assert "https://neotoma.example.test/mcp" in text
        for name in PERSONAL_SERVERS:
            assert f"{name}.personal.example.test" not in text and f"{name}-shim" not in text
        assert SHARED_BEARER not in text and SHARED_BEARER not in json.dumps(isolated.env)
        assert not (isolated.home / "AGENTS.md").exists()  # no personal instructions
    finally:
        _cleanup(isolated)


def test_the_codex_home_carries_only_the_login_and_model_settings(operator_home):
    isolated = _prepare("codex", ["*"])
    try:
        assert sorted(p.name for p in isolated.home.iterdir()) == ["auth.json", "config.toml"]
        auth = isolated.home / "auth.json"
        assert auth.is_symlink() and auth.resolve() == (operator_home / ".codex/auth.json").resolve()
        parsed = tomllib.loads(isolated.config_path.read_text())
        assert parsed["model"] == "gpt-operator-default"
        assert parsed["model_reasoning_effort"] == "high"
        assert "approval_policy" not in parsed
        server = parsed["mcp_servers"]["mcpsrv_neotoma"]
        assert server["bearer_token_env_var"] == skill_runner.ROLE_NEOTOMA_BEARER_ENV
        assert "command" not in server  # remote endpoint, no local shim
        assert ROLE_TOKEN not in isolated.config_path.read_text()  # only in the env
        assert isolated.env == {"CODEX_HOME": str(isolated.home),
                                skill_runner.ROLE_NEOTOMA_BEARER_ENV: ROLE_TOKEN}  # fmt: skip
    finally:
        _cleanup(isolated)


def test_the_cursor_home_carries_only_the_login_link(operator_home):
    isolated = _prepare("cursor", ["*"])
    try:
        assert sorted(p.name for p in isolated.home.iterdir()) == [".cursor", "Library"]
        link = isolated.home / "Library" / "Keychains"
        assert link.is_symlink() and link.resolve() == (operator_home / "Library/Keychains").resolve()
        assert isolated.env["HOME"] == str(isolated.home)
        assert isolated.env["CURSOR_CONFIG_DIR"] == str(isolated.home / ".cursor")
        assert isolated.env["GIT_CONFIG_GLOBAL"] == str(operator_home / ".gitconfig")
        assert oct(isolated.config_path.stat().st_mode & 0o777) == "0o600"
    finally:
        _cleanup(isolated)


def test_a_restricted_neotoma_grant_restricts_the_codex_tools(operator_home):
    isolated = _prepare("codex", ["mcp__mcpsrv_neotoma__retrieve_entities", "Read"])
    try:
        parsed = tomllib.loads(isolated.config_path.read_text())
        assert parsed["mcp_servers"]["mcpsrv_neotoma"]["enabled_tools"] == ["retrieve_entities"]
    finally:
        _cleanup(isolated)


@pytest.mark.parametrize("provider", ["codex", "cursor"])
def test_a_role_with_no_neotoma_tools_gets_no_server(operator_home, provider):
    isolated = _prepare(provider, ["Read", "Bash", "mcp__ateles__route_task"])
    try:
        assert skill_runner.effective_mcp_servers(provider, isolated.config_path) == set()
    finally:
        _cleanup(isolated)


@pytest.mark.parametrize("provider", ["codex", "cursor"])
def test_a_role_without_its_own_token_never_gets_the_operators(operator_home, monkeypatch,
                                                               provider):  # fmt: skip
    monkeypatch.delenv("LANIUS_NEOTOMA_TOKEN")
    isolated = _prepare(provider, ["*"])
    try:
        assert skill_runner.effective_mcp_servers(provider, isolated.config_path) == set()
        assert SHARED_BEARER not in isolated.config_path.read_text()
        assert SHARED_BEARER not in json.dumps(isolated.env)
    finally:
        _cleanup(isolated)


def test_a_workspace_cursor_config_with_other_servers_is_refused(operator_home, tmp_path):
    workspace = tmp_path / "workspace"
    (workspace / ".cursor").mkdir(parents=True)
    (workspace / ".cursor" / "mcp.json").write_text(
        json.dumps({"mcpServers": {"client-hosted": {"url": "https://x.example.test"}}}))
    with pytest.raises(RuntimeError, match="outside the role's allowlist"):
        _prepare("cursor", ["*"], cwd=str(workspace))


def test_codex_without_its_login_is_refused(operator_home):
    (operator_home / ".codex" / "auth.json").unlink()
    with pytest.raises(RuntimeError, match="auth.json"):
        _prepare("codex", ["*"])


# ── A real child process: what it actually sees ─────────────────────────────

_STAND_IN_CLI = r'''#!{python}
import json, os, sys, tomllib
from pathlib import Path
if "CODEX_HOME" in os.environ and Path(os.environ["CODEX_HOME"], "config.toml").is_file():
    servers = tomllib.loads(Path(os.environ["CODEX_HOME"], "config.toml").read_text()).get("mcp_servers", {{}})
else:
    servers = json.loads(Path(os.environ["HOME"], ".cursor", "mcp.json").read_text()).get("mcpServers", {{}})
print(json.dumps({{"servers": sorted(servers), "home": os.environ.get("CODEX_HOME") or os.environ["HOME"]}}))
'''


@pytest.mark.parametrize("provider", ["codex", "cursor"])
def test_a_dispatched_child_sees_only_the_allowed_servers(operator_home, monkeypatch,
                                                         tmp_path, provider):  # fmt: skip
    stand_in = tmp_path / f"{provider}-stand-in"
    stand_in.write_text(_STAND_IN_CLI.format(python=sys.executable), encoding="utf-8")
    stand_in.chmod(0o755)
    monkeypatch.setattr(skill_runner, "CODEX_BIN", str(stand_in))
    monkeypatch.setattr(skill_runner, "CURSOR_BIN", str(stand_in))
    skills = tmp_path / ".claude" / "skills" / "lanius"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text("# lanius\n")
    monkeypatch.setattr(skill_runner, "ATELES_REPO", tmp_path)
    monkeypatch.setattr(skill_runner, "_load_agent_def", lambda role: AgentDefinition(
        entity_id="e", name=role, prompt_markdown="You are lanius.", tool_allowlist="*",
        aauth_sub=f"{role}@ateles-swarm"))  # fmt: skip
    monkeypatch.setattr(skill_runner, "_load_active_policy_prompt", lambda role: "")
    monkeypatch.setattr(skill_runner, "_write_harness_event", lambda **kw: None)
    result = asyncio.run(skill_runner._run_skill_once(
        "lanius", "do the task", provider=provider, role="lanius"))  # fmt: skip
    assert result.returncode == 0, result.stderr
    seen = json.loads(result.stdout.strip().splitlines()[-1])
    assert seen["servers"] == ["mcpsrv_neotoma"]
    assert seen["home"] != str(operator_home / ".codex") and seen["home"] != str(operator_home)
    assert not Path(seen["home"]).exists()  # the isolated home is removed afterwards
