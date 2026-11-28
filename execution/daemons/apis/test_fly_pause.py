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
    # One fixed path, whatever the executor's ledger (conftest isolates it).
    return fly_pause.pause_file()


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


def test_the_pause_path_is_fixed_whatever_the_ledger_setting(
    fly_env, monkeypatch, tmp_path, capsys
):
    """Class 3: the command and the daemon always mean the same file. RED on
    the round-2 head: the record lived beside ``APIS_FLY_LEDGER_FILE``, so a
    command run without the daemon's override paused nothing."""
    monkeypatch.undo()  # the real fixed path, not the conftest replacement
    real = fly_pause.pause_file()
    assert (
        real
        == fly_pause._account_home() / ".config" / "ateles" / "fly-runner-pause.json"
    )
    monkeypatch.setenv(
        "APIS_FLY_LEDGER_FILE", str(tmp_path / "elsewhere" / "ledger.json")
    )
    monkeypatch.setenv("HOME", str(tmp_path / "other-home"))
    assert fly_pause.pause_file() == real  # neither setting moves it
    # Back to an isolated record; the daemon's config points its ledger away.
    monkeypatch.setattr(fly_pause, "PAUSE_FILE", tmp_path / "fixed" / "p.json")
    config = fly_runner.FlyConfig.from_env()
    assert config.ledger_path == tmp_path / "elsewhere" / "ledger.json"
    monkeypatch.delenv("APIS_FLY_LEDGER_FILE")  # the command runs without it
    assert _cli().main(["set", "--reason", "neotoma deploy"]) == 0
    out = capsys.readouterr().out
    assert str(tmp_path / "fixed" / "p.json") in out  # set names the path read
    assert config.pause_state().paused  # the daemon, ledger elsewhere, sees it
    assert fly_runner.pause_state().paused
    assert _lens_review().fly_pause_message()
    assert _cli().main(["clear"]) == 0
    assert not config.pause_state().paused


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
                                     '{"until": NaN, "reason": "x"}',
                                     # far future, each with a valid reason:
                                     '{"until": 1e300, "reason": "x"}',
                                     '{"until": 1e18, "reason": "x"}',
                                     '{"until": 9e15, "reason": "x"}',
                                     '{"until": 1e12, "reason": "x"}',
                                     '{"until": 1.8e12, "reason": "x"}',
                                     '{"until": 253402300800, "reason": "x"}',
                                     '{"until": 1' + "0" * 400 + ', "reason": "x"}',
                                     '{"until": 1' + "0" * 5000 + ', "reason": "x"}',
                                     '{"until": 1e400, "reason": "x"}',
                                     '{"until": -1e400, "reason": "x"}',
                                     '{"until": {"a": 1}, "reason": "x"}',
                                     '{"until": [1], "reason": "x"}',
                                     '{"until": null, "reason": "x"}',
                                     '{"until": 2000000000, "reason": 7}',
                                     '{"until": 2000000000, "reason": ["x"]}',
                                     '{"until": 2000000000, "reason": ""}',
                                     '{"until": 2000000000, "reason": "\\ud800"}',
                                     '{"until": 2000000000, "reason": "' + "r" * 500 + '"}',
                                     "[" * 100000 + "]" * 100000,
                                     '"just text"', "42", "null"])  # fmt: skip
def test_unreadable_record_fails_closed_and_says_how_to_clear(tmp_path, content):
    path = tmp_path / "p.json"
    path.write_text(content)
    state = fly_pause.read_pause(path, now=1_900_000_000.0)
    assert state.paused and state.unreadable and state.until is None
    assert fly_pause.CLEAR_COMMAND in state.message()
    assert state.until_text() == ""


@pytest.mark.parametrize(
    "raw",
    [
        b"\xff\xfe\x00garbage",
        b'{"until": 2e9, "reason": "\xff"}',
        b"\x00" * 10,
        b" " * (fly_pause.MAX_RECORD_BYTES + 1),
    ],
)
def test_undecodable_or_huge_bytes_fail_closed(tmp_path, raw):
    path = tmp_path / "p.json"
    path.write_bytes(raw)
    state = fly_pause.read_pause(path, now=1_900_000_000.0)
    assert state.paused and state.unreadable
    assert fly_pause.CLEAR_COMMAND in state.message()


