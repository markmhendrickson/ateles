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
# Endpoint values whose authority or port is malformed: each must be a
# value-free configuration repair, never an exception or an accepted URL.
MALFORMED_AUTHORITIES = (
    "https://[",
    "https://[zz::1]",
    "https://neotoma.example.test:bad",
    "https://neotoma.example.test:0",
    "https://neotoma.example.test:65536",
    "https://neotoma.example.test:",
)
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


@pytest.mark.parametrize("seated_review", [False, True])
@pytest.mark.parametrize("tools", [
    ["*"],
    ["mcp__mcpsrv_neotoma__retrieve_entities", "mcp__mcpsrv_neotoma__correct"],
])
def test_codex_preapproves_only_granted_known_reads(operator_home, tmp_path, seated_review, tools):
    """A headless child must reach granted reads without approving any write."""
    text = (skill_runner._codex_gate_config_text("https://neotoma.example.test", tools)
            if seated_review else skill_runner._isolated_codex_config_text(
                skill_runner.allowed_mcp_servers(tools), "https://neotoma.example.test/mcp"))
    server = tomllib.loads(text)["mcp_servers"]["mcpsrv_neotoma"]
    assert server.get("default_tools_approval_mode") == "prompt"
    preapproved = {name for name, config in server.get("tools", {}).items()
                   if config.get("approval_mode") == "approve"}
    assert "retrieve_entities" in preapproved
    assert not preapproved & {"correct", "store", "create_relationship", "delete_entity"}
    if "enabled_tools" in server:
        assert preapproved <= set(server["enabled_tools"])
    else:
        assert preapproved == {"get_session_identity", "retrieve_entities",
                               "retrieve_entity_by_identifier", "retrieve_entity_snapshot",
                               "retrieve_related_entities"}
    if seated_review:
        assert server["disabled_tools"] == ["correct"]


@pytest.mark.parametrize("seated_review", [False, True])
def test_codex_refuses_a_write_preapproval_in_generated_config(operator_home, tmp_path,
                                                              seated_review):
    tools = ["*"]
    text = (skill_runner._codex_gate_config_text("https://neotoma.example.test", tools)
            if seated_review else skill_runner._isolated_codex_config_text(
                skill_runner.allowed_mcp_servers(tools), "https://neotoma.example.test/mcp"))
    config = tmp_path / "config.toml"
    config.write_text(text + '\n[mcp_servers.mcpsrv_neotoma.tools.store]\napproval_mode = "approve"\n')
    if seated_review:
        assert skill_runner._codex_gate_config_contract(config, "https://neotoma.example.test")
    else:
        with pytest.raises(RuntimeError, match="approval"):
            skill_runner.effective_mcp_servers("codex", config)


@pytest.mark.parametrize("provider", ["codex", "cursor"])
def test_a_role_with_no_neotoma_tools_gets_no_server(operator_home, provider):
    isolated = _prepare(provider, ["Read", "Bash", "mcp__ateles__route_task"])
    try:
        assert skill_runner.effective_mcp_servers(provider, isolated.config_path) == set()
    finally:
        _cleanup(isolated)


@pytest.mark.parametrize("provider", ["codex", "cursor"])
def test_a_granted_role_without_its_own_token_is_refused(operator_home, monkeypatch, provider):
    """Neotoma is a hard dependency for a role granted its tools: no own
    token is a configuration repair, never a launch without memory, and
    never the operator's bearer."""
    monkeypatch.delenv("LANIUS_NEOTOMA_TOKEN")
    with pytest.raises(skill_runner.IsolationRefusal) as exc:
        _prepare(provider, ["*"])
    assert exc.value.setting == "LANIUS_NEOTOMA_TOKEN"
    assert SHARED_BEARER not in str(exc.value)


@pytest.mark.parametrize("base", ["", "http://neotoma.example.test", "https://",
                                  "https://user@neotoma.example.test",
                                  "https://neotoma.example.test/ x", "neotoma.example.test",
                                  "https://neotoma.example.test?x=1",
                                  *MALFORMED_AUTHORITIES])  # fmt: skip
@pytest.mark.parametrize("provider", ["codex", "cursor"])
def test_a_granted_role_with_an_invalid_endpoint_is_refused(operator_home, monkeypatch,
                                                            provider, base):  # fmt: skip
    monkeypatch.setenv("NEOTOMA_BASE_URL", base)
    with pytest.raises(skill_runner.IsolationRefusal) as exc:
        _prepare(provider, ["*"])
    assert exc.value.setting == "NEOTOMA_BASE_URL" and ROLE_TOKEN not in str(exc.value)
    if base:
        assert base not in str(exc.value)  # the refusal never echoes the value


@pytest.mark.parametrize("base", ["https://neotoma.example.test", "https://neotoma.example.test:8443",
                                  "https://neotoma.example.test/api", "https://[2001:db8::1]:443",
                                  "https://neotoma.example.test:65535"])  # fmt: skip
def test_a_well_formed_https_endpoint_is_accepted(operator_home, monkeypatch, base):
    monkeypatch.setenv("NEOTOMA_BASE_URL", base)
    isolated = _prepare("cursor", ["*"])
    _cleanup(isolated)


