"""Tests for claude_subprocess_env — OAuth-over-API-key env preference."""

from __future__ import annotations

from lib.daemon_runtime.claude_auth import claude_subprocess_env


def test_oauth_token_present_drops_api_key(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-xxx")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-yyy")
    env = claude_subprocess_env()
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-xxx"
    assert "ANTHROPIC_API_KEY" not in env, (
        "API key must be removed when OAuth token is set"
    )


def test_no_oauth_token_leaves_api_key(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-yyy")
    env = claude_subprocess_env()
    assert env["ANTHROPIC_API_KEY"] == "sk-ant-api03-yyy", (
        "no token -> API key stays (graceful fallback)"
    )


def test_blank_oauth_token_leaves_api_key(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "   ")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-yyy")
    env = claude_subprocess_env()
    assert env["ANTHROPIC_API_KEY"] == "sk-ant-api03-yyy", (
        "blank token must not count as present"
    )


def test_extra_overrides_are_applied(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-xxx")
    env = claude_subprocess_env({"GITHUB_TOKEN": "ghp_zzz"})
    assert env["GITHUB_TOKEN"] == "ghp_zzz"
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-xxx"


def test_extra_can_supply_the_oauth_token(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-yyy")
    env = claude_subprocess_env({"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-fromextra"})
    assert "ANTHROPIC_API_KEY" not in env
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-fromextra"
