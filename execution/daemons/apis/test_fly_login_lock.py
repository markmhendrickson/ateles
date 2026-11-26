"""Login machines must not be left locked (execution/daemons/apis/fly_runner.py).

A stopped login machine keeping a job that no reservation accounts for refuses
every later run ("it holds another run's job"). Three ways in: a claim update
Fly applied after the reservation was released on a clean read; a start Fly
refused with 412 "machine build not ready" and never got again; and a job left
behind with nothing in the ledger to clean it up. A refused dispatch must also
leave the run id its diagnosis is filed under.

The Fly API is the ``FakeFly`` double from ``test_fly_runner``; no test touches
the network.

What these looked like RED: run against the unchanged ``fly_runner.py``,
``skill_runner.py`` and ``dispatch_role.py``, the tests of a late claim, the
retried start, the orphaned config and the run id fail. The ones marked
"preserved" pass there too: they hold refusals and releases that must not move,
and each guard test of the orphan clear fails with its guard removed.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import fcntl
import json
import os
from unittest.mock import patch

import httpx
import pytest

import dispatch_role
import fly_runner
import skill_runner
import test_fly_runner
from test_fly_runner import (
    EXHAUSTING,
    Clock,
    FakeFly,
    _claim_backoff_executor,
    _codex_fake,
    _codex_job,
    _dead_pid,
    _job_config,
    _once,
    make_executor,
)

# Fixtures of the Fly runner tests, used here by name.
fly_env, seam = test_fly_runner.fly_env, test_fly_runner.seam

MACHINE = "m-codex"
OK_RECORD = {"exit_code": 0, "stdout": "ok"}
NOT_READY = "failed_precondition: machine build not ready, retry the start request"


@pytest.fixture(autouse=True)
def _no_settle_delay(monkeypatch):
    monkeypatch.setattr(fly_runner, "LOGIN_SETTLE_POLL_SECONDS", 0.0)


def _route(executor, handler) -> None:
    executor.client = fly_runner.FlyMachinesClient(
        executor.config, transport=httpx.MockTransport(handler))  # fmt: skip


def _is_claim(request) -> bool:
    return (request.method == "POST" and request.url.path.endswith(f"/machines/{MACHINE}")
            and bool(json.loads(request.content)["config"].get("files")))  # fmt: skip


def _is_clear(request) -> bool:
    return (request.method == "POST" and request.url.path.endswith(f"/machines/{MACHINE}")
            and not json.loads(request.content)["config"].get("files"))  # fmt: skip


def _updates(fake: FakeFly) -> list:
    return [r for r in fake.requests
            if r.method == "POST" and r.url.path.endswith(f"/machines/{MACHINE}")]  # fmt: skip


def _has_job(fake: FakeFly) -> bool:
    return fly_runner._has_job_config(fake.machines[MACHINE]["config"])


class SlowClaim:
    """Claim updates Fly takes but has not applied: the caller gets *answer*
    while every read still serves the old config at the old version.
    ``promote()`` applies the update, as Fly did minutes later."""

    def __init__(self, fake: FakeFly, answer):
        self.fake, self.answer, self.slow = fake, answer, True
        self.pending: list[dict] = []
        self.claims = 0

    def handler(self, request):
        if _is_claim(request):
            self.claims += 1
            if self.slow:
                self.fake.requests.append(request)
                self.pending.append(json.loads(request.content)["config"])
                if self.answer == "read-timeout":
                    raise httpx.ReadTimeout("injected: no answer")
                if self.answer == 200:
                    return httpx.Response(200, json={"id": MACHINE, "instance_id": "v1+"})
                return httpx.Response(self.answer, json={"error": "deadline exceeded"})
        return self.fake.handler(request)

    def promote(self) -> None:
        machine = self.fake.machines[MACHINE]
        machine["config"] = self.pending.pop()
        machine["instance_id"] += "+"


def _slow_claim(monkeypatch, answer=408, record=OK_RECORD):
    fake = _codex_fake(monkeypatch)
    clock = Clock()
    executor, _, _ = make_executor(fake, clock=clock, record=record)
    slow = SlowClaim(fake, answer)
    _route(executor, slow.handler)

    async def no_wait(seconds):  # a claim's backoff takes no time here
        return None

    executor._sleep = no_wait
    return fake, clock, executor, slow


def _fail_the_claim(executor, raised=fly_runner.FlyLaunchError):
    job = _codex_job()
    with pytest.raises(raised):
        asyncio.run(executor.run(job))
    return job


# ── 1. No release on a clean read while Fly may still apply the claim ───────


@pytest.mark.parametrize("answer", [408, 500, "read-timeout", 200])
def test_a_claim_fly_applies_late_keeps_its_reservation_until_it_is_cleared(
        fly_env, monkeypatch, answer):  # fmt: skip
    fake, clock, executor, slow = _slow_claim(monkeypatch, answer)
    # An accepted update (200) that is not seen is retried like a reverted one
    # and ends as a retry-later refusal; an unclear answer is not sent again.
    raised = fly_runner.FlyClaimRefused if answer == 200 else fly_runner.FlyLaunchError
    job = _fail_the_claim(executor, raised)
    entry = executor.ledger.entry(job.run_id)
    assert entry is not None and entry["unconfirmed"] is True
    assert MACHINE in fake.leases  # the machine stays this run's
    assert not fake.calls("POST", "/start")

    # The periodic step reads the same clean, old machine: still held.
    assert asyncio.run(executor.settle_marked()) == []
    assert executor.ledger.entry(job.run_id) is not None
    # And the next launch does not claim it.
    claims = slow.claims
    with pytest.raises(fly_runner.FlyLaunchError, match="not confirmed stopped"):
        asyncio.run(executor.run(_codex_job()))
    assert slow.claims == claims

    slow.promote()  # Fly applies it
    assert _has_job(fake)
    assert asyncio.run(executor.settle_marked()) == [job.run_id]
    assert not _has_job(fake) and MACHINE not in fake.leases
    assert executor.ledger.entry(job.run_id) is None
    slow.slow = False
    rc, _, _ = asyncio.run(executor.run(_codex_job()))
    assert rc == 0


def test_a_claim_never_applied_is_released_only_after_the_pending_window(fly_env, monkeypatch):
    fake, clock, executor, _ = _slow_claim(monkeypatch)
    job = _fail_the_claim(executor)
    sent = executor.ledger.entry(job.run_id)["claim_write_at"]
    clock.now = sent + fly_runner.LOGIN_PENDING_WRITE_SECONDS - 1
    assert asyncio.run(executor.settle_marked()) == []
    assert executor.ledger.entry(job.run_id) is not None
    clock.now = sent + fly_runner.LOGIN_PENDING_WRITE_SECONDS
    assert asyncio.run(executor.settle_marked()) == [job.run_id]
    assert executor.ledger.snapshot(clock.now)["in_flight"] == {}
    assert MACHINE not in fake.leases


def test_a_claim_applied_after_the_window_is_cleared_as_an_orphan(fly_env, monkeypatch):
    fake, clock, executor, slow = _slow_claim(monkeypatch)
    job = _fail_the_claim(executor)
    clock.now += fly_runner.LOGIN_PENDING_WRITE_SECONDS
    assert asyncio.run(executor.settle_marked()) == [job.run_id]
    slow.promote()
    assert _has_job(fake) and executor.ledger.snapshot(clock.now)["in_flight"] == {}
    asyncio.run(executor.settle_marked())
    assert not _has_job(fake) and MACHINE not in fake.leases


def test_a_machine_still_updating_keeps_its_reservation(fly_env, monkeypatch):
    fake = _codex_fake(monkeypatch)
    clock = Clock()
    executor, _, _ = make_executor(fake, clock=clock)

    def updating(request):
        if _is_claim(request):  # the version has moved, the update is not done
            fake.never_settles = True
            fake.machines[MACHINE].update(
                {"instance_id": "v2", "state": "replacing", "_settle": 0})
            return httpx.Response(408, json={"error": "deadline exceeded"})
        return fake.handler(request)

    _route(executor, updating)
    job = _fail_the_claim(executor)
    assert executor.ledger.entry(job.run_id)["unconfirmed"] is True
    clock.now += fly_runner.LOGIN_PENDING_WRITE_SECONDS * 2
    assert asyncio.run(executor.settle_marked()) == []
    assert executor.ledger.entry(job.run_id) is not None


def test_a_dead_dispatchers_pending_claim_is_not_reaped_on_a_clean_read(fly_env, monkeypatch):
    fake, clock, executor, slow = _slow_claim(monkeypatch)
    job = _fail_the_claim(executor)
    data = executor.ledger._load()
    data["owners"][executor.instance_id]["pid"] = _dead_pid()  # that process exited
    executor.ledger.owner = None
    executor.ledger._save(data)
    other, _, _ = make_executor(fake, clock=clock)
    assert asyncio.run(other.reap()) == []
    assert other.ledger.entry(job.run_id) is not None
    slow.promote()
    assert asyncio.run(other.reap()) == [job.run_id]
    assert not _has_job(fake)


def test_a_claim_with_a_moved_version_and_no_job_is_released_at_once(fly_env, monkeypatch):
    # preserved: the update settled (reverted), so the clean read is proof.
    fake = _codex_fake(monkeypatch)
    executor, _, _ = make_executor(fake)

    def reverted(request):
        if _is_claim(request):
            fake.machines[MACHINE]["instance_id"] = "v2"
            return httpx.Response(500, json={"error": "update reverted"})
        return fake.handler(request)

    _route(executor, reverted)
    job = _fail_the_claim(executor)
    assert executor.ledger.entry(job.run_id) is None and MACHINE not in fake.leases


@pytest.mark.parametrize("answer", [422, 404, 429, "connect"])
def test_a_claim_that_provably_did_nothing_is_released_at_once(fly_env, monkeypatch, answer):
    # preserved: nothing was or will be stored.
    fake = _codex_fake(monkeypatch)
    executor, _, _ = make_executor(fake)

    def rejected(request):
        if _is_claim(request):
            if answer == "connect":
                raise httpx.ConnectError("injected: refused")
            return httpx.Response(answer, json={"error": "rejected"})
        return fake.handler(request)

    _route(executor, rejected)
    job = _fail_the_claim(executor)
    assert executor.ledger.entry(job.run_id) is None and MACHINE not in fake.leases


# ── 3. Fly's "machine build not ready" start refusal is retried, bounded ────


def _start_answers(monkeypatch, *answers, record=OK_RECORD):
    """An executor whose n-th start gets ``answers[n]``: an (status, text)
    pair, a callable returning one, or (past the end) the fake's own answer."""
    fake = _codex_fake(monkeypatch)
    executor, clock, slept, free, _ = _claim_backoff_executor(fake, record=record)
    starts: list = []

    def handler(request):
        if request.method == "POST" and request.url.path.endswith("/start"):
            starts.append(request)
            if len(starts) <= len(answers):
                answer = answers[len(starts) - 1]
                status, text = answer(fake) if callable(answer) else answer
                return httpx.Response(status, json={"error": text})
        return fake.handler(request)

    _route(executor, handler)
    return fake, executor, clock, starts, slept, free


