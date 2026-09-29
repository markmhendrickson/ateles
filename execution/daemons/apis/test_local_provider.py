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
from lib.pytest_env_guard import assert_env_keys_absent  # noqa: E402

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

VENDOR_BINDING = {"claude": {"mechanical": "haiku", "mid": "sonnet", "top": "opus"}}


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv(local_provider.CONFIG_ENV, str(tmp_path / "claude-local.json"))
    monkeypatch.setenv(local_provider.GUARDS_PATH_ENV, str(tmp_path / "guards.json"))
    monkeypatch.setenv("APIS_HARNESS_PROVIDERS", "claude")
    monkeypatch.setenv("APIS_HARNESS_HEADROOM_FILE", str(tmp_path / "missing-headroom.json"))
    monkeypatch.delenv("APIS_HARNESS_HEADROOM", raising=False)
    monkeypatch.setenv("NEOTOMA_BEARER_TOKEN", "test-bearer")
    monkeypatch.setenv("NEOTOMA_BASE_URL", "http://neotoma.test")
    # No ambient vendor_binding: with none, a failed local run has nowhere to
    # fall over to. Tests that want the cheapest-tier fallback use `fallback_bound`.
    monkeypatch.setenv("APIS_VENDOR_BINDING_FILE", str(tmp_path / "no-vendor-binding.json"))
    monkeypatch.delenv("APIS_VENDOR_BINDING", raising=False)
    # Deterministic default: pin ATELES_REPO_PATH to the real repo root
    # rather than leaving the host shell's ambient value (or lack of one) to
    # leak in. Most tests here need it set to exercise a successful
    # claude-local launch; the one test that needs it unset explicitly
    # `monkeypatch.delenv`s it to override this default.
    monkeypatch.setenv("ATELES_REPO_PATH", str(_REPO_ROOT))
    harness_router.reset_state()
    skill_runner._agent_def_cache.clear()
    yield
    harness_router.reset_state()
    skill_runner._agent_def_cache.clear()


@pytest.fixture
def fallback_bound(tmp_path, monkeypatch):
    """A vendor_binding that names a cheapest-tier ('mechanical') claude model."""
    path = tmp_path / "vendor-binding.json"
    path.write_text(json.dumps(VENDOR_BINDING), encoding="utf-8")
    monkeypatch.setenv("APIS_VENDOR_BINDING_FILE", str(path))
    return VENDOR_BINDING


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


# ── lean local prompt (ateles task ent_71387d9c1d1d3d1eef9ecc01) ──────────


def test_lean_prompt_is_tiny_regardless_of_work_class():
    # Regression bound: the full frontier prompt this replaces measured
    # ~44K tokens (132K chars) for one role. Every mechanical work class's
    # lean prompt must stay a couple of orders of magnitude smaller so it
    # leaves the 32K local ceiling almost entirely for tool output.
    for work_class in local_provider.MECHANICAL_WORK_CLASSES:
        prompt = local_provider.build_lean_prompt(work_class)
        assert len(prompt) < 2000, f"{work_class}: lean prompt is {len(prompt)} chars"


def test_lean_prompt_carries_no_agent_definition_or_policy_content():
    prompt = local_provider.build_lean_prompt("worktree_hygiene")
    # None of the frontier-only scaffolding this replaces may leak in: no
    # agent_policy rendering, no SKILL.md/agent_definition mirror content.
    for marker in ("Active agent policies", "entity_type: agent_definition", "gate_status"):  # vocab-ok: asserts the retired name is ABSENT
        assert marker not in prompt


def test_lean_prompt_states_the_work_classs_own_hard_rule():
    prompt = local_provider.build_lean_prompt("worktree_hygiene")
    assert "prunable" in prompt
    prompt = local_provider.build_lean_prompt("rebase")
    assert "rebase" in prompt.lower()


def test_lean_prompt_with_no_work_class_is_just_the_role_summary():
    assert local_provider.build_lean_prompt(None) == local_provider.LEAN_ROLE_SUMMARY


def test_lean_hard_rules_unknown_class_returns_empty():
    assert local_provider.lean_hard_rules("not_a_real_class") == ""
    assert local_provider.lean_hard_rules(None) == ""


