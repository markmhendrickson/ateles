"""The host-wide Fly pause (execution/daemons/apis/fly_pause.py).

While a pause record is in force no code path through the Fly runner sends a
Machines API request: the transport is asserted to see nothing. New launches
refuse up front with "Fly paused until <time>: <reason>", mint nothing,
reserve nothing and never run locally. The periodic step and the reaper skip
and keep marked reservations, which settle once the pause ends. An unreadable
record is a pause; an expired one is not.

The Fly API is the ``FakeFly`` double from ``test_fly_runner``.

What these looked like RED: against the unchanged code ``fly_pause`` does not
exist, and with only the module added (no checks wired) every "no request"
assertion fails because the transport records the call.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

import fly_pause
import fly_runner
import test_fly_runner
from test_fly_runner import (
    Clock,
    FakeFly,
    _codex_fake,
    _codex_job,
    _job,
    _job_config,
    _once,
    _seed_marked_login,
    make_executor,
)

# Fixtures of the Fly runner tests, used here by name.
fly_env, seam = test_fly_runner.fly_env, test_fly_runner.seam
_per_job_claude = (
    test_fly_runner._claude_runs_per_job_unless_its_login_machine_is_tested
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPTS = _REPO_ROOT / "execution" / "scripts"


def _pause_file(executor=None) -> Path:
    ledger = executor.config.ledger_path if executor else fly_pause.ledger_path()
    return fly_pause.pause_path(ledger)


def _pause(executor=None, reason="neotoma deploy", seconds=600) -> fly_pause.PauseState:
    return fly_pause.set_pause(_pause_file(executor), seconds=seconds, reason=reason)


class _Transport:
    """Counts every request a client would send; answers like FakeFly."""

    def __init__(self, fake: FakeFly):
        self.fake = fake
        self.seen: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        return self.fake.handler(request)


def _counted(executor, fake) -> _Transport:
    transport = _Transport(fake)
    executor.client = fly_runner.FlyMachinesClient(
        executor.config, transport=httpx.MockTransport(transport.handler)
    )
    return transport


# ── The record ────────────────────────────────────────────────────────────────


def test_ledger_path_matches_the_runner_config(fly_env):
    assert fly_pause.ledger_path() == fly_runner.FlyConfig.from_env().ledger_path


def test_absent_record_is_not_a_pause(tmp_path):
    assert fly_pause.read_pause(tmp_path / "p.json").paused is False


def test_set_show_and_clear(tmp_path):
    path = tmp_path / "p.json"
    state = fly_pause.set_pause(path, reason="deploy", now=1000.0)
    assert state.paused and state.until == 1000.0 + fly_pause.DEFAULT_PAUSE_SECONDS
    assert (
        fly_pause.read_pause(path, now=1001.0).message().startswith("Fly paused until ")
    )
    assert fly_pause.read_pause(path, now=1001.0).message().endswith(": deploy")
    assert fly_pause.clear_pause(path) is True
    assert fly_pause.read_pause(path).paused is False
    assert fly_pause.clear_pause(path) is False


@pytest.mark.parametrize("seconds", [0, -5, fly_pause.MAX_PAUSE_SECONDS + 1, 24 * 3600])
def test_durations_out_of_range_are_refused(tmp_path, seconds):
    with pytest.raises(ValueError):
        fly_pause.set_pause(tmp_path / "p.json", seconds=seconds, reason="deploy")
    assert not (tmp_path / "p.json").exists()


def test_twelve_hours_is_allowed_and_a_reason_is_required(tmp_path):
    fly_pause.set_pause(
        tmp_path / "p.json", seconds=fly_pause.MAX_PAUSE_SECONDS, reason="x"
    )
    with pytest.raises(ValueError):
        fly_pause.set_pause(tmp_path / "q.json", reason="   ")


def test_expired_record_is_not_a_pause(tmp_path):
    path = tmp_path / "p.json"
    fly_pause.set_pause(path, seconds=60, reason="deploy", now=1000.0)
    assert fly_pause.read_pause(path, now=1059.0).paused is True
    assert fly_pause.read_pause(path, now=1060.0).paused is False


@pytest.mark.parametrize("content", ["", "{not json", "[]", '{"until": "soon", "reason": "x"}',
                                     '{"reason": "x"}', '{"until": 9e99}',
                                     '{"until": true, "reason": "x"}',
                                     '{"until": NaN, "reason": "x"}'])  # fmt: skip
def test_unreadable_record_fails_closed_and_says_how_to_clear(tmp_path, content):
    path = tmp_path / "p.json"
    path.write_text(content)
    state = fly_pause.read_pause(path)
    assert state.paused and state.unreadable and state.until is None
    assert fly_pause.CLEAR_COMMAND in state.message()


def test_a_record_that_cannot_be_opened_fails_closed(tmp_path):
    path = tmp_path / "p.json"
    path.mkdir()  # reading a directory raises an OSError that is not "absent"
    state = fly_pause.read_pause(path)
    assert state.paused and state.unreadable


# ── The command ───────────────────────────────────────────────────────────────


def _cli():
    # Loaded by path: the script shares its name with the daemon module.
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "fly_pause_cli", _SCRIPTS / "fly_pause.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_command_sets_shows_and_clears(fly_env, capsys):
    cli = _cli()
    assert cli.main(["show"]) == 0
    assert cli.main(["set", "--reason", "neotoma deploy"]) == 0
    assert "Fly paused until " in capsys.readouterr().out
    state = fly_pause.read_pause(_pause_file())
    assert 3500 < state.until - time.time() <= 3600  # the default hour
    assert cli.main(["show"]) == 1
    assert cli.main(["set", "--reason", "x", "--minutes", "721"]) == 2
    assert cli.main(["clear"]) == 0
    assert cli.main(["show"]) == 0


# ── No Fly request while paused ───────────────────────────────────────────────


def test_every_client_call_is_refused_before_the_transport(fly_env):
    fake = FakeFly()
    fake.machines["m1"] = {
        "id": "m1",
        "state": "started",
        "instance_id": "v1",
        "config": {},
    }
    executor, _, _ = make_executor(fake)
    transport = _counted(executor, fake)
    _pause(executor)
    client = executor.client

    calls = [
        client.create({"config": {}}),
        client.get("m1"),
        client.update("m1", {}),
        client.start("m1"),
        client.stop("m1"),
        client.acquire_lease("m1", ttl=60, description="d"),
        client.release_lease("m1", "n"),
        client.get_lease("m1"),
        client.destroy("m1"),
        client.list(),
        client.wait("m1", state="stopped"),
    ]
    for call in calls:
        with pytest.raises(fly_runner.FlyPausedError) as exc:
            asyncio.run(call)
        assert "Fly paused until" in str(exc.value)
    assert transport.seen == []
    assert fake.machines["m1"]["state"] == "started"


def test_a_launch_is_refused_up_front_with_nothing_minted_or_reserved(fly_env):
    fake = FakeFly()
    executor, minted, revoked = make_executor(
        fake, record={"exit_code": 0, "stdout": "x"}
    )
    transport = _counted(executor, fake)
    _pause(executor, reason="neotoma deploy")
    with pytest.raises(fly_runner.FlyPaused) as exc:
        asyncio.run(executor.run(_job()))
    assert str(exc.value).startswith("Fly paused until ")
    assert str(exc.value).endswith(": neotoma deploy")
    assert exc.value.cooled_until  # when to try again
    assert isinstance(exc.value, fly_runner.FlyRefusal)  # never a local fallback
    assert transport.seen == [] and minted == [] and revoked == []
    assert executor.ledger.snapshot()["in_flight"] == {}


def test_an_unreadable_pause_refuses_a_launch(fly_env):
    fake = FakeFly()
    executor, minted, _ = make_executor(fake, record={"exit_code": 0, "stdout": "x"})
    transport = _counted(executor, fake)
    _pause_file(executor).write_text("{garbled")
    with pytest.raises(fly_runner.FlyPaused) as exc:
        asyncio.run(executor.run(_job()))
    assert fly_pause.CLEAR_COMMAND in str(exc.value)
    assert transport.seen == [] and minted == []


def test_a_pause_that_begins_mid_launch_is_a_refusal_not_an_uncertain_launch(fly_env):
    fake = FakeFly()
    executor, _, revoked = make_executor(fake, record={"exit_code": 0, "stdout": "x"})
    transport = _counted(executor, fake)
    real_mint = executor._mint

    def mint_then_pause(repo, prefix):
        token = real_mint(repo, prefix)
        _pause(executor)  # set after run()'s up-front check, before the create
        return token

    executor._mint = mint_then_pause
    job = _job()
    with pytest.raises(fly_runner.FlyPaused):
        asyncio.run(executor.run(job))
    assert transport.seen == []
    assert job.launch_requested is False
    assert revoked  # the minted token was still revoked


def test_an_expired_pause_lets_a_launch_through(fly_env):
    fake = FakeFly()
    executor, _, _ = make_executor(fake, record={"exit_code": 0, "stdout": "done"})
    fly_pause.set_pause(_pause_file(executor), seconds=60, reason="old",
                        now=time.time() - 120)  # fmt: skip
    rc, out, _ = asyncio.run(executor.run(_job()))
    assert (rc, out) == (0, b"done")


def test_dispatch_refuses_and_never_runs_locally(fly_env, seam, monkeypatch):
    spawned, fake_exec = seam
    fake = FakeFly()
    executor, minted, _ = make_executor(fake, record={"exit_code": 0, "stdout": "x"})
    transport = _counted(executor, fake)
    monkeypatch.setattr(fly_runner, "get_executor", lambda: executor)
    _pause(executor, reason="neotoma deploy")
    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        result = _once(runner="fly", repository="owner/repo", github_token="")
    assert not result.ok
    assert result.error.startswith("fly runner refused: Fly paused until ")
    assert result.error.endswith(": neotoma deploy")
    assert spawned == [] and transport.seen == [] and minted == []
    assert test_fly_runner._session(fly_env).requests == []  # no admission check either


def test_an_opted_in_class_does_not_fall_back_locally_while_paused(
    fly_env, seam, monkeypatch
):
    spawned, fake_exec = seam
    monkeypatch.setenv("APIS_FLY_RUNNER_CLASSES", "carry_forward_check")
    _pause()
    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        result = _once(action_class="carry_forward_check", model="m", repository="owner/repo",
                       github_token="t-local")  # fmt: skip
    assert not result.ok and spawned == []
    assert "Fly paused until" in result.error


# ── Periodic step, reaper and a running job ───────────────────────────────────


def test_periodic_step_skips_keeps_the_reservation_and_settles_after_resume(
        fly_env, monkeypatch, caplog):  # fmt: skip
    caplog.set_level(logging.WARNING)
    fake = _codex_fake(monkeypatch)
    clock = Clock()
    executor, _, _ = make_executor(fake, clock=clock)
    run_id = _seed_marked_login(executor, clock)
    fake.machines["m-codex"]["config"] = _job_config(run_id)
    transport = _counted(executor, fake)
    monkeypatch.setattr(fly_runner, "_EXECUTOR", executor)  # what Apis's sweep reaches

    _pause(executor, reason="neotoma deploy")
    assert asyncio.run(fly_runner.settle_marked_reservations()) == []
    assert transport.seen == []
    assert "periodic step skipped: Fly paused until" in caplog.text
    entry = executor.ledger.entry(run_id)
    assert entry is not None and entry["unconfirmed"] is True

    fly_pause.clear_pause(_pause_file(executor))
    assert asyncio.run(fly_runner.settle_marked_reservations()) == [run_id]
    assert executor.ledger.entry(run_id) is None
    assert fake.machines["m-codex"]["config"]["files"] == []


def test_reaper_skips_while_paused(fly_env, monkeypatch, caplog):
    caplog.set_level(logging.WARNING)
    fake = _codex_fake(monkeypatch)
    clock = Clock()
    executor, _, _ = make_executor(fake, clock=clock)
    run_id = _seed_marked_login(executor, clock)
    transport = _counted(executor, fake)
    _pause(executor)
    assert asyncio.run(executor.reap()) == []
    assert transport.seen == [] and executor.ledger.entry(run_id) is not None
    assert "reaper skipped: Fly paused until" in caplog.text


def test_a_running_job_is_left_alone_while_paused_and_settles_after(
    fly_env, monkeypatch
):
    """A pause set while a job runs: the wait stops asking Fly, the job is not
    stopped, and when the run's deadline passes during the pause its outcome
    is reported unknown and its reservation kept, marked, for the periodic
    step after the pause."""
    fake = _codex_fake(monkeypatch)
    clock = Clock()
    executor, _, _ = make_executor(
        fake, record={"exit_code": 0, "stdout": "x"}, clock=clock
    )
    transport = _counted(executor, fake)
    sent_while_paused: list[httpx.Request] = []
    paused = {"on": False}
    real_handler = transport.handler

    def handler(request):
        if paused["on"]:
            sent_while_paused.append(request)
        if request.url.path.endswith("/wait") and not paused["on"]:
            # The job is running: set the pause, then answer "still waiting".
            paused["on"] = True
            _pause(executor, seconds=7200)
            return httpx.Response(408, json={"error": "timeout"})
        return real_handler(request)

    executor.client = fly_runner.FlyMachinesClient(
        executor.config, transport=httpx.MockTransport(handler)
    )
    slept: list[float] = []

    async def fake_sleep(seconds):
        slept.append(seconds)
        clock.now += seconds

    executor._sleep = fake_sleep
    job = _codex_job()
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(executor.run(job))
    assert sent_while_paused == []  # nothing asked of Fly after the pause began
    assert slept and all(s <= fly_runner.PAUSE_POLL_SECONDS for s in slept)
    assert fake.machines["m-codex"]["state"] == "started"  # not stopped
    assert job.remote_state == "unconfirmed" and job.outcome_unknown is True
    entry = executor.ledger.entry(job.run_id)
    assert entry is not None and entry["unconfirmed"] is True
    assert entry.get("lease_nonce")  # the lease is kept for settlement
    assert "m-codex" in fake.leases


def test_a_paused_settle_never_releases(fly_env, monkeypatch):
    fake = _codex_fake(monkeypatch)
    clock = Clock()
    executor, _, _ = make_executor(fake, clock=clock)
    run_id = _seed_marked_login(executor, clock)
    transport = _counted(executor, fake)
    _pause(executor)
    assert asyncio.run(executor._settle(run_id)) is False
    assert transport.seen == []
    assert executor.ledger.entry(run_id)["unconfirmed"] is True


# ── Session lens reviews ──────────────────────────────────────────────────────


def _lens_review():
    if str(_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(_SCRIPTS))
    import fly_lens_review

    return fly_lens_review


def test_lens_review_refuses_up_front_and_dispatches_nothing(fly_env, tmp_path):
    review = _lens_review()
    _pause(reason="neotoma deploy")
    dispatched = []
    rc = review.main(
        ["--repo", "owner/repo", "--pr", "1", "--lens", "pm",
         "--task-entity-id", "ent_x", "--out-dir", str(tmp_path / "out")],
        dispatch=lambda **kw: dispatched.append(kw) or {},
        head_resolver=lambda repo, pr: pytest.fail("no work before the pause check"),
    )  # fmt: skip
    assert rc == 5 and dispatched == []


def test_lens_review_stops_before_the_next_lens_when_paused_mid_run(
    fly_env, tmp_path, capsys
):
    review = _lens_review()
    dispatched = []

    def run_lens(**kw):
        dispatched.append(kw["lens"])
        _pause(reason="neotoma deploy")  # set while the first lens ran
        raise review.FallBack("refused")

    with patch.object(review, "run_lens", run_lens):
        rc = review.main(
            ["--repo", "owner/repo", "--pr", "1", "--lens", "all",
             "--task-entity-id", "ent_x", "--out-dir", str(tmp_path / "out")],
            head_resolver=lambda repo, pr: "a" * 40,
            files_resolver=lambda repo, pr: ["execution/daemons/apis/fly_runner.py"],
        )  # fmt: skip
    assert rc == 5 and len(dispatched) == 1
    summary = json.loads((tmp_path / "out" / "summary.json").read_text())
    assert any(r["status"] == "not_run" and "Fly paused" in r["reason"]
               for r in summary["lenses"])  # fmt: skip