def _assert_released(fake, executor, job):
    assert executor.ledger.entry(job.run_id) is None
    assert not _has_job(fake) and MACHINE not in fake.leases


def test_a_start_refused_as_not_ready_is_retried_until_it_starts(fly_env, monkeypatch):
    fake, executor, _, starts, slept, free = _start_answers(
        monkeypatch, (412, NOT_READY), (412, NOT_READY))
    job = _codex_job()
    rc, _, _ = asyncio.run(executor.run(job))
    assert rc == 0 and len(starts) == 3
    assert slept == list(fly_runner.LOGIN_START_BACKOFF_SECONDS[:2])
    assert free == [True, True]  # other dispatches are not stalled meanwhile
    assert len(fake.calls("POST", "/start")) == 1  # one start ever took effect
    _assert_released(fake, executor, job)


def test_the_start_retry_is_bounded_and_then_cleans_up(fly_env, monkeypatch):
    bound = len(fly_runner.LOGIN_START_BACKOFF_SECONDS) + 1
    fake, executor, _, starts, slept, _ = _start_answers(
        monkeypatch, *[(412, NOT_READY)] * (bound + 3))
    job = _codex_job()
    with pytest.raises(fly_runner.FlyLaunchError, match="build not ready"):
        asyncio.run(executor.run(job))
    assert len(starts) == bound == 5
    assert slept == list(fly_runner.LOGIN_START_BACKOFF_SECONDS)
    _assert_released(fake, executor, job)


