"""A task tag ``harness-provider:<name>`` requires that provider, or refuses.

Three layers, each entered through the real code: the tag reader, the router
(real ``run_skill`` and provider loop, only the single attempt faked) and the
task dispatcher (real ``dispatch_task`` and spawn). Offline: no model CLI, Fly,
GitHub, token mint or Neotoma calls.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import apis
import build_delivery as delivery
import harness_router
import skill_runner
import task_watchdog
from lib.daemon_runtime.gating import ExecutionPolicy
from skill_runner import SkillResult
from test_build_delivery import _RecNotifier
from test_build_delivery import dispatch_env as _dispatch_env  # noqa: F401 -- fixture

_REAL_SPAWN = apis._spawn_harness_skill

TASK = "ent_task_provider_pin"
REPO = "owner/repo"
ALL = ("claude", "codex", "cursor")
PINNED_CODEX = "harness-provider:codex"


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv(
        delivery.FLY_DELIVERY_REGISTRY_ENV, str(tmp_path / "receipts.json")
    )
    monkeypatch.setenv("APIS_HARNESS_HEADROOM_FILE", str(tmp_path / "headroom.json"))
    monkeypatch.setenv("APIS_HARNESS_PROVIDERS", ",".join(ALL))
    for name in ("APIS_FLY_RUNNER_CLASSES", "APIS_FLY_CLAUDE_MACHINE_ID",
                 "APIS_VENDOR_BINDING_FILE"):
        monkeypatch.delenv(name, raising=False)
    harness_router.reset_state()
    yield
    harness_router.reset_state()


def task(*tags, **over):
    data = dict(
        title="Implement the approved spec",
        description="Commit the change",
        assigned_to="cicada",
        action_type="open_or_merge_pr",
        status="pending",
        confidence=0.95,
        tags=list(tags),
    )
    data.update(over)
    return data


# -- The tag reader -----------------------------------------------------------


@pytest.mark.parametrize("tags", [
    None, [], "", "[]", ["apis", "swarm"], "apis, swarm", '["apis"]',
    # Mentioning the words is not a pin: only a tag that begins as one is.
    ["harness-provider"], ["harness-provider-pinning"], ["provider:codex"],
    ["about harness-provider:codex"], [7, {"k": "v"}],
])  # fmt: skip
def test_tags_that_pin_nothing(tags):
    assert harness_router.task_provider_pin(tags) is None


@pytest.mark.parametrize("name", ALL)
def test_each_supported_pin_is_read(name):
    tag = f"harness-provider:{name}"
    assert harness_router.task_provider_pin(["apis", tag]) == name
    assert harness_router.task_provider_pin([tag, tag]) == name
    assert harness_router.task_provider_pin(json.dumps(["apis", tag])) == name


@pytest.mark.parametrize("tags", [
    ["harness-provider:gemini"], ["harness-provider:claude-local"],
    ["harness-provider:"], ["harness-provider:codex-cli"], ["harness-provider:cod"],
    ["harness-provider:Codex"], ["Harness-Provider:codex"], ["HARNESS-PROVIDER:CODEX"],
    [" harness-provider:codex"], ["harness-provider:codex "],
    ["harness-provider: codex"], ["harness-provider :codex"],
    ["harness_provider:codex"], ["harness provider:codex"], ["harnessprovider:codex"],
    ["harness-provider=codex"], ["harness-provider:codex,claude"],
    [["harness-provider:codex"]],
    # Two different pins, and a good pin beside a bad one.
    ["harness-provider:codex", "harness-provider:claude"],
    ["harness-provider:codex", "harness-provider:gemini"],
    # A tags value that is not a list but mentions a pin.
    "harness-provider:codex", "apis,harness-provider:codex",
    '"harness-provider:codex"', {"tag": "harness-provider:codex"},
])  # fmt: skip
def test_every_other_attempt_at_a_pin_is_refused(tags):
    with pytest.raises(harness_router.ProviderPinRefused):
        harness_router.task_provider_pin(tags)


# -- The router ---------------------------------------------------------------


@pytest.fixture
def router(monkeypatch):
    """Real ``run_skill`` over the real router; ``_run_skill_once`` is faked.
    ``seen['outcome']`` builds each attempt's result."""
    binaries = {name: f"/usr/bin/{name}-test" for name in ALL}
    seen = {
        "attempts": [], "kwargs": [], "binaries": binaries,
        "outcome": lambda provider: SkillResult(
            "cicada", True, 0, "", "", provider=provider),
    }  # fmt: skip

    async def once(skill, prompt, *, provider, **kw):
        seen["attempts"].append(provider)
        seen["kwargs"].append(kw)
        return seen["outcome"](provider)

    monkeypatch.setattr(skill_runner, "_run_skill_once", once)
    monkeypatch.setattr(skill_runner, "_provider_binaries", lambda: dict(binaries))
    monkeypatch.setattr(skill_runner, "_refresh_usage_snapshot", lambda *a, **k: None)
    monkeypatch.setattr(skill_runner, "notify_dispatch_failure", lambda *a, **k: None)

    def run(**kw):
        return asyncio.run(skill_runner.run_skill("cicada", "build it", **kw))

    return run, seen