def test_symlinks_fail_closed_dangling_or_not(tmp_path):
    target = tmp_path / "real.json"
    fly_pause.set_pause(target, reason="deploy")
    dangling = tmp_path / "dangling.json"
    dangling.symlink_to(tmp_path / "missing.json")
    live = tmp_path / "live.json"
    live.symlink_to(target)
    loop = tmp_path / "loop.json"  # noqa: F811
    loop.symlink_to(loop)
    for path in (dangling, live, loop):
        state = fly_pause.read_pause(path)
        assert state.paused and state.unreadable, path
        assert fly_pause.CLEAR_COMMAND in state.message()


def test_a_fifo_fails_closed_without_blocking(tmp_path):
    import os

    path = tmp_path / "p.json"
    os.mkfifo(path)
    state = fly_pause.read_pause(path)
    assert state.paused and state.unreadable


def test_a_record_past_the_twelve_hour_cap_fails_closed(tmp_path):
    path = tmp_path / "p.json"
    now = 1_900_000_000.0
    path.write_text(
        json.dumps({"until": now + fly_pause.MAX_PAUSE_SECONDS + 3600, "reason": "x"})
    )
    state = fly_pause.read_pause(path, now=now)
    assert state.paused and state.unreadable
    path.write_text(
        json.dumps({"until": now + fly_pause.MAX_PAUSE_SECONDS, "reason": "x"})
    )
    state = fly_pause.read_pause(path, now=now)
    assert state.paused and not state.unreadable and state.until_text()


@pytest.mark.parametrize(
    "until", [1e300, 1e18, 9e15, 1e12, float("nan"), float("inf"), -1e300]
)
def test_message_never_raises_whatever_the_state_holds(until):
    for state in (fly_pause.PauseState(True, until, "x"),
                  fly_pause.PauseState(True, until, object()),  # type: ignore[arg-type]
                  fly_pause.PauseState(True, until, "x", unreadable=True)):  # fmt: skip
        assert state.message().startswith("Fly paused")
        assert isinstance(state.until_text(), str)
        fly_runner.FlyPaused(state)
        fly_runner.FlyPausedError(state)


_FAR_FUTURE = ['{"until": 1e18, "reason": "x"}', '{"until": 9e15, "reason": "x"}',
               '{"until": 1e300, "reason": "x"}', '{"until": 1' + "0" * 400 + ', "reason": "x"}',
               b"\xff\xfe"]  # fmt: skip


def _write_bad(path: Path, content) -> None:
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content)


@pytest.mark.parametrize("content", _FAR_FUTURE)
def test_a_bad_record_is_the_clean_refusal_on_every_surface(fly_env, monkeypatch, caplog,
                                                            capsys, tmp_path, content):  # fmt: skip
    caplog.set_level(logging.WARNING)
    fake = _codex_fake(monkeypatch)
    clock = Clock()
    executor, minted, _ = make_executor(
        fake, record={"exit_code": 0, "stdout": "x"}, clock=clock
    )
    run_id = _seed_marked_login(executor, clock)
    transport = _counted(executor, fake)
    monkeypatch.setattr(fly_runner, "_EXECUTOR", executor)
    _write_bad(_pause_file(executor), content)

    with pytest.raises(fly_runner.FlyPausedError) as exc:  # _call
        asyncio.run(executor.client.get("m-codex"))
    assert fly_pause.CLEAR_COMMAND in str(exc.value)
    with pytest.raises(fly_runner.FlyPaused):  # run()
        asyncio.run(executor.run(_codex_job()))
    with pytest.raises(fly_runner.FlyPaused):
        fly_runner.refuse_if_paused()
    assert asyncio.run(fly_runner.settle_marked_reservations()) == []
    assert asyncio.run(executor.reap()) == []
    assert asyncio.run(executor._settle(run_id)) is False
    assert executor.ledger.entry(run_id)["unconfirmed"] is True
    assert transport.seen == [] and minted == []
    assert _cli().main(["show"]) == 1
    assert fly_pause.CLEAR_COMMAND in capsys.readouterr().out
    assert fly_pause.CLEAR_COMMAND in _lens_review().fly_pause_message()


def test_clear_on_a_directory_is_refused_not_a_traceback(fly_env, capsys):
    path = _pause_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.mkdir()
    assert _cli().main(["clear"]) == 2
    assert "remove it by hand" in capsys.readouterr().err
    assert fly_pause.read_pause(path).paused


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
    assert "neotoma deploy" not in str(exc.value)  # the reason stays in the record
    assert exc.value.state.reason == "neotoma deploy"
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
    assert "neotoma deploy" not in result.error and result.fly_paused
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


# ── A pause that begins during a login-machine claim ──────────────────────────