def test_every_mechanical_class_has_lean_hard_rules():
    # Drift guard: MECHANICAL_WORK_CLASSES and _LEAN_HARD_RULES are two
    # independently hand-maintained structures keyed by the same strings.
    # Without this, a new mechanical class silently gets a lean prompt with
    # NO task-specific guidance (just the bare role summary) and nothing
    # fails — this test is what fails instead.
    missing = local_provider.MECHANICAL_WORK_CLASSES - local_provider._LEAN_HARD_RULES.keys()
    assert not missing, f"mechanical work class(es) with no lean hard rules: {missing}"


def test_every_mechanical_class_is_accounted_for_in_postcondition_checks():
    # Same drift guard, for post-condition coverage: every mechanical work
    # class must be EITHER in _POSTCONDITION_CHECKS (has a ground-truth
    # checker) OR explicitly named as not-yet-covered. A class in neither
    # set is a silent, un-tracked verification gap — exactly the shape of
    # the regression this module exists to fix, just for a different class.
    covered = set(local_provider._POSTCONDITION_CHECKS)
    declared_uncovered = local_provider._MECHANICAL_CLASSES_WITHOUT_POSTCONDITION_CHECK
    accounted_for = covered | declared_uncovered
    missing = local_provider.MECHANICAL_WORK_CLASSES - accounted_for
    assert not missing, (
        f"mechanical work class(es) neither checked nor declared uncovered: {missing}"
    )
    overlap = covered & declared_uncovered
    assert not overlap, f"class(es) both checked AND declared uncovered: {overlap}"


# ── post-condition checks ───────────────────────────────────────────────────


def test_verify_worktree_hygiene_passes_on_matching_count(tmp_path):
    with patch(
        "local_provider._git_porcelain_prunable_count", return_value=3
    ):
        assert local_provider.verify_worktree_hygiene(
            "I found 3 prunable worktrees.\nprunable_count: 3", cwd=str(tmp_path)
        ) is None


def test_verify_worktree_hygiene_fails_red_on_wrong_count(tmp_path):
    # This is the exact shape of the regression this checker exists to catch:
    # a confident, well-formed answer that is simply wrong (7 claimed vs. 56
    # true prunable worktrees — ateles task ent_71387d9c1d1d3d1eef9ecc01).
    with patch(
        "local_provider._git_porcelain_prunable_count", return_value=56
    ):
        failure = local_provider.verify_worktree_hygiene(
            "prunable_count: 7", cwd=str(tmp_path)
        )
    assert failure and "7" in failure and "56" in failure


def test_verify_worktree_hygiene_uses_the_final_stated_count(tmp_path):
    # A model that reasons out loud and self-corrects states its actual
    # answer LAST. Checking the first number would validate against an
    # interim count the model itself abandoned.
    with patch("local_provider._git_porcelain_prunable_count", return_value=9):
        assert local_provider.verify_worktree_hygiene(
            "prunable_count: 3\n\nWait, let me recount.\nprunable_count: 9",
            cwd=str(tmp_path),
        ) is None
        failure = local_provider.verify_worktree_hygiene(
            "prunable_count: 9\n\nWait, let me recount.\nprunable_count: 3",
            cwd=str(tmp_path),
        )
        assert failure and "claimed prunable_count=3" in failure


def test_verify_worktree_hygiene_fails_red_on_no_parseable_count(tmp_path):
    with patch(
        "local_provider._git_porcelain_prunable_count", return_value=5
    ):
        failure = local_provider.verify_worktree_hygiene(
            "There are some prunable worktrees.", cwd=str(tmp_path)
        )
    assert failure and "no parseable" in failure


def test_verify_worktree_hygiene_abstains_when_git_unavailable(tmp_path):
    with patch("local_provider._git_porcelain_prunable_count", return_value=None):
        # Whatever the model said, a checker with no ground truth must not
        # manufacture a failure.
        assert local_provider.verify_worktree_hygiene(
            "prunable_count: 999", cwd=str(tmp_path)
        ) is None