@pytest.mark.parametrize("provider", ["codex", "cursor"])
def test_a_role_without_neotoma_tools_needs_no_token(operator_home, monkeypatch, provider):
    monkeypatch.delenv("LANIUS_NEOTOMA_TOKEN")
    isolated = _prepare(provider, ["Read", "Bash"])
    try:
        assert skill_runner.effective_mcp_servers(provider, isolated.config_path) == set()
    finally:
        _cleanup(isolated)


def _git(path):
    import subprocess

    subprocess.run(["git", "init", "-q", str(path)], check=True)


_PROJECT_FILES = {
    "plain": json.dumps({"mcpServers": {"client-hosted": {"url": "https://x.example.test"}}}),
    "allowed-name": json.dumps({"mcpServers": {"mcpsrv_neotoma": {"url": "https://x.example.test"}}}),
    "allowed-name-extra-keys": json.dumps({"mcpServers": {"mcpsrv_neotoma": {
        "args": ["--x"], "headers": {"X-Other": "1"}}}}),
    "jsonc": '{\n // comment\n "mcpServers": {"x": {"command": "/usr/bin/true"},},\n}',
    "duplicate-keys": '{"mcpServers": {}, "mcpServers": {"x": {"command": "/usr/bin/true"}}}',
    "name-variant": json.dumps({"mcpServers": {"MCPSRV_NEOTOMA ": {"command": "/usr/bin/true"}}}),
    "empty": "",
}


@pytest.mark.parametrize("content", sorted(_PROJECT_FILES))
@pytest.mark.parametrize("where", ["workspace", "git-root-above-workspace", "symlink"])
def test_any_project_cursor_mcp_file_refuses_the_dispatch(operator_home, tmp_path, where,
                                                          content):  # fmt: skip
    """Cursor reads <project root>/.cursor/mcp.json, the project root being the
    git root of --workspace (else --workspace). Whatever it holds, a file
    there refuses the dispatch."""
    repo = tmp_path / "repo"
    workspace = repo / "sub" / "deeper" if where == "git-root-above-workspace" else repo
    workspace.mkdir(parents=True)
    _git(repo)
    target = repo / ".cursor" / "mcp.json"
    target.parent.mkdir()
    if where == "symlink":
        real = tmp_path / "elsewhere.json"
        real.write_text(_PROJECT_FILES[content])
        target.symlink_to(real)
    else:
        target.write_text(_PROJECT_FILES[content])
    with pytest.raises(skill_runner.IsolationRefusal) as exc:
        _prepare("cursor", ["*"], cwd=str(workspace))
    # A fixed setting name: no local path reaches the repair.
    assert exc.value.setting == "the workspace project's .cursor/mcp.json"
    assert str(tmp_path) not in str(exc.value) + exc.value.setting


def test_a_linked_worktree_checks_its_main_checkout(operator_home, tmp_path):
    import subprocess

    main = tmp_path / "main"
    main.mkdir()
    _git(main)
    subprocess.run(["git", "-C", str(main), "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-q", "--allow-empty", "-m", "x"], check=True)  # fmt: skip
    worktree = tmp_path / "linked"
    subprocess.run(["git", "-C", str(main), "worktree", "add", "-q", str(worktree)], check=True)
    (main / ".cursor").mkdir()
    (main / ".cursor" / "mcp.json").write_text("{}")
    with pytest.raises(skill_runner.IsolationRefusal):
        _prepare("cursor", ["*"], cwd=str(worktree))


def test_a_cursor_workspace_without_project_config_is_fine(operator_home, tmp_path):
    repo = tmp_path / "repo"
    (repo / "sub" / ".cursor").mkdir(parents=True)
    _git(repo)
    # A .cursor/mcp.json below the project root is never read by Cursor.
    (repo / "sub" / ".cursor" / "rules").mkdir()
    isolated = _prepare("cursor", ["*"], cwd=str(repo / "sub"))
    _cleanup(isolated)


def test_codex_without_its_login_is_refused(operator_home):
    (operator_home / ".codex" / "auth.json").unlink()
    with pytest.raises(skill_runner.IsolationRefusal) as exc:
        _prepare("codex", ["*"])
    assert "auth.json" in exc.value.setting


@pytest.mark.parametrize("cmd,refused", [
    (["codex", "exec", "-c", "sandbox_workspace_write.network_access=true", "-"], False),
    (["codex", "exec", "-c", "mcp_servers.x.command=\"/bin/x\"", "-"], True),
    (["codex", "exec", "--config=mcp_servers.x.url=\"https://x\"", "-"], True),
    (["codex", "exec", "-c", "projects.\"/r\".trust_level=\"trusted\"", "-"], True),
    (["codex", "exec", "--profile", "personal", "-"], True),
    (["codex", "exec", "-p", "personal", "-"], True),
])  # fmt: skip
def test_codex_flags_that_bring_outside_config_are_found(cmd, refused):
    assert bool(skill_runner.codex_config_overrides(cmd)) is refused


