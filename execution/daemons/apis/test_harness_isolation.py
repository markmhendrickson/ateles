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
    assert exc.value.setting == str(target.resolve().parent.parent / ".cursor" / "mcp.json") \
        or exc.value.setting.endswith("/.cursor/mcp.json")  # fmt: skip


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
    monkeypatch.setattr(skill_runner, "_load_active_policy_prompt", lambda role: "")
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
    monkeypatch.setattr(skill_runner, "_load_active_policy_prompt", lambda role: "")
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
        assert result.configuration_repair.endswith("/.cursor/mcp.json)")
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


def test_a_seated_repair_fails_over_without_a_cooldown(operator_home, monkeypatch, tmp_path):
    monkeypatch.delenv("LANIUS_NEOTOMA_TOKEN")
    # Seated reviews run on the providers that can deny `correct` (Claude and
    # Codex); each refuses as a repair, and the dispatch moves on to the next.
    monkeypatch.setenv("APIS_HARNESS_PROVIDERS", "codex,claude")
    stand_in, record = _env_child(tmp_path)
    marker, notified = _seam(monkeypatch, tmp_path)
    monkeypatch.setattr(skill_runner, "CODEX_BIN", str(stand_in))
    monkeypatch.setattr(skill_runner, "CLAUDE_BIN", str(stand_in))
    result = asyncio.run(skill_runner.run_skill("lanius", "review", role="lanius",
                                                seated_reviewer=True))  # fmt: skip
    assert not record.exists()
    assert set(result.attempted_providers) == {"codex", "claude"}
    assert result.configuration_repair.count("(repair: LANIUS_NEOTOMA_TOKEN)") == 2
    assert "dispatch not launched: configuration repair needed" in result.error
    import harness_router

    assert not ({"codex", "claude"} & set(harness_router.cooling_providers()))


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


def test_a_no_grant_role_child_holds_no_neotoma_bearer_at_all(operator_home, monkeypatch,
                                                              tmp_path):  # fmt: skip
    monkeypatch.delenv("LANIUS_NEOTOMA_TOKEN")
    stand_in, record = _env_child(tmp_path)
    _seam(monkeypatch, tmp_path, tools="Read,Bash")
    monkeypatch.setattr(skill_runner, "CODEX_BIN", str(stand_in))
    result = asyncio.run(skill_runner.run_skill("lanius", "task", role="lanius",
                                                provider="codex"))  # fmt: skip
    assert result.ok, result.error
    seen = json.loads(record.read_text())
    assert not any(k.endswith("_TOKEN") or k.startswith("NEOTOMA_BEARER") for k in seen
                   if k != "ATELES_ROLE_NEOTOMA_TOKEN")  # fmt: skip
    assert SHARED_BEARER not in json.dumps(seen)