def test_git_porcelain_prunable_count_matches_real_git_output(tmp_path):
    """Ground truth against REAL git, not a stub: create N prunable worktrees
    (a worktree registration whose gitdir has been deleted) in a throwaway
    repo, and confirm the count matches exactly."""
    import subprocess as sp

    repo = tmp_path / "repo"
    repo.mkdir()
    sp.run(["git", "init", "-q"], cwd=repo, check=True)
    sp.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    sp.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "f.txt").write_text("x")
    sp.run(["git", "add", "."], cwd=repo, check=True)
    sp.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)

    n_prunable = 3
    for i in range(n_prunable):
        wt = tmp_path / f"wt{i}"
        sp.run(
            ["git", "worktree", "add", "-q", "-b", f"branch{i}", str(wt)],
            cwd=repo, check=True,
        )
        # Delete the worktree directory without `git worktree remove`, which
        # is exactly what makes git report it `prunable`.
        import shutil as sh

        sh.rmtree(wt)

    assert local_provider._git_porcelain_prunable_count(str(repo)) == n_prunable


def test_verify_postcondition_no_checker_for_class_returns_none(tmp_path):
    assert local_provider.verify_postcondition("rebase", "anything", cwd=str(tmp_path)) is None
    assert local_provider.verify_postcondition(None, "anything", cwd=str(tmp_path)) is None


def test_verify_postcondition_swallows_checker_exceptions(tmp_path):
    with patch(
        "local_provider._POSTCONDITION_CHECKS",
        {"worktree_hygiene": lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))},
    ):
        assert local_provider.verify_postcondition(
            "worktree_hygiene", "prunable_count: 1", cwd=str(tmp_path)
        ) is None


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
    assert_env_keys_absent(
        env, "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_MODEL"
    )
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


def _stub_agent_def():
    """Empty prompt_markdown — simulates AgentLoader's load-failure fallback
    (lib/daemon_runtime/agent_loader.py's `_stub()`): a synthesized, plausible
    aauth_sub but no real role instructions ever loaded."""
    return AgentDefinition(name="cicada", tool_allowlist="Bash,Read,Edit",
                           aauth_sub="cicada@ateles-swarm")


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
    assert_env_keys_absent(env, "CLAUDE_CODE_OAUTH_TOKEN")
    done = [e for e in events if e["event_type"] == "subprocess" and e["success"] == "true"]
    assert done and done[0]["tool_name"] == f"{LOCAL}:cicada"
    fields = done[0]["usage"].as_event_fields()
    assert fields["provider"] == LOCAL and fields["model"] == "qwen3-coder-ollama"


def test_tiered_mechanical_work_still_runs_local(tmp_path, monkeypatch):
    """Model tiering must not push mechanical work off claude-local: a
    vendor_binding with no claude-local entry (claude-local takes its model
    from its own config, not vendor_binding) must neither refuse the local
    attempt nor replace the local model with a frontier one. The frontier claude
    fallback is bound, so a refusal would show up as a failover to it."""
    _write_config(tmp_path)
    monkeypatch.setenv("APIS_ACTION_POLICY", '{"rebase": "mechanical"}')
    monkeypatch.setenv("APIS_ACTION_POLICY_FILE", str(tmp_path / "no-policy.json"))
    monkeypatch.setenv(
        "APIS_VENDOR_BINDING", '{"claude": {"mechanical": "claude-haiku-4-5"}}'
    )
    monkeypatch.setenv("APIS_VENDOR_BINDING_FILE", str(tmp_path / "no-binding.json"))
    spawns, events = _Spawns(local_reply=(0, b"rebased", b"")), []
    result = _run(spawns, events, work_class="rebase", action_class="rebase")

    assert result.ok and result.provider == LOCAL
    assert result.attempted_providers == (LOCAL,)
    (cmd, _), = spawns.calls
    assert cmd[cmd.index("--model") + 1] == "qwen3-coder-ollama"
    assert "claude-haiku-4-5" not in cmd
    done = [e for e in events if e["event_type"] == "subprocess" and e["success"] == "true"]
    assert done and done[0]["resolved_tier"].tier == "mechanical"