def test_the_codex_config_trusts_no_project(operator_home):
    isolated = _prepare("codex", ["*"])
    try:
        parsed = tomllib.loads(isolated.config_path.read_text())
        assert "projects" not in parsed and "profiles" not in parsed
    finally:
        _cleanup(isolated)


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
    monkeypatch.setattr(skill_runner, "_load_active_policy_prompt", lambda role, **kw: "")
    monkeypatch.setattr(skill_runner, "_write_harness_event", lambda **kw: None)
    result = asyncio.run(skill_runner._run_skill_once(
        "lanius", "do the task", provider=provider, role="lanius"))  # fmt: skip
    assert result.returncode == 0, result.stderr
    seen = json.loads(result.stdout.strip().splitlines()[-1])
    assert seen["servers"] == ["mcpsrv_neotoma"]
    assert seen["home"] != str(operator_home / ".codex") and seen["home"] != str(operator_home)
    assert not Path(seen["home"]).exists()  # the isolated home is removed afterwards



# ── The whole attempt path: refusals are configuration repairs, kept to the end ─


def _seam(monkeypatch, tmp_path, *, tools="*"):
    stand_in = tmp_path / "stand-in"
    marker = tmp_path / "spawned"
    stand_in.write_text("#!%s\nimport pathlib, sys\npathlib.Path(%r).write_text('1')\n"
                        "print('ran')\n" % (sys.executable, str(marker)), encoding="utf-8")
    stand_in.chmod(0o755)
    monkeypatch.setattr(skill_runner, "CODEX_BIN", str(stand_in))
    monkeypatch.setattr(skill_runner, "CURSOR_BIN", str(stand_in))
    skills = tmp_path / ".claude" / "skills" / "lanius"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text("# lanius\n")
    monkeypatch.setattr(skill_runner, "ATELES_REPO", tmp_path)
    monkeypatch.setattr(skill_runner, "_load_agent_def", lambda role: AgentDefinition(
        entity_id="e", name=role, prompt_markdown="You are lanius.", tool_allowlist=tools,
        aauth_sub=f"{role}@ateles-swarm"))  # fmt: skip
    monkeypatch.setattr(skill_runner, "_load_active_policy_prompt", lambda role, **kw: "")
    monkeypatch.setattr(skill_runner, "_write_harness_event", lambda **kw: None)
    notified = []
    monkeypatch.setattr(skill_runner, "notify_dispatch_failure",
                        lambda notifier, **kw: notified.append(kw) or True)  # fmt: skip
    import harness_router

    harness_router.reset_state()
    return marker, notified


@pytest.mark.parametrize("provider", ["codex", "cursor"])
def test_a_pinned_dispatch_keeps_the_repair_and_starts_nothing(operator_home, monkeypatch,
                                                              tmp_path, provider):  # fmt: skip
    marker, notified = _seam(monkeypatch, tmp_path)
    monkeypatch.delenv("LANIUS_NEOTOMA_TOKEN")
    result = asyncio.run(skill_runner.run_skill("lanius", "task", role="lanius",
                                                provider=provider))  # fmt: skip
    assert not result.ok and not marker.exists()  # no subprocess started
    assert result.configuration_repair.startswith(f"{provider}: ")
    assert "LANIUS_NEOTOMA_TOKEN" in result.configuration_repair
    assert "configuration repair needed" in result.error
    assert result.attempted_providers == (provider,) and not result.cooled_until
    assert notified and "LANIUS_NEOTOMA_TOKEN" in notified[0]["stderr"]
    import harness_router

    assert provider not in harness_router.cooling_providers()  # a repair is not a cooldown
    assert ROLE_TOKEN not in result.error and SHARED_BEARER not in result.error


def test_exhausted_alternatives_keep_every_providers_repair(operator_home, monkeypatch,
                                                            tmp_path):  # fmt: skip
    marker, notified = _seam(monkeypatch, tmp_path)
    monkeypatch.setenv("APIS_HARNESS_PROVIDERS", "codex,cursor")
    monkeypatch.delenv("LANIUS_NEOTOMA_TOKEN")
    result = asyncio.run(skill_runner.run_skill("lanius", "task", role="lanius"))
    assert not result.ok and not marker.exists()
    assert set(result.attempted_providers) == {"codex", "cursor"}
    assert "codex: " in result.configuration_repair and "cursor: " in result.configuration_repair
    assert "dispatch not launched: configuration repair needed" in result.error
    assert "configuration repair" in notified[0]["stderr"]


def test_one_providers_repair_lets_the_next_provider_run(operator_home, monkeypatch, tmp_path):
    marker, notified = _seam(monkeypatch, tmp_path)
    monkeypatch.setenv("APIS_HARNESS_PROVIDERS", "codex,cursor")
    (operator_home / ".codex" / "auth.json").unlink()  # only Codex needs a repair
    result = asyncio.run(skill_runner.run_skill("lanius", "task", role="lanius"))
    assert result.ok and marker.exists()
    assert result.attempted_providers == ("codex", "cursor")