def _pause_after(executor, fake, match):
    """A transport that sets the pause right after the first request *match*
    accepts, and records every request sent once the pause is in force."""
    transport = _counted(executor, fake)
    after: list[httpx.Request] = []
    state = {"on": False}

    def handler(request):
        if state["on"]:
            after.append(request)
        response = transport.handler(request)
        if not state["on"] and match(request):
            state["on"] = True
            _pause(executor, reason="neotoma deploy")
        return response

    executor.client = fly_runner.FlyMachinesClient(
        executor.config, transport=httpx.MockTransport(handler))  # fmt: skip
    return after


_CLAIM_STAGES = {
    # after the reconcile/pre-read GET, before the lease is asked for
    "before_lease": lambda r: (
        r.method == "GET" and r.url.path.endswith("/machines/m-codex")
    ),
    # after the lease is granted, before the config write
    "before_config_write": lambda r: (
        r.method == "POST" and r.url.path.endswith("/lease")
    ),
    # after the config write, before the start
    "before_start": lambda r: (
        r.method == "POST" and r.url.path.endswith("/machines/m-codex")
    ),
}


@pytest.mark.parametrize("stage", sorted(_CLAIM_STAGES))
def test_a_pause_during_a_login_claim_is_the_same_refusal(fly_env, monkeypatch, stage):
    fake = _codex_fake(monkeypatch)
    clock = Clock()
    executor, _, revoked = make_executor(fake, record={"exit_code": 0, "stdout": "x"},
                                         clock=clock)  # fmt: skip
    after = _pause_after(executor, fake, _CLAIM_STAGES[stage])
    job = _codex_job()
    with pytest.raises(fly_runner.FlyPaused) as exc:
        asyncio.run(executor.run(job))
    assert not isinstance(exc.value, fly_runner.FlyLaunchError)
    assert str(exc.value).startswith("Fly paused until ")
    assert exc.value.cooled_until  # the retry time travels with it
    assert after == []  # nothing asked of Fly once the pause began
    assert job.launch_requested is False
    assert fake.machines["m-codex"]["state"] == "stopped"  # never started
    entry = executor.ledger.entry(job.run_id)
    assert revoked  # the job's token is still revoked
    if stage == "before_lease":
        # No lease taken and nothing written: the reservation is released
        # locally with no Fly call (``after`` is empty), as nothing is unknown.
        assert entry is None and "m-codex" not in fake.leases
        return
    # Nothing that could not be confirmed is released: the reservation and the
    # lease the claim took are kept, marked, for after the pause.
    assert entry is not None and entry["unconfirmed"] is True
    assert entry.get("lease_nonce") and "m-codex" in fake.leases

    fly_pause.clear_pause(_pause_file(executor))
    assert asyncio.run(executor.settle_marked()) == [job.run_id]
    assert executor.ledger.entry(job.run_id) is None


def test_a_pause_during_the_lease_request_is_a_pause_refusal(fly_env, monkeypatch):
    fake = _codex_fake(monkeypatch)
    executor, _, _ = make_executor(
        fake, record={"exit_code": 0, "stdout": "x"}, clock=Clock()
    )
    transport = _counted(executor, fake)

    async def acquire_lease(*a, **k):
        _pause(executor)
        return await fly_runner.FlyMachinesClient.acquire_lease(
            executor.client, *a, **k
        )

    executor.client.acquire_lease = acquire_lease
    with pytest.raises(fly_runner.FlyPaused) as exc:
        asyncio.run(executor._acquire_lease("m-codex", _codex_job(), time.time() + 600))
    assert "Fly paused until" in str(exc.value)
    assert not any(r.url.path.endswith("/lease") for r in transport.seen)