def test_local_dispatch_uses_the_lean_prompt_not_agent_def_or_policy(tmp_path, monkeypatch):
    """The regression this whole change exists to fix: a claude-local dispatch
    must carry the tiny lean prompt (role + work-class hard rules) as its
    --system-prompt, never the agent_definition.prompt_markdown ("You are
    Cicada.", from _agent_def()) or a live-policy rendering. A frontier
    dispatch is untouched — this only asserts the local branch."""
    _write_config(tmp_path)
    spawns, events = _Spawns(local_reply=(0, b"rebased", b"")), []
    result = _run(spawns, events, work_class="rebase")
    assert result.ok and result.provider == LOCAL

    (cmd, _), = spawns.calls
    system_prompt = cmd[cmd.index("--system-prompt") + 1]
    assert system_prompt == local_provider.build_lean_prompt("rebase")
    assert "You are Cicada." not in system_prompt  # agent_def.prompt_markdown
    assert "Active agent policies" not in system_prompt  # policy_prompt
    assert len(system_prompt) < 2000


def test_local_dispatch_fails_over_on_wrong_worktree_hygiene_answer(tmp_path, fallback_bound):
    """The exact regression: a local run that exits 0 with a plausible but
    WRONG count must not be accepted as ok — it must fail over to frontier,
    same as any other local_run_failed, without cooling local down (a wrong
    answer says nothing about endpoint health)."""
    _write_config(tmp_path, {**CONFIG, "eligible_work_classes": ["worktree_hygiene"]})
    spawns, events = _Spawns(local_reply=(0, b"prunable_count: 7", b"")), []
    with patch("local_provider._git_porcelain_prunable_count", return_value=56):
        result = _run(spawns, events, work_class="worktree_hygiene")

    assert result.provider == "claude"  # fell over
    assert result.attempted_providers == (LOCAL, "claude")
    failover, = [e for e in events if e["event_type"] == "provider_failover"]
    assert "failover_reason=local_run_failed" in failover["output_summary"]
    assert LOCAL not in harness_router.cooling_providers()


def test_local_dispatch_accepts_correct_worktree_hygiene_answer(tmp_path):
    _write_config(tmp_path, {**CONFIG, "eligible_work_classes": ["worktree_hygiene"]})
    spawns, events = _Spawns(local_reply=(0, b"prunable_count: 56", b"")), []
    with patch("local_provider._git_porcelain_prunable_count", return_value=56):
        result = _run(spawns, events, work_class="worktree_hygiene")

    assert result.ok and result.provider == LOCAL


def test_local_dispatch_with_stub_agent_def_is_reported_degraded(tmp_path, monkeypatch):
    """A local dispatch for a role whose agent_definition failed to load
    (stub: empty prompt_markdown, synthesized aauth_sub) must be treated as
    DEGRADED exactly like a frontier dispatch would be — not silently treated
    as a healthy load just because the lean prompt never reads
    prompt_markdown in the first place. Regression: `degraded` was briefly
    hardcoded False for every claude-local run, which would have (a) skipped
    the undefined-role alert path and (b) let a stub's synthesized aauth_sub
    receive AAuth signing-key injection."""
    _write_config(tmp_path)
    monkeypatch.setenv("ATELES_PRIVATE_KEYS_DIR", str(tmp_path / "keys"))
    # A JWK IS present at the expected path — if `degraded` were wrongly
    # False here, signing vars would be injected for a role with no real
    # loaded definition.
    (tmp_path / "keys").mkdir()
    (tmp_path / "keys" / "cicada.jwk.json").write_text("{}")

    loader = MagicMock()
    loader.return_value.load.return_value = _stub_agent_def()
    spawns, events = _Spawns(local_reply=(0, b"result", b"")), []
    with (
        patch("skill_runner.AgentLoader", loader),
        patch("skill_runner.CLAUDE_BIN", "/bin/claude"),
        patch("skill_runner.ATELES_REPO", _REPO_ROOT),
        patch("skill_runner.CODEX_BIN", None),
        patch("skill_runner.CURSOR_BIN", None),
        patch("skill_runner._write_harness_event", side_effect=lambda **kw: events.append(kw)),
        patch("asyncio.create_subprocess_exec", side_effect=spawns.spawn),
    ):
        result = asyncio.run(
            skill_runner.run_skill(
                "cicada", "rebase onto main", role="cicada",
                task_entity_id="ent_task", work_class="rebase",
            )
        )

    assert result.ok and result.provider == LOCAL
    (_, env), = spawns.calls
    assert_env_keys_absent(
        env,
        "NEOTOMA_AAUTH_PRIVATE_JWK_PATH",
        why="a stub agent_def must not receive AAuth signing-key injection on the local path",
    )
    degraded_events = [e for e in events if e.get("output_summary") == "degraded_generic_subagent"]
    assert degraded_events, (
        "a stub agent_def on the local path must still write the degraded_generic_subagent "
        "harness_event, same as the frontier path"
    )