def test_a_no_grant_role_launches_with_no_server(operator_home, monkeypatch, tmp_path):
    marker, _ = _seam(monkeypatch, tmp_path, tools="Read,Bash")
    monkeypatch.delenv("LANIUS_NEOTOMA_TOKEN")
    result = asyncio.run(skill_runner.run_skill("lanius", "task", role="lanius",
                                                provider="codex"))  # fmt: skip
    assert result.ok and marker.exists() and not result.configuration_repair


def test_a_fly_bound_run_builds_no_local_home(operator_home, monkeypatch, tmp_path):
    import fly_runner

    marker, _ = _seam(monkeypatch, tmp_path)
    built = []
    monkeypatch.setattr(skill_runner, "prepare_isolated_harness_home",
                        lambda *a, **k: built.append(a) or pytest.fail("local home built"))
    # Forced to Fly, which is not configured here: refused, nothing local.
    result = asyncio.run(skill_runner._run_skill_once(
        "lanius", "task", provider="codex", role="lanius", runner="fly"))  # fmt: skip
    assert not result.ok and built == [] and not marker.exists()
    assert fly_runner.wants_fly("fly", None)


# ── A malformed endpoint through the whole attempt path ─────────────────────


@pytest.mark.parametrize("base", MALFORMED_AUTHORITIES)
@pytest.mark.parametrize("provider", ["codex", "cursor"])
def test_a_malformed_endpoint_is_a_repair_on_the_pinned_path(operator_home, monkeypatch,
                                                             tmp_path, provider, base):  # fmt: skip
    marker, notified = _seam(monkeypatch, tmp_path)
    monkeypatch.setenv("NEOTOMA_BASE_URL", base)
    result = asyncio.run(skill_runner.run_skill("lanius", "task", role="lanius",
                                                provider=provider))  # fmt: skip
    assert not result.ok and not marker.exists()  # no subprocess started
    assert result.configuration_repair.startswith(f"{provider}: ")
    assert "NEOTOMA_BASE_URL" in result.configuration_repair
    assert "configuration repair needed" in result.error and not result.cooled_until
    assert notified and "NEOTOMA_BASE_URL" in notified[0]["stderr"]
    for text in (result.error, result.configuration_repair, notified[0]["stderr"]):
        assert base not in text and ROLE_TOKEN not in text
    import harness_router

    assert provider not in harness_router.cooling_providers()


@pytest.mark.parametrize("base", MALFORMED_AUTHORITIES)
def test_a_malformed_endpoint_fails_over_and_keeps_both_repairs(operator_home, monkeypatch,
                                                                tmp_path, base):  # fmt: skip
    marker, notified = _seam(monkeypatch, tmp_path)
    monkeypatch.setenv("APIS_HARNESS_PROVIDERS", "codex,cursor")
    monkeypatch.setenv("NEOTOMA_BASE_URL", base)
    result = asyncio.run(skill_runner.run_skill("lanius", "task", role="lanius"))
    assert not result.ok and not marker.exists()
    assert set(result.attempted_providers) == {"codex", "cursor"}  # failed over
    assert result.configuration_repair.count("(repair: NEOTOMA_BASE_URL)") == 2
    assert "codex: " in result.configuration_repair and "; cursor: " in result.configuration_repair
    assert "dispatch not launched: configuration repair needed" in result.error
    assert base not in result.error and base not in notified[0]["stderr"]
    import harness_router

    assert not ({"codex", "cursor"} & set(harness_router.cooling_providers()))


def test_an_unexpected_parse_error_is_still_a_value_free_repair(operator_home, monkeypatch,
                                                                tmp_path):  # fmt: skip
    """Defence in depth: should any isolation step raise ValueError, the launch
    is still refused as a repair and the value is not echoed."""
    marker, _ = _seam(monkeypatch, tmp_path)

    def broken(*a, **k):
        raise ValueError("secret-looking-value-9999")

    monkeypatch.setattr(skill_runner, "prepare_isolated_harness_home", broken)
    result = asyncio.run(skill_runner.run_skill("lanius", "task", role="lanius",
                                                provider="cursor"))  # fmt: skip
    assert not result.ok and not marker.exists() and result.configuration_repair
    assert "secret-looking-value-9999" not in result.error + result.configuration_repair


# ── One workspace for the guard and the child ───────────────────────────────

# A stand-in CLI that resolves its workspace flag the way a real CLI does,
# against its OWN working directory, and reports the project root it would use.
_WORKSPACE_PROBE = r'''#!{python}
import json, os, subprocess, sys
argv = sys.argv[1:]
flag = "--workspace" if "--workspace" in argv else "--cd" if "--cd" in argv else None
value = argv[argv.index(flag) + 1] if flag else None
ws = os.path.realpath(value) if value else os.getcwd()
top = subprocess.run(["git", "-C", ws, "rev-parse", "--show-toplevel"],
                     capture_output=True, text=True)
root = os.path.realpath(top.stdout.strip()) if top.returncode == 0 and top.stdout.strip() else ws
print(json.dumps({{"flag_value": value, "workspace": ws, "cwd": os.getcwd(),
                  "project_mcp": os.path.lexists(os.path.join(root, ".cursor", "mcp.json"))}}))
'''


