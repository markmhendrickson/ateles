"""Tests for the claude-local provider (local_provider.py) and its router/runner seam."""

from __future__ import annotations

import asyncio
import json
import shlex
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_DAEMON_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _DAEMON_DIR.parent.parent.parent
for _p in (str(_REPO_ROOT), str(_DAEMON_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from lib.daemon_runtime import AgentDefinition  # noqa: E402

import harness_router  # noqa: E402
import local_provider  # noqa: E402
import skill_runner  # noqa: E402

LOCAL = local_provider.LOCAL_PROVIDER

CONFIG = {
    "enabled": True,
    "base_url": "http://127.0.0.1:4000",
    "model": "qwen3-coder-ollama",
    "context_ceiling_tokens": 32768,
    "eligible_work_classes": ["rebase", "regenerate_generated_files"],
}


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv(local_provider.CONFIG_ENV, str(tmp_path / "claude-local.json"))
    monkeypatch.setenv(local_provider.GUARDS_PATH_ENV, str(tmp_path / "guards.json"))
    monkeypatch.setenv("APIS_HARNESS_PROVIDERS", "claude")
    monkeypatch.setenv("APIS_HARNESS_HEADROOM_FILE", str(tmp_path / "missing-headroom.json"))
    monkeypatch.delenv("APIS_HARNESS_HEADROOM", raising=False)
    monkeypatch.setenv("NEOTOMA_BEARER_TOKEN", "test-bearer")
    monkeypatch.setenv("NEOTOMA_BASE_URL", "http://neotoma.test")
    harness_router.reset_state()
    skill_runner._agent_def_cache.clear()
    yield
    harness_router.reset_state()
    skill_runner._agent_def_cache.clear()


def _write_config(tmp_path, record=None):
    (tmp_path / "claude-local.json").write_text(json.dumps(record or CONFIG), encoding="utf-8")


def _cfg(**over):
    return local_provider.parse_config({**CONFIG, **over})


# ── configuration ──────────────────────────────────────────────────────────


def test_config_is_read_from_the_configured_file(tmp_path):
    assert local_provider.load_config() is None  # no file: provider does not exist
    _write_config(tmp_path)
    cfg = local_provider.load_config()
    assert (cfg.base_url, cfg.model, cfg.context_ceiling_tokens) == (
        "http://127.0.0.1:4000", "qwen3-coder-ollama", 32768)


def test_vendor_binding_shaped_record_is_accepted(tmp_path):
    _write_config(tmp_path, {"entity_type": "vendor_binding", "capability_slot": "local_inference",
                             "config": {k: v for k, v in CONFIG.items() if k != "enabled"}})
    assert local_provider.load_config().model == "qwen3-coder-ollama"


@pytest.mark.parametrize("override", [
    {"enabled": False},
    {"base_url": "http://10.0.0.5:4000"},
    {"base_url": "https://api.example.com"},
    {"base_url": "ftp://127.0.0.1"},
    {"model": ""},
    {"context_ceiling_tokens": 0},
    {"default_tools": ["Bash", "mcp__mcpsrv_neotoma__store"]},
])
def test_invalid_or_disabled_config_disables_the_provider(tmp_path, override):
    _write_config(tmp_path, {**CONFIG, **override})
    assert local_provider.load_config() is None


def test_config_cannot_widen_eligibility_beyond_mechanical_classes():
    cfg = _cfg(eligible_work_classes=["rebase", "security_review", "arch_review", "security_fix"])
    assert cfg.eligible_work_classes == {"rebase"}
    assert local_provider.is_eligible("rebase", cfg)
    assert not local_provider.is_eligible("security_review", cfg)
    assert not local_provider.is_eligible(None, cfg)
    assert not local_provider.is_eligible("rebase", None)


# ── context ceiling (pre-launch) ───────────────────────────────────────────


def test_ceiling_refusal_accounts_for_overhead_and_reserve():
    cfg = _cfg(context_ceiling_tokens=10_000, harness_overhead_tokens=6000,
               output_reserve_tokens=1000, chars_per_token=3.0)
    assert local_provider.ceiling_refusal("s" * 3000, "w" * 6000, cfg) is None  # 3000 + 7000
    refusal = local_provider.ceiling_refusal("s" * 3000, "w" * 6003, cfg)
    assert refusal and "context ceiling" in refusal


# ── command and environment ────────────────────────────────────────────────


def test_command_binds_guards_isolates_settings_and_never_passes_bare():
    cmd = local_provider.build_command("/bin/claude", "SYSTEM", ["Bash", "Read", "mcp__x__*"],
                                       _cfg(), "/tmp/guards.json")
    assert "--bare" not in cmd
    assert cmd[cmd.index("--settings") + 1] == "/tmp/guards.json"
    assert cmd[cmd.index("--setting-sources") + 1] == ""
    assert "--strict-mcp-config" in cmd
    assert json.loads(cmd[cmd.index("--mcp-config") + 1]) == {"mcpServers": {}}
    assert cmd[cmd.index("--model") + 1] == "qwen3-coder-ollama"
    assert cmd[cmd.index("--tools") + 1] == "Bash,Read"
    assert cmd[cmd.index("--allowed-tools") + 1] == "Bash,Read"
    assert cmd[cmd.index("--system-prompt") + 1] == "SYSTEM"


def test_unrestricted_agent_gets_the_configured_default_tools():
    cmd = local_provider.build_command("/bin/claude", "S", ["*"], _cfg(default_tools=["Bash", "Read"]),
                                       "/g.json")
    assert cmd[cmd.index("--tools") + 1] == "Bash,Read"


def test_env_points_at_local_proxy_and_strips_frontier_credentials():
    env = {"CLAUDE_CODE_OAUTH_TOKEN": "oauth", "ANTHROPIC_AUTH_TOKEN": "tok",
           "ANTHROPIC_MODEL": "claude-sonnet-5", "PATH": "/bin"}
    local_provider.apply_env(env, _cfg())
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env and "ANTHROPIC_AUTH_TOKEN" not in env
    assert "ANTHROPIC_MODEL" not in env
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:4000"
    assert env["ANTHROPIC_API_KEY"] == local_provider.PLACEHOLDER_API_KEY
    assert env["ANTHROPIC_SMALL_FAST_MODEL"] == "qwen3-coder-ollama"
    assert env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] == "32768"
    assert env["PATH"] == "/bin"


@pytest.mark.parametrize("text,kind", [
    ("API Error: 400 ateles-local context ceiling exceeded: estimated", "context_ceiling"),
    ('{"code": "context_length_exceeded"}', "context_ceiling"),
    ("API Error: Connection error. connect ECONNREFUSED 127.0.0.1:4000", "endpoint_unreachable"),
    ("guards_unavailable: required PreToolUse Bash guards not wired", "guards_unavailable"),
    ("the tests still fail", "local_run_failed"),
])
def test_failure_classification(text, kind):
    assert local_provider.classify_failure(text) == kind


# ── guards: derived from the repo, and the stash guard actually binds ──────


def _rendered():
    return local_provider.render_guards_settings(_REPO_ROOT)


def _guard_command(settings: dict, filename: str) -> str:
    for entry in settings["hooks"]["PreToolUse"]:
        for hook in entry["hooks"]:
            if filename in hook["command"]:
                assert "Bash" in entry["matcher"].split("|")
                return hook["command"]
    raise AssertionError(f"{filename} not wired")


def test_guards_are_derived_from_repo_settings_and_pinned_to_the_repo():
    settings = _rendered()
    assert set(settings) == {"hooks"} and set(settings["hooks"]) == {"PreToolUse"}
    for guard in local_provider.REQUIRED_GUARDS:
        command = _guard_command(settings, guard)
        assert "$CLAUDE_PROJECT_DIR" not in command
        assert str(_REPO_ROOT.resolve()) in command


def test_missing_required_guard_refuses_to_render(tmp_path):
    hooks = tmp_path / ".claude" / "hooks"
    hooks.mkdir(parents=True)
    wired = [g for g in local_provider.REQUIRED_GUARDS if g != "git_stash_guard.py"]
    for g in wired:
        (hooks / g).write_text("", encoding="utf-8")
    (tmp_path / ".claude" / "settings.json").write_text(json.dumps({"hooks": {"PreToolUse": [{
        "matcher": "Edit|Write|Bash",
        "hooks": [{"type": "command", "command": f'python3 "$CLAUDE_PROJECT_DIR/.claude/hooks/{g}"'}
                  for g in wired]}]}}), encoding="utf-8")
    with pytest.raises(local_provider.LocalProviderError, match="git_stash_guard.py"):
        local_provider.render_guards_settings(tmp_path)


def test_stash_guard_from_the_generated_settings_blocks_git_stash(tmp_path):
    """Integration: run the stash guard exactly as the local child's --settings wires it.

    The command is taken from the rendered guards file (written to disk the way
    the runner writes it), executed from an unrelated cwd, and fed the PreToolUse
    payload the claude CLI sends for a Bash `git stash`. It must deny; a read-only
    `git stash list` must pass.
    """
    path = local_provider.write_guards_file(_REPO_ROOT)
    command = _guard_command(json.loads(Path(path).read_text()), "git_stash_guard.py")

    def run(cmd_text):
        payload = {"hook_event_name": "PreToolUse", "tool_name": "Bash",
                   "tool_input": {"command": cmd_text}, "cwd": str(_REPO_ROOT)}
        return subprocess.run(shlex.split(command), input=json.dumps(payload), capture_output=True,
                              text=True, cwd=tmp_path, timeout=30, check=False)

    blocked = run("git stash push -m wip")
    denied = blocked.returncode == 2 or '"deny"' in blocked.stdout
    assert denied, (blocked.returncode, blocked.stdout, blocked.stderr)
    allowed = run("git stash list")
    assert allowed.returncode == 0 and '"deny"' not in allowed.stdout


# ── router ─────────────────────────────────────────────────────────────────


def _available(local=True):
    return {"claude": "/bin/claude", "codex": None, "cursor": None, LOCAL: "/bin/claude" if local else None}


def test_local_first_puts_claude_local_ahead_of_frontier_fallback():
    assert harness_router.provider_candidates(_available(), local_first=True) == [LOCAL, "claude"]


def test_local_never_enters_rotation_without_local_first(monkeypatch):
    monkeypatch.setenv("APIS_HARNESS_PROVIDERS", f"{LOCAL},claude")
    assert harness_router.configured_providers() == ["claude"]
    assert harness_router.provider_candidates(_available()) == ["claude"]


def test_local_first_skips_unconfigured_or_cooling_local():
    assert harness_router.provider_candidates(_available(local=False), local_first=True) == ["claude"]
    harness_router.cool_down(LOCAL, now=100.0)
    assert harness_router.provider_candidates(_available(), now=101.0, local_first=True) == ["claude"]


def test_pinned_local_is_the_only_candidate():
    assert harness_router.provider_candidates(_available(), preferred=LOCAL) == [LOCAL]


# ── runner: routing, provenance and fallback ───────────────────────────────


def _agent_def():
    return AgentDefinition(entity_id="ent_test", name="cicada", prompt_markdown="You are Cicada.",
                           tool_allowlist="Bash,Read,Edit", aauth_sub="cicada@ateles-swarm")


class _Spawns:
    """Fake create_subprocess_exec: records argv/env, replies per provider."""

    def __init__(self, local_reply=(1, b"", b"API Error: Connection error. ECONNREFUSED")):
        self.calls: list[tuple[list[str], dict]] = []
        self.local_reply = local_reply

    async def spawn(self, *cmd, **kwargs):
        self.calls.append((list(cmd), kwargs.get("env") or {}))
        is_local = "--settings" in cmd and "--setting-sources" in cmd
        rc, out, err = self.local_reply if is_local else (0, b"frontier ok", b"")
        proc = MagicMock()
        proc.returncode = rc

        async def communicate(input=None):
            return out, err

        proc.communicate = communicate
        return proc


def _run(spawns, events, *, prompt="rebase onto main", **kwargs):
    loader = MagicMock()
    loader.return_value.load.return_value = _agent_def()
    with (
        patch("skill_runner.AgentLoader", loader),
        patch("skill_runner.CLAUDE_BIN", "/bin/claude"),
        patch("skill_runner.ATELES_REPO", _REPO_ROOT),
        patch("skill_runner.CODEX_BIN", None),
        patch("skill_runner.CURSOR_BIN", None),
        patch("skill_runner._write_harness_event", side_effect=lambda **kw: events.append(kw)),
        patch("asyncio.create_subprocess_exec", side_effect=spawns.spawn),
    ):
        return asyncio.run(skill_runner.run_skill("cicada", prompt, role="cicada",
                                                  task_entity_id="ent_task", **kwargs))


def test_mechanical_work_runs_local_with_guards_and_provenance(tmp_path, monkeypatch):
    _write_config(tmp_path)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "frontier-oauth")
    spawns, events = _Spawns(local_reply=(0, b"rebased", b"")), []
    result = _run(spawns, events, work_class="rebase")

    assert result.ok and result.provider == LOCAL
    assert result.attempted_providers == (LOCAL,)
    (cmd, env), = spawns.calls
    assert "--bare" not in cmd
    guards = json.loads(Path(cmd[cmd.index("--settings") + 1]).read_text())
    _guard_command(guards, "git_stash_guard.py")
    assert cmd[cmd.index("--tools") + 1] == "Bash,Read,Edit"
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:4000"
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
    done = [e for e in events if e["event_type"] == "subprocess" and e["success"] == "true"]
    assert done and done[0]["tool_name"] == f"{LOCAL}:cicada"
    fields = done[0]["usage"].as_event_fields()
    assert fields["provider"] == LOCAL and fields["model"] == "qwen3-coder-ollama"


