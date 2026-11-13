"""The Fly-only dispatch timeout (APIS_FLY_DISPATCH_TIMEOUT).

A Fly job may run longer than a local one without widening the shared
APIS_DISPATCH_TIMEOUT that every Mac dispatch uses. The setting is resolved
after the executor is chosen: a Fly job with no caller timeout takes it, a
shorter caller timeout is kept, a longer one is capped; a local run, including
a fallback from Fly, keeps the shared timeout. Unset, nothing changes. An
invalid value disables Fly before any token, machine or local fallback.

Offline: the executor, the provider process and every clock are stand-ins.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

_DAEMON_DIR = Path(__file__).resolve().parent
if str(_DAEMON_DIR) not in sys.path:
    sys.path.insert(0, str(_DAEMON_DIR))

import fly_runner  # noqa: E402
import skill_runner  # noqa: E402
from test_fly_runner import _job, _once  # noqa: E402
from test_fly_runner import fly_env as _fly_env_fixture  # noqa: E402
from test_fly_runner import seam as _seam_fixture  # noqa: E402

# The shared fixtures, re-exported so pytest finds them here.
fly_env = _fly_env_fixture
seam = _seam_fixture

LOCAL = 1800
FLY = 2700
FLY_SETTING = "APIS_FLY_DISPATCH_TIMEOUT"


class RecordingExecutor:
    """Stands in for ``FlyExecutor``: records each job; never mints a token
    or requests a machine."""

    def __init__(self, raise_exc: Exception | None = None):
        self.jobs: list = []
        self.raise_exc = raise_exc
        self.fallbacks = 0
        self.config = SimpleNamespace(local_fallback=True)
        self.ledger = SimpleNamespace(count_local_fallback=self._count)

    def _count(self) -> int:
        self.fallbacks += 1
        return self.fallbacks

    async def run(self, job):
        self.jobs.append(job)
        if self.raise_exc is not None:
            raise self.raise_exc
        return 0, b"fly output", b""


@pytest.fixture
def world(fly_env, seam, monkeypatch):
    """A Fly-configured dispatcher whose local timeout is 1800, with the
    local process's ``wait_for`` timeout recorded."""
    spawned, fake_exec = seam
    monkeypatch.setattr(skill_runner, "DISPATCH_TIMEOUT_SECONDS", LOCAL)
    monkeypatch.delenv(FLY_SETTING, raising=False)
    monkeypatch.setenv("APIS_FLY_CLAUDE_MACHINE_ID", "m-claude")
    executor = RecordingExecutor()
    monkeypatch.setattr(fly_runner, "get_executor", lambda: executor)
    local_timeouts: list = []
    real_wait_for = asyncio.wait_for

    async def recording_wait_for(aw, timeout=None):
        local_timeouts.append(timeout)
        return await real_wait_for(aw, timeout=timeout)

    monkeypatch.setattr(skill_runner.asyncio, "wait_for", recording_wait_for)
    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        yield SimpleNamespace(executor=executor, spawned=spawned, local=local_timeouts,
                              monkeypatch=monkeypatch)  # fmt: skip


def _fly(**kw):
    return _once(runner="fly", repository="owner/repo", github_token="", **kw)


def _opted_in(**kw):
    # An opted-in class: Fly when eligible, the Mac when not (fallback on).
    return _once(action_class="carry_forward_check", model="m", repository="owner/repo",
                 github_token="t-local", include_github_contract=True,
                 github_read_only=True, **kw)  # fmt: skip


# ── The normal dispatch path ─────────────────────────────────────────────────


def test_run_skill_gives_fly_the_fly_timeout_and_local_the_shared_one(world):
    world.monkeypatch.setenv(FLY_SETTING, str(FLY))
    world.monkeypatch.setenv("APIS_HARNESS_PROVIDERS", "claude")
    world.monkeypatch.setenv("APIS_HARNESS_HEADROOM", '{"claude": 1.0}')
    import harness_router

    harness_router.reset_state()
    result = asyncio.run(skill_runner.run_skill(
        "lanius", "check the gates", role="lanius", runner="fly",
        repository="owner/repo", github_token="",
    ))  # fmt: skip
    assert result.ok, result.error
    assert [j.timeout for j in world.executor.jobs] == [FLY]
    files = {f["guest_path"]: f for f in fly_runner.job_files(
        world.executor.jobs[0], fly_runner.FlyConfig.from_env())}  # fmt: skip
    import base64

    job_json = json.loads(base64.b64decode(files["/run/job/job.json"]["raw_value"]))
    assert job_json["timeout_seconds"] == FLY

    harness_router.reset_state()
    result = asyncio.run(skill_runner.run_skill(
        "lanius", "check the gates", role="lanius", runner="local",
    ))  # fmt: skip
    assert result.ok, result.error
    assert world.local == [LOCAL]
    assert len(world.executor.jobs) == 1  # the local run never reached Fly


