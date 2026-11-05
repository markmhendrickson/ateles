"""A local seated review run takes a read-scoped, per-run GitHub token.

A lens run (``run_skill(github_read_only=True, seated_reviewer=True)``) whose
agent has neither its own App nor its own ``<AGENT>_AGENT_PAT`` would
otherwise hand the child the dispatcher's shared per-repo token, which can
write. A review only reads; the dispatcher publishes its verdict from the
returned text. So the local run takes the Fly job's read-class token
(``fly_runner.mint_job_github_token``), minted off the event loop for one
repository and revoked when the run ends. These tests drive the real
``run_skill`` -> ``_run_skill_once`` path and fake only the subprocess and
the GitHub mint/revoke.
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_DAEMON_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _DAEMON_DIR.parents[2]
for _p in (str(_REPO_ROOT), str(_DAEMON_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import fly_runner  # noqa: E402
import harness_router  # noqa: E402
import skill_runner  # noqa: E402
import swarm_dispatch  # noqa: E402
from lib.daemon_runtime import AgentDefinition  # noqa: E402

REPO = "owner/example"
CALLER_TOKEN = "caller-shared-write-token"
READ_TOKEN = "minted-read-token"


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("APIS_HARNESS_PROVIDERS", "claude")
    monkeypatch.setenv("APIS_HARNESS_HEADROOM", '{"claude": 1.0}')
    monkeypatch.setenv("APIS_HARNESS_HEADROOM_FILE", str(tmp_path / "missing.json"))
    monkeypatch.setenv("NEOTOMA_BEARER_TOKEN", "test-bearer")
    monkeypatch.delenv("APIS_ALLOW_METERED_HARNESS", raising=False)
    monkeypatch.delenv("APIS_FLY_RUNNER_CLASSES", raising=False)
    monkeypatch.delenv("APIS_FLY_GITHUB_APP_PREFIX", raising=False)
    for name in list(os.environ):
        if name.endswith("_AGENT_PAT") or name.startswith("ATELES_APP_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("ATELES_EXPECTED_LENS_APPS", raising=False)
    harness_router.reset_state()
    skill_runner._agent_def_cache.clear()
    yield
    harness_router.reset_state()


class _GitHub:
    """Fake mint and revoke; records what was asked and on which thread."""

    def __init__(self, *, fail: bool = False, gate: threading.Event | None = None):
        self.fail = fail
        self.gate = gate
        self.mints: list[tuple[str, str]] = []
        self.mint_threads: list[int] = []
        self.revoked: list[str] = []
        self.revoke_threads: list[int] = []

    def mint(self, repository: str, prefix: str) -> str:
        self.mint_threads.append(threading.get_ident())
        self.mints.append((repository, prefix))
        if self.gate is not None:
            assert self.gate.wait(5), "mint was never released"
        if self.fail:
            raise fly_runner.FlyIneligible("GitHub App not configured")
        return READ_TOKEN

    def revoke(self, token: str) -> None:
        self.revoke_threads.append(threading.get_ident())
        self.revoked.append(token)


def _definition(name: str) -> AgentDefinition:
    return AgentDefinition(
        entity_id="ent_test", name=name, prompt_markdown=f"Role: {name}.",
        tool_allowlist="*", aauth_sub=f"{name}@ateles-swarm",
    )  # fmt: skip


def _run(github: _GitHub, *, agent: str = "pavo", raise_in_child: bool = False,
         extra=None, **kwargs):
    """Run one skill through run_skill with a fake child; return (result, envs)."""
    envs: list[dict] = []

    async def fake_exec(*cmd, **kw):
        envs.append(dict(kw.get("env", {})))
        proc = MagicMock()
        proc.returncode = 0

        async def communicate(input=None):
            if raise_in_child:
                raise RuntimeError("child blew up")
            return b"**APPROVE**", b""

        proc.communicate = communicate
        return proc

    loader = MagicMock()
    loader.load.return_value = _definition(agent)
    loader.load_policies.return_value = []
    params = dict(
        role=agent, task_entity_id="ent_task", github_token=CALLER_TOKEN,
        include_github_contract=True, github_read_only=True, seated_reviewer=True,
        github_token_repository=REPO,
    )  # fmt: skip
    params.update(kwargs)

    async def main():
        task = asyncio.ensure_future(skill_runner.run_skill(agent, "review", **params))
        if extra is not None:
            await extra(task)
        return await task

    with (
        patch("skill_runner._write_harness_event"),
        patch("skill_runner.AgentLoader", return_value=loader),
        patch("skill_runner.CLAUDE_BIN", "/usr/bin/claude"),
        patch.object(Path, "exists", return_value=True),
        patch.object(Path, "read_text", return_value="skill md"),
        patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
        patch("os.path.exists", return_value=False),
        patch.object(fly_runner, "mint_job_github_token", github.mint),
        patch.object(fly_runner, "revoke_job_github_token", github.revoke),
    ):
        return asyncio.run(main()), envs


def test_read_only_review_run_gets_a_read_scoped_token():
    github = _GitHub()
    result, envs = _run(github)
    assert result.ok, result.error
    # One repository, from the read-class App: mint_job_github_token requests
    # READ_TOKEN_PERMISSIONS only and refuses (and revokes) anything broader.
    assert github.mints == [(REPO, "ATELES_REVIEWER_APP")]
    assert fly_runner.READ_TOKEN_PERMISSIONS == {"contents": "read", "pull_requests": "read"}
    assert envs[0]["GITHUB_TOKEN"] == READ_TOKEN
    assert envs[0]["GH_TOKEN"] == READ_TOKEN
    assert CALLER_TOKEN not in envs[0].values()


def test_review_token_mint_runs_off_the_event_loop():
    gate = threading.Event()
    github = _GitHub(gate=gate)
    loop_thread: list[int] = []
    ticks: list[int] = []

    async def other_coroutine(task):
        loop_thread.append(threading.get_ident())
        # The mint is blocked on `gate`; this coroutine still gets the loop.
        for i in range(5):
            await asyncio.sleep(0.01)
            ticks.append(i)
        assert not task.done()
        assert github.mints, "the mint should be pending by now"
        gate.set()

    result, envs = _run(github, extra=other_coroutine)
    assert result.ok, result.error
    assert ticks == [0, 1, 2, 3, 4]
    assert github.mint_threads and github.mint_threads[0] != loop_thread[0]
    assert github.revoke_threads and github.revoke_threads[0] != loop_thread[0]
    assert envs[0]["GH_TOKEN"] == READ_TOKEN


def test_review_token_is_revoked_when_the_run_ends():
    github = _GitHub()
    result, _ = _run(github)
    assert result.ok
    assert github.revoked == [READ_TOKEN]


def test_review_token_is_revoked_when_the_run_raises():
    github = _GitHub()
    try:
        _run(github, raise_in_child=True)
    except RuntimeError:
        pass  # whether it surfaces or becomes a failed result, it must revoke
    assert github.revoked == [READ_TOKEN]


def test_review_token_mint_failure_falls_back_to_caller_token(caplog):
    github = _GitHub(fail=True)
    with caplog.at_level("WARNING", logger="apis.skill_runner"):
        result, envs = _run(github)
    assert result.ok, result.error
    assert envs[0]["GH_TOKEN"] == CALLER_TOKEN
    assert envs[0]["GITHUB_TOKEN"] == CALLER_TOKEN
    assert github.revoked == []
    lines = [r.getMessage() for r in caplog.records if "read-scoped review token" in r.getMessage()]
    assert len(lines) == 1
    assert CALLER_TOKEN not in lines[0] and READ_TOKEN not in lines[0]


@pytest.mark.parametrize("own", ["pat", "app", "expected_app"])
def test_agent_own_app_and_own_pat_still_win(monkeypatch, own):
    if own == "pat":
        monkeypatch.setenv("PAVO_AGENT_PAT", "pavo-own-pat")
    elif own == "app":
        monkeypatch.setenv("ATELES_APP_REVIEWER_PM_ID", "123")
    else:
        # The roster expects pm to have an App this process lacks: the caller
        # refuses with "", and nothing is minted in its place.
        monkeypatch.setenv("ATELES_EXPECTED_LENS_APPS", "pm")
    github = _GitHub()
    token = "" if own == "expected_app" else "agent-own-token"
    result, envs = _run(github, github_token=token)
    assert github.mints == []
    if own == "expected_app":
        assert not result.ok and envs == []
    else:
        assert result.ok, result.error
        assert envs[0]["GH_TOKEN"] == "agent-own-token"


def test_pr_opener_agent_with_its_app_still_wins(monkeypatch):
    monkeypatch.setenv("ATELES_APP_PR_OPENER_ID", "456")
    github = _GitHub()
    result, envs = _run(github, agent="cicada", github_token="opener-app-token")
    assert result.ok, result.error
    assert github.mints == []
    assert envs[0]["GH_TOKEN"] == "opener-app-token"


@pytest.mark.parametrize("change", [{"github_read_only": False}, {"seated_reviewer": False}])
def test_non_read_only_run_is_unchanged(change):
    github = _GitHub()
    result, envs = _run(github, **change)
    assert result.ok, result.error
    assert github.mints == [] and github.revoked == []
    assert envs[0]["GH_TOKEN"] == CALLER_TOKEN


def test_pr_opener_agents_match_the_dispatcher():
    assert skill_runner._PR_OPENER_AGENTS == swarm_dispatch._PR_OPENER_AGENTS
