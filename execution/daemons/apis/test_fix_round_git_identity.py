"""Fix rounds commit as the agent, unsigned, never as the host owner.

A fix round's child inherits the host's HOME, so without an explicit identity
its commits take the host's ``~/.gitconfig`` name, email and signing key. These
tests pin the three halves of the fix: the child carries the agent's author and
committer with signing off (and a real ``git commit`` under that environment
lands as the agent, unsigned, even when the host config demands signing); with
no agent identity configured the round is refused and no child starts; and a
run that is not a fix round keeps its environment as before.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import skill_runner
from test_skill_runner import _loader_with_no_policies, _make_def
from test_tiering_call_sites import (  # noqa: F401 (world is a fixture)
    _LOCAL_FIX_BRANCH,
    _Recorder,
    _dispatcher,
    _run_revision_sweep,
    _trigger,
    world,
)

AGENT_EMAIL = "agent@example.invalid"
AGENT_NAME = "example-agent"


@pytest.fixture(autouse=True)
def _readable_fly_ledger(_fly_runner_off_and_isolated):
    """Give this file's tests a readable Fly ledger with no receipts.

    A fix round reads the ledger to tell whether the PR came from Fly, and a
    missing ledger holds the round (origin unknown), and so does a missing
    audit file. These tests exercise the
    local round, so the PR must be one a readable ledger does not name."""
    import build_delivery
    import fly_delivery_receipt

    ledger = build_delivery._delivery_ledger()
    with ledger._locked():
        ledger._save(ledger._load())
    fly_delivery_receipt.audit_path(ledger).write_text("")


@pytest.fixture
def host_home(monkeypatch, tmp_path) -> Path:
    """A host HOME whose git config names a person and demands signing with a
    signer that always fails, so any commit that signs or uses it is visible."""
    home = tmp_path / "host-home"
    home.mkdir()
    (home / ".gitconfig").write_text(
        "[user]\n\tname = Host Owner\n\temail = host@example.invalid\n"
        "[commit]\n\tgpgsign = true\n[tag]\n\tgpgsign = true\n"
        "[gpg]\n\tprogram = false\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    # Synthetic values: the claude adapter refuses to build its MCP config
    # without them, and nothing here connects anywhere.
    monkeypatch.setenv("NEOTOMA_BASE_URL", "https://neotoma.example.invalid")
    monkeypatch.setenv("NEOTOMA_BEARER_TOKEN", "test-bearer")
    return home


def _run_claude_child(monkeypatch, **run_kwargs):
    """Run `run_skill` on the claude adapter with the spawn mocked; return
    (result, captured env or None when no child was started)."""
    skill_runner._agent_def_cache.clear()
    captured: dict = {}

    async def fake_exec(*cmd, **kwargs):
        captured["env"] = kwargs["env"]
        proc = MagicMock()
        proc.returncode = 0

        async def _communicate(input=None):
            return b"output", b""

        proc.communicate = _communicate
        return proc

    instance = _loader_with_no_policies()
    instance.load.return_value = _make_def(name="cicada", aauth_sub="cicada@ateles-swarm")
    with (
        patch("skill_runner._write_harness_event"),
        patch("skill_runner.AgentLoader", return_value=instance),
        patch("skill_runner.CLAUDE_BIN", "/usr/bin/claude"),
        patch.object(Path, "exists", return_value=True),
        patch.object(Path, "read_text", return_value="skill content"),
        patch("asyncio.create_subprocess_exec", side_effect=fake_exec) as spawn,
    ):
        result = asyncio.run(
            skill_runner.run_skill(
                "cicada", "fix prompt", role="cicada", provider="claude", **run_kwargs
            )
        )
    return result, captured.get("env"), spawn


def test_fix_round_child_commits_as_the_agent_unsigned(monkeypatch, host_home, tmp_path):
    monkeypatch.setenv("ATELES_AGENT_GIT_EMAIL", AGENT_EMAIL)
    monkeypatch.setenv("ATELES_AGENT_GIT_NAME", AGENT_NAME)
    result, env, _ = _run_claude_child(monkeypatch, agent_git_identity=True)
    assert result.ok
    assert env["GIT_AUTHOR_NAME"] == AGENT_NAME
    assert env["GIT_AUTHOR_EMAIL"] == AGENT_EMAIL
    assert env["GIT_COMMITTER_NAME"] == AGENT_NAME
    assert env["GIT_COMMITTER_EMAIL"] == AGENT_EMAIL
    overrides = {
        env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"]
        for i in range(int(env["GIT_CONFIG_COUNT"]))
    }
    assert overrides["commit.gpgsign"] == "false"
    assert overrides["tag.gpgsign"] == "false"

    # The effect: a commit made under that environment, against a host config
    # that names a person and requires signing, lands as the agent, unsigned.
    git = shutil.which("git")
    if git is None:
        pytest.skip("git is not installed")
    repo = tmp_path / "repo"
    repo.mkdir()
    child_env = {**env, "PATH": env.get("PATH") or str(Path(git).parent)}
    subprocess.run([git, "init", "-q", str(repo)], check=True, env=child_env)
    (repo / "f.txt").write_text("fix\n", encoding="utf-8")
    subprocess.run([git, "-C", str(repo), "add", "f.txt"], check=True, env=child_env)
    subprocess.run(
        [git, "-C", str(repo), "commit", "-q", "-m", "fix"], check=True, env=child_env
    )
    shown = subprocess.run(
        [git, "-C", str(repo), "log", "-1", "--format=%an <%ae>|%cn <%ce>|%G?"],
        check=True, env=child_env, capture_output=True, text=True,
    ).stdout.strip()
    agent = f"{AGENT_NAME} <{AGENT_EMAIL}>"
    assert shown == f"{agent}|{agent}|N"


def test_name_falls_back_to_the_agent_login_not_the_host(monkeypatch, host_home):
    monkeypatch.setenv("ATELES_AGENT_GIT_EMAIL", AGENT_EMAIL)
    monkeypatch.delenv("ATELES_AGENT_GIT_NAME", raising=False)
    _, env, _ = _run_claude_child(monkeypatch, agent_git_identity=True)
    assert env["GIT_AUTHOR_NAME"] == env["GIT_COMMITTER_NAME"] == "ateles-agent"


def test_fix_round_with_no_agent_identity_is_refused_and_starts_no_child(
    monkeypatch, host_home, caplog
):
    monkeypatch.delenv("ATELES_AGENT_GIT_EMAIL", raising=False)
    monkeypatch.setenv("ATELES_AGENT_GIT_NAME", AGENT_NAME)
    result, env, spawn = _run_claude_child(monkeypatch, agent_git_identity=True)
    assert not result.ok
    assert result.error == skill_runner.AGENT_GIT_IDENTITY_MISSING
    assert env is None
    spawn.assert_not_called()
    assert any(
        skill_runner.AGENT_GIT_IDENTITY_MISSING in r.getMessage() for r in caplog.records
    )


def test_a_run_that_is_not_a_fix_round_keeps_its_environment(monkeypatch, host_home):
    monkeypatch.setenv("ATELES_AGENT_GIT_EMAIL", AGENT_EMAIL)
    monkeypatch.setenv("ATELES_AGENT_GIT_NAME", AGENT_NAME)
    result, env, _ = _run_claude_child(monkeypatch)
    assert result.ok
    for key in (
        "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME",
        "GIT_COMMITTER_EMAIL", "GIT_CONFIG_COUNT",
    ):
        assert key not in env


def test_identity_appends_after_existing_config_overrides():
    env = {
        "GIT_CONFIG_COUNT": "2",
        "GIT_CONFIG_KEY_0": "credential.helper", "GIT_CONFIG_VALUE_0": "",
        "GIT_CONFIG_KEY_1": "credential.interactive", "GIT_CONFIG_VALUE_1": "never",
    }
    skill_runner.apply_agent_git_identity(
        env, skill_runner.agent_git_identity_env({"ATELES_AGENT_GIT_EMAIL": AGENT_EMAIL})
    )
    assert env["GIT_CONFIG_COUNT"] == "4"
    assert env["GIT_CONFIG_KEY_0"] == "credential.helper"
    assert (env["GIT_CONFIG_KEY_2"], env["GIT_CONFIG_VALUE_2"]) == ("commit.gpgsign", "false")
    assert (env["GIT_CONFIG_KEY_3"], env["GIT_CONFIG_VALUE_3"]) == ("tag.gpgsign", "false")


# ── The dispatcher's fix rounds ask for the agent identity ────────────────────


def test_ci_fix_round_asks_for_the_agent_identity(monkeypatch, world):
    rec = _Recorder(monkeypatch, stdout={"cicada": "fixed"})
    asyncio.run(
        _dispatcher()._route_ci_failure(_trigger(head_ref=_LOCAL_FIX_BRANCH), parent=80)
    )
    assert rec.only("cicada").get("agent_git_identity") is True


def test_findings_fix_round_asks_for_the_agent_identity(monkeypatch, world):
    rec = _Recorder(monkeypatch, stdout={"cicada": "fixed"})
    asyncio.run(
        _dispatcher()._route_blocking_findings(
            _trigger(head_ref=_LOCAL_FIX_BRANCH), parent=80,
            reviews=[("ux", "[BLOCKING] naming: the flag is undiscoverable\ndetail")],
            verdict="request_changes",
        )
    )
    assert rec.only("cicada").get("agent_git_identity") is True
    # The lens that writes fix guidance commits nothing and is left alone.
    assert "agent_git_identity" not in rec.only("accipiter")


def test_resumed_fix_round_asks_for_the_agent_identity(monkeypatch, world):
    rec = _run_revision_sweep(monkeypatch)
    assert rec.only("cicada").get("agent_git_identity") is True