@pytest.mark.parametrize(("status", "text", "error"), [
    (412, "failed_precondition: machine version changed", fly_runner.FlyLaunchError),
    (422, NOT_READY, fly_runner.FlyLaunchError),
    (409, NOT_READY, fly_runner.FlyUncertainLaunch),
    (500, NOT_READY, fly_runner.FlyUncertainLaunch),
])  # fmt: skip
def test_only_the_not_ready_412_is_retried(fly_env, monkeypatch, status, text, error):
    # preserved: every other start failure is handled as before, with one start.
    _, executor, _, starts, slept, _ = _start_answers(monkeypatch, (status, text))
    with pytest.raises(error):
        asyncio.run(executor.run(_codex_job()))
    assert len(starts) == 1 and slept == []


def test_an_unclear_answer_to_a_retried_start_is_never_sent_again(fly_env, monkeypatch):
    fake, executor, _, starts, _, _ = _start_answers(
        monkeypatch, (412, NOT_READY), (500, "response lost"))
    job = _codex_job()
    with pytest.raises(fly_runner.FlyUncertainLaunch):
        asyncio.run(executor.run(job))
    assert len(starts) == 2
    _assert_released(fake, executor, job)  # settled: stopped, cleared, charged


def test_a_machine_found_running_after_a_refusal_is_not_started_again(fly_env, monkeypatch):
    def runs_anyway(fake):
        fake.machines[MACHINE]["state"] = "started"
        return 412, NOT_READY

    fake, executor, _, starts, _, _ = _start_answers(monkeypatch, runs_anyway)
    job = _codex_job()
    with pytest.raises(fly_runner.FlyUncertainLaunch):
        asyncio.run(executor.run(job))
    assert len(starts) == 1 and fake.calls("POST", "/stop")
    _assert_released(fake, executor, job)


