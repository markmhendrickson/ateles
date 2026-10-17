"""Opted-in delivery dispatch, durable replay refusal, and truthful artifacts.

Offline entrypoint tests; no Fly, GitHub, token mint, or Neotoma network calls.
"""

from __future__ import annotations

import asyncio
import dataclasses
import importlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import apis
import build_delivery as delivery
import fly_runner
import skill_runner
from skill_runner import SkillResult
from test_swarm_dispatch import (
    SwarmDispatcher,
    _StubNotifier,
    _config,
    _empty_spec_state,
    _issue_trigger,
)

_REAL_SPAWN = apis._spawn_harness_skill

REPO = "owner/repo"
BRANCH = "cicada/delivery-123456"
COMMIT = "a" * 40


@pytest.fixture(autouse=True)
def isolated_receipt(monkeypatch, tmp_path):
    monkeypatch.setenv(
        delivery.FLY_DELIVERY_REGISTRY_ENV, str(tmp_path / "receipts.json")
    )
    monkeypatch.delenv("APIS_FLY_RUNNER_CLASSES", raising=False)
    monkeypatch.setattr(delivery, "_new_branch", lambda label: (BRANCH, "test", "test"))
    import subprocess
    real_run = subprocess.run

    def remote_read(argv, *args, **kwargs):
        if argv[:3] == ["gh", "pr", "view"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps(remote_pr()), "")
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", remote_read)


@pytest.fixture
def component_producer_admission(monkeypatch, request):
    """Only the legacy artifact/receipt component's producer boundary.

    This explicit per-test stand-in supplies no origin evidence. Genuine
    test_common_ executor/consumer controls must retain real admission.
    """
    assert not request.node.originalname.startswith("test_common_")
    calls = []

    async def admitted(result, plan=None):
        calls.append((result, plan))

    monkeypatch.setattr(fly_runner, "require_delivery_admission", admitted)
    return calls


def remote_pr():
    return {"number": 99, "url": f"https://github.com/{REPO}/pull/99",
            "baseRefName": "main", "headRefName": BRANCH, "headRefOid": COMMIT,
            "headRepository": {"nameWithOwner": REPO}}


def result(status="opened", **over):
    data = dict(
        fly_delivery_status=status,
        remote_run_id="run-123",
        fly_delivery_branch=BRANCH,
        fly_delivery_commit=COMMIT,
    )
    if status in {"opened", "updated"}:
        data.update(fly_pr_number=99, fly_pr_url=f"https://github.com/{REPO}/pull/99")
    data.update(over)
    return SkillResult(
        "cicada", status in {"opened", "updated", "no_changes"}, 0, "", "", **data
    )


def task_snapshot(issue=123):
    return dict(
        title="Implement the approved spec",
        description="Commit the change",
        assigned_to="cicada",
        repository=REPO,
        issue_number=issue,
        action_type="open_or_merge_pr",
        status="pending",
        confidence=0.95,
    )


def run_task():
    return apis._spawn_harness_skill(
        "cicada",
        "ent_task_example",
        task_snapshot(),
        "created",
        _StubNotifier(),
        role="cicada",
    )


def dispatcher():
    return SwarmDispatcher(_StubNotifier(), _config())


def trigger():
    return dataclasses.replace(
        _issue_trigger(),
        repository=REPO,
        number=123,
        html_url=f"https://github.com/{REPO}/issues/123",
    )


def test_default_off_is_pinned_even_when_fly_itself_is_configured(monkeypatch):
    monkeypatch.setenv("APIS_FLY_APP", "runner-test")
    assert (
        delivery.plan_fly_delivery(REPO, "test", key=REPO + "#123", issue_number=123)
        is None
    )
    assert not fly_runner.delivery_opted_in()
    monkeypatch.setenv("APIS_FLY_RUNNER_CLASSES", "open_or_merge_pr")
    assert fly_runner.delivery_opted_in()
    assert not fly_runner.wants_fly(None, "open_or_merge_pr")
    assert fly_runner.wants_fly("fly", "open_or_merge_pr")