def _failed(error="", stderr=""):
    return lambda provider: SkillResult(
        "cicada", False, 1, "", stderr, error=error.format(provider=provider),
        provider=provider)  # fmt: skip


@pytest.mark.parametrize("name", ALL)
def test_every_run_uses_only_the_required_provider(router, name):
    run, seen = router
    for _ in range(4):  # weighted rotation would otherwise move between them
        assert run(required_provider=name).ok
    assert seen["attempts"] == [name] * 4


@pytest.mark.parametrize("outcome", [
    _failed(stderr="You've hit your usage limit. Try again later."),
    _failed(error="{provider} launch failed: no such file"),
    _failed(error="the task failed"),
])  # fmt: skip
def test_a_failing_required_provider_never_fails_over(router, outcome):
    run, seen = router
    seen["outcome"] = outcome
    result = run(required_provider="codex")
    assert not result.ok
    assert seen["attempts"] == ["codex"]
    assert "requires harness provider 'codex'" in result.error


def test_without_a_requirement_a_launch_failure_still_fails_over(router):
    run, seen = router
    seen["outcome"] = _failed(error="{provider} launch failed: no such file")
    result = run()
    assert not result.ok
    assert sorted(seen["attempts"]) == sorted(ALL)
    assert "requires harness provider" not in result.error


def _refused(router, reason, name="codex", **kw):
    run, seen = router
    result = run(required_provider=name, **kw)
    assert not result.ok
    assert seen["attempts"] == []  # nothing ran, on this provider or another
    assert name in result.error and reason in result.error, result.error
    return result


def test_a_cooling_required_provider_is_refused(router):
    harness_router.cool_down("codex")
    _refused(router, "cooling down")


def test_a_persisted_cooling_window_is_refused_with_its_end(router):
    harness_router.record_cooling(
        "codex", time.time() + 3600, reason="session limit")
    assert _refused(router, "session limit").cooled_until


def test_a_required_provider_with_no_binary_is_refused(router):
    router[1]["binaries"]["codex"] = None
    _refused(router, "binary unavailable")


def test_a_required_provider_over_the_usage_gate_is_refused(router, monkeypatch):
    monkeypatch.setenv("APIS_USAGE_GATE", "on")  # no reading recorded: refuses
    _refused(router, "usage reading missing")


def test_a_required_provider_without_headroom_is_refused(router, monkeypatch):
    monkeypatch.setenv("APIS_HARNESS_MIN_HEADROOM", "1.0")
    _refused(router, "headroom")


@pytest.mark.parametrize("name", ALL)
def test_a_requirement_cannot_enable_a_disabled_provider(router, monkeypatch, name):
    monkeypatch.setenv(
        "APIS_HARNESS_PROVIDERS", ",".join(p for p in ALL if p != name))
    _refused(router, "not enabled", name=name)


def test_an_unsupported_or_conflicting_requirement_is_refused(router):
    _refused(router, "not supported", name="claude-local")
    _refused(router, "conflicts", provider="claude")


def test_a_fly_run_that_falls_back_to_the_mac_is_gated_like_an_unpinned_one(
        router, monkeypatch):
    # Claude is selectable on Fly (a login machine) but disabled on the Mac.
    monkeypatch.setenv("APIS_HARNESS_PROVIDERS", "codex")
    monkeypatch.setenv("APIS_FLY_CLAUDE_MACHINE_ID", "m-claude")
    run, seen = router
    assert run(required_provider="claude", runner="fly").ok
    assert seen["attempts"] == ["claude"]
    check = seen["kwargs"][0]["local_fallback_check"]
    assert "not enabled for local dispatch" in check("claude")
    # The same requirement on a local dispatch is simply refused.
    seen["attempts"].clear()
    _refused(router, "not enabled", name="claude")


