"""A dispatched agent process gets an allowlisted environment and a safe cwd.

Finding: dispatched agent processes inherited every credential the daemon had
loaded (the daemon reads the operator's whole dotenv into its own environment),
and ran with the filesystem root as their working directory when the caller
named none. Now a child starts from nothing and receives only the names in
``skill_runner.CHILD_ENV_ALLOWLIST``; a delivery child additionally gets the one
per-task token it is handed; and a child never runs in ``/`` or the home
directory (a dispatch with no directory gets a fresh scratch one).

These run a stand-in CLI as a real process and read what it actually saw.
"""

from __future__ import annotations

import asyncio
import json
import os
import plistlib
import sys
from pathlib import Path

import pytest

_DAEMON_DIR = Path(__file__).resolve().parent
if str(_DAEMON_DIR) not in sys.path:
    sys.path.insert(0, str(_DAEMON_DIR))

import skill_runner  # noqa: E402
from lib.daemon_runtime import AgentDefinition  # noqa: E402

# Canary values: none of these may ever reach a child that was not handed them.
DAEMON_SECRETS = {
    "GITHUB_TOKEN": "canary-daemon-github-token",
    "GH_TOKEN": "canary-daemon-gh-token",
    "ATELES_AGENT_PAT": "canary-agent-pat",
    "NEOTOMA_AGENT_PAT": "canary-other-agent-pat",
    "WISE_API_TOKEN": "canary-wise-token",
    "WISE_PROFILE_ID": "canary-wise-profile",
    "TELEGRAM_BOT_TOKEN": "canary-telegram-token",
    "SOME_FUTURE_SERVICE_SECRET": "canary-future-secret",
    "SSH_AUTH_SOCK": "/canary/agent.sock",
    "NEOTOMA_BEARER_TOKEN_PROD": "canary-prod-bearer",
    "PAVO_NEOTOMA_TOKEN": "canary-other-role-neotoma-token",
}
ROLE_TOKEN = "canary-lanius-own-token"
SHARED_BEARER = "canary-shared-bearer"
SCOPED = "canary-scoped-delivery-token"

# Records everything a child can observe about its own start.
_RECORDER = r"""#!{python}
import json, os, sys
from pathlib import Path
cwd = os.getcwd()
print(json.dumps({{
    "env": dict(os.environ),
    "cwd": os.path.realpath(cwd),
    "argv": sys.argv[1:],
    "cwd_entries": sorted(os.listdir(cwd)),
}}))
Path({marker!r}).write_text("1")
"""


@pytest.fixture
def operator_home(monkeypatch, tmp_path):
    home = tmp_path / "operator"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "auth.json").write_text("{}")
    (home / "Library" / "Keychains").mkdir(parents=True)
    (home / ".gitconfig").write_text("[user]\n\tname = Operator\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setenv("NEOTOMA_BASE_URL", "https://neotoma.example.test")
    monkeypatch.setenv("LANIUS_NEOTOMA_TOKEN", ROLE_TOKEN)
    monkeypatch.setenv("NEOTOMA_BEARER_TOKEN", SHARED_BEARER)
    monkeypatch.setenv("LANG", "C.UTF-8")
    monkeypatch.setenv("TERM", "xterm")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "canary-subscription-login")
    for name, value in DAEMON_SECRETS.items():
        monkeypatch.setenv(name, value)
    return home


@pytest.fixture
def child(monkeypatch, tmp_path, operator_home):
    """Wire a stand-in CLI for every provider; returns ``run`` -> (result, seen)."""
    marker = tmp_path / "ran"
    stand_in = tmp_path / "stand-in"
    stand_in.write_text(
        _RECORDER.format(python=sys.executable, marker=str(marker)), encoding="utf-8"
    )
    stand_in.chmod(0o755)
    for name in ("CLAUDE_BIN", "CODEX_BIN", "CURSOR_BIN"):
        monkeypatch.setattr(skill_runner, name, str(stand_in))
    skills = tmp_path / ".claude" / "skills" / "lanius"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text("# lanius\n")
    monkeypatch.setattr(skill_runner, "ATELES_REPO", tmp_path)
    monkeypatch.setattr(
        skill_runner,
        "_load_agent_def",
        lambda role, **kw: AgentDefinition(
            entity_id="e",
            name=role,
            prompt_markdown="You are lanius.",
            tool_allowlist="*",
            aauth_sub=f"{role}@ateles-swarm",
        ),
    )
    monkeypatch.setattr(
        skill_runner, "_load_active_policy_prompt", lambda role, **kw: ""
    )
    monkeypatch.setattr(skill_runner, "_write_harness_event", lambda **kw: None)
    import harness_router

    harness_router.reset_state()

    def run(provider, **kwargs):
        result = asyncio.run(
            skill_runner.run_skill(
                "lanius", "task", role="lanius", provider=provider, **kwargs
            )
        )
        seen = None
        if marker.exists():
            seen = json.loads(result.stdout.strip().splitlines()[-1])
        return result, seen

    run.marker = marker
    return run