@pytest.mark.parametrize("entry", ["task", "pipeline"])
def test_both_entries_pass_the_delivery_spec_without_local_grants(
    monkeypatch, entry, component_producer_admission
):
    monkeypatch.setenv("APIS_FLY_RUNNER_CLASSES", "open_or_merge_pr")
    calls = []

    async def run(skill, prompt, **kw):
        calls.append((prompt, kw))
        assert delivery.active_build(REPO, 123)
        return result()

    async def never(*a, **kw):
        raise AssertionError("local provisioning or PR search after a Fly delivery")

    monkeypatch.setattr(apis, "run_skill", run)
    monkeypatch.setattr("swarm_dispatch.run_skill", run)
    monkeypatch.setattr(delivery, "prepare_build_worktree", never)
    monkeypatch.setattr(SwarmDispatcher, "_find_open_pr_for_issue", never)
    if entry == "task":
        got = asyncio.run(run_task())
        assert got.ok
        assert got.stdout.endswith(
            f"[cicada] pull_request_link: https://github.com/{REPO}/pull/99\n"
        )
        assert COMMIT in got.stdout and "run-123" in got.stdout
    else:
        got = asyncio.run(
            dispatcher()._open_implementation_pr(trigger(), _empty_spec_state())
        )
        assert got == f"https://github.com/{REPO}/pull/99"
    ((prompt, kw),) = calls
    assert kw["runner"] == "fly" and kw["repository"] == REPO
    assert set(kw["fly_delivery"]) == set(fly_runner.DELIVERY_SPEC_KEYS)
    assert (
        "github_token" not in kw
        and "include_github_contract" not in kw
        and "cwd" not in kw
    )
    assert fly_runner.PR_FILE_PLACEHOLDER in prompt
    assert "TOKEN" not in prompt and "credential" not in prompt.lower()
    assert "Do not push" in prompt
    if entry == "pipeline":
        assert "OPEN THE PR" not in prompt
        assert "OPEN AN IMPLEMENTATION PULL REQUEST" not in prompt
        assert "PREPARE THE PR TEXT" in prompt
    assert delivery.active_build(REPO, 123) is None
    assert len(component_producer_admission) >= 2
    assert delivery._delivery_ledger().delivery_receipts()[REPO + "#123"]["status"] == "opened"


@pytest.mark.parametrize(
    "status",
    ["unknown", "no_time", "unreported", "pr_failed", "push_failed", "no_changes"],
)
@pytest.mark.parametrize("entry", ["task", "pipeline"])
def test_terminal_delivery_cannot_repeat_after_restart_or_optout(
    monkeypatch, status, entry, component_producer_admission
):
    monkeypatch.setenv("APIS_FLY_RUNNER_CLASSES", "open_or_merge_pr")
    launches = []

    async def run(*a, **kw):
        launches.append(kw)
        # The existing receipt grammar forbids a commit for no_changes.
        return result(status, fly_delivery_commit="" if status == "no_changes" else COMMIT)

    async def never(*a, **kw):
        raise AssertionError("terminal delivery fell back locally")

    monkeypatch.setattr(apis, "run_skill", run)
    monkeypatch.setattr("swarm_dispatch.run_skill", run)
    monkeypatch.setattr(delivery, "prepare_build_worktree", never)
    if entry == "task":
        assert not asyncio.run(run_task()).ok
    else:
        with pytest.raises(delivery.FlyDeliveryEnded):
            asyncio.run(
                dispatcher()._open_implementation_pr(trigger(), _empty_spec_state())
            )
    # Reach the original terminal report, rather than passing because an
    # earlier admission refusal changed every status to unknown.
    assert delivery._delivery_ledger().delivery_receipts()[REPO + "#123"]["status"] == status
    assert bool(component_producer_admission) is (status == "no_changes")
    # Reload discards process state. The receipt, not an in-memory cooldown,
    # must prevent a second delivery through either launch entrance.
    importlib.reload(delivery)
    monkeypatch.delenv("APIS_FLY_RUNNER_CLASSES")
    assert not asyncio.run(run_task()).ok
    with pytest.raises(delivery.FlyDeliveryEnded, match="already recorded"):
        asyncio.run(
            dispatcher()._open_implementation_pr(trigger(), _empty_spec_state())
        )
    assert len(launches) == 1