def _probe_dispatch(monkeypatch, tmp_path, provider, cwd):
    probe = tmp_path / "probe-cli"
    probe.write_text(_WORKSPACE_PROBE.format(python=sys.executable), encoding="utf-8")
    probe.chmod(0o755)
    _seam(monkeypatch, tmp_path)
    monkeypatch.setattr(skill_runner, "CODEX_BIN", str(probe))
    monkeypatch.setattr(skill_runner, "CURSOR_BIN", str(probe))
    checked = []
    real_prepare = skill_runner.prepare_isolated_harness_home

    def recording_prepare(*a, **k):
        checked.append(str(Path(k["cwd"]).resolve()))
        return real_prepare(*a, **k)

    monkeypatch.setattr(skill_runner, "prepare_isolated_harness_home", recording_prepare)
    result = asyncio.run(skill_runner._run_skill_once(
        "lanius", "task", provider=provider, role="lanius", cwd=cwd))  # fmt: skip
    seen = json.loads(result.stdout.strip().splitlines()[-1]) if result.ok else None
    return result, checked, seen


@pytest.mark.parametrize("layout", ["clean", "config-at-the-checked-root",
                                    "config-below-the-checked-root"])  # fmt: skip
@pytest.mark.parametrize("provider", ["codex", "cursor"])
def test_a_relative_workspace_is_resolved_once_for_the_guard_and_the_child(
        operator_home, monkeypatch, tmp_path, provider, layout):  # fmt: skip
    """The workspace is resolved to one absolute path before the guard and the
    spawn: whatever project root the child would use is the one checked."""
    daemon_cwd = tmp_path / "daemon-cwd"
    workspace = daemon_cwd / "ws"
    (workspace / "ws").mkdir(parents=True)
    if layout == "config-at-the-checked-root":
        (workspace / ".cursor").mkdir()
        (workspace / ".cursor" / "mcp.json").write_text("{}")
    elif layout == "config-below-the-checked-root":
        (workspace / "ws" / ".cursor").mkdir()
        (workspace / "ws" / ".cursor" / "mcp.json").write_text("{}")
    monkeypatch.chdir(daemon_cwd)
    result, checked, seen = _probe_dispatch(monkeypatch, tmp_path, provider, "ws")
    if provider == "cursor" and layout == "config-at-the-checked-root":
        assert not result.ok and "NEOTOMA" not in result.configuration_repair
        assert result.configuration_repair.endswith("(repair: the workspace project's .cursor/mcp.json)")
        return
    assert result.ok, result.error
    assert checked == [str(workspace.resolve())]
    # The child is told, and runs in, exactly the workspace the guard checked.
    assert seen["flag_value"] == checked[0] == seen["workspace"] == os.path.realpath(seen["cwd"])
    if provider == "cursor":
        assert seen["project_mcp"] is False  # nothing the guard did not check is loaded


def test_a_symlinked_workspace_is_checked_and_launched_at_its_real_path(
        operator_home, monkeypatch, tmp_path):  # fmt: skip
    real = tmp_path / "real-ws"
    real.mkdir()
    (tmp_path / "link-ws").symlink_to(real)
    result, checked, seen = _probe_dispatch(monkeypatch, tmp_path, "cursor",
                                            str(tmp_path / "link-ws"))  # fmt: skip
    assert result.ok
    assert seen["flag_value"] == checked[0] == str(real.resolve())


@pytest.mark.parametrize("provider,flag", [("cursor", "--workspace"), ("codex", "--cd")])
def test_a_workspace_flag_that_differs_from_the_checked_one_refuses(provider, flag):
    checked = "/abs/ws"
    assert skill_runner.launch_workspace_mismatch(provider, ["x", flag, checked], checked) == ""
    for other in ("ws", "./ws", "/abs/other"):
        assert skill_runner.launch_workspace_mismatch(provider, ["x", flag, other], checked)
    assert skill_runner.launch_workspace_mismatch(provider, ["x", flag], checked)


# ── Seated reviews and every other local child: the role's own token only ────

# The roles that dispatch today; the operator provisioned each its own token.
DISPATCHING_ROLES = ("waxwing", "falco", "cicada", "phoenicurus", "pavo", "accipiter",
                     "lanius", "corvus", "vanellus", "buteo")


@pytest.mark.parametrize("role", DISPATCHING_ROLES)
def test_dispatching_roles_resolve_their_own_token_names(role):
    assert skill_runner.neotoma_token_env_name(role) == f"{role.upper()}_NEOTOMA_TOKEN"


def _env_child(tmp_path):
    """A stand-in CLI that records its environment's Neotoma bearers."""
    stand_in = tmp_path / "env-stand-in"
    record = tmp_path / "child-env.json"
    stand_in.write_text(
        "#!%s\nimport json, os, pathlib\n"
        "pathlib.Path(%r).write_text(json.dumps({k: v for k, v in os.environ.items()"
        " if 'NEOTOMA' in k}))\nprint('ran')\n" % (sys.executable, str(record)),
        encoding="utf-8")  # fmt: skip
    stand_in.chmod(0o755)
    return stand_in, record