def test_unset_keeps_the_prior_behaviour_on_both_executors(world):
    assert _fly().ok
    assert [j.timeout for j in world.executor.jobs] == [LOCAL]
    assert _once(action_class=None).ok
    assert world.local == [LOCAL]


def test_an_explicit_shorter_caller_timeout_is_kept_on_fly(world):
    world.monkeypatch.setenv(FLY_SETTING, str(FLY))
    assert _fly(timeout=600).ok
    assert [j.timeout for j in world.executor.jobs] == [600]


def test_an_explicit_longer_caller_timeout_cannot_run_past_the_fly_ceiling(world):
    world.monkeypatch.setenv(FLY_SETTING, str(FLY))
    assert _fly(timeout=4000).ok
    assert [j.timeout for j in world.executor.jobs] == [FLY]


def test_a_local_run_never_reads_the_fly_setting(world):
    world.monkeypatch.setenv(FLY_SETTING, str(FLY))
    assert _once(action_class=None).ok
    assert world.local == [LOCAL] and world.executor.jobs == []


# ── Fallback from Fly keeps the local timeout ────────────────────────────────


def test_an_ineligible_job_falls_back_with_the_local_timeout(world):
    world.monkeypatch.setenv(FLY_SETTING, str(FLY))
    world.monkeypatch.setenv("APIS_FLY_RUNNER_CLASSES", "carry_forward_check")
    world.monkeypatch.delenv("ATELES_PRIVATE_KEYS_DIR")  # no role identity: ineligible
    result = _once(action_class="carry_forward_check", model="m", repository="owner/repo",
                   github_token="t-local")  # fmt: skip
    assert result.ok and result.stdout == "local output"
    assert world.local == [LOCAL] and world.executor.jobs == []


def test_an_unavailable_fly_launch_falls_back_with_the_local_timeout(world):
    world.monkeypatch.setenv(FLY_SETTING, str(FLY))
    world.monkeypatch.setenv("APIS_FLY_RUNNER_CLASSES", "carry_forward_check")
    world.executor.raise_exc = fly_runner.FlyLaunchError("no capacity")
    result = _opted_in()
    assert result.ok and result.stdout == "local output", result.error
    assert [j.timeout for j in world.executor.jobs] == [FLY]  # Fly was tried with it
    assert world.local == [LOCAL] and world.executor.fallbacks == 1


# ── An invalid setting disables Fly before anything is launched ──────────────

INVALID = ["0", "-7777", "abc", "NaN", "inf", "1e9", "", "99999999", "2700.5", "+2700"]


@pytest.mark.parametrize("bad", INVALID, ids=repr)
def test_an_invalid_setting_refuses_before_any_token_machine_or_fallback(world, bad):
    world.monkeypatch.setenv(FLY_SETTING, bad)
    world.monkeypatch.setenv("APIS_FLY_RUNNER_CLASSES", "carry_forward_check")
    world.executor.run = lambda job: pytest.fail("Fly reached")
    result = _opted_in()
    assert not result.ok
    assert result.error.startswith("fly runner refused:") and FLY_SETTING in result.error
    assert world.spawned == [] and world.local == []  # no local fallback either
    assert world.executor.fallbacks == 0
    if len(bad) > 2:
        assert bad not in result.error  # the value is never echoed


@pytest.mark.parametrize("bad", INVALID, ids=repr)
def test_an_invalid_setting_is_a_limit_problem(fly_env, monkeypatch, bad):
    monkeypatch.setenv(FLY_SETTING, bad)
    problems = fly_runner.FlyConfig.from_env().limit_problems()
    assert any(p.startswith(FLY_SETTING + " ") for p in problems)
    with pytest.raises(fly_runner.FlyConfigInvalid, match=FLY_SETTING):
        _job(timeout=None)