def test_resumed_issue_pipeline_never_starts_a_second_delivery(monkeypatch):
    monkeypatch.setenv("APIS_FLY_RUNNER_CLASSES", "open_or_merge_pr")
    launches = []

    async def run(*a, **kw):
        launches.append(kw)
        return result("unknown")

    monkeypatch.setattr("swarm_dispatch.run_skill", run)
    d = dispatcher()
    with pytest.raises(delivery.FlyDeliveryEnded):
        asyncio.run(d._open_implementation_pr(trigger(), _empty_spec_state()))
    importlib.reload(delivery)
    fresh = dispatcher()

    async def issues(*a):
        return [dict(number=123, title="Resume", body="", labels=[], user={})]

    async def yes(*a, **kw):
        return True

    async def resume(t):
        await fresh._hand_off_to_build(
            t,
            _empty_spec_state(),
            completed=[],
            spec_action="review",
            spec_ready_key="spec",
        )

    monkeypatch.setattr(fresh, "_issues_with_inflight_marker", issues)
    monkeypatch.setattr(fresh, "_label_gate_allows", yes)
    monkeypatch.setattr(fresh, "_pipeline_advanced_past_triage", yes)
    monkeypatch.setattr(fresh, "_handle_issue_opened", resume)
    report = asyncio.run(fresh.resume_interrupted_pipelines([REPO]))
    assert report["resumed"] == 1 and report["failed"] == 0
    assert len(launches) == 1


def test_prelaunch_refusal_is_the_only_local_fallback(monkeypatch):
    monkeypatch.setenv("APIS_FLY_RUNNER_CLASSES", "open_or_merge_pr")
    calls = []

    async def run(*a, **kw):
        calls.append(kw)
        return SkillResult(
            "cicada", False, None, "", "", fly_ineligible_before_launch=True
        )

    monkeypatch.setattr(apis, "run_skill", run)
    monkeypatch.setenv("CICADA_AGENT_PAT", "fixture-only-token")
    asyncio.run(run_task())
    assert len(calls) == 2
    assert calls[0]["runner"] == "fly" and calls[1]["include_github_contract"]
    assert delivery.fly_delivery_blocker(REPO + "#123") == ""


@pytest.mark.parametrize(
    "corrupt", ['{"deliveries": []}', '{"deliveries":{"x":{}}}', "broken"]
)
def test_unreadable_receipt_refuses_before_either_launch(monkeypatch, corrupt):
    Path(delivery._registry_path()).write_text(corrupt)

    async def never(*a, **kw):
        raise AssertionError("unreadable registry launched a builder")

    monkeypatch.setattr(apis, "run_skill", never)
    monkeypatch.setattr("swarm_dispatch.run_skill", never)
    assert not asyncio.run(run_task()).ok
    with pytest.raises(delivery.FlyDeliveryEnded):
        asyncio.run(
            dispatcher()._open_implementation_pr(trigger(), _empty_spec_state())
        )


def test_crash_leaves_launching_receipt_for_the_next_process(monkeypatch):
    plan = delivery.FlyDeliveryPlan(REPO, BRANCH, "main", REPO + "#123", 123)

    async def crash():
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(delivery.run_fly_delivery(plan, crash, lambda x: x))
    importlib.reload(delivery)
    assert "already recorded" in delivery.fly_delivery_blocker(plan.key)


def test_receipt_reservation_is_atomic_across_processes(tmp_path):
    import subprocess

    script = """import sys, time
sys.path.insert(0, sys.argv[1])
import build_delivery as d
p = d.FlyDeliveryPlan("owner/repo", "cicada/build", "main", "owner/repo#123", 123)
print(d._reserve_delivery(p), flush=True)
"""
    args = [sys.executable, "-c", script, str(Path(__file__).parent)]
    children = [
        subprocess.Popen(args, stdout=subprocess.PIPE, text=True) for _ in range(4)
    ]
    got = [p.communicate(timeout=20)[0].strip() for p in children]
    assert got.count("True") == 1 and got.count("False") == 3


@pytest.mark.parametrize("status", ["unknown", "no_time", "pr_failed"])
def test_provider_failover_stops_on_a_launched_delivery(monkeypatch, status):
    attempts = []

    async def attempt(provider, *a):
        attempts.append(provider)
        return result(status, remote_outcome_unknown=False)

    monkeypatch.setattr(
        skill_runner, "provider_candidates", lambda *a, **kw: ["codex", "cursor"]
    )
    got = asyncio.run(
        skill_runner._run_provider_attempts(
            "cicada",
            attempt,
            binaries={"codex": "/bin/codex", "cursor": "/bin/cursor"},
            retry_safe=True,
        )
    )
    assert got.fly_delivery_status == status and len(attempts) == 1