def _seated(monkeypatch, tmp_path, *, role="lanius", provider="codex", seated=True):
    stand_in, record = _env_child(tmp_path)
    marker, notified = _seam(monkeypatch, tmp_path)
    monkeypatch.setattr(skill_runner, "CODEX_BIN", str(stand_in))
    monkeypatch.setattr(skill_runner, "CURSOR_BIN", str(stand_in))
    (tmp_path / ".claude" / "skills" / role).mkdir(parents=True, exist_ok=True)
    (tmp_path / ".claude" / "skills" / role / "SKILL.md").write_text(f"# {role}\n")
    result = asyncio.run(skill_runner.run_skill(role, "review", role=role, provider=provider,
                                                seated_reviewer=seated))  # fmt: skip
    seen = json.loads(record.read_text()) if record.exists() else None
    return result, seen, notified


@pytest.mark.parametrize("token", [None, "", "   "])
def test_a_seated_codex_review_without_its_own_token_is_refused(operator_home, monkeypatch,
                                                                tmp_path, token):  # fmt: skip
    if token is None:
        monkeypatch.delenv("LANIUS_NEOTOMA_TOKEN")
    else:
        monkeypatch.setenv("LANIUS_NEOTOMA_TOKEN", token)
    result, seen, notified = _seated(monkeypatch, tmp_path)
    assert not result.ok and seen is None  # no child started
    assert "LANIUS_NEOTOMA_TOKEN" in result.configuration_repair
    assert not result.cooled_until
    for text in (result.error, result.configuration_repair, notified[0]["stderr"]):
        assert SHARED_BEARER not in text
    import harness_router

    assert "codex" not in harness_router.cooling_providers()


def test_a_seated_codex_review_with_an_invalid_endpoint_is_refused(operator_home, monkeypatch,
                                                                   tmp_path):  # fmt: skip
    monkeypatch.setenv("NEOTOMA_BASE_URL", "https://neotoma.example.test:bad")
    result, seen, _ = _seated(monkeypatch, tmp_path)
    assert not result.ok and seen is None
    assert "NEOTOMA_BASE_URL" in result.configuration_repair


def test_a_seated_codex_review_with_its_own_token_launches_with_it(operator_home, monkeypatch,
                                                                   tmp_path):  # fmt: skip
    result, seen, _ = _seated(monkeypatch, tmp_path)
    assert result.ok, result.error
    assert seen["NEOTOMA_BEARER_TOKEN"] == ROLE_TOKEN  # the record server's bearer: own
    assert SHARED_BEARER not in json.dumps(seen)


@pytest.mark.parametrize("role", DISPATCHING_ROLES)
def test_every_dispatching_role_still_launches_seated_with_its_token(operator_home, monkeypatch,
                                                                     tmp_path, role):  # fmt: skip
    # The operator's ten tokens are set, each equal to the shared value (as on
    # 2026-10-20): accepted, and every role still launches.
    for name in DISPATCHING_ROLES:
        monkeypatch.setenv(f"{name.upper()}_NEOTOMA_TOKEN", SHARED_BEARER)
    result, seen, _ = _seated(monkeypatch, tmp_path, role=role)
    assert result.ok, result.error
    assert seen["NEOTOMA_BEARER_TOKEN"] == SHARED_BEARER  # its own token, equal by choice


def test_a_seated_repair_is_not_a_cooldown(operator_home, monkeypatch, tmp_path):
    # Seated reviews run on Codex here (Cursor cannot deny `correct`): the
    # repair ends the dispatch without cooling the provider.
    monkeypatch.delenv("LANIUS_NEOTOMA_TOKEN")
    monkeypatch.setenv("APIS_HARNESS_PROVIDERS", "codex,cursor")
    stand_in, record = _env_child(tmp_path)
    marker, notified = _seam(monkeypatch, tmp_path)
    monkeypatch.setattr(skill_runner, "CODEX_BIN", str(stand_in))
    monkeypatch.setattr(skill_runner, "CURSOR_BIN", str(stand_in))
    result = asyncio.run(skill_runner.run_skill("lanius", "review", role="lanius",
                                                seated_reviewer=True))  # fmt: skip
    assert not record.exists()
    assert "codex" in result.attempted_providers
    assert "(repair: LANIUS_NEOTOMA_TOKEN)" in result.configuration_repair
    assert "dispatch not launched: configuration repair needed" in result.error
    import harness_router

    assert not ({"codex", "cursor"} & set(harness_router.cooling_providers()))


@pytest.mark.parametrize("provider", ["codex", "cursor"])
def test_a_local_child_holds_no_bearer_but_its_roles_own(operator_home, monkeypatch, tmp_path,
                                                         provider):  # fmt: skip
    monkeypatch.setenv("NEOTOMA_BEARER_TOKEN_PROD", "shared-prod-bearer-canary")
    monkeypatch.setenv("PAVO_NEOTOMA_TOKEN", "pavo-token-canary")
    result, seen, _ = _seated(monkeypatch, tmp_path, provider=provider, seated=False)
    assert result.ok, result.error
    assert seen.get("NEOTOMA_BEARER_TOKEN") == ROLE_TOKEN
    assert "NEOTOMA_BEARER_TOKEN_PROD" not in seen and "PAVO_NEOTOMA_TOKEN" not in seen
    assert SHARED_BEARER not in json.dumps(seen)