def test_a_pause_between_wait_reads_keeps_polling_not_unknown(fly_env, monkeypatch):
    fake = _codex_fake(monkeypatch)
    clock = Clock()
    executor, _, _ = make_executor(
        fake, record={"exit_code": 0, "stdout": "done"}, clock=clock
    )
    calls = {"n": 0}
    real_wait = executor.client.wait

    async def wait(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:  # paused just after the loop's own check, then cleared
            raise fly_runner.FlyPausedError(
                fly_pause.PauseState(True, time.time() + 60, "x")
            )
        return await real_wait(*a, **k)

    executor.client.wait = wait

    async def no_sleep(seconds):
        clock.now += seconds

    executor._sleep = no_sleep
    job = _codex_job()
    rc, out, _ = asyncio.run(executor.run(job))
    assert (rc, out) == (0, b"done") and job.outcome_unknown is False
    assert calls["n"] >= 2


# ── Every Fly call site in dispatch: a pause never yields a local run ─────────


def test_dispatch_carries_the_retry_time_when_paused_up_front(
    fly_env, seam, monkeypatch
):
    spawned, fake_exec = seam
    monkeypatch.setenv("APIS_FLY_RUNNER_CLASSES", "carry_forward_check")
    state = _pause(reason="neotoma deploy")
    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        result = _once(action_class="carry_forward_check", model="m", repository="owner/repo",
                       github_token="t-local")  # fmt: skip
    assert not result.ok and spawned == []
    assert result.error.startswith("fly runner refused: Fly paused until ")
    assert result.cooled_until == state.until_text() != ""
    assert result.fly_nothing_launched is True


def test_dispatch_with_an_unreadable_pause_has_no_retry_time(
    fly_env, seam, monkeypatch
):
    spawned, fake_exec = seam
    monkeypatch.setenv("APIS_FLY_RUNNER_CLASSES", "carry_forward_check")
    path = _pause_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"until": 1e18, "reason": "x"}')
    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        result = _once(action_class="carry_forward_check", model="m", repository="owner/repo",
                       github_token="t-local")  # fmt: skip
    assert not result.ok and spawned == [] and result.cooled_until == ""
    assert fly_pause.CLEAR_COMMAND in result.error


@pytest.mark.parametrize("where", ["mint", "create"])
def test_dispatch_paused_mid_launch_is_the_same_refusal(
    fly_env, seam, monkeypatch, where
):
    spawned, fake_exec = seam
    monkeypatch.setenv("APIS_FLY_RUNNER_CLASSES", "carry_forward_check")
    fake = FakeFly()
    executor, _, _ = make_executor(fake, record={"exit_code": 0, "stdout": "x"})
    transport = _counted(executor, fake)
    monkeypatch.setattr(fly_runner, "get_executor", lambda: executor)
    if where == "mint":
        real_mint = executor._mint

        def mint(repo, prefix):
            token = real_mint(repo, prefix)
            _pause(executor)
            return token

        executor._mint = mint
    else:
        real_create = executor.client.create

        async def create(body):
            _pause(executor)
            return await real_create(body)

        executor.client.create = create
    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        result = _once(action_class="carry_forward_check", model="m", repository="owner/repo",
                       github_token="t-local")  # fmt: skip
    assert not result.ok and spawned == []  # never run locally
    assert result.error.startswith("fly runner refused: Fly paused until ")
    assert result.cooled_until
    assert transport.seen == []


# ── Lens review: the last (or only) lens refused by a pause ───────────────────


@pytest.mark.parametrize("lens", ["pm", "all"])
def test_lens_review_a_lens_refused_by_a_pause_never_falls_back(
    fly_env, tmp_path, capsys, lens
):
    review = _lens_review()
    dispatched = []

    def run_lens(**kw):
        dispatched.append(kw["lens"])
        if len(dispatched) == (1 if lens == "pm" else 5):
            _pause(reason="neotoma deploy")  # set while the last lens ran
            raise review.FallBack("fly runner refused: Fly paused", {"attempts": []})
        return {
            "lens": kw["lens"],
            "role": "r",
            "verdict": "APPROVE",
            "provider": "p",
            "duration_s": 1,
            "verdict_file": str(tmp_path / f"{kw['lens']}.md"),
        }

    with patch.object(review, "run_lens", run_lens):
        rc = review.main(
            ["--repo", "owner/repo", "--pr", "1", "--lens", lens,
             "--task-entity-id", "ent_x", "--out-dir", str(tmp_path / "out")],
            head_resolver=lambda repo, pr: "a" * 40,
            files_resolver=lambda repo, pr: ["execution/daemons/apis/fly_runner.py"],
        )  # fmt: skip
    out = capsys.readouterr().out
    assert rc == 5
    assert "FALL BACK TO LOCAL LENS" not in out
    summary = json.loads((tmp_path / "out" / "summary.json").read_text())
    last = summary["lenses"][-1]
    assert last["lens"] == dispatched[-1] and last["status"] == "not_run"
    assert "Fly paused" in last["reason"]


# ── Round 3: one boundary per class ───────────────────────────────────────────


_REAL_CALL = fly_runner.FlyMachinesClient._call