def test_delivery_record_rejects_wrong_repository_and_malformed_status(monkeypatch):
    assert (
        fly_runner.delivery_result_from_record({"delivery_status": []})[
            "delivery_status"
        ]
        == "unreported"
    )
    from test_fly_runner import complete_record

    job = fly_runner.FlyJob(
        run_id="run-123",
        role="cicada",
        provider="codex",
        repository=REPO,
        argv=[],
        stdin=b"",
        timeout=60,
        needs_github=False,
        delivery={"branch": BRANCH},
    )
    record = complete_record(
        job.run_id,
        delivery_status="opened",
        delivery_branch=BRANCH,
        delivery_commit=COMMIT,
        pr_number=99,
        pr_url="https://github.com/other/repo/pull/99",
    )
    # _result_from_record does not use executor state.
    with pytest.raises(fly_runner.FlyResultUnreadable):
        fly_runner.FlyExecutor._result_from_record(None, job, record)


from test_fly_runner import fly_env  # noqa: E402,F401 -- fixture


@pytest.mark.parametrize("outcome", ["success", "refused", "unknown", "cancelled"])
@pytest.mark.usefixtures("fly_env")
def test_delivery_token_is_job_only_minted_at_launch_and_revoked(monkeypatch, outcome):
    from test_fly_runner import FakeFly, make_executor, _files, MINTED

    minted = []
    monkeypatch.setattr(fly_runner, "LOGIN_PROVIDERS", frozenset({"codex", "cursor"}))
    fake = FakeFly()
    executor, read_mints, revoked = make_executor(
        fake,
        record={
            "delivery_status": "no_changes",
            "delivery_branch": BRANCH,
        },
    )

    def mint(repo, **kw):
        minted.append((repo, kw["timeout"]))
        if outcome == "refused":
            raise fly_runner.FlyIneligible("scope refused")
        return fly_runner.DeliveryToken(MINTED, 2_000_000_000.0, REPO)

    executor._mint_delivery = mint
    job = fly_runner.FlyJob(
        run_id="run-123",
        role="cicada",
        provider="claude",
        repository=REPO,
        argv=["claude", "--print"],
        stdin=b"commit the change",
        timeout=60,
        needs_github=False,
        delivery={"branch": BRANCH},
    )
    assert minted == []

    async def collect(handle):
        if outcome == "cancelled":
            raise asyncio.CancelledError()
        handle.job.outcome_unknown = True
        return 1, b"", b"unknown"

    if outcome in {"cancelled", "unknown"}:
        monkeypatch.setattr(executor, "_collect", collect)
    if outcome == "refused":
        with pytest.raises(fly_runner.FlyIneligible):
            asyncio.run(executor.run(job))
        assert not fake.calls("POST", "/machines")
        assert revoked == []
    elif outcome == "cancelled":
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(executor.run(job))
        assert revoked == [MINTED]
    else:
        asyncio.run(executor.run(job))
        assert revoked == [MINTED]
    assert minted == [(REPO, 60)] and read_mints == []
    assert job.secrets == {}
    if outcome != "refused":
        files = _files(json.loads(fake.calls("POST", "/machines")[0].content))
        spec = json.loads(files["/run/job/job.json"])
        assert fly_runner.DELIVERY_TOKEN_NAME not in spec["child_credential_names"]
        assert (
            files[f"/run/job/secrets/{fly_runner.DELIVERY_TOKEN_NAME}"]
            == MINTED.encode()
        )
        assert MINTED.encode() not in files["/run/job/stdin"]


def test_matching_fields_without_runner_origin_stay_unconfirmed():
    from test_fly_runner import complete_record

    job = fly_runner.FlyJob(
        run_id="run-123",
        role="cicada",
        provider="codex",
        repository=REPO,
        argv=[],
        stdin=b"",
        timeout=60,
        needs_github=False,
        delivery={"branch": BRANCH},
    )
    record = complete_record(
        job.run_id, role=job.role, provider=job.provider, repository=job.repository,
        delivery_status="opened",
        delivery_branch=BRANCH,
        delivery_commit=COMMIT,
        pr_number=99,
        pr_url=f"https://github.com/{REPO}/pull/99",
    )
    with pytest.raises(fly_runner.FlyResultUnreadable, match="runner-origin proof"):
        fly_runner.FlyExecutor._result_from_record(None, job, record)
    assert job.outcome_unknown and job.delivery_result is None
    assert skill_runner._fly_delivery_fields(job, unknown=True)["fly_delivery_status"] == "unknown"