def test_a_no_grant_role_child_holds_no_neotoma_credential_even_with_a_token(
        operator_home, monkeypatch, tmp_path):  # fmt: skip
    # The role HAS its own token (operator_home sets it) but no Neotoma tools:
    # its child gets no Neotoma credential at all.
    monkeypatch.setenv("NEOTOMA_BEARER_TOKEN_PROD", "shared-prod-bearer-canary")
    monkeypatch.setenv("PAVO_NEOTOMA_TOKEN", "pavo-token-canary")
    stand_in, record = _env_child(tmp_path)
    _seam(monkeypatch, tmp_path, tools="Read,Bash")
    monkeypatch.setattr(skill_runner, "CODEX_BIN", str(stand_in))
    result = asyncio.run(skill_runner.run_skill("lanius", "task", role="lanius",
                                                provider="codex"))  # fmt: skip
    assert result.ok, result.error
    seen = json.loads(record.read_text())
    assert [k for k in seen if k.endswith("_TOKEN") or k.startswith("NEOTOMA_BEARER")] == []
    for value in (ROLE_TOKEN, SHARED_BEARER, "pavo-token-canary", "shared-prod-bearer-canary"):
        assert value not in json.dumps(seen)


@pytest.mark.parametrize("provider", ["codex", "cursor"])
@pytest.mark.parametrize("error", [OSError("disk full"), RuntimeError("could not verify")])
def test_an_isolated_home_that_cannot_be_built_refuses_the_launch(operator_home, monkeypatch,
                                                                  tmp_path, provider, error):  # fmt: skip
    marker, _ = _seam(monkeypatch, tmp_path)

    def broken(*a, **k):
        raise error

    monkeypatch.setattr(skill_runner, "prepare_isolated_harness_home", broken)
    result = asyncio.run(skill_runner.run_skill("lanius", "task", role="lanius",
                                                provider=provider))  # fmt: skip
    assert not result.ok and not marker.exists() and result.configuration_repair
    import harness_router

    assert provider not in harness_router.cooling_providers()


@pytest.mark.parametrize("error", [OSError("no such file"), RuntimeError("contract not proved")])
def test_a_seated_config_that_cannot_be_built_refuses_as_a_repair(operator_home, monkeypatch,
                                                                  tmp_path, error):  # fmt: skip
    def broken(**k):
        raise error

    monkeypatch.setattr(skill_runner, "_prepare_codex_gate_owner_config", broken)
    result, seen, notified = _seated(monkeypatch, tmp_path)
    assert not result.ok and seen is None  # nothing started
    assert skill_runner.GATE_OWNER_TOOL_DENY_UNAVAILABLE in result.configuration_repair
    assert "no such file" not in result.configuration_repair  # an OSError's text is not echoed
    import harness_router

    assert "codex" not in harness_router.cooling_providers()


def test_claude_dispatch_is_unchanged_by_this_isolation(operator_home, monkeypatch, tmp_path):
    """Claude is not isolated by this change (it is not an active swarm
    provider): a Claude child keeps the dispatcher's environment and the
    existing MCP configuration mechanism, as before. Isolating Claude is
    separate work, required before Claude is reactivated; this test pins
    today's behaviour so that work changes it deliberately."""
    monkeypatch.delenv("LANIUS_NEOTOMA_TOKEN")
    stand_in, record = _env_child(tmp_path)
    marker, _ = _seam(monkeypatch, tmp_path)
    monkeypatch.setattr(skill_runner, "CLAUDE_BIN", str(stand_in))
    seen_cfg = {}
    real_mkstemp = skill_runner.tempfile.mkstemp

    def capture(*a, **k):
        fd, path = real_mkstemp(*a, **k)
        seen_cfg["path"] = path
        return fd, path

    monkeypatch.setattr(skill_runner.tempfile, "mkstemp", capture)
    real_exec = asyncio.create_subprocess_exec

    async def exec_and_read(*cmd, **kw):
        if "--mcp-config" in cmd:
            seen_cfg["cfg"] = json.loads(Path(cmd[cmd.index("--mcp-config") + 1]).read_text())
        return await real_exec(*cmd, **kw)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", exec_and_read)
    result = asyncio.run(skill_runner.run_skill("lanius", "task", role="lanius",
                                                provider="claude"))  # fmt: skip
    assert result.ok, result.error
    env = json.loads(record.read_text())
    assert env.get("NEOTOMA_BEARER_TOKEN") == SHARED_BEARER
    header = seen_cfg["cfg"]["mcpServers"]["mcpsrv_neotoma"]["headers"]["Authorization"]
    assert header == f"Bearer {SHARED_BEARER}"


# ── The generated home is mandatory; a caller-supplied one only by opt-in ────