def test_a_valid_setting_is_not_a_limit_problem(fly_env, monkeypatch):
    monkeypatch.setenv("APIS_FLY_CLAUDE_MACHINE_ID", "m-claude")
    assert fly_runner.FlyConfig.from_env().limit_problems() == []
    monkeypatch.setenv(FLY_SETTING, str(FLY))
    config = fly_runner.FlyConfig.from_env()
    assert config.limit_problems() == [] and config.dispatch_timeout_seconds == FLY
    assert _job(timeout=None).timeout == FLY


# ── The delivery token's fixed life still bounds a delivery ──────────────────


def _delivery_job():
    return fly_runner.build_job(
        provider="claude", role="lanius", cmd=["/x/claude", "--print"],
        stdin=b"build it; write " + fly_runner.PR_FILE_PLACEHOLDER.encode(),
        timeout=None, default_timeout=LOCAL, repository="owner/repo",
        github_requested=False, mcp_config_path=None,
        delivery={"branch": "b", "base_branch": "main", "author_name": "a",
                  "author_email": "a@example.test"},
    )  # fmt: skip


def test_a_45_minute_delivery_fits_the_token_and_60_minutes_is_refused_before_mint(
        fly_env, monkeypatch):
    def no_mint(*a, **k):
        pytest.fail("a token was minted")

    monkeypatch.setattr(fly_runner, "mint_delivery_github_token", no_mint)
    monkeypatch.setenv("APIS_FLY_CLAUDE_MACHINE_ID", "m-claude")
    monkeypatch.setenv(FLY_SETTING, str(FLY))
    assert fly_runner.delivery_fits_token_life(FLY, 3600.0, 0.0)
    assert _delivery_job().timeout == FLY
    monkeypatch.setenv(FLY_SETTING, "3600")
    assert not fly_runner.delivery_fits_token_life(3600, 3600.0, 0.0)
    with pytest.raises(fly_runner.FlyIneligible, match="outlive its GitHub token"):
        _delivery_job()


# ── Accounting for a machine whose window is unknown ─────────────────────────


def test_an_unreadable_entry_is_charged_the_longer_fly_window(fly_env, monkeypatch):
    monkeypatch.setenv(FLY_SETTING, str(FLY))
    config = fly_runner.FlyConfig.from_env()
    assert config.longest_default_run_seconds() == FLY
    charged = fly_runner._charged_seconds({}, 0.0, config.longest_default_run_seconds())
    assert charged == FLY + fly_runner.APIS_DEADLINE_GRACE_SECONDS
    monkeypatch.delenv(FLY_SETTING)
    assert fly_runner.FlyConfig.from_env().longest_default_run_seconds() == LOCAL


# ── The watchdog outlasts a whole Fly dispatch ───────────────────────────────


def _stall(**env):
    from task_watchdog import stall_seconds

    base = {"APIS_DISPATCH_TIMEOUT": str(LOCAL), "APIS_STALL_SECONDS": "60"}
    return stall_seconds({**base, **env})


def test_the_watchdog_outlasts_queue_execution_delivery_and_grace():
    fly = {"APIS_FLY_RUNNER_CLASSES": "open_or_merge_pr",
           "APIS_FLY_QUEUE_MAX_WAIT_SECONDS": "3600", FLY_SETTING: str(FLY)}  # fmt: skip
    span = (3600 + FLY + fly_runner.DELIVERY_RESERVE_SECONDS
            + fly_runner.APIS_DEADLINE_GRACE_SECONDS)  # fmt: skip
    assert span == 3600 + 2700 + 300 + 120
    assert _stall(**fly) == span + 600  # the existing stall padding


def test_the_watchdog_uses_the_longer_of_the_local_and_fly_timeouts():
    fly = {"APIS_FLY_RUNNER_CLASSES": "carry_forward_check",
           "APIS_FLY_QUEUE_MAX_WAIT_SECONDS": "600", FLY_SETTING: "900"}  # fmt: skip
    assert _stall(**fly) == 600 + LOCAL + 300 + 120 + 600


@pytest.mark.parametrize("bad", INVALID, ids=repr)
def test_an_invalid_fly_setting_never_extends_the_watchdog(bad):
    fly = {"APIS_FLY_RUNNER_CLASSES": "carry_forward_check",
           "APIS_FLY_QUEUE_MAX_WAIT_SECONDS": "3600", FLY_SETTING: bad}  # fmt: skip
    assert _stall(**fly) == LOCAL + 600  # Fly is disabled: the local span only


def test_the_fly_setting_alone_never_extends_the_local_watchdog():
    assert _stall(**{FLY_SETTING: str(FLY)}) == LOCAL + 600