def test_accounting_settlement_and_month_rollover_preserve_delivery_binding(
    monkeypatch,
):
    from datetime import datetime, timezone

    key = REPO + "#123"
    ledger = delivery._delivery_ledger()
    now = [datetime(2026, 10, 16, tzinfo=timezone.utc).timestamp()]
    ledger._clock = lambda: now[0]
    entry = {"status": "unknown", "run_id": "run-123", "branch": BRANCH, "recorded_at": 1.0}
    assert ledger.record_delivery(key, entry, retryable=frozenset())
    config = fly_runner.FlyConfig.from_env()
    ledger.reserve(config, run_id="run-123", owner="test-owner", max_run_seconds=60)
    ledger.settle("run-123", config=config, now=now[0] + 10)
    ledger.count_local_fallback()
    now[0] = datetime(2026, 11, 1, tzinfo=timezone.utc).timestamp()
    ledger.count_local_fallback()
    assert ledger.snapshot()["in_flight"] == {}
    assert ledger.delivery_receipts()[key] == entry


from test_build_delivery import dispatch_env as _dispatch_env  # noqa: E402,F401 -- fixture


@pytest.fixture(name="dispatch_env")
def _load_dispatch_env(request):
    return request.getfixturevalue("_dispatch_env")


@pytest.mark.parametrize("status", ["unknown", "no_time", "pr_failed"])
def test_failed_fly_task_is_blocked_with_verified_result(
    monkeypatch,
    dispatch_env,
    status,  # noqa: F811
):
    writes, _, store = dispatch_env
    eid = "ent_eval_terminal_task"
    snapshot = task_snapshot()
    store.rows[eid] = snapshot.copy()

    def save(eid, state, **kw):
        store.status(eid, state, **kw)
        if kw.get("result") is not None:
            store.field(eid, "result", kw["result"])
        return True

    async def spawn(*a, **kw):
        return result(status)

    monkeypatch.setattr(apis, "set_task_status", save)
    monkeypatch.setattr(apis, "_spawn_harness_skill", spawn)
    notice = _StubNotifier()
    asyncio.run(
        apis.dispatch_task(eid, snapshot, "approved", notice, gate_override=True)
    )
    assert store.rows[eid]["status"] == "blocked"
    assert (
        "run-123" in store.rows[eid]["result"] and COMMIT in store.rows[eid]["result"]
    )
    assert notice.sent


@pytest.mark.parametrize("entry", ["watchdog_retry", "build_released"])
@pytest.mark.parametrize("status", ["unknown", "no_time"])
def test_retry_and_deferred_task_entrances_read_the_durable_ledger(
    monkeypatch,
    dispatch_env,
    entry,
    status,  # noqa: F811
):
    _, _, store = dispatch_env
    eid = "ent_eval_retry_task"
    snapshot = task_snapshot()
    store.rows[eid] = snapshot.copy()
    delivery._record(
        REPO + "#123", {"status": status, "branch": BRANCH, "run_id": "run-123",
                        "recorded_at": 1.0, "commit": "", "reason": "", "pr_number": None, "pr_url": ""}
    )

    async def never(*a, **kw):
        raise AssertionError("retry launched a second delivery")

    monkeypatch.setattr(apis, "_spawn_harness_skill", _REAL_SPAWN)
    monkeypatch.setattr(apis, "run_skill", never)
    monkeypatch.setattr(delivery, "prepare_build_worktree", never)
    asyncio.run(
        apis.dispatch_task(eid, snapshot, entry, _StubNotifier(), gate_override=True)
    )
    assert store.rows[eid]["status"] == "blocked"
    assert "already recorded" in store.rows[eid]["blocked_reason"]


def test_remote_unknown_flag_alone_never_clears_the_delivery_binding():
    plan = delivery.FlyDeliveryPlan(REPO, BRANCH, "main", REPO + "#123", 123)
    async def lost():
        return SkillResult("cicada", False, 1, "", "", remote_outcome_unknown=True)
    asyncio.run(delivery.run_fly_delivery(plan, lost, lambda reason: reason))
    assert "already recorded" in delivery.fly_delivery_blocker(plan.key)