def test_local_failure_falls_over_to_frontier_and_records_why(tmp_path, fallback_bound):
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


def test_over_ceiling_prompt_is_refused_before_launch_and_falls_over(tmp_path, fallback_bound):
    _write_config(tmp_path, {**CONFIG, "context_ceiling_tokens": 8000})
    spawns, events = _Spawns(local_reply=(0, b"should not run", b"")), []
    result = _run(spawns, events, prompt="x" * 60_000, work_class="rebase")

    assert result.ok and result.provider == "claude"
    assert len(spawns.calls) == 1 and "--settings" not in spawns.calls[0][0]
    failover, = [e for e in events if e["event_type"] == "provider_failover"]
    assert "failover_reason=context_ceiling" in failover["output_summary"]
    # A prompt that is too long says nothing about the endpoint's health.
    assert LOCAL not in harness_router.cooling_providers()


def test_missing_repo_path_env_refuses_local_launch_and_falls_over(tmp_path, monkeypatch, fallback_bound):
    """ATELES_REPO_PATH unset must refuse the claude-local launch rather than
    silently reading guards from the ATELES_REPO fallback (~/repos/ateles, a
    possibly-stale shared clone) — the launch stays safe (falls over to
    frontier) but must not do so silently. Before this check existed,
    `_run`'s `patch("skill_runner.ATELES_REPO", _REPO_ROOT)` alone was enough
    to bind real guards regardless of the env var, so this assertion fails
    red against the pre-change code (attempted_providers includes LOCAL)."""
    _write_config(tmp_path)
    monkeypatch.delenv("ATELES_REPO_PATH", raising=False)
    spawns, events = _Spawns(local_reply=(0, b"should not run", b"")), []
    result = _run(spawns, events, work_class="rebase")

    # The local attempt is refused before any subprocess launches (no local
    # command ever reaches _Spawns), and the run falls over to frontier —
    # same shape as any other guards_unavailable refusal.
    assert result.ok and result.provider == "claude"
    assert result.attempted_providers == (LOCAL, "claude")
    assert all("--settings" not in cmd for cmd, _ in spawns.calls)
    failover, = [e for e in events if e["event_type"] == "provider_failover"]
    assert "failover_reason=guards_unavailable" in failover["output_summary"]
    assert LOCAL in harness_router.cooling_providers()


def test_repo_path_env_present_logs_which_path_was_read(tmp_path, monkeypatch, caplog):
    """The success path names the exact ATELES_REPO_PATH it bound guards
    from, so a stale-checkout diagnosis does not require re-deriving it from
    the daemon's ambient environment after the fact."""
    _write_config(tmp_path)
    monkeypatch.setenv("ATELES_REPO_PATH", str(_REPO_ROOT))
    spawns, events = _Spawns(local_reply=(0, b"rebased", b"")), []
    with caplog.at_level("INFO"):
        result = _run(spawns, events, work_class="rebase")

    assert result.ok and result.provider == LOCAL
    assert any(
        "claude-local guards read from ATELES_REPO_PATH=" in rec.message
        and str(_REPO_ROOT) in rec.message
        for rec in caplog.records
    )