def _paused_at_call(executor, n):
    """Make the *n*th Machines API call (0-based) set the pause just before
    it is attempted; returns the calls that actually reached the transport
    (a call refused by the pause is not one)."""
    sent: list[tuple[str, str]] = []
    attempts = [0]

    async def call(client, method, path, **kw):
        if attempts[0] == n:
            _pause(executor, reason="neotoma deploy")
        attempts[0] += 1
        try:
            return await _REAL_CALL(client, method, path, **kw)
        except fly_runner.FlyPausedError:
            raise  # refused before the transport: not sent
        finally:
            if not client.config.pause_state().paused:
                sent.append((method, path))

    return sent, call


def _launch_calls(make, job_factory, monkeypatch):
    """The Machines API calls of an unpaused launch, up to and including the
    create or start: the indexes the sweep raises the pause at."""
    executor, fake = make()
    calls, call = _paused_at_call(executor, 10**6)
    monkeypatch.setattr(fly_runner.FlyMachinesClient, "_call", call)
    asyncio.run(executor.run(job_factory()))
    monkeypatch.setattr(fly_runner.FlyMachinesClient, "_call", _REAL_CALL)
    ends = [
        i
        for i, (m, p) in enumerate(calls)
        if m == "POST" and (p.endswith("/start") or p.rstrip("/").endswith("/machines"))
    ]
    assert ends, calls
    return ends[0] + 1


def _fresh_ledger(tmp_path) -> Path:
    n = len(list(tmp_path.glob("sweep-*")))
    path = tmp_path / f"sweep-{n}"
    path.mkdir()
    return path / "fly-runner-ledger.json"


def _codex_with_dead_reservation(monkeypatch, tmp_path):
    def make():
        fake = _codex_fake(monkeypatch)
        clock = Clock()
        executor, _, _ = make_executor(fake, record={"exit_code": 0, "stdout": "x"},
                                       clock=clock, ledger_path=_fresh_ledger(tmp_path))  # fmt: skip
        # A reservation from a previous job that _reconcile_login must settle.
        run_id = _seed_marked_login(executor, clock)
        fake.machines["m-codex"]["config"] = _job_config(run_id)
        return executor, fake

    return make


def test_sweep_a_pause_at_every_launch_call_is_the_pause_refusal(
    fly_env, monkeypatch, tmp_path
):
    """Class 1, executor boundary: whichever Machines API call the pause
    lands on (reconcile of a previous reservation, read, settle, lease,
    config write, start), run() ends as FlyPaused, never FlyLaunchError.
    RED on the round-2 head at the _reconcile_login indexes (FlyLaunchError)."""
    make = _codex_with_dead_reservation(monkeypatch, tmp_path)
    total = _launch_calls(make, _codex_job, monkeypatch)
    assert total >= 4  # reconcile + claim + start are all swept
    for n in range(total):
        monkeypatch.setenv("APIS_FLY_CODEX_MACHINE_ID", "m-codex")
        executor, fake = make()
        calls, call = _paused_at_call(executor, n)
        monkeypatch.setattr(fly_runner.FlyMachinesClient, "_call", call)
        job = _codex_job()
        with pytest.raises(fly_runner.FlyPaused) as exc:
            asyncio.run(executor.run(job))
        assert not isinstance(exc.value, fly_runner.FlyLaunchError), n
        assert str(exc.value).startswith("Fly paused until "), n
        assert len(calls) == n, (n, calls)  # nothing sent once paused
        assert fake.machines["m-codex"]["state"] == "stopped", n  # never started
        fly_pause.clear_pause(_pause_file(executor))


def test_sweep_dispatch_paused_at_each_call_never_runs_locally(
    fly_env, seam, monkeypatch, tmp_path
):
    """Class 1, dispatch boundary, with local fallback on: a pause landing on
    any Machines API call of a launch, up to its create, is the pause
    refusal, never a local run and never "Fly launch failed"."""
    spawned, fake_exec = seam
    monkeypatch.setenv("APIS_FLY_RUNNER_CLASSES", "carry_forward_check")
    monkeypatch.setenv("APIS_FLY_LOCAL_FALLBACK", "1")

    def make():
        fake = FakeFly()
        executor, _, _ = make_executor(fake, record={"exit_code": 0, "stdout": "x"},
                                       ledger_path=_fresh_ledger(tmp_path))  # fmt: skip
        return executor, fake

    total = _launch_calls(make, _job, monkeypatch)
    for n in range(total):
        executor, _ = make()
        monkeypatch.setattr(fly_runner, "get_executor", lambda: executor)
        calls, call = _paused_at_call(executor, n)
        monkeypatch.setattr(fly_runner.FlyMachinesClient, "_call", call)
        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            result = _once(action_class="carry_forward_check", model="m",
                           repository="owner/repo", github_token="t-local")  # fmt: skip
        assert spawned == [], n  # never run locally
        assert not result.ok and result.fly_paused is True, (n, result.error)
        assert "launch failed" not in result.error, n
        assert result.error.startswith("fly runner refused: Fly paused until "), n
        assert len(calls) == n, (n, calls)  # nothing sent once paused
        fly_pause.clear_pause(_pause_file(executor))