PROVIDERS = ["claude", "codex", "cursor"]


# ── The environment a child gets ─────────────────────────────────────────────


@pytest.mark.parametrize("provider", PROVIDERS)
def test_a_non_delivery_child_holds_no_daemon_credential(child, provider):
    result, seen = child(provider)
    assert result.ok, result.error
    env = seen["env"]
    for name in ("GITHUB_TOKEN", "GH_TOKEN", "ATELES_AGENT_PAT", "WISE_API_TOKEN"):
        assert name not in env, name
    # Nothing the daemon held leaks under any name, and none of it by value.
    blob = json.dumps(env)
    for name, value in DAEMON_SECRETS.items():
        if name in ("PAVO_NEOTOMA_TOKEN", "NEOTOMA_BEARER_TOKEN_PROD"):
            assert name not in env, name
        assert value not in blob, name


@pytest.mark.parametrize("provider", PROVIDERS)
def test_an_unknown_variable_is_dropped(child, provider, monkeypatch):
    monkeypatch.setenv("TOTALLY_UNLISTED_SETTING", "some-value")
    monkeypatch.setenv("ANOTHER_UNLISTED_API_KEY", "some-key")
    _, seen = child(provider)
    assert "TOTALLY_UNLISTED_SETTING" not in seen["env"]
    assert "ANOTHER_UNLISTED_API_KEY" not in seen["env"]


def test_an_unlisted_name_is_dropped_by_the_builder_itself(monkeypatch):
    monkeypatch.setenv("SOME_FUTURE_VENDOR_TOKEN", "future")
    monkeypatch.setenv("WISE_API_TOKEN", "wise")
    built = skill_runner._subscription_only_env()
    assert "SOME_FUTURE_VENDOR_TOKEN" not in built and "WISE_API_TOKEN" not in built


@pytest.mark.parametrize("provider", PROVIDERS)
def test_a_delivery_child_gets_exactly_its_scoped_token(child, provider):
    result, seen = child(provider, include_github_contract=True, github_token=SCOPED)
    assert result.ok, result.error
    env = seen["env"]
    assert env["GITHUB_TOKEN"] == SCOPED and env["GH_TOKEN"] == SCOPED
    assert DAEMON_SECRETS["GITHUB_TOKEN"] not in json.dumps(env)
    assert DAEMON_SECRETS["GH_TOKEN"] not in json.dumps(env)
    assert "ATELES_AGENT_PAT" not in env and "WISE_API_TOKEN" not in env


def test_the_scoped_token_does_not_outlive_its_dispatch(child):
    child("codex", include_github_contract=True, github_token=SCOPED)
    child.marker.unlink()
    _, seen = child("codex")
    assert "GITHUB_TOKEN" not in seen["env"] and SCOPED not in json.dumps(seen["env"])


# What each provider's launcher needs to run: it must survive the filter.
_NEEDED = {
    "claude": {
        "PATH",
        "HOME",
        "LANG",
        "TERM",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "NEOTOMA_BASE_URL",
    },
    "codex": {"PATH", "HOME", "LANG", "TERM", "CODEX_HOME", "NEOTOMA_BASE_URL"},
    "cursor": {"PATH", "HOME", "LANG", "TERM", "CURSOR_CONFIG_DIR", "NEOTOMA_BASE_URL"},
}


@pytest.mark.parametrize("provider", PROVIDERS)
def test_each_provider_still_gets_what_it_needs(child, provider):
    result, seen = child(provider)
    assert result.ok, result.error
    env = seen["env"]
    missing = _NEEDED[provider] - set(env)
    assert not missing, f"{provider} child is missing {sorted(missing)}"
    assert env["LANG"] == "C.UTF-8"
    if provider == "claude":
        assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "canary-subscription-login"
    if provider == "codex":
        # the generated, verified home replaced any ambient one
        assert Path(env["CODEX_HOME"]) != Path(os.environ["HOME"]) / ".codex"
    assert env[skill_runner.AGENT_CHILD_MARKER_ENV] == "1"


def test_every_allowlisted_name_has_a_reason_and_no_credential_name_slips_in():
    for name, reason in skill_runner.CHILD_ENV_ALLOWLIST.items():
        assert reason.strip(), f"{name} is on the child allowlist with no reason"
    forbidden = (
        "GITHUB_TOKEN", "GH_TOKEN", "GITHUB_PAT", "NEOTOMA_BEARER_TOKEN",
        "SSH_AUTH_SOCK", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CURSOR_API_KEY",
        "XDG_CONFIG_HOME", "GIT_CONFIG_GLOBAL",
    )  # fmt: skip
    for name in forbidden:
        assert name not in skill_runner.CHILD_ENV_ALLOWLIST, name
    for name in skill_runner.CHILD_ENV_ALLOWLIST:
        assert not name.endswith(("_PAT", "_SECRET", "_API_KEY")), name


