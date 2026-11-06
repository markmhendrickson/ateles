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

    def __init__(self, *, fail: bool | str = False, gate: threading.Event | None = None,
                 revoke_gate: threading.Event | None = None):
        self.fail = fail
        self.gate = gate
        self.revoke_gate = revoke_gate
        self.revoke_done = threading.Event()
        self.events: list[str] = []
        self.mints: list[tuple[str, str]] = []
        self.mint_threads: list[int] = []
        self.revoked: list[str] = []
        self.revoke_threads: list[int] = []

    def mint(self, repository: str, prefix: str) -> str:
        self.mint_threads.append(threading.get_ident())
        self.mints.append((repository, prefix))
        if self.gate is not None:
            assert self.gate.wait(5), "mint was never released"
        if self.fail == "empty":
            return ""
        if self.fail:
            raise fly_runner.FlyIneligible("GitHub App not configured")
        return READ_TOKEN

    def revoke(self, token: str) -> None:
        self.revoke_threads.append(threading.get_ident())
        if self.revoke_gate is not None:
            assert self.revoke_gate.wait(5), "revoke was never released"
        self.events.append("revoke")
        self.revoked.append(token)
        self.revoke_done.set()


def _definition(name: str) -> AgentDefinition:
    return AgentDefinition(
        entity_id="ent_test", name=name, prompt_markdown=f"Role: {name}.",
        tool_allowlist="*", aauth_sub=f"{name}@ateles-swarm",
    )  # fmt: skip


def _run(github: _GitHub, *, agent: str = "pavo", raise_in_child: bool = False,
         extra=None, envs: list | None = None, communicate=None, returncode: int = 0,
         spawn_error: Exception | None = None, **kwargs):
    """Run one skill through run_skill with a fake child; return (result, envs).

    ``communicate``: an async ``fn(input) -> (stdout, stderr)`` replacing the
    child's default reply. ``envs`` collects one env per spawned child (pass a
    list to read it after a cancelled run). ``os.killpg`` is faked and logged
    into ``github.events`` so ordering against the revoke can be asserted."""
    envs = [] if envs is None else envs

    async def fake_exec(*cmd, **kw):
        if spawn_error is not None:
            raise spawn_error
        envs.append(dict(kw.get("env", {})))
        proc = MagicMock()
        proc.returncode = returncode
        proc.pid = 424242
        calls: list[int] = []

        async def _communicate(input=None):
            calls.append(1)
            if raise_in_child:
                raise RuntimeError("child blew up")
            if communicate is not None and len(calls) == 1:
                return await communicate(input)
            return b"**APPROVE**", b""

        proc.communicate = _communicate
        return proc

    def fake_killpg(pid, sig):
        github.events.append("kill")

    loader = MagicMock()
    loader.load.return_value = _definition(agent)
    loader.render_policy_prompt.return_value = ""
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
        patch("os.killpg", side_effect=fake_killpg),
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


@pytest.mark.parametrize("failure", [True, "empty"])
def test_review_token_mint_failure_refuses_the_run_and_spawns_no_child(caplog, failure):
    github = _GitHub(fail=failure)
    with caplog.at_level("INFO", logger="apis.skill_runner"):
        result, envs = _run(github)
    assert not result.ok
    assert envs == [], "no child may start without a valid read token"
    assert github.revoked == []
    assert result.returncode is None and result.stdout == ""
    assert CALLER_TOKEN not in result.error and READ_TOKEN not in result.error
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


def test_review_token_is_redacted_from_all_result_and_diagnostic_surfaces(caplog):
    """The run's token is removed from the output on success and on failed exits."""
    leak_out = f"findings ... GH_TOKEN={READ_TOKEN} ... **APPROVE**".encode()
    leak_err = f"auth: https://x-access-token:{READ_TOKEN}@github.com/o/r\n".encode()

    async def leaky(_input):
        return leak_out, leak_err

    for returncode in (0, 1, 2):
        github = _GitHub()
        failure_logs: list = []
        notified: list = []
        with (
            caplog.at_level("DEBUG"),
            patch("skill_runner.write_dispatch_failure_log",
                  side_effect=lambda **kw: failure_logs.append(kw) or "/tmp/failure.log"),
            patch("skill_runner.notify_dispatch_failure",
                  side_effect=lambda *a, **kw: notified.append((a, kw))),
        ):
            result, envs = _run(github, communicate=leaky, returncode=returncode)
        assert envs[0]["GH_TOKEN"] == READ_TOKEN  # the child did get it
        assert github.revoked == [READ_TOKEN]
        assert result.ok is (returncode == 0)
        if returncode:
            assert failure_logs, "the failure path ran"
        surfaces = {
            "stdout": result.stdout, "stderr": result.stderr,
            "error": result.error or "", "repr": repr(result),
            "failure_log": repr(failure_logs), "notified": repr(notified),
            "logs": "\n".join(r.getMessage() for r in caplog.records),
        }
        for name, text in surfaces.items():
            assert READ_TOKEN not in text, f"token leaked into {name} (rc={returncode})"
        # Only the token is removed; the rest of the output survives.
        assert "findings" in result.stdout and "auth:" in result.stderr
        caplog.clear()