def test_fly_refusing_the_required_provider_tries_no_other(router):
    run, seen = router
    seen["outcome"] = lambda provider: SkillResult(
        "cicada", False, None, "", "", error="fly runner refused: not eligible",
        provider=provider, fly_provider_ineligible=True,
        fly_ineligible_before_launch=True)  # fmt: skip
    result = run(required_provider="cursor", runner="fly")
    assert not result.ok
    assert seen["attempts"] == ["cursor"]
    assert result.fly_next_providers == ()


# -- The task dispatcher ------------------------------------------------------


@pytest.fixture
def spawned(monkeypatch):
    """Real spawn with ``run_skill`` captured and no clone or token."""
    calls = []
    results = []

    async def run(skill, prompt, **kw):
        calls.append(kw)
        return results.pop(0) if results else SkillResult(skill, True, 0, "", "")

    async def worktree(repository, label):
        return SimpleNamespace(path="/tmp/pin-worktree", branch="cicada/pin")

    monkeypatch.setattr(apis, "run_skill", run)
    monkeypatch.setattr(delivery, "prepare_build_worktree", worktree)
    monkeypatch.setattr(delivery, "worktree_instructions", lambda wt: "worktree")
    monkeypatch.setattr(apis, "_token_for_agent_on_repo", lambda *a: "token")
    return calls, results


def _spawn(snapshot, skill="cicada", trigger="created"):
    return asyncio.run(_REAL_SPAWN(
        skill, TASK, snapshot, trigger, _RecNotifier(), role=skill))


@pytest.mark.parametrize("over", [{}, {"repository": REPO}], ids=["no-repo", "worktree"])
def test_the_local_delivery_run_receives_the_requirement(spawned, over):
    calls, _ = spawned
    assert _spawn(task(PINNED_CODEX, **over)).ok
    assert [c["required_provider"] for c in calls] == ["codex"]
    assert "provider" not in calls[0]


@pytest.mark.parametrize("over", [{}, {"repository": REPO}], ids=["no-repo", "worktree"])
def test_an_unpinned_delivery_passes_nothing_new(spawned, over):
    calls, _ = spawned
    assert _spawn(task("apis", **over)).ok
    assert "required_provider" not in calls[0] and "provider" not in calls[0]


def test_the_fly_job_and_its_local_fallback_both_receive_the_requirement(
        spawned, monkeypatch):
    calls, results = spawned
    monkeypatch.setenv("APIS_FLY_RUNNER_CLASSES", "open_or_merge_pr")
    results.append(SkillResult(
        "cicada", False, None, "", "", error="fly runner refused: not eligible",
        fly_ineligible_before_launch=True))  # fmt: skip
    _spawn(task(PINNED_CODEX, repository=REPO))
    assert len(calls) == 2
    assert calls[0]["fly_delivery"] and calls[0]["runner"] == "fly"
    assert "fly_delivery" not in calls[1]  # the local path
    assert [c["required_provider"] for c in calls] == ["codex", "codex"]


def test_an_unpinned_fly_job_passes_nothing_new(spawned, monkeypatch):
    calls, results = spawned
    monkeypatch.setenv("APIS_FLY_RUNNER_CLASSES", "open_or_merge_pr")
    results.append(SkillResult(
        "cicada", False, None, "", "", error="the run failed",
        fly_delivery_status="unknown", remote_run_id="run-1"))  # fmt: skip
    _spawn(task(repository=REPO))
    assert calls[0]["fly_delivery"] and "required_provider" not in calls[0]