@pytest.mark.parametrize("config", [{"mounts": []}, _job_config("another-run")])
def test_a_machine_no_longer_carrying_the_job_is_not_started_again(
        fly_env, monkeypatch, config):  # fmt: skip
    def changed(fake):
        fake.machines[MACHINE]["config"] = copy.deepcopy(config)
        return 412, NOT_READY

    fake, executor, _, starts, _, _ = _start_answers(monkeypatch, changed)
    with pytest.raises(fly_runner.FlyLaunchError):
        asyncio.run(executor.run(_codex_job()))
    assert len(starts) == 1
    assert fake.machines[MACHINE]["config"] == config  # nothing written over it


def test_no_start_retry_wait_reaches_the_deadline(fly_env, monkeypatch):
    monkeypatch.setattr(fly_runner, "LOGIN_START_BACKOFF_SECONDS", (600.0,))
    fake, executor, _, starts, slept, _ = _start_answers(monkeypatch, (412, NOT_READY))
    job = _codex_job()
    with pytest.raises(fly_runner.FlyLaunchError):
        asyncio.run(executor.run(job))
    assert len(starts) == 1 and slept == []
    _assert_released(fake, executor, job)


def test_no_start_is_sent_once_the_deadline_has_passed(fly_env, monkeypatch):
    # preserved: the read-back itself used up the run's time.
    fake, executor, clock, starts, _, _ = _start_answers(monkeypatch)
    inner = executor.client._transport.handler
    claimed: list = []

    def slow_read_back(request):
        if _is_claim(request):
            claimed.append(request)
        elif claimed and request.method == "GET" and len(claimed) == 1:
            claimed.append(request)
            clock.now += 10_000
        return inner(request)

    _route(executor, slow_read_back)
    job = _codex_job()
    with pytest.raises(fly_runner.FlyRefusal, match="deadline before start"):
        asyncio.run(executor.run(job))
    assert starts == []
    _assert_released(fake, executor, job)


def test_a_deadline_passing_in_the_start_retry_wait_stops_it(fly_env, monkeypatch):
    fake, executor, clock, starts, _, _ = _start_answers(monkeypatch, (412, NOT_READY))

    async def overslept(seconds):
        clock.now += 10_000

    executor._sleep = overslept
    job = _codex_job()
    with pytest.raises(fly_runner.FlyRefusal, match="deadline before start"):
        asyncio.run(executor.run(job))
    assert len(starts) == 1
    _assert_released(fake, executor, job)


# ── 2. A job config nothing owns is cleared by the reaper and periodic step ─


def _orphaned(monkeypatch, *, run_id="fly-orphan"):
    fake = _codex_fake(monkeypatch)
    clock = Clock()
    executor, _, _ = make_executor(fake, clock=clock, record=OK_RECORD)
    fake.machines[MACHINE]["config"] = _job_config(run_id)
    return fake, clock, executor