def test_guards_are_bound_from_the_env_var_not_the_frozen_module_constant(
    tmp_path, monkeypatch
):
    """The guards path passed to `write_guards_file` must trace to
    ATELES_REPO_PATH itself, not to `skill_runner.ATELES_REPO` (resolved
    once at import time from whatever the env var held THEN — a separate
    concern from SKILL.md resolution, which legitimately still uses
    `ATELES_REPO`). `_run` unconditionally patches `ATELES_REPO` to the real
    repo root for every other test in this file; here it is patched to a
    distinct bogus path instead, while ATELES_REPO_PATH (env, read fresh at
    call time) points at the real repo. Before this fix, `write_guards_file`
    was called with `ATELES_REPO` directly — this spy would have observed
    the bogus path, not the real one."""
    _write_config(tmp_path)
    monkeypatch.setenv("ATELES_REPO_PATH", str(_REPO_ROOT))
    # ATELES_REPO also resolves the SKILL.md path (skill_runner.py:1452),
    # unconditionally, before provider selection — a distinct concern from
    # guard binding that this fix does not touch. So `bogus_repo` needs a
    # real SKILL.md (skill loading must succeed) but NO .claude/settings.json
    # (so write_guards_file would fail if it were ever called against this
    # path instead of the env var).
    bogus_repo = tmp_path / "not-a-real-checkout"
    bogus_skill_dir = bogus_repo / ".claude" / "skills" / "cicada"
    bogus_skill_dir.mkdir(parents=True)
    (bogus_skill_dir / "SKILL.md").write_text("# cicada\n", encoding="utf-8")
    spawns, events = _Spawns(local_reply=(0, b"rebased", b"")), []

    loader = MagicMock()
    loader.return_value.load.return_value = _agent_def()
    real_write_guards_file = local_provider.write_guards_file
    seen_repo_roots: list = []

    def spy_write_guards_file(repo_root):
        seen_repo_roots.append(repo_root)
        return real_write_guards_file(repo_root)

    with (
        patch("skill_runner.AgentLoader", loader),
        patch("skill_runner.CLAUDE_BIN", "/bin/claude"),
        patch("skill_runner.ATELES_REPO", bogus_repo),  # deliberately wrong
        patch("skill_runner.CODEX_BIN", None),
        patch("skill_runner.CURSOR_BIN", None),
        patch("skill_runner._write_harness_event", side_effect=lambda **kw: events.append(kw)),
        patch("local_provider.write_guards_file", side_effect=spy_write_guards_file),
        patch("asyncio.create_subprocess_exec", side_effect=spawns.spawn),
    ):
        result = asyncio.run(
            skill_runner.run_skill(
                "cicada", "rebase onto main", role="cicada",
                task_entity_id="ent_task", work_class="rebase",
            )
        )

    assert result.ok and result.provider == LOCAL
    assert seen_repo_roots == [Path(str(_REPO_ROOT))]
    assert bogus_repo not in seen_repo_roots


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


def test_github_delivery_never_routes_local_even_for_an_eligible_class(tmp_path, monkeypatch):
    """local_provider.build_lean_prompt has no SWARM_GITHUB_CONTRACT /
    SWARM_PRIOR_ART_CONTRACT — a GitHub-delivery dispatch must stay off the
    local path even when its work_class is otherwise eligible."""
    _write_config(tmp_path, {**CONFIG, "eligible_work_classes": ["ci_log_triage"]})
    monkeypatch.setenv("APIS_HARNESS_PROVIDERS", "claude")
    spawns, events = _Spawns(local_reply=(0, b"local", b"")), []
    result = _run(
        spawns, events, work_class="ci_log_triage",
        include_github_contract=True, github_token="tok",
    )
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


def test_ordinary_local_task_failure_falls_over_without_cooling_local(tmp_path, fallback_bound):
    """A failed task (e.g. a merge conflict) is not evidence the endpoint is down."""
    _write_config(tmp_path)
    spawns, events = _Spawns(local_reply=(1, b"", b"CONFLICT (content): merge conflict in f.txt")), []
    result = _run(spawns, events, work_class="rebase")
    assert result.provider == "claude" and result.attempted_providers == (LOCAL, "claude")
    failover, = [e for e in events if e["event_type"] == "provider_failover"]
    assert "failover_reason=local_run_failed" in failover["output_summary"]
    assert LOCAL not in harness_router.cooling_providers()


# ── never thrash: the child is given caps and no compaction ────────────────
# Observed 2026-09-29: a rebase dispatch on the 32K local window ran 158s and
# aborted with "Autocompact is thrashing". The probe that reproduced it took
# ~80s; with these settings the same probe no longer thrashes.