@pytest.mark.parametrize("snapshot, skill, why", [
    (task("harness-provider:gemini", repository=REPO), "cicada", "unsupported"),
    (task(PINNED_CODEX, "harness-provider:claude", repository=REPO), "cicada",
     "more than one"),
    (task(PINNED_CODEX, action_type="local_edit"), "cicada", "PR-delivery"),
    (task(PINNED_CODEX, assigned_to="pavo"), "pavo", "PR-delivery"),
])  # fmt: skip
def test_a_pin_that_cannot_be_honoured_runs_nothing(
        spawned, monkeypatch, snapshot, skill, why):
    calls, _ = spawned

    async def never(*a, **kw):
        raise AssertionError("an effect ran before the pin was refused")

    monkeypatch.setattr(delivery, "prepare_build_worktree", never)
    monkeypatch.setattr(apis, "_run_fly_delivery_task", never)
    result = _spawn(snapshot, skill=skill)
    assert not result.ok and calls == []
    assert "harness provider pin refused" in result.error and why in result.error


def test_a_refused_pin_on_a_correction_is_a_known_non_launch(spawned):
    calls, _ = spawned
    result = _spawn(task("harness-provider:gemini", repository=REPO,
                         task_type="existing_pr_correction"))  # fmt: skip
    assert calls == []
    assert result.fly_delivery_not_launched
    assert result.fly_delivery_cleanup == "not_reserved"
    assert "harness provider pin refused" in result.error


# -- Every attempt, through the real dispatcher and router --------------------


@pytest.fixture
def dispatcher(request, router, monkeypatch):
    writes, _, store = request.getfixturevalue("_dispatch_env")
    monkeypatch.setattr(apis, "_spawn_harness_skill", _REAL_SPAWN)
    policy = ExecutionPolicy(
        entity_id="default",
        low_blast_action_types=frozenset({"open_or_merge_pr"}),
        high_blast_action_types=frozenset(),
        loaded=True,
    )
    monkeypatch.setattr(apis, "resolve_policy_for_agent", lambda skill: policy)
    router[1]["outcome"] = _failed(error="{provider} launch failed: no such file")
    return store, router[1]


@pytest.mark.parametrize("trigger, kwargs", [
    ("created", {}),
    ("reconcile", {}),
    ("build_released", {}),
    ("approved", {"gate_override": True, "release_key": "ent_brief"}),
])  # fmt: skip
def test_each_dispatch_entrance_runs_only_the_required_provider(
        dispatcher, trigger, kwargs):
    store, seen = dispatcher
    snapshot = task(PINNED_CODEX)
    if trigger == "approved":
        snapshot.update(status="awaiting_approval", blocked_reason="held")
    store.rows[TASK] = dict(snapshot)
    asyncio.run(apis.dispatch_task(
        TASK, snapshot, trigger, _RecNotifier(), **kwargs))
    assert seen["attempts"] == ["codex"]
    assert store.rows[TASK]["status"] == "failed"
    assert "requires harness provider 'codex'" in store.rows[TASK]["blocked_reason"]


def test_the_watchdog_retry_keeps_the_requirement_while_it_cools(
        dispatcher, monkeypatch):
    """First attempt fails and cools codex; the watchdog's retry of the same
    task refuses with that reason instead of moving to a healthy provider."""
    store, seen = dispatcher
    store.rows[TASK] = task(PINNED_CODEX)
    asyncio.run(apis.dispatch_task(
        TASK, dict(store.rows[TASK]), "created", _RecNotifier()))
    assert seen["attempts"] == ["codex"]

    monkeypatch.setattr(
        task_watchdog, "_query_tasks",
        lambda limit: [(TASK, dict(store.rows[TASK]))])
    monkeypatch.setattr(task_watchdog, "set_task_status", store.status)

    async def redispatch(task_id, snapshot, trigger):
        await apis.dispatch_task(task_id, snapshot, trigger, _RecNotifier())

    counts = asyncio.run(
        task_watchdog.TaskWatchdog(stall_seconds=3600).sweep(
            _RecNotifier(), redispatch))
    assert counts["retried"] == 1
    assert seen["attempts"] == ["codex"]  # nothing further ran anywhere
    assert store.rows[TASK]["status"] == "failed"
    reason = store.rows[TASK]["blocked_reason"]
    assert "cooling down" in reason and "codex" in reason, reason


def test_a_refused_pin_is_recorded_on_the_task(dispatcher):
    store, seen = dispatcher
    store.rows[TASK] = task("harness-provider:Codex")
    asyncio.run(apis.dispatch_task(
        TASK, dict(store.rows[TASK]), "created", _RecNotifier()))
    assert seen["attempts"] == []
    assert store.rows[TASK]["status"] == "failed"
    assert "harness provider pin refused" in store.rows[TASK]["blocked_reason"]