@pytest.mark.parametrize("step", ["settle_marked", "reap"])
def test_an_orphaned_job_config_is_cleared(fly_env, monkeypatch, step):
    fake, _, executor = _orphaned(monkeypatch)
    asyncio.run(getattr(executor, step)())
    assert not _has_job(fake) and MACHINE not in fake.leases
    assert fake.lease_log == [("acquire", MACHINE), ("release", MACHINE)]
    assert fake.machines[MACHINE]["config"]["mounts"] == []  # the rest is untouched
    assert not fake.calls("POST", "/start") and not fake.calls("POST", "/stop")
    rc, _, _ = asyncio.run(executor.run(_codex_job()))
    assert rc == 0


def test_the_next_launch_of_a_new_process_heals_the_machine(fly_env, monkeypatch):
    fake, _, executor = _orphaned(monkeypatch)
    executor._last_reap = 0.0  # as a freshly started dispatcher
    rc, _, _ = asyncio.run(executor.run(_codex_job()))
    assert rc == 0 and not _has_job(fake)


def test_a_killed_runs_job_is_cleared_only_once_its_lease_is_gone(fly_env, monkeypatch):
    fake, _, executor = _orphaned(monkeypatch)  # nothing about it in this ledger
    fake.leases[MACHINE] = {"nonce": "theirs", "expires_at": 4_000_000_000,
                            "description": fly_runner._lease_description("fly-orphan")}
    asyncio.run(executor.settle_marked())
    assert _has_job(fake) and _updates(fake) == []
    del fake.leases[MACHINE]  # it expired
    asyncio.run(executor.settle_marked())
    assert not _has_job(fake)


def test_a_refused_orphan_clear_is_tried_again_next_pass(fly_env, monkeypatch):
    fake, _, executor = _orphaned(monkeypatch)
    refused = []

    def once(request):
        if _is_clear(request) and not refused:
            refused.append(request)
            return httpx.Response(412, json={"error": "insufficient memory"})
        return fake.handler(request)

    _route(executor, once)
    asyncio.run(executor.settle_marked())
    assert _has_job(fake) and MACHINE not in fake.leases
    asyncio.run(executor.settle_marked())
    assert not _has_job(fake)


def _guard(case, fake, clock, executor, held):
    machine = fake.machines[MACHINE]
    if case == "leased_by_another":
        fake.leases[MACHINE] = {"nonce": "theirs", "description": "another dispatcher",
                                "expires_at": 4_000_000_000}
    elif case == "lease_unreadable":
        fake.lease_view_override = {"data": {}}
    elif case == "lease_not_granted":
        fake.lease_mode = "error"
    elif case == "running":
        machine["state"] = "started"
    elif case == "unattributed":
        machine["config"] = {"files": [{"guest_path": "/run/job/job.json", "raw_value": "eA=="}]}
    elif case == "machine_unreadable":
        fake.bad["get"] = "null"
    elif case in ("reserved_for_machine", "reserved_for_run"):
        run_id, machine_id = (("fly-other", MACHINE) if case == "reserved_for_machine"
                              else ("fly-orphan", "m-elsewhere"))  # fmt: skip
        executor.ledger.reserve(executor.config, run_id=run_id, owner=executor.instance_id,
                                max_run_seconds=600, login=True, machine_id=machine_id,
                                now=clock.now)  # fmt: skip
    elif case == "live_here":
        executor._active.add("fly-orphan")
    elif case == "login_lock_held_by_another_process":
        path = fly_runner._login_lock("codex", executor.config.ledger_path.parent).path
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        held.append(fd)
    elif case == "ledger_invalid":
        executor.ledger.path.write_text("{}")


@pytest.mark.parametrize("step", ["settle_marked", "reap"])
@pytest.mark.parametrize("case", [
    "leased_by_another", "lease_unreadable", "lease_not_granted", "running", "unattributed",
    "machine_unreadable", "reserved_for_machine", "reserved_for_run", "live_here",
    "login_lock_held_by_another_process", "ledger_invalid",
])  # fmt: skip
def test_a_job_that_may_still_be_owned_is_never_cleared(fly_env, monkeypatch, case, step):
    # preserved: ownership that cannot be ruled out leaves the machine alone.
    fake, clock, executor = _orphaned(monkeypatch)
    held: list[int] = []
    _guard(case, fake, clock, executor, held)
    before = copy.deepcopy(fake.machines[MACHINE]["config"])
    try:
        with contextlib.suppress(fly_runner.FlyLedgerError):
            asyncio.run(getattr(executor, step)())
    finally:
        for fd in held:
            os.close(fd)
    assert fake.machines[MACHINE]["config"] == before
    assert _updates(fake) == []
    assert not fake.calls("POST", "/stop")
    assert fake.lease_log == []  # not even a lease is taken on it