def _home_dir(tmp_path, provider):
    home = tmp_path / f"supplied-{provider}-home"
    home.mkdir()
    (home / "auth.json").write_text("{}")
    return home


@pytest.mark.parametrize("provider", ["codex", "cursor"])
def test_a_home_in_env_extra_is_refused(operator_home, monkeypatch, tmp_path, provider):
    marker, notified = _seam(monkeypatch, tmp_path)
    key = "CODEX_HOME" if provider == "codex" else "HOME"
    home = _home_dir(tmp_path, provider)
    result = asyncio.run(skill_runner.run_skill("lanius", "task", role="lanius", provider=provider,
                                                env_extra={key: str(home)}))  # fmt: skip
    assert not result.ok and not marker.exists()
    assert f"(repair: env_extra {key})" in result.configuration_repair
    assert str(home) not in result.error + result.configuration_repair
    import harness_router

    assert provider not in harness_router.cooling_providers()


def test_run_skill_offers_no_way_to_supply_another_home():
    import inspect

    params = inspect.signature(skill_runner.run_skill).parameters
    assert not [p for p in params if "home" in p.lower()]
    with pytest.raises(TypeError):
        asyncio.run(skill_runner.run_skill("lanius", "task", trusted_harness_home="/tmp/x"))


@pytest.mark.parametrize("provider", ["codex", "cursor"])
def test_the_lens_runner_local_review_home_is_unchanged(operator_home, monkeypatch, tmp_path,
                                                        provider):  # fmt: skip
    """The separately guarded local-review path (the lens runner) still runs
    with its own home from env_extra; the generated home is not built."""
    stand_in = tmp_path / "home-stand-in"
    record = tmp_path / "home-seen.txt"
    stand_in.write_text("#!%s\nimport os, pathlib\npathlib.Path(%r).write_text("
                        "os.environ.get('CODEX_HOME') or os.environ['HOME'])\nprint('ran')\n"
                        % (sys.executable, str(record)), encoding="utf-8")  # fmt: skip
    stand_in.chmod(0o755)
    _seam(monkeypatch, tmp_path)
    monkeypatch.setattr(skill_runner, "CODEX_BIN", str(stand_in))
    monkeypatch.setattr(skill_runner, "CURSOR_BIN", str(stand_in))
    monkeypatch.setattr(skill_runner, "prepare_isolated_harness_home",
                        lambda *a, **k: pytest.fail("no generated home on the local-review path"))
    home = _home_dir(tmp_path, provider)
    key = "CODEX_HOME" if provider == "codex" else "HOME"
    result = asyncio.run(skill_runner.run_skill(
        "lanius", "review", role="lanius", provider=provider, local_review=True,
        env_extra={key: str(home), "ATELES_LOCAL_REVIEW_HOME": str(home)}))  # fmt: skip
    assert result.ok, result.error
    assert Path(record.read_text()).resolve() == home.resolve()


# ── Repairs carry a fixed reason and the exception class, nothing else ───────

SENTINEL = "sentinel-7c1e"


def _sentinel_errors():
    return [OSError(2, f"errno text {SENTINEL}", f"/private/{SENTINEL}/path"),
            RuntimeError(f"contract detail at /private/{SENTINEL}/config.toml")]


def _assert_sentinel_free(result, notified, caplog):
    texts = [result.error, result.configuration_repair, repr(result), str(result)]
    texts += [str(n.get("stderr", "")) for n in notified]
    texts += [r.getMessage() + str(r.exc_text or "") for r in caplog.records]
    for text in texts:
        assert SENTINEL not in text


@pytest.mark.parametrize("error", _sentinel_errors(), ids=["oserror", "runtimeerror"])
def test_a_seated_config_failure_reaches_no_surface_with_its_detail(operator_home, monkeypatch,
                                                                     tmp_path, caplog, error):  # fmt: skip
    caplog.set_level("DEBUG")

    def broken(**k):
        raise error

    monkeypatch.setattr(skill_runner, "_prepare_codex_gate_owner_config", broken)
    result, seen, notified = _seated(monkeypatch, tmp_path)
    assert not result.ok and seen is None
    assert f"({type(error).__name__})" in result.configuration_repair
    _assert_sentinel_free(result, notified, caplog)


@pytest.mark.parametrize("provider", ["codex", "cursor"])
@pytest.mark.parametrize("error", _sentinel_errors(), ids=["oserror", "runtimeerror"])
def test_an_isolated_home_failure_reaches_no_surface_with_its_detail(operator_home, monkeypatch,
                                                                      tmp_path, caplog, provider,
                                                                      error):  # fmt: skip
    caplog.set_level("DEBUG")
    marker, notified = _seam(monkeypatch, tmp_path)

    def broken(*a, **k):
        raise error

    monkeypatch.setattr(skill_runner, "prepare_isolated_harness_home", broken)
    result = asyncio.run(skill_runner.run_skill("lanius", "task", role="lanius",
                                                provider=provider))  # fmt: skip
    assert not result.ok and not marker.exists()
    assert f"({type(error).__name__})" in result.configuration_repair
    _assert_sentinel_free(result, notified, caplog)