def test_metered_keys_still_need_the_explicit_override(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "metered")
    monkeypatch.setenv("OPENAI_API_KEY", "metered")
    assert "ANTHROPIC_API_KEY" not in skill_runner._subscription_only_env()
    monkeypatch.setenv("APIS_ALLOW_METERED_HARNESS", "1")
    built = skill_runner._subscription_only_env()
    assert built["OPENAI_API_KEY"] == "metered"  # the documented override still works
    monkeypatch.setenv("GEMINI_API_KEY", "generation")
    assert "GEMINI_API_KEY" not in skill_runner._subscription_only_env()


def test_a_proxy_that_carries_credentials_is_dropped(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://user:secret@proxy.example.test:8080")
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.example.test:8080")
    built = skill_runner._subscription_only_env()
    assert "HTTPS_PROXY" not in built
    assert built["HTTP_PROXY"] == "http://proxy.example.test:8080"


def test_a_claude_child_holds_only_the_neotoma_token_its_mcp_session_uses(child):
    _, seen = child("claude")
    env = seen["env"]
    assert env["NEOTOMA_BEARER_TOKEN"] == ROLE_TOKEN  # the role's own, as before
    assert "PAVO_NEOTOMA_TOKEN" not in env and "NEOTOMA_BEARER_TOKEN_PROD" not in env
    assert SHARED_BEARER not in json.dumps(env)


@pytest.mark.parametrize("provider", ["codex", "cursor"])
def test_an_isolated_child_keeps_its_roles_own_neotoma_token_only(child, provider):
    _, seen = child(provider)
    env = seen["env"]
    assert env["NEOTOMA_BEARER_TOKEN"] == ROLE_TOKEN
    assert "PAVO_NEOTOMA_TOKEN" not in env
    assert SHARED_BEARER not in json.dumps(env)


# ── The directory a child runs in ────────────────────────────────────────────


@pytest.mark.parametrize("provider", PROVIDERS)
def test_a_dispatch_with_no_cwd_runs_in_a_fresh_scratch_directory(
    child, provider, monkeypatch, operator_home
):
    # The daemon under launchd has "/" as its working directory.
    monkeypatch.chdir("/")
    result, seen = child(provider)
    assert result.ok, result.error
    cwd = Path(seen["cwd"])
    assert str(cwd) != "/" and cwd != operator_home.resolve()
    assert cwd.name.startswith("apis_work_")
    assert seen["cwd_entries"] == []  # fresh and empty
    assert not cwd.exists()  # removed after the run
    if provider == "codex":  # the CLI's workspace root is that directory too
        assert seen["argv"][seen["argv"].index("--cd") + 1] == str(cwd)
    if provider == "cursor":
        assert seen["argv"][seen["argv"].index("--workspace") + 1] == str(cwd)


def test_two_dispatches_do_not_share_a_scratch_directory(child):
    _, first = child("claude")
    child.marker.unlink()
    _, second = child("claude")
    assert first["cwd"] != second["cwd"]


def test_a_named_cwd_is_used_and_kept(child, tmp_path):
    work = tmp_path / "worktree"
    work.mkdir()
    result, seen = child("claude", cwd=str(work))
    assert result.ok and seen["cwd"] == str(work.resolve())
    assert work.exists()  # only a scratch directory the dispatch made is removed


@pytest.mark.parametrize("provider", PROVIDERS)
def test_a_launch_with_cwd_at_the_filesystem_root_is_refused(child, provider):
    result, seen = child(provider, cwd="/")
    assert not result.ok and seen is None and not child.marker.exists()
    assert "filesystem root" in result.error


@pytest.mark.parametrize("provider", PROVIDERS)
def test_a_launch_with_cwd_at_the_home_directory_is_refused(
    child, provider, operator_home
):
    result, seen = child(provider, cwd=str(operator_home))
    assert not result.ok and seen is None and not child.marker.exists()
    assert "home directory" in result.error


def test_the_cwd_guard_judges_root_home_and_the_daemons_own_directory(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("HOME", str(tmp_path / "h"))
    (tmp_path / "h").mkdir()
    (tmp_path / "ws").mkdir()
    assert skill_runner._unsafe_child_cwd("/")
    assert skill_runner._unsafe_child_cwd(str(tmp_path / "h"))
    assert skill_runner._unsafe_child_cwd(str(tmp_path / "h" / ".." / "h"))
    assert not skill_runner._unsafe_child_cwd(str(tmp_path / "ws"))
    monkeypatch.chdir("/")
    assert skill_runner._unsafe_child_cwd(None)  # None means the daemon's own: "/"
    monkeypatch.chdir(tmp_path / "ws")
    assert not skill_runner._unsafe_child_cwd(None)


# ── The launch definition ────────────────────────────────────────────────────


def test_the_apis_launch_definition_names_a_working_directory_that_is_not_root_or_home():
    plist = plistlib.loads((_DAEMON_DIR / "com.ateles.apis.plist").read_bytes())
    work = plist.get("WorkingDirectory")
    assert work, "the Apis plist must set WorkingDirectory (launchd defaults to /)"
    assert work != "/" and Path(work) != Path(plist["EnvironmentVariables"]["HOME"])