def test_a_run_in_this_process_holding_the_login_lock_is_never_cleared(fly_env, monkeypatch):
    fake, _, executor = _orphaned(monkeypatch)

    async def go():
        lock = fly_runner._login_lock("codex", executor.config.ledger_path.parent)
        await lock.acquire()
        try:
            await executor.settle_marked()
        finally:
            lock.release()

    asyncio.run(go())
    assert _has_job(fake) and _updates(fake) == []


@pytest.mark.parametrize("when", ["takes_the_lease", "ran_and_left_another_job"])
def test_a_dispatcher_arriving_between_the_reads_keeps_its_job(fly_env, monkeypatch, when):
    fake, _, executor = _orphaned(monkeypatch)
    theirs = _job_config("fly-theirs")

    def arrives(request):
        if request.method == "POST" and request.url.path.endswith("/lease"):
            fake.machines[MACHINE]["config"] = copy.deepcopy(theirs)
            if when == "takes_the_lease":
                fake.leases[MACHINE] = {"nonce": "theirs", "description": "another",
                                        "expires_at": 4_000_000_000}
        return fake.handler(request)

    _route(executor, arrives)
    asyncio.run(executor.settle_marked())
    assert fake.machines[MACHINE]["config"] == theirs and _updates(fake) == []


# ── 4. A refused or failed dispatch names its run id for the private record ─


def _refusal(fly_env, seam, monkeypatch, fake, **overrides):
    spawned, fake_exec = seam
    executor, _, _ = make_executor(fake, **overrides)
    monkeypatch.setattr(fly_runner, "get_executor", lambda: executor)
    jobs: list[str] = []
    real = executor.run

    async def run(job):
        jobs.append(job.run_id)
        return await real(job)

    monkeypatch.setattr(executor, "run", run)
    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        result = _once(runner="fly", repository="owner/repo", github_token="")
    assert spawned == []
    return result, jobs


@pytest.mark.parametrize("case", ["cap_refusal", "launch_failure", "machine_busy"])
def test_a_refused_or_failed_fly_dispatch_keeps_its_run_id(fly_env, seam, monkeypatch, case):
    fake = FakeFly()
    if case == "machine_busy":
        monkeypatch.setenv("APIS_FLY_CLAUDE_MACHINE_ID", "m-claude")
        fake.machines["m-claude"] = {"id": "m-claude", "state": "stopped", "login": True,
                                     "instance_id": "v1",
                                     "config": _job_config("fly-theirs")}  # fmt: skip
    else:  # Claude on a per-job machine, so the create path is the one exercised
        monkeypatch.setattr(fly_runner, "LOGIN_PROVIDERS", frozenset({"codex", "cursor"}))
    if case == "launch_failure":
        fake.create_mode = "refused"
    result, jobs = _refusal(fly_env, seam, monkeypatch, fake,
                            **(EXHAUSTING if case == "cap_refusal" else {}))  # fmt: skip
    assert not result.ok and result.error.startswith("fly runner refused: ")
    assert result.fly_run_id == jobs[0]
    assert result.remote_run_id == ""  # still no claim that a remote run happened


def test_the_dispatch_envelope_carries_a_refused_runs_id(monkeypatch, capsys, tmp_path):
    skills = tmp_path / ".claude" / "skills" / "cicada"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text("# cicada\n")
    monkeypatch.setattr(dispatch_role, "ATELES_REPO", tmp_path)

    async def refused(skill, prompt, **kwargs):
        return skill_runner.SkillResult(
            skill, False, None, "", "", error="fly runner refused: busy", provider="codex",
            fly_run_id="fly-0123456789abcdef")

    monkeypatch.setattr(dispatch_role, "run_skill", refused)
    monkeypatch.setattr(dispatch_role, "_load_agent_def", lambda role: skill_runner.AgentDefinition(
        entity_id="e", name=role, prompt_markdown="# cicada", tool_allowlist="*",
        aauth_sub="cicada@ateles-swarm"))  # fmt: skip
    assert dispatch_role.main(["--role", "cicada", "--task", "x", "--json"]) == 1
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["fly_run_id"] == "fly-0123456789abcdef"
    assert "remote_run_id" not in envelope
