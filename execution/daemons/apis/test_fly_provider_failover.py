"""A Fly-bound dispatch whose first provider Fly cannot run tries the next one.

Provider rotation once sent a builder to codex first; the Fly runner refused it
before launch ("cannot yet give a codex job its Neotoma tools") and the task
ended blocked although claude runs on Fly. These tests drive the real
``_run_skill_once`` (and the real ``fly_runner.build_job``) through the real
provider loop, with only the Machines API and the local spawn faked.

Offline: no Fly, GitHub, token mint, or Neotoma network calls.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fly_runner
import skill_runner
from test_fly_runner import (  # noqa: F401 -- fixtures
    Clock,
    FakeFly,
    fly_env,
    make_executor,
    seam,
)

FLY_BOUND = frozenset({"claude", "codex", "cursor"})
BINARIES = {"claude": "/usr/bin/claude-test", "codex": "/usr/bin/codex", "cursor": "/usr/bin/cursor"}


@pytest.fixture
def loop(fly_env, seam, monkeypatch, tmp_path):  # noqa: F811 -- pytest fixtures
    """The provider loop over real Fly-forced attempts. Returns a runner and
    what it observed: attempts, local spawns, cooldowns, Machines API."""
    spawned, fake_exec = seam
    # Claude runs per job; codex and cursor are login providers. Codex has its
    # login machine, so what refuses it is the Neotoma-tools check (the case
    # seen in production, before codex got the signing proxy); cursor has
    # none. The proxy is withheld from codex here to keep that refusal.
    monkeypatch.setattr(fly_runner, "LOGIN_PROVIDERS", frozenset({"codex", "cursor"}))
    monkeypatch.setattr(fly_runner, "FLY_NEOTOMA_PROXY_PROVIDERS", frozenset({"claude"}))
    monkeypatch.setenv("APIS_FLY_CODEX_MACHINE_ID", "m-codex")
    monkeypatch.delenv("APIS_FLY_CURSOR_MACHINE_ID", raising=False)
    # _run_skill_once looks up the provider binary before the Fly path, so a
    # host without codex or cursor would end on "binary unavailable" rather
    # than the Fly refusal. The *_BIN constants are resolved at import, so set
    # the constant as well as the variable.
    for name in ("codex", "cursor"):
        stub = tmp_path / name
        stub.write_text("#!/bin/sh\nexit 0\n")
        stub.chmod(0o755)
        monkeypatch.setenv(f"APIS_{name.upper()}_BIN", str(stub))
        monkeypatch.setattr(skill_runner, f"{name.upper()}_BIN", str(stub))
    fake = FakeFly()
    executor, minted, _ = make_executor(
        fake, record={"exit_code": 0, "stdout": "GATE_INHERITANCE: clear\n"}
    )
    monkeypatch.setattr(fly_runner, "get_executor", lambda: executor)
    monkeypatch.setattr(skill_runner, "_refresh_usage_snapshot", lambda *a, **k: None)
    cooled = []
    monkeypatch.setattr(skill_runner, "cool_down", lambda name, *a, **k: cooled.append(name))
    monkeypatch.setattr(
        skill_runner, "_cool_after_capacity_failure",
        lambda name, result, *, model_fallback=False: cooled.append(name),
    )  # fmt: skip
    seen = {"attempts": [], "spawned": spawned, "cooled": cooled, "fake": fake,
            "minted": minted}  # fmt: skip

    def run(candidates, *, binaries=BINARIES, fly_bound=FLY_BOUND, **kw):
        monkeypatch.setattr(skill_runner, "provider_candidates", lambda *a, **k: list(candidates))

        async def attempt(selected, **_):
            seen["attempts"].append(selected)
            return await skill_runner._run_skill_once(
                "lanius", "check the gates", provider=selected, role="lanius",
                runner="fly", repository="owner/repo", github_token="",
            )  # fmt: skip

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            return asyncio.run(skill_runner._run_provider_attempts(
                "lanius", attempt, binaries=dict(binaries), role="lanius",
                fly_bound=fly_bound, **kw,
            ))  # fmt: skip

    return run, seen


def test_codex_refused_by_fly_launches_once_on_claude_with_no_local_run(loop):
    run, seen = loop
    result = run(["codex", "claude"])
    assert result.ok, result.error
    assert result.provider == "claude"
    assert seen["attempts"] == ["codex", "claude"]
    assert result.attempted_providers == ("codex", "claude")
    assert seen["minted"] == ["owner/repo"]  # exactly one Fly launch, for claude
    assert seen["spawned"] == []  # nothing ran on the Mac
    assert seen["cooled"] == []  # codex is healthy; Fly just cannot run it here



def test_cursor_with_no_login_machine_fails_over_to_claude(loop):
    # Red, seen with provider_specific=True temporarily removed from the
    # "no ... login machine configured" refusal in fly_runner.build_job:
    # the loop stopped after the first attempt, claude never ran, and
    # `assert result.ok` failed with "fly runner refused: not eligible for
    # Fly: no cursor login machine configured (APIS_FLY_CURSOR_MACHINE_ID)".
    run, seen = loop
    result = run(["cursor", "claude"])
    assert result.ok, result.error
    assert result.provider == "claude"
    assert seen["attempts"] == ["cursor", "claude"]
    assert result.attempted_providers == ("cursor", "claude")
    assert seen["minted"] == ["owner/repo"]  # exactly one Fly launch, for claude
    assert seen["spawned"] == []  # nothing ran on the Mac
    assert seen["cooled"] == []  # cursor is healthy; Fly just has no login machine for it


def test_refusal_not_specific_to_the_provider_stops_with_no_failover(loop, monkeypatch):
    # A non-https NEOTOMA_BASE_URL refuses every provider alike, so trying
    # the next one would only repeat the refusal.
    monkeypatch.setenv("NEOTOMA_BASE_URL", "http://neotoma.example.test")
    run, seen = loop
    result = run(["claude", "cursor"])
    assert not result.ok
    assert result.error.startswith("fly runner refused: not eligible for Fly")
    assert "NEOTOMA_BASE_URL must be an https URL" in result.error
    assert not result.fly_provider_ineligible
    assert seen["attempts"] == ["claude"]
    assert seen["minted"] == [] and seen["spawned"] == [] and seen["cooled"] == []


@pytest.mark.parametrize("pin", ["provider", "preferred_provider"])
def test_pinned_codex_is_refused_with_no_failover(loop, pin):
    run, seen = loop
    result = run(["codex", "claude"], **{pin: "codex"})
    assert not result.ok
    assert "the Fly runner cannot yet give a codex job its Neotoma tools" in result.error
    assert seen["attempts"] == ["codex"]
    assert seen["minted"] == [] and seen["spawned"] == [] and seen["cooled"] == []


def test_no_eligible_provider_ends_with_the_refusal_as_before(loop):
    run, seen = loop
    result = run(["codex", "cursor"])
    assert not result.ok
    assert result.error.startswith("fly runner refused: not eligible for Fly")
    assert "no cursor login machine configured" in result.error
    # The builder reads this to decide its local-delivery path, as before.
    assert result.fly_ineligible_before_launch
    assert result.attempted_providers == ("codex", "cursor")
    assert seen["minted"] == [] and seen["spawned"] == [] and seen["cooled"] == []


def test_fly_launch_failure_neither_fails_over_nor_cools(loop):
    run, seen = loop
    seen["fake"].create_mode = "refused"  # 422: the machine was never created
    result = run(["claude", "codex"])
    assert not result.ok
    assert result.error.startswith("fly runner refused: Fly launch failed")
    assert not result.fly_provider_ineligible
    assert seen["attempts"] == ["claude"]
    assert seen["spawned"] == [] and seen["cooled"] == []


# A provider the loop may select that has no binary on the Fly runner image.
NO_RUNNER = "gemini"


@pytest.fixture
def no_runner_provider(monkeypatch, tmp_path):
    """Make NO_RUNNER Fly-bound with a local binary, so its attempt reaches
    ``fly_runner.build_job`` and the ``RUNNER_BINARIES`` check is what stops it."""
    assert NO_RUNNER not in fly_runner.RUNNER_BINARIES
    stub = tmp_path / NO_RUNNER
    stub.write_text("#!/bin/sh\nexit 0\n")
    stub.chmod(0o755)
    real_binaries = skill_runner._provider_binaries
    monkeypatch.setattr(
        skill_runner, "_provider_binaries", lambda: {**real_binaries(), NO_RUNNER: str(stub)}
    )
    # The local command builder knows only real harnesses; give NO_RUNNER a
    # plain command so the attempt gets as far as the Fly check.
    real_command = skill_runner._provider_command

    def command(provider, binary, system_prompt, work_prompt, **kw):
        if provider == NO_RUNNER:
            return [binary, "--print"], work_prompt.encode()
        return real_command(provider, binary, system_prompt, work_prompt, **kw)

    monkeypatch.setattr(skill_runner, "_provider_command", command)
    return {"fly_bound": FLY_BOUND | {NO_RUNNER}, "binaries": {**BINARIES, NO_RUNNER: str(stub)}}


def test_provider_with_no_fly_runner_fails_over_to_claude(loop, no_runner_provider):
    # Red, seen with provider_specific=True temporarily removed from the
    # RUNNER_BINARIES refusal in fly_runner.build_job: the loop stopped after
    # the first attempt, claude never ran, and `assert result.ok` failed with
    # "fly runner refused: not eligible for Fly: provider 'gemini' has no Fly
    # runner".
    run, seen = loop
    result = run([NO_RUNNER, "claude"], **no_runner_provider)
    assert result.ok, result.error
    assert result.provider == "claude"
    assert seen["attempts"] == [NO_RUNNER, "claude"]
    assert result.attempted_providers == (NO_RUNNER, "claude")
    assert seen["minted"] == ["owner/repo"]  # exactly one Fly launch, for claude
    assert seen["spawned"] == []
    assert seen["cooled"] == []


def test_only_provider_with_no_fly_runner_ends_with_the_refusal(loop, no_runner_provider):
    run, seen = loop
    result = run([NO_RUNNER], **no_runner_provider)
    assert not result.ok
    assert result.error.startswith("fly runner refused: not eligible for Fly")
    assert f"provider {NO_RUNNER!r} has no Fly runner" in result.error
    assert result.fly_ineligible_before_launch
    assert seen["attempts"] == [NO_RUNNER]
    assert seen["minted"] == [] and seen["spawned"] == [] and seen["cooled"] == []


# ── A refused login claim moves an unpinned, unforced run to the next provider ─
#
# The dispatches here go to Fly because their action class is opted in
# (APIS_FLY_RUNNER_CLASSES), with no runner and no pinned provider, through the
# real ``run_skill``. Red, seen with ``fly_runner.py`` and ``skill_runner.py``
# as they were on main: the tests that expect claude to take the run failed with
# one codex attempt and "fly runner refused: ...".

HOST_FULL = (409, "insufficient memory available to fulfill request")


@pytest.fixture
def unforced(loop, monkeypatch):
    """``run_skill`` with codex first and claude second, both bound for Fly.
    ``claim(handler)`` answers every claim update of a login machine."""
    _, seen = loop
    fake = seen["fake"]
    # Codex can take this job here: what stops it is only its login machine.
    monkeypatch.setattr(
        fly_runner, "FLY_NEOTOMA_PROXY_PROVIDERS", frozenset({"claude", "codex"}))
    monkeypatch.setenv("APIS_FLY_RUNNER_CLASSES", "carry_forward_check")
    # A Mac that could run anything: an unwanted local fallback would show.
    monkeypatch.setattr(skill_runner, "_local_fallback_refusal", lambda *a, **k: None)
    fake.machines["m-codex"] = {"id": "m-codex", "state": "stopped", "login": True,
                                "instance_id": "v1", "config": {"mounts": []}}  # fmt: skip
    executor = fly_runner.get_executor()
    clock = Clock()
    executor._clock = executor.ledger._clock = clock

    async def sleep(seconds):
        clock.now += seconds

    executor._sleep = sleep
    claims: list = []

    def claim(answer):
        def handler(request):
            parts = request.url.path.split("/")
            if (request.method == "POST" and parts[-2] == "machines"
                    and json.loads(request.content)["config"].get("files")):  # fmt: skip
                response = answer(parts[-1])
                if response is not None:
                    claims.append(parts[-1])
                    fake.requests.append(request)
                    return response
            return fake.handler(request)

        executor.client = fly_runner.FlyMachinesClient(
            executor.config, transport=httpx.MockTransport(handler))  # fmt: skip

    def run(candidates=("codex", "claude"), **kw):
        monkeypatch.setattr(
            skill_runner, "provider_candidates",
            lambda binaries, preferred=None, **k:
                [preferred] if preferred else list(candidates))  # fmt: skip
        spawned_exec = _record_spawn(seen["spawned"])
        with patch("asyncio.create_subprocess_exec", side_effect=spawned_exec):
            return asyncio.run(skill_runner.run_skill(
                "lanius", "check the gates", role="lanius",
                action_class="carry_forward_check", repository="owner/repo",
                github_token="t-local", **{"model": None, **kw},
            ))  # fmt: skip

    claim(lambda machine: _refuse(*HOST_FULL) if machine == "m-codex" else None)
    return run, claim, claims, seen, executor


def _record_spawn(spawned):
    async def fake_exec(*cmd, **kwargs):
        spawned.append(list(cmd))
        raise OSError("no local run is expected")

    return fake_exec


def _refuse(status, text):
    return httpx.Response(status, json={"error": text})


def _revert(fake, machine_id, *, moves_version=True):
    machine = fake.machines[machine_id]
    machine.update({"state": "replacing", "_settle": 1})
    if moves_version:
        machine["instance_id"] += "r"
    return httpx.Response(200, json={"id": machine_id, "instance_id": "v-attempted"})


def _assert_nothing_left(seen, executor, machine="m-codex"):
    fake = seen["fake"]
    assert not fly_runner._has_job_config(fake.machines[machine]["config"])
    assert machine not in fake.leases
    assert executor.ledger.find_by_machine(machine) == {}


@pytest.mark.parametrize("how", ["host_memory", "reverted"])
def test_an_unforced_unpinned_run_takes_the_next_provider_after_a_refused_claim(unforced, how):
    run, claim, claims, seen, executor = unforced
    fake = seen["fake"]
    if how == "reverted":
        claim(lambda machine: _revert(fake, machine) if machine == "m-codex" else None)
    result = run()
    assert result.ok, result.error
    assert result.provider == "claude"
    assert result.attempted_providers == ("codex", "claude")
    assert claims == ["m-codex"] * (len(fly_runner.LOGIN_CLAIM_BACKOFF_SECONDS) + 1)
    assert not fake.calls("POST", "/m-codex/start")
    assert len(fake.calls("POST", "/machines")) == 1  # claude's one Fly launch
    assert seen["spawned"] == [] and seen["cooled"] == []
    _assert_nothing_left(seen, executor)


def _assert_codex_refusal_ends_it(result, seen):
    assert not result.ok
    assert result.provider == "codex"
    assert result.attempted_providers == ("codex",)
    assert result.cooled_until  # retry later
    assert not seen["fake"].calls("POST", "/machines")  # claude never launched
    assert seen["spawned"] == [] and seen["cooled"] == []


@pytest.mark.parametrize("pin", [
    {"provider": "codex"},
    {"required_provider": "codex"},
    {"preferred_provider": "codex"},
    {"runner": "fly"},
    {"model": "some-model"},
], ids=["provider", "required", "preferred", "forced-fly", "model"])
def test_a_pinned_or_forced_run_stays_with_its_refused_claim(unforced, monkeypatch, pin):
    monkeypatch.setattr(skill_runner, "configured_providers", lambda: ("codex", "claude"))
    run, _, _, seen, executor = unforced
    result = run(**pin)
    assert "reason host_memory" in result.error, result.error
    _assert_codex_refusal_ends_it(result, seen)
    _assert_nothing_left(seen, executor)


@pytest.mark.parametrize("answer", ["not_applied", "version_conflict", "cleanup-unverified"])
def test_a_claim_that_may_leave_something_or_is_not_capacity_does_not_move_on(unforced, answer):
    run, claim, _, seen, executor = unforced
    fake = seen["fake"]
    if answer == "not_applied":  # Fly may still apply it
        claim(lambda m: _revert(fake, m, moves_version=False) if m == "m-codex" else None)
    elif answer == "version_conflict":
        claim(lambda m: _refuse(409, "machine version changed") if m == "m-codex" else None)
    else:
        fake.release_fails = True
    result = run()
    assert "m-codex" in result.error
    _assert_codex_refusal_ends_it(result, seen)
    if answer == "version_conflict":
        _assert_nothing_left(seen, executor)
    else:  # kept for the reaper, with its lease
        assert executor.ledger.find_by_machine("m-codex")
        assert "m-codex" in fake.leases


def test_with_no_other_provider_the_refusal_is_the_result(unforced):
    run, _, _, seen, executor = unforced
    result = run(("codex",))
    _assert_codex_refusal_ends_it(result, seen)
    _assert_nothing_left(seen, executor)


@pytest.mark.parametrize("second", ["ineligible", "launch-failed"])
def test_when_the_next_provider_cannot_run_either_the_retry_later_refusal_stands(
        unforced, monkeypatch, second):  # fmt: skip
    run, _, _, seen, executor = unforced
    fake = seen["fake"]
    if second == "ineligible":  # unforced, but after a failover: never the Mac
        monkeypatch.setattr(
            fly_runner, "FLY_NEOTOMA_PROXY_PROVIDERS", frozenset({"codex"}))
    else:
        fake.create_mode = "refused"
    result = run()
    assert not result.ok
    assert result.provider == "codex" and result.cooled_until
    assert result.attempted_providers == ("codex", "claude")
    assert "reason host_memory" in result.error
    assert "claude did not run on Fly either" in result.error
    assert seen["spawned"] == [] and seen["cooled"] == []
    _assert_nothing_left(seen, executor)


def test_a_second_refused_claim_moves_on_again(unforced, monkeypatch):
    # Codex and cursor both have full hosts; claude then takes the run.
    monkeypatch.setenv("APIS_FLY_CURSOR_MACHINE_ID", "m-cursor")
    run, claim, _, seen, executor = unforced
    executor.config.login_machines["cursor"] = "m-cursor"
    fake = seen["fake"]
    fake.machines["m-cursor"] = {"id": "m-cursor", "state": "stopped", "login": True,
                                 "instance_id": "v1", "config": {"mounts": []}}  # fmt: skip
    monkeypatch.setattr(
        fly_runner, "FLY_NEOTOMA_PROXY_PROVIDERS", frozenset({"claude", "codex", "cursor"}))
    claim(lambda m: _refuse(*HOST_FULL) if m in ("m-codex", "m-cursor") else None)
    result = run(("codex", "cursor", "claude"))
    assert result.ok, result.error
    assert result.provider == "claude"
    assert result.attempted_providers == ("codex", "cursor", "claude")
    assert seen["spawned"] == [] and seen["cooled"] == []
    _assert_nothing_left(seen, executor)
    _assert_nothing_left(seen, executor, "m-cursor")