@pytest.mark.parametrize("exc_type", ["launch", "ineligible", "other_refusal"])
def test_the_local_fallback_branch_refuses_whenever_paused(
    fly_env, seam, monkeypatch, exc_type
):
    """Class 1, the one local-fallback branch: whatever the failure, while a
    pause is in force nothing runs locally. RED on the round-2 head for a
    FlyLaunchError raised with the pause in force (it ran locally)."""
    spawned, fake_exec = seam
    monkeypatch.setenv("APIS_FLY_RUNNER_CLASSES", "carry_forward_check")
    monkeypatch.setenv("APIS_FLY_LOCAL_FALLBACK", "1")
    executor, _, _ = make_executor(FakeFly(), record={"exit_code": 0, "stdout": "x"})
    monkeypatch.setattr(fly_runner, "get_executor", lambda: executor)
    errors = {
        "launch": fly_runner.FlyLaunchError("machine refused (409)"),
        "ineligible": fly_runner.FlyIneligible("image unknown"),
        "other_refusal": fly_runner.FlyLaunchError("network down"),
    }

    async def run(job):
        _pause(executor, reason="neotoma deploy")
        raise errors[exc_type]

    monkeypatch.setattr(executor, "run", run)
    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        result = _once(action_class="carry_forward_check", model="m", repository="owner/repo",
                       github_token="t-local")  # fmt: skip
    assert spawned == []
    assert result.fly_paused is True and result.fly_nothing_launched is True
    assert result.error.startswith("fly runner refused: Fly paused until ")


_ALARMING_REASON = (
    "usage limit window: quota exceeded, rate limit 429, capacity, overloaded, "
    "session limit, login required, unauthorized 401, auth expired"
)


def test_the_pause_reason_never_reaches_a_failure_classifier(
    fly_env, seam, monkeypatch
):
    """Class 2: a reason full of capacity, quota, rate-limit and auth words
    never enters SkillResult.error, and the provider loop ends on the
    structured flag with no cooldown or failover. RED on the round-2 head:
    the reason was in error and classified as capacity."""
    spawned, fake_exec = seam
    monkeypatch.setenv("APIS_FLY_RUNNER_CLASSES", "carry_forward_check")
    state = _pause(reason=_ALARMING_REASON)
    assert state.reason == _ALARMING_REASON  # the record keeps it
    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        result = _once(action_class="carry_forward_check", model="m", repository="owner/repo",
                       github_token="t-local")  # fmt: skip
    assert spawned == [] and result.fly_paused is True
    for word in (
        "usage",
        "quota",
        "rate",
        "capacity",
        "login",
        "unauthorized",
        "401",
        "429",
    ):
        assert word not in result.error.lower(), word
    import skill_runner

    assert (
        skill_runner._provider_failure_kind(result.error, result.stderr, result.stdout)
        is None
    )
    # Mid-run refusal text too (FlyPaused, FlyPausedError): no reason.
    assert _ALARMING_REASON not in str(fly_runner.FlyPaused(state))
    assert _ALARMING_REASON not in str(fly_runner.FlyPausedError(state))


from test_fly_provider_failover import loop  # noqa: E402,F401 - fixture, used by name


def test_the_provider_loop_ends_on_a_pause_without_cooling(loop):  # noqa: F811
    """Class 2, the provider loop: a pause whose reason reads as capacity and
    auth failures ends the dispatch on its structured flag, before any
    classifier: one attempt, no provider cooled, no failover, no local run.
    RED on the round-2 head: the reason classified as capacity, the provider
    was cooled and the next one tried."""
    run, seen = loop
    _pause(reason=_ALARMING_REASON)
    result = run(["claude", "codex", "cursor"])
    assert result.fly_paused is True and not result.ok
    assert seen["attempts"] == ["claude"]  # no failover
    assert seen["cooled"] == []  # no cooldown
    assert seen["spawned"] == []  # no local run
    assert _ALARMING_REASON not in result.error