def test_local_failure_falls_over_to_frontier_and_records_why(tmp_path):
    _write_config(tmp_path)
    spawns, events = _Spawns(), []
    result = _run(spawns, events, work_class="rebase")

    assert result.ok and result.provider == "claude"
    assert result.attempted_providers == (LOCAL, "claude")
    assert "--settings" in spawns.calls[0][0] and "--settings" not in spawns.calls[1][0]
    failover, = [e for e in events if e["event_type"] == "provider_failover"]
    assert "failover_reason=endpoint_unreachable" in failover["output_summary"]
    assert "next_provider=claude" in failover["output_summary"]
    assert failover["usage"].as_event_fields()["model"] == "qwen3-coder-ollama"
    assert LOCAL in harness_router.cooling_providers()


def test_over_ceiling_prompt_is_refused_before_launch_and_falls_over(tmp_path):
    _write_config(tmp_path, {**CONFIG, "context_ceiling_tokens": 8000})
    spawns, events = _Spawns(local_reply=(0, b"should not run", b"")), []
    result = _run(spawns, events, prompt="x" * 60_000, work_class="rebase")

    assert result.ok and result.provider == "claude"
    assert len(spawns.calls) == 1 and "--settings" not in spawns.calls[0][0]
    failover, = [e for e in events if e["event_type"] == "provider_failover"]
    assert "failover_reason=context_ceiling" in failover["output_summary"]
    # A prompt that is too long says nothing about the endpoint's health.
    assert LOCAL not in harness_router.cooling_providers()


