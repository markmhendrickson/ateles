"""Candidate environment (credential-free), commands and output parsing.

Tests that check the environment print and compare NAMES only, never values.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import harness_lens_runner as hlr
from review_replay import candidate_runner as cr

# Names an ambient shell could plausibly hold. The values are fake and are never printed.
AMBIENT_SECRET_NAMES = (
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "GH_ENTERPRISE_TOKEN",
    "NEOTOMA_BEARER_TOKEN",
    "NEOTOMA_BASE_URL",
    "OPENROUTER_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "AWS_SECRET_ACCESS_KEY",
    "SSH_AUTH_SOCK",
    "SOPS_AGE_KEY",
    "OP_SERVICE_ACCOUNT_TOKEN",
    "FLY_API_TOKEN",
    "NPM_TOKEN",
    "TELEGRAM_BOT_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CODEX_HOME",
)


def _sandbox(tmp_path: Path):
    root = tmp_path / "home"
    (root / "tmp").mkdir(parents=True)
    return SimpleNamespace(
        root=root,
        env_extra={
            "ATELES_LOCAL_REVIEW_HOME": str(root),
            "TMPDIR": str(root / "tmp"),
            "TMP": str(root / "tmp"),
            "TEMP": str(root / "tmp"),
            "PATH": f"{root}/shim-bin:/usr/bin:/bin",
        },
    )


@pytest.fixture
def ambient(monkeypatch):
    for name in AMBIENT_SECRET_NAMES:
        monkeypatch.setenv(name, f"FAKE-{name}-VALUE")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("SOME_UNLISTED_API_KEY", "FAKE-UNLISTED")


def _leaked_names(env: dict[str, str]) -> list[str]:
    """Names whose VALUE carries the ambient fake marker (names only are returned)."""
    return sorted(k for k, v in env.items() if "FAKE-" in v)


@pytest.mark.parametrize(
    "kind,model", [("openrouter", "vendor/model"), ("ollama", "qwen3.6:35b")]
)
def test_candidate_env_carries_no_credential_of_any_kind(
    ambient, tmp_path, kind, model
):
    env = cr.build_child_env(
        kind, model, _sandbox(tmp_path), proxy_url="http://127.0.0.1:9"
    )
    print("env names:", sorted(env))  # names only
    assert set(env) <= cr.CHILD_ENV_ALLOWED_NAMES
    assert _leaked_names(env) == []
    for forbidden in AMBIENT_SECRET_NAMES + ("SOME_UNLISTED_API_KEY",):
        if forbidden not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
            assert forbidden not in env, forbidden
    assert env["ANTHROPIC_AUTH_TOKEN"] == cr.PLACEHOLDER_TOKEN
    assert env["ANTHROPIC_API_KEY"] == ""
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:9"
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env and "CODEX_HOME" not in env
    assert (
        env["GH_CONFIG_DIR"].startswith(str(tmp_path))
        and env["GIT_CONFIG_GLOBAL"] == "/dev/null"
    )


def test_final_allowlist_holds_even_if_the_underlying_builder_regresses(
    monkeypatch, tmp_path
):
    def leaky(env_extra, **kwargs):
        return {
            "PATH": "/usr/bin",
            "GH_TOKEN": "FAKE-x",
            "NEOTOMA_BEARER_TOKEN": "FAKE-y",
            "HOME": "/h",
        }

    monkeypatch.setattr(cr.skill_runner, "_subscription_only_env", leaky)
    child = cr.build_child_env("claude", None, _sandbox(tmp_path))
    assert sorted(child) == ["HOME", "PATH"]


def test_claude_baseline_env_keeps_only_its_subscription_capability(ambient, tmp_path):
    env = cr.build_child_env("claude", None, _sandbox(tmp_path))
    assert _leaked_names(env) == ["CLAUDE_CODE_OAUTH_TOKEN"]
    assert set(env) <= cr.CHILD_ENV_ALLOWED_NAMES
    assert (
        "ANTHROPIC_API_KEY" not in env
        and "GITHUB_TOKEN" not in env
        and "NEOTOMA_BEARER_TOKEN" not in env
    )


def test_codex_env_keeps_only_its_own_isolated_home(ambient, tmp_path):
    sb = _sandbox(tmp_path)
    sb.env_extra["CODEX_HOME"] = str(sb.root)
    env = cr.build_child_env("codex", None, sb)
    assert _leaked_names(env) == []
    assert env["CODEX_HOME"] == str(sb.root) and "CLAUDE_CODE_OAUTH_TOKEN" not in env


def test_local_candidate_requires_the_proxy(tmp_path):
    with pytest.raises(cr.CandidateError):
        cr.build_child_env("ollama", "m", _sandbox(tmp_path))


def test_the_endpoint_key_never_reaches_a_command_line(tmp_path):
    cmd = cr.claude_family_command(
        "/bin/claude",
        "vendor/model",
        guards_path=tmp_path / "g.json",
        empty_mcp_path=tmp_path / "m.json",
    )
    assert "--strict-mcp-config" in cmd and "--disable-slash-commands" in cmd
    assert cmd[cmd.index("--setting-sources") + 1] == ""
    assert cmd[cmd.index("--tools") + 1] == cr.DEFAULT_TOOLS
    assert "--dangerously-skip-permissions" not in cmd
    assert "sk-or" not in " ".join(cmd)


def test_codex_command_asks_for_events_and_runs_inside_the_outer_sandbox():
    cmd = cr.codex_command("/bin/codex", "gpt-x", "/work", "medium")
    assert cmd[:3] == ["/bin/codex", "exec", "--json"]
    assert cmd[cmd.index("--sandbox") + 1] == "danger-full-access"
    assert cmd[cmd.index("--model") + 1] == "gpt-x"
    assert 'model_reasoning_effort="medium"' in cmd and cmd[-1] == "-"


def test_provider_spec_parsing():
    assert hlr.parse_provider("ollama:qwen3.6:35b") == ("ollama", "qwen3.6:35b")
    assert hlr.parse_provider("openrouter:z-ai/glm-5.2") == (
        "openrouter",
        "z-ai/glm-5.2",
    )
    assert hlr.parse_provider("codex") == ("codex", None)
    assert hlr.parse_provider("codex:gpt-x") == ("codex", "gpt-x")
    for bad in ("ollama", "openrouter:", "nonsense", "nonsense:x"):
        with pytest.raises(ValueError):
            hlr.parse_provider(bad)


CLAUDE_STREAM = "\n".join(
    json.dumps(e)
    for e in [
        {"type": "system", "subtype": "init", "model": "m-1"},
        {
            "type": "assistant",
            "message": {"id": "a1", "content": [{"type": "tool_use", "name": "Bash"}]},
        },
        {
            "type": "user",
            "message": {
                "content": [
                    {"type": "tool_result", "is_error": True, "content": "denied"}
                ]
            },
        },
        {
            "type": "assistant",
            "message": {"id": "a2", "content": [{"type": "tool_use", "name": "Read"}]},
        },
        {
            "type": "user",
            "message": {"content": [{"type": "tool_result", "content": "ok"}]},
        },
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "FINAL TEXT",
            "num_turns": 3,
            "total_cost_usd": 0.5,
            "usage": {
                "input_tokens": 10,
                "output_tokens": 20,
                "cache_read_input_tokens": 30,
                "cache_creation_input_tokens": 40,
            },
        },
    ]
)


def test_claude_stream_parsing_counts_turns_tools_errors_and_usage():
    m = cr.parse_claude_stream(CLAUDE_STREAM + "\nnot json at all")
    assert (m.final_text, m.turns, m.tool_calls, m.tool_errors, m.model) == (
        "FINAL TEXT",
        3,
        2,
        1,
        "m-1",
    )
    assert (
        m.input_tokens,
        m.output_tokens,
        m.cache_read_tokens,
        m.cache_write_tokens,
    ) == (10, 20, 30, 40)
    assert m.reported_cost_usd == 0.5 and not m.is_error


def test_claude_stream_error_result_is_flagged():
    m = cr.parse_claude_stream(
        json.dumps({"type": "result", "is_error": True, "result": "API Error: boom"})
    )
    assert m.is_error and "boom" in m.error_text and m.input_tokens is None


CODEX_STREAM = "\n".join(
    json.dumps(e)
    for e in [
        {"type": "thread.started"},
        {"type": "turn.started"},
        {
            "type": "item.completed",
            "item": {"type": "command_execution", "exit_code": 0},
        },
        {
            "type": "item.completed",
            "item": {"type": "command_execution", "exit_code": 1},
        },
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "the verdict"},
        },
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 1000,
                "cached_input_tokens": 600,
                "output_tokens": 50,
            },
        },
    ]
)


def test_codex_stream_parsing_reports_fresh_input_apart_from_cache():
    m = cr.parse_codex_stream(CODEX_STREAM)
    assert (m.final_text, m.turns, m.tool_calls, m.tool_errors) == (
        "the verdict",
        1,
        2,
        1,
    )
    assert (m.input_tokens, m.cache_read_tokens, m.output_tokens) == (400, 600, 50)


def test_unreported_usage_stays_none_not_zero():
    m = cr.parse_codex_stream(json.dumps({"type": "turn.started"}))
    assert m.input_tokens is None and m.output_tokens is None


def test_openrouter_key_comes_from_the_password_manager_and_is_not_cached_on_failure(
    monkeypatch,
):
    cr._KEY_CACHE.clear()

    def fail(*a, **k):
        return SimpleNamespace(returncode=1, stdout="", stderr="locked")

    monkeypatch.setattr(cr.subprocess, "run", fail)
    with pytest.raises(cr.CandidateError, match="unlocked"):
        cr.load_openrouter_key("op://x/y/z")
    assert cr._KEY_CACHE == {}


def test_a_local_window_is_a_model_tag_built_once_from_the_base(monkeypatch):
    calls = []

    class Resp:
        def __init__(self, payload):
            self.payload = payload

        def read(self):
            return json.dumps(self.payload).encode()

    state = {"models": []}

    def fake_urlopen(req, timeout=0):
        url = req if isinstance(req, str) else req.full_url
        if url.endswith("/api/tags"):
            return Resp(state)
        calls.append(json.loads(req.data))
        state["models"].append({"name": calls[-1]["model"] + ":latest"})
        return Resp({"status": "success"})

    monkeypatch.setattr(cr.urllib.request, "urlopen", fake_urlopen)
    name = cr.ensure_ollama_context_model("qwen3.6:35b", 131072)
    assert name == "replay-qwen3.6-35b-ctx131072"
    assert calls == [
        {
            "model": name,
            "from": "qwen3.6:35b",
            "parameters": {"num_ctx": 131072},
            "stream": False,
        }
    ]
    assert (
        cr.ensure_ollama_context_model("qwen3.6:35b", 131072) == name
        and len(calls) == 1
    )


def test_window_suffix_is_stripped_before_the_local_server_sees_the_model():
    assert cr.strip_window_suffix({"model": "tag[1m]", "x": 1}) == {"model": "tag", "x": 1}
    assert cr.strip_window_suffix({"model": "tag"}) == {"model": "tag"}
    assert cr.force_thinking_off({"model": "m"})["thinking"] == {"type": "disabled"}