def test_runner_pr_reaches_existing_artifact_gate_and_verified_task_completion(
    monkeypatch, dispatch_env, common_admission_executor
):
    _, _, store = dispatch_env
    _, jobs, reads, _ = common_admission_executor
    real_build_job = fly_runner.build_job

    def fixed_run(**kwargs):
        job = real_build_job(**kwargs)
        job.run_id = "run-123"
        return job

    monkeypatch.setattr(fly_runner, "build_job", fixed_run)
    original_run = apis.run_skill
    artifact_reads = []
    eid = "ent_eval_success_task"
    snapshot = task_snapshot()
    store.rows[eid] = snapshot.copy()
    monkeypatch.setenv("APIS_FLY_RUNNER_CLASSES", "open_or_merge_pr")
    async def run(*a, **kw):
        got = await original_run(*a, **kw)
        assert got.ok, got.error
        got.stdout = "[cicada] pull_request_link: BLOCKED before runner delivery"
        return got

    def artifact(*args, **kwargs):
        artifact_reads.append((args, kwargs))
        return "exists"

    monkeypatch.setattr(apis, "_spawn_harness_skill", _REAL_SPAWN)
    monkeypatch.setattr(apis, "run_skill", run)
    monkeypatch.setattr(apis, "resolve_artifact_ref", artifact)
    monkeypatch.setattr("lib.daemon_runtime.task_lifecycle._correct", store.field)
    asyncio.run(apis.dispatch_task(eid, snapshot, "approved", _StubNotifier(), gate_override=True))
    assert store.rows[eid]["status"] == "done", store.rows[eid]
    saved = store.rows[eid]["result"]
    assert f"[cicada] pull_request_link: https://github.com/{REPO}/pull/99\n" in saved
    assert COMMIT in saved and "run-123" in saved
    assert len(jobs) == 1 and len(reads) >= 4
    assert artifact_reads, "the original task artifact gate was not reached"


@pytest.fixture
def common_admission_executor(monkeypatch, tmp_path):
    """Synthetic origin/read seams; real executor, skill and consumers.

    These fixture values are not installed producer authority. Production's
    origin acquisition remains unavailable, with no configuration override.
    """
    import httpx
    import time
    from test_fly_runner import FakeFly, canonical_delivery_fixture, complete_record, make_executor
    from lib.daemon_runtime import AgentDefinition

    monkeypatch.setenv("APIS_FLY_APP", "runner-app")
    monkeypatch.setenv("APIS_FLY_API_TOKEN", "synthetic-fly-token")
    monkeypatch.setenv("APIS_FLY_IMAGE", "fixture-image")
    monkeypatch.setenv("APIS_FLY_REGION", "ams")
    monkeypatch.setenv("CICADA_NEOTOMA_TOKEN", "synthetic-role-token")
    monkeypatch.setenv("NEOTOMA_BASE_URL", "https://canonical.invalid")
    monkeypatch.setenv("NEOTOMA_BEARER_TOKEN", "synthetic-controller-token")
    monkeypatch.setenv("APIS_HARNESS_PROVIDERS", "claude")
    monkeypatch.setattr(fly_runner, "LOGIN_PROVIDERS", frozenset())
    monkeypatch.setattr(skill_runner, "CLAUDE_BIN", "/fixture/claude")
    skill = tmp_path / ".claude/skills/cicada"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# Synthetic builder\n")
    monkeypatch.setattr(skill_runner, "ATELES_REPO", tmp_path)
    monkeypatch.setattr(skill_runner, "_load_agent_def", lambda role: AgentDefinition(
        entity_id="fixture-definition", name=role, prompt_markdown="Build the fixture.",
        tool_allowlist="*", aauth_sub="fixture-builder"))
    monkeypatch.setattr(skill_runner, "_load_active_policy_prompt", lambda role: "")
    monkeypatch.setattr(skill_runner, "_write_harness_event", lambda **kw: None)
    monkeypatch.setattr("lib.activity._store_activity_log", lambda *a, **kw: None)
    monkeypatch.setattr(skill_runner, "_refresh_usage_snapshot", lambda *a: None)
    monkeypatch.setattr(skill_runner, "_provider_binaries", lambda: {"claude": "/fixture/claude"})
    monkeypatch.setenv("APIS_HARNESS_HEADROOM_FILE", str(tmp_path / "absent-headroom.json"))
    monkeypatch.setattr(fly_runner, "this_process_owner", lambda identity: {
        "id": identity, "pid": 123, "start": "synthetic-start", "host": "synthetic-host"})
    jobs, acquisitions, state = [], [], {}
    real_build_job = fly_runner.build_job

    def build_job(**kwargs):
        job = real_build_job(**kwargs)
        jobs.append(job)
        return job

    monkeypatch.setattr(fly_runner, "build_job", build_job)
    executor, _, _ = make_executor(FakeFly(), clock=time.time)
    executor._mint_delivery = lambda *a, **kw: fly_runner.DeliveryToken(
        "synthetic-installation-token", executor._clock() + 3600, REPO)

    def record():
        job = jobs[-1]
        return complete_record(job.run_id, role=job.role, provider=job.provider,
            repository=job.repository, machine_id=job.machine_id, delivery_status="opened",
            delivery_reason="", delivery_branch=job.delivery["branch"], delivery_commit=COMMIT,
            pr_number=99, pr_url=f"https://github.com/{REPO}/pull/99")

    async def read(run_id):
        return record()

    async def origin(handle):
        binding, _ = canonical_delivery_fixture(record())
        return {"regime": "controller_independent_platform_readback", "record": record(), "binding": binding}

    def canonical_http(request):
        acquisitions.append(request.url.path)
        _, responses = canonical_delivery_fixture(record())
        if state.get("changed_winner"):
            responses["/entities/query"]["entities"][0]["provenance"]["probe_ok"] = "obs_other"
        assert request.url.host == "canonical.invalid"
        assert request.url.path in responses
        return httpx.Response(200, json=responses[request.url.path])

    original_client = httpx.AsyncClient

    def client(*args, **kwargs):
        kwargs.setdefault("transport", httpx.MockTransport(canonical_http))
        return original_client(*args, **kwargs)

    executor._read_result = read
    monkeypatch.setattr(executor, "_read_delivery_origin", origin, raising=False)
    monkeypatch.setattr(httpx, "AsyncClient", client)
    monkeypatch.setattr(fly_runner, "get_executor", lambda: executor)
    real_run = skill_runner.run_skill

    async def run(*args, **kwargs):
        return await real_run(*args, **{**kwargs, "provider": "claude"})

    monkeypatch.setattr(apis, "run_skill", run)
    monkeypatch.setattr("swarm_dispatch.run_skill", run)
    return executor, jobs, acquisitions, state