@pytest.mark.parametrize("kwargs", [
    {"work_class": "security_review"},
    {"work_class": None},
    {"work_class": "rebase", "seated_reviewer": True},
    {"work_class": "rebase", "owns_pending_gate": True},
])
def test_non_mechanical_or_gated_work_never_runs_local(tmp_path, kwargs):
    _write_config(tmp_path)
    spawns, events = _Spawns(local_reply=(0, b"local", b"")), []
    result = _run(spawns, events, **kwargs)
    assert result.provider == "claude"
    assert all("--settings" not in cmd for cmd, _ in spawns.calls)


def test_unconfigured_host_never_runs_local():
    spawns, events = _Spawns(local_reply=(0, b"local", b"")), []
    result = _run(spawns, events, work_class="rebase")
    assert result.provider == "claude" and result.attempted_providers == ("claude",)


# ── dispatch_role: the bootstrap entry point ───────────────────────────────


def test_dispatch_role_forwards_work_class_and_gates_pinned_local(tmp_path, monkeypatch):
    import dispatch_role

    skills = tmp_path / "repo" / ".claude" / "skills" / "cicada"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text("# cicada\n", encoding="utf-8")
    monkeypatch.setattr(dispatch_role, "ATELES_REPO", tmp_path / "repo")

    refusal = dispatch_role._preflight("cicada", provider=LOCAL)
    assert refusal and "not configured" in refusal
    _write_config(tmp_path)
    assert dispatch_role._preflight("cicada", provider=LOCAL) is None

    seen: dict = {}

    async def capture(skill, prompt, **kwargs):
        seen.update(kwargs)
        return skill_runner.SkillResult(skill, True, 0, "", "", provider=LOCAL)

    monkeypatch.setattr(dispatch_role, "run_skill", capture)
    asyncio.run(dispatch_role.dispatch("cicada", "rebase it", work_class="rebase"))
    assert seen["work_class"] == "rebase" and seen["provider"] is None

    seen.clear()
    # A non-mechanical class is a usage error, refused before any dispatch.
    rc = dispatch_role.main(["--role", "cicada", "--task", "x", "--work-class", "security_review"])
    assert rc != 0 and seen == {}


def test_ordinary_local_task_failure_falls_over_without_cooling_local(tmp_path):
    """A failed task (e.g. a merge conflict) is not evidence the endpoint is down."""
    _write_config(tmp_path)
    spawns, events = _Spawns(local_reply=(1, b"", b"CONFLICT (content): merge conflict in f.txt")), []
    result = _run(spawns, events, work_class="rebase")
    assert result.provider == "claude" and result.attempted_providers == (LOCAL, "claude")
    failover, = [e for e in events if e["event_type"] == "provider_failover"]
    assert "failover_reason=local_run_failed" in failover["output_summary"]
    assert LOCAL not in harness_router.cooling_providers()