def test_local_child_settings_disable_compaction_and_cap_tool_output():
    child: dict[str, str] = {}
    cfg = _cfg()
    local_provider.apply_env(child, cfg)
    cap = local_provider.tool_output_cap(cfg)
    assert child["DISABLE_AUTO_COMPACT"] == "1"
    assert cap == 32768 // 10
    assert child["CLAUDE_CODE_FILE_READ_MAX_OUTPUT_TOKENS"] == str(cap)
    assert child["BASH_MAX_OUTPUT_LENGTH"] == str(int(cap * cfg.chars_per_token))
    # The configured cap wins over the derived one.
    child = {}
    local_provider.apply_env(child, _cfg(tool_output_cap_tokens=1000))
    assert child["CLAUDE_CODE_FILE_READ_MAX_OUTPUT_TOKENS"] == "1000"


def test_the_spawned_local_child_receives_the_thrash_guards(tmp_path):
    _write_config(tmp_path)
    spawns, events = _Spawns(local_reply=(0, b"rebased", b"")), []
    result = _run(spawns, events, work_class="rebase")
    assert result.ok
    (_, child_env), = spawns.calls
    assert child_env["DISABLE_AUTO_COMPACT"] == "1"
    assert int(child_env["BASH_MAX_OUTPUT_LENGTH"]) < 32768


# ── local failures carry a specific reason ──────────────────────────────────

THRASH_STDOUT = (
    b"Autocompact is thrashing: the context refilled to the limit within 3 turns "
    b"of the previous compact, 3 times in a row."
)
UNRECOGNIZED = b'[claude-code:unrecognized_model] {"model":"qwen3-coder-ollama","query_source":"sdk"}'


def test_thrash_signature_is_a_local_run_failure_with_a_specific_reason():
    assert local_provider.classify_failure(THRASH_STDOUT.decode()) == "local_run_failed"
    assert local_provider.describe_failure(THRASH_STDOUT.decode(), UNRECOGNIZED.decode()) == (
        "local_run_failed:autocompact_thrash"
    )


def test_unrecognized_model_is_only_blamed_when_nothing_else_explains_the_failure():
    assert local_provider.describe_failure("", UNRECOGNIZED.decode()) == (
        "local_run_failed:unrecognized_model"
    )
    # A more specific cause wins over the note.
    assert local_provider.describe_failure("Prompt is too long", UNRECOGNIZED.decode()) == "context_ceiling"
    assert local_provider.describe_failure("the tests still fail") == "local_run_failed"


def test_unrecognized_model_note_on_a_successful_run_is_not_a_failure(tmp_path):
    """The CLI prints the notice on healthy runs too (verified live 2026-09-29)."""
    _write_config(tmp_path)
    spawns, events = _Spawns(local_reply=(0, b"rebased", UNRECOGNIZED)), []
    result = _run(spawns, events, work_class="rebase")
    assert result.ok and result.provider == LOCAL and result.local_failure == ""
    assert not [e for e in events if e["event_type"] == "provider_failover"]


def test_thrash_run_is_recorded_with_its_reason(tmp_path, fallback_bound):
    _write_config(tmp_path)
    spawns, events = _Spawns(local_reply=(1, THRASH_STDOUT, UNRECOGNIZED)), []
    result = _run(spawns, events, work_class="rebase")
    failover, = [e for e in events if e["event_type"] == "provider_failover"]
    assert "failover_reason=local_run_failed:autocompact_thrash" in failover["output_summary"]
    assert result.local_failure == "local_run_failed:autocompact_thrash"
    # A thrash says nothing about the endpoint's health.
    assert LOCAL not in harness_router.cooling_providers()


# ── no silent frontier fallback ─────────────────────────────────────────────


def test_fallback_tier_is_the_cheapest_frontier_tier():
    import model_tiering
    assert skill_runner.LOCAL_FALLBACK_TIER == model_tiering.TIERS[model_tiering.TIERS.index("local") + 1]


def test_fallback_models_come_only_from_the_bound_cheapest_tier(tmp_path, monkeypatch):
    # No binding at all: nothing may be a fallback (not the ambient default).
    assert skill_runner._local_fallback_models(["claude", "codex"]) == {}
    path = tmp_path / "vb.json"
    monkeypatch.setenv("APIS_VENDOR_BINDING_FILE", str(path))
    path.write_text(json.dumps({"claude": {"mechanical": "haiku"}, "codex": {"top": "big"}}))
    # codex is bound, but not at the cheapest tier: not a fallback.
    assert skill_runner._local_fallback_models(["claude", "codex", "cursor"]) == {"claude": "haiku"}