@pytest.mark.parametrize("entry", ["task", "pipeline"])
def test_common_admission_reaches_both_real_consumers(
    monkeypatch, dispatch_env, common_admission_executor, entry
):
    _, _, store = dispatch_env
    executor, jobs, reads, _ = common_admission_executor
    monkeypatch.setenv("APIS_FLY_RUNNER_CLASSES", "open_or_merge_pr")
    monkeypatch.setattr(apis, "_spawn_harness_skill", _REAL_SPAWN)
    monkeypatch.setattr(apis, "resolve_artifact_ref", lambda *a, **kw: "exists")
    monkeypatch.setattr("lib.daemon_runtime.task_lifecycle._correct", store.field)
    if entry == "task":
        eid = "ent_synthetic_admission_task"
        snapshot = task_snapshot()
        store.rows[eid] = snapshot.copy()
        asyncio.run(apis.dispatch_task(eid, snapshot, "approved", _StubNotifier(), gate_override=True))
        assert store.rows[eid]["status"] == "done", store.rows[eid]
        assert f"pull_request_link: https://github.com/{REPO}/pull/99" in store.rows[eid]["result"]
    else:
        assert asyncio.run(dispatcher()._open_implementation_pr(trigger(), _empty_spec_state())) == f"https://github.com/{REPO}/pull/99"
    assert len(jobs) == 1 and len(reads) >= 4
    assert jobs[0]._fly_admission.scope.closed
    assert jobs[0]._fly_admission.origin is None