def test_redaction_takes_a_run_local_value():
    shown = skill_runner._redact_secrets("a minted-read-token b", ("minted-read-token",))
    assert shown == f"a {skill_runner._RUN_TOKEN_PLACEHOLDER} b"
    # Without the value passed in, nothing process-global stands in for it.
    assert skill_runner._redact_secrets("a minted-read-token b") == "a minted-read-token b"


def test_cancel_during_mint_revokes_late_token():
    gate = threading.Event()
    github = _GitHub(gate=gate)
    envs: list = []
    loop_thread: list[int] = []

    async def cancel_while_minting(task):
        loop_thread.append(threading.get_ident())
        while not github.mints:
            await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.sleep(0.05)
        gate.set()  # the mint finishes only after the cancellation
        for _ in range(100):
            if github.revoke_done.is_set():
                break
            await asyncio.sleep(0.02)

    with pytest.raises(asyncio.CancelledError):
        _run(github, extra=cancel_while_minting, envs=envs)
    assert github.revoke_done.wait(5)
    assert envs == [], "no child may spawn after a cancelled mint"
    assert github.revoked == [READ_TOKEN]
    assert github.revoke_threads[0] != loop_thread[0]


def test_cancel_during_child_revokes_token():
    github = _GitHub()
    envs: list = []
    loop_thread: list[int] = []

    async def hang(_input):
        await asyncio.sleep(30)
        return b"", b""

    async def cancel_while_running(task):
        loop_thread.append(threading.get_ident())
        while not envs:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.02)
        task.cancel()

    with pytest.raises(asyncio.CancelledError):
        _run(github, extra=cancel_while_running, envs=envs, communicate=hang)
    assert len(envs) == 1
    assert github.revoked == [READ_TOKEN]
    assert github.revoke_threads[0] != loop_thread[0]


def test_cancel_during_revoke_finishes_revoke():
    revoke_gate = threading.Event()
    github = _GitHub(revoke_gate=revoke_gate)
    envs: list = []
    loop_thread: list[int] = []

    async def cancel_while_revoking(task):
        loop_thread.append(threading.get_ident())
        while not github.revoke_threads:
            await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.sleep(0.05)
        assert github.revoked == [], "the revoke is still in flight"
        revoke_gate.set()  # the revoke completes only after the cancellation
        for _ in range(100):
            if github.revoke_done.is_set():
                break
            await asyncio.sleep(0.02)

    with pytest.raises(asyncio.CancelledError):
        _run(github, extra=cancel_while_revoking, envs=envs)
    assert github.revoke_done.wait(5)
    assert github.revoked == [READ_TOKEN]
    assert github.revoke_threads[0] != loop_thread[0]


@pytest.mark.parametrize("minted", [True, False], ids=["token-bearing", "no-token"])
def test_review_timeout_revokes_and_kills_child_group(minted):
    github = _GitHub()

    async def hang(_input):
        await asyncio.sleep(30)
        return b"", b""

    result, envs = _run(
        github, communicate=hang, timeout=0.2,
        # A run that is not read-only gets no token: the unchanged behaviour.
        github_read_only=minted,
    )
    assert len(envs) == 1
    assert not result.ok and result.returncode is None
    assert "timed out after" in result.error
    assert github.events.count("kill") == 1, "the group is killed exactly once"
    if minted:
        assert github.revoked == [READ_TOKEN]
        # The deadline is enforced first: the kill precedes the (slow) revoke.
        assert github.events == ["kill", "revoke"]
        assert envs[0]["GH_TOKEN"] == READ_TOKEN
    else:
        assert github.revoked == [] and github.mints == []
        assert envs[0]["GH_TOKEN"] == CALLER_TOKEN


@pytest.mark.parametrize("minted", [True, False], ids=["token-bearing", "no-token"])
def test_review_spawn_failure_revokes_and_returns_launch_failure(minted, caplog):
    github = _GitHub()
    with caplog.at_level("INFO", logger="apis.skill_runner"):
        result, envs = _run(
            github, spawn_error=OSError("exec format error"), github_read_only=minted,
        )
    assert envs == []
    # A structured failed result (run_skill wraps the single provider's launch
    # failure), not an exception; the launch failure itself is logged.
    assert not result.ok and result.returncode is None and result.stdout == ""
    assert "claude launch failed: exec format error" in caplog.text
    if minted:
        assert github.revoked == [READ_TOKEN]
        assert len(github.revoke_threads) == 1
    else:
        assert github.revoked == [] and github.mints == []