def test_failed_local_run_is_refused_not_replayed_on_the_frontier_default(tmp_path):
    _write_config(tmp_path)  # no vendor_binding: no cheapest-tier model to fall back to
    spawns, events = _Spawns(local_reply=(1, THRASH_STDOUT, UNRECOGNIZED)), []
    result = _run(spawns, events, work_class="rebase")

    assert result.ok is False
    assert result.attempted_providers == (LOCAL,)
    assert len(spawns.calls) == 1 and "--settings" in spawns.calls[0][0], "no frontier spawn"
    assert "frontier fallback was refused" in result.error
    assert result.local_failure == "local_run_failed:autocompact_thrash"
    failover, = [e for e in events if e["event_type"] == "provider_failover"]
    assert "next_provider=none" in failover["output_summary"]
    assert "frontier_fallback=refused" in failover["output_summary"]


def test_fallback_runs_only_on_the_named_model_and_still_records_the_local_failure(tmp_path, fallback_bound):
    _write_config(tmp_path)
    spawns, events = _Spawns(local_reply=(1, THRASH_STDOUT, b"")), []
    result = _run(spawns, events, work_class="rebase")

    assert result.ok and result.provider == "claude"
    frontier_cmd = spawns.calls[1][0]
    assert frontier_cmd[frontier_cmd.index("--model") + 1] == "haiku"
    # Success on the frontier must not read as a local success.
    assert result.local_failure == "local_run_failed:autocompact_thrash"


def test_fallback_to_a_provider_bound_only_at_a_higher_tier_is_refused(tmp_path, monkeypatch):
    _write_config(tmp_path)
    path = tmp_path / "vb-high.json"
    path.write_text(json.dumps({"claude": {"top": "opus"}}))
    monkeypatch.setenv("APIS_VENDOR_BINDING_FILE", str(path))
    spawns, events = _Spawns(local_reply=(1, THRASH_STDOUT, b"")), []
    result = _run(spawns, events, work_class="rebase")
    assert result.ok is False and result.attempted_providers == (LOCAL,)
    assert len(spawns.calls) == 1


def test_an_explicit_caller_model_is_used_for_the_fallback_as_given(tmp_path):
    _write_config(tmp_path)  # no binding, but the caller named its own model
    spawns, events = _Spawns(local_reply=(1, THRASH_STDOUT, b"")), []
    result = _run(spawns, events, work_class="rebase", model="sonnet")
    assert result.ok and result.provider == "claude"
    frontier = spawns.calls[1][0]
    assert frontier[frontier.index("--model") + 1] == "sonnet"


def test_cooling_local_takes_the_same_policy_instead_of_a_silent_frontier_run(tmp_path, monkeypatch):
    _write_config(tmp_path)
    harness_router.cool_down(LOCAL)
    spawns, events = _Spawns(), []
    refused = _run(spawns, events, work_class="rebase")
    assert refused.ok is False and spawns.calls == []
    assert refused.local_failure.startswith("endpoint_unreachable")

    bound = tmp_path / "vb.json"
    bound.write_text(json.dumps(VENDOR_BINDING))
    monkeypatch.setenv("APIS_VENDOR_BINDING_FILE", str(bound))
    spawns, events = _Spawns(), []
    allowed = _run(spawns, events, work_class="rebase")
    assert allowed.ok and allowed.provider == "claude"
    (cmd, _), = spawns.calls
    assert cmd[cmd.index("--model") + 1] == "haiku"


def test_frontier_only_work_is_untouched_by_the_fallback_policy(tmp_path):
    """No work_class: no local-first, no --model, no refusal — exactly as before."""
    _write_config(tmp_path)
    spawns, events = _Spawns(), []
    result = _run(spawns, events)
    assert result.ok and result.provider == "claude" and result.local_failure == ""
    (cmd, _), = spawns.calls
    assert "--model" not in cmd


# ── rebase rules and the caller's intent agree ──────────────────────────────


def test_rebase_hard_rules_allow_a_merge_commit_for_a_pushed_branch():
    rules = local_provider.lean_hard_rules("rebase")
    assert "git merge --no-ff" in rules and "already pushed" in rules
    assert "git rebase <base>" in rules  # still the default