@pytest.mark.parametrize("entry", ["task", "pipeline"])
@pytest.mark.parametrize("loss", ["missing", "closed", "foreign", "copied", "lost_origin", "changed_winner"])
def test_common_admission_loss_blocks_both_real_consumers(
    monkeypatch, dispatch_env, common_admission_executor, entry, loss
):
    import copy

    _, _, store = dispatch_env
    _, jobs, _, state = common_admission_executor
    monkeypatch.setenv("APIS_FLY_RUNNER_CLASSES", "open_or_merge_pr")
    original_run = apis.run_skill

    async def lose(*args, **kwargs):
        got = await original_run(*args, **kwargs)
        assert got.ok, got.error
        admission = got._fly_admission
        if loss == "missing":
            del got._fly_admission
        elif loss == "closed":
            admission.scope.close()
        elif loss == "foreign":
            admission.scope = fly_runner._CompletionScope()
        elif loss == "copied":
            got._fly_admission = copy.copy(admission)
        elif loss == "lost_origin":
            admission.origin = None
        else:
            state["changed_winner"] = True
        return got

    monkeypatch.setattr(apis, "run_skill", lose)
    monkeypatch.setattr("swarm_dispatch.run_skill", lose)
    monkeypatch.setattr(apis, "_spawn_harness_skill", _REAL_SPAWN)
    monkeypatch.setattr(apis, "resolve_artifact_ref", lambda *a, **kw: "exists")
    monkeypatch.setattr("lib.daemon_runtime.task_lifecycle._correct", store.field)
    if entry == "task":
        eid = "ent_synthetic_refused_task"
        snapshot = task_snapshot()
        store.rows[eid] = snapshot.copy()
        asyncio.run(apis.dispatch_task(eid, snapshot, "approved", _StubNotifier(), gate_override=True))
        assert store.rows[eid]["status"] != "done", store.rows[eid]
        assert "pull_request_link: https://" not in store.rows[eid].get("result", "")
    else:
        with pytest.raises(delivery.FlyDeliveryEnded):
            asyncio.run(dispatcher()._open_implementation_pr(trigger(), _empty_spec_state()))
    assert len(jobs) == 1
    receipt = delivery._delivery_ledger().delivery_receipts()[REPO + "#123"]
    assert receipt["status"] == "unknown", "missing admission became a successful receipt"


@pytest.mark.parametrize("entry", ["task", "pipeline"])
@pytest.mark.parametrize("failure", ["closed", "foreign_job"])
def test_common_late_loss_at_both_final_consumers(
    monkeypatch, dispatch_env, common_admission_executor, entry, failure
):
    _, _, store = dispatch_env
    _, jobs, _, _ = common_admission_executor
    monkeypatch.setenv("APIS_FLY_RUNNER_CLASSES", "open_or_merge_pr")
    monkeypatch.setattr(apis, "_spawn_harness_skill", _REAL_SPAWN)
    monkeypatch.setattr("lib.daemon_runtime.task_lifecycle._correct", store.field)
    def lose():
        if failure == "closed":
            jobs[0]._fly_admission.scope.close()
        else:
            import copy

            admission = jobs[0]._fly_admission
            # Otherwise self-consistent, current and same-scope evidence for
            # a DIFFERENT job object must not replace the parent's original.
            foreign = copy.copy(jobs[0])
            foreign._fly_admission = admission
            admission.job = foreign

    if entry == "task":
        def lose_at_artifact(*args, **kwargs):
            lose()
            return "exists"

        monkeypatch.setattr(apis, "resolve_artifact_ref", lose_at_artifact)
        eid = "ent_synthetic_late_loss"
        snapshot = task_snapshot()
        store.rows[eid] = snapshot.copy()
        asyncio.run(apis.dispatch_task(eid, snapshot, "approved", _StubNotifier(), gate_override=True))
        assert store.rows[eid]["status"] != "done", "closed scope reached final task completion"
    else:
        original_wrapper = delivery.run_fly_delivery

        async def lose_after_wrapper(*args, **kwargs):
            got = await original_wrapper(*args, **kwargs)
            assert got.ok
            lose()
            return got

        monkeypatch.setattr(delivery, "run_fly_delivery", lose_after_wrapper)
        with pytest.raises(delivery.FlyDeliveryEnded, match="admission changed"):
            asyncio.run(dispatcher()._open_implementation_pr(trigger(), _empty_spec_state()))
    assert len(jobs) == 1


@pytest.mark.parametrize("mode", ["default_off", "prelaunch"])
def test_common_ordinary_routing_preserved(monkeypatch, mode):
    if mode == "default_off":
        test_default_off_is_pinned_even_when_fly_itself_is_configured(monkeypatch)
    else:
        test_prelaunch_refusal_is_the_only_local_fallback(monkeypatch)


def test_common_default_origin_still_refuses_matching_fields():
    test_matching_fields_without_runner_origin_stay_unconfirmed()
