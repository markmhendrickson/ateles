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
from lib import github_app_token
from lib.daemon_runtime import artifact_resolver
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
# Every status a launched delivery can end with. None is retryable and none
# permits the local path: the runner assigns each only after the machine launched.
LAUNCHED_STATUSES = [
    "unknown",
    "unreported",
    "opened",
    "updated",
    "no_changes",
    "clone_failed",
    "not_delivered",
    "push_failed",
    "pr_failed",
    "no_time",
]


@pytest.fixture(autouse=True)
def isolated_receipt(monkeypatch, tmp_path):
    monkeypatch.setenv(
        delivery.FLY_DELIVERY_REGISTRY_ENV, str(tmp_path / "receipts.json")
    )
    monkeypatch.delenv("APIS_FLY_RUNNER_CLASSES", raising=False)


def pr_answer(**over):
    data = {
        "number": 99,
        "baseRefName": "main",
        "headRefName": BRANCH,
        "headRefOid": COMMIT,
        "headRepository": {"nameWithOwner": REPO},
    }
    data.update(over)
    return data


@pytest.fixture(autouse=True)
def github_pr(monkeypatch):
    """The real pull request as GitHub would return it for `gh pr view`. A test
    changes ``box["answer"]`` to put a different pull request on the remote.

    The read-back credential is a stand-in PR-opener App token; ``box`` records
    each mint (identity, repo) and the token every read was made with."""
    box = {"answer": (0, json.dumps(pr_answer()), ""), "mints": [], "tokens": []}

    def mint(identity, repo=None):
        box["mints"].append((identity, repo))
        return f"app-token-for-{repo}"

    def view(repo, number, token=None):
        box["tokens"].append(token)
        return box["answer"]

    monkeypatch.setattr(github_app_token, "identity_token", mint)
    monkeypatch.setattr(artifact_resolver, "_gh_pr_view", view)
    monkeypatch.setattr(artifact_resolver.time, "sleep", lambda s: None)
    return box


def seed(key, entry):
    """Put a receipt in the ledger as an earlier dispatch would have left it."""
    assert delivery._delivery_ledger().reserve_delivery(
        key, {**entry, "token": "seeded-" + entry["status"]}
    )


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


def task_snapshot(issue=123, repo=REPO):
    return dict(
        title="Implement the approved spec",
        description="Commit the change",
        assigned_to="cicada",
        repository=repo,
        issue_number=issue,
        action_type="open_or_merge_pr",
        status="pending",
        confidence=0.95,
    )


def run_task(repo=REPO):
    return apis._spawn_harness_skill(
        "cicada",
        "ent_task_example",
        task_snapshot(repo=repo),
        "created",
        _StubNotifier(),
        role="cicada",
    )


def dispatcher():
    return SwarmDispatcher(_StubNotifier(), _config())


def trigger(repo=REPO):
    return dataclasses.replace(
        _issue_trigger(),
        repository=repo,
        number=123,
        html_url=f"https://github.com/{REPO}/issues/123",
    )


def assert_recovery_text(text, status, key=None):
    """The initial operator text for a launched delivery: status, run and
    branch first, then the four steps in order with the exact command, and
    the statement that re-dispatch is refused until the receipt is cleared."""
    key = key or REPO + "#123"
    marks = [
        f"Status: {status}",
        "Run: run-123",
        f"Branch: {BRANCH}",
        "1. Inspect the remote branch",
        "2. Reconcile what actually happened",
        f"3. Clear the delivery receipt for this issue: {delivery.FLY_RECEIPT_COMMAND} "
        f"clear {key}",
        f"--confirm {key}",
        "4. Only then re-dispatch",
        "any re-dispatch is refused and nothing falls back to the local path",
    ]
    at = [text.index(m) for m in marks]
    assert at == sorted(at), text
    # No cause and no claim about what the machine did beyond the record.
    for claim in ("did not run", "pushed nothing", "nothing was pushed"):
        assert claim not in text or status == "no_changes"


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
def test_both_entries_pass_the_delivery_spec_without_local_grants(monkeypatch, entry):
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


@pytest.mark.parametrize(
    "status",
    [s for s in LAUNCHED_STATUSES if s not in {"opened", "updated"}],
)
@pytest.mark.parametrize("entry", ["task", "pipeline"])
def test_terminal_delivery_cannot_repeat_after_restart_or_optout(
    monkeypatch, status, entry
):
    monkeypatch.setenv("APIS_FLY_RUNNER_CLASSES", "open_or_merge_pr")
    launches = []

    async def run(*a, **kw):
        launches.append(kw)
        return result(status)

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


@pytest.mark.parametrize("opted_in", [True, False])
@pytest.mark.parametrize("recorded", [*LAUNCHED_STATUSES, "inflight"])
def test_resumed_issue_pipeline_never_starts_a_second_delivery(
    monkeypatch, recorded, opted_in
):
    """The boot resume sweep re-runs an interrupted issue pipeline. When that
    issue already has a Fly delivery on record, finished or still in flight
    (a launching receipt left by a dispatcher killed mid-run), the re-run must
    stop at the hand-off: no second Fly launch, no local worktree, no PR search,
    whether or not the class is still opted in."""
    monkeypatch.setenv("APIS_FLY_RUNNER_CLASSES", "open_or_merge_pr")
    launches = []

    async def run(*a, **kw):
        launches.append(kw)
        return result(recorded)

    monkeypatch.setattr("swarm_dispatch.run_skill", run)
    if recorded == "inflight":
        # SIGKILL mid-delivery: the reservation exists, no outcome was recorded.
        plan = delivery.FlyDeliveryPlan(REPO, BRANCH, "main", REPO + "#123", 123)
        assert delivery._reserve_delivery(plan)
        expected_launches = 0
    else:
        d = dispatcher()
        if recorded in {"opened", "updated"}:
            assert asyncio.run(d._open_implementation_pr(trigger(), _empty_spec_state()))
        else:
            with pytest.raises(delivery.FlyDeliveryEnded):
                asyncio.run(d._open_implementation_pr(trigger(), _empty_spec_state()))
        expected_launches = 1
    importlib.reload(delivery)  # a new process: only the durable receipt remains
    if not opted_in:
        monkeypatch.delenv("APIS_FLY_RUNNER_CLASSES")
    fresh = dispatcher()

    async def never(*a, **kw):
        raise AssertionError("a resumed pipeline provisioned or searched locally")

    monkeypatch.setattr(delivery, "prepare_build_worktree", never)
    monkeypatch.setattr(SwarmDispatcher, "_find_open_pr_for_issue", never)

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
    assert len(launches) == expected_launches
    refusal = next(m for m in fresh.notifier.sent if "already recorded" in m)
    assert "3. Clear the delivery receipt" in refusal and delivery.FLY_RECEIPT_COMMAND in refusal
    assert delivery.active_build(REPO, 123) is None


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


@pytest.mark.parametrize("opted_in", [True, False])
@pytest.mark.parametrize("status", [*LAUNCHED_STATUSES, "launching"])
def test_blocker_refuses_every_launched_status_whatever_the_setting(
    monkeypatch, status, opted_in
):
    """No receipt status is retryable and none lets a new reservation through."""
    if opted_in:
        monkeypatch.setenv("APIS_FLY_RUNNER_CLASSES", "open_or_merge_pr")
    plan = delivery.FlyDeliveryPlan(REPO, BRANCH, "main", REPO + "#123", 123)
    seed(plan.key, {"status": status, "branch": BRANCH, "run_id": "run-123"})
    importlib.reload(delivery)
    blocker = delivery.fly_delivery_blocker(plan.key)
    assert "already recorded" in blocker
    assert f"Status: {status}" in blocker and "3. Clear the delivery receipt" in blocker
    assert not delivery._reserve_delivery(plan)


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
print(bool(d._reserve_delivery(p)), flush=True)
"""
    args = [sys.executable, "-c", script, str(Path(__file__).parent)]
    children = [
        subprocess.Popen(args, stdout=subprocess.PIPE, text=True) for _ in range(4)
    ]
    got = [p.communicate(timeout=20)[0].strip() for p in children]
    assert got.count("True") == 1 and got.count("False") == 3


@pytest.mark.parametrize("status", LAUNCHED_STATUSES)
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


def test_matching_delivery_readback_supplies_the_runner_artifact():
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
        pr_url=f"https://github.com/{REPO}/pull/99",
    )
    assert fly_runner.FlyExecutor._result_from_record(None, job, record)[0] == 0
    assert (
        skill_runner._fly_delivery_fields(job, unknown=False)["fly_pr_url"]
        == record["pr_url"]
    )


def test_accounting_settlement_and_month_rollover_preserve_delivery_binding(
    monkeypatch,
):
    from datetime import datetime, timezone

    key = REPO + "#123"
    ledger = delivery._delivery_ledger()
    now = [datetime(2026, 10, 16, tzinfo=timezone.utc).timestamp()]
    ledger._clock = lambda: now[0]
    entry = {"status": "unknown", "run_id": "run-123", "branch": BRANCH}
    assert ledger.reserve_delivery(key, entry)
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


# no_changes is excluded: this test replaces the spawn, and the stand-in marks
# that result ok; the real spawn's handling is covered by the entrance tests.
@pytest.mark.parametrize(
    "status",
    [s for s in LAUNCHED_STATUSES if s not in {"opened", "updated", "no_changes"}],
)
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
    # The stored result and the notification both carry the status, run and
    # branch and the four recovery steps with the exact command.
    assert_recovery_text(store.rows[eid]["result"], status)
    assert_recovery_text(notice.sent[-1], status)


@pytest.mark.parametrize("entry", ["watchdog_retry", "build_released"])
@pytest.mark.parametrize("status", LAUNCHED_STATUSES)
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
    seed(REPO + "#123", {"status": status, "branch": BRANCH, "run_id": "run-123"})

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
    monkeypatch, dispatch_env
):
    _, _, store = dispatch_env
    eid = "ent_eval_success_task"
    snapshot = task_snapshot()
    store.rows[eid] = snapshot.copy()
    monkeypatch.setenv("APIS_FLY_RUNNER_CLASSES", "open_or_merge_pr")
    async def run(*a, **kw):
        got = result()
        got.stdout = "[cicada] pull_request_link: BLOCKED before runner delivery"
        return got
    monkeypatch.setattr(apis, "_spawn_harness_skill", _REAL_SPAWN)
    monkeypatch.setattr(apis, "run_skill", run)
    monkeypatch.setattr(apis, "resolve_artifact_ref", lambda *a, **kw: "exists")
    monkeypatch.setattr("lib.daemon_runtime.task_lifecycle._correct", store.field)
    asyncio.run(apis.dispatch_task(eid, snapshot, "approved", _StubNotifier(), gate_override=True))
    assert store.rows[eid]["status"] == "done", store.rows[eid]
    saved = store.rows[eid]["result"]
    assert f"[cicada] pull_request_link: https://github.com/{REPO}/pull/99\n" in saved
    assert COMMIT in saved and "run-123" in saved


# ── One receipt key, whatever each entrance's spelling of the repository ──

SPELLINGS = [
    "Owner/Repo",
    "owner/repo",
    "OWNER/REPO",
    "  Owner/Repo  ",
    "Owner/Repo.git",
    "https://github.com/Owner/Repo",
    "https://github.com/Owner/Repo.git",
    "git@github.com:Owner/Repo.git",
]
# Spellings a delivery can actually launch under (the plan validates strictly).
LAUNCH_SPELLINGS = ["Owner/Repo", "owner/repo"]


@pytest.mark.parametrize("spelling", SPELLINGS)
def test_every_spelling_derives_the_same_receipt_key(spelling):
    assert delivery.fly_delivery_key(spelling, 123) == "owner/repo#123"
    assert delivery.fly_delivery_key(spelling, "123") == "owner/repo#123"
    assert delivery.fly_delivery_key(spelling, None, " t1 ") == "owner/repo#task:t1"
    assert delivery.canonical_key(f"{spelling}#123") == "owner/repo#123"


@pytest.mark.parametrize("spelling", SPELLINGS)
def test_both_entrances_ask_the_ledger_for_the_same_key(monkeypatch, spelling):
    seen = []

    def blocker(key):
        seen.append(key)
        return "stop"

    monkeypatch.setattr(delivery, "fly_delivery_blocker", blocker)
    asyncio.run(run_task(spelling))
    with pytest.raises(delivery.FlyDeliveryEnded):
        asyncio.run(
            dispatcher()._open_implementation_pr(trigger(spelling), _empty_spec_state())
        )
    assert seen == ["owner/repo#123", "owner/repo#123"]


@pytest.mark.parametrize("replay_spelling", SPELLINGS)
@pytest.mark.parametrize("launch_spelling", LAUNCH_SPELLINGS)
@pytest.mark.parametrize("launched_by", ["task", "pipeline"])
def test_a_delivery_launched_by_one_entrance_is_refused_by_the_other(
    monkeypatch, launched_by, launch_spelling, replay_spelling
):
    monkeypatch.setenv("APIS_FLY_RUNNER_CLASSES", "open_or_merge_pr")
    launches = []

    async def run(*a, **kw):
        launches.append(kw)
        return result("unknown")

    async def never(*a, **kw):
        raise AssertionError("the other entrance built or searched locally")

    monkeypatch.setattr(apis, "run_skill", run)
    monkeypatch.setattr("swarm_dispatch.run_skill", run)
    monkeypatch.setattr(delivery, "prepare_build_worktree", never)
    monkeypatch.setattr(SwarmDispatcher, "_find_open_pr_for_issue", never)
    if launched_by == "task":
        assert not asyncio.run(run_task(launch_spelling)).ok
    else:
        with pytest.raises(delivery.FlyDeliveryEnded):
            asyncio.run(
                dispatcher()._open_implementation_pr(
                    trigger(launch_spelling), _empty_spec_state()
                )
            )
    importlib.reload(delivery)
    other = "pipeline" if launched_by == "task" else "task"
    if other == "task":
        got = asyncio.run(run_task(replay_spelling))
        assert not got.ok and "already recorded" in got.error
    else:
        with pytest.raises(delivery.FlyDeliveryEnded, match="already recorded"):
            asyncio.run(
                dispatcher()._open_implementation_pr(
                    trigger(replay_spelling), _empty_spec_state()
                )
            )
    assert len(launches) == 1
    receipts = delivery._delivery_ledger().delivery_receipts()
    assert list(receipts) == ["owner/repo#123"]


def test_a_receipt_under_an_older_key_spelling_is_still_found():
    """No real ledger can hold one (no class has been opted in), but a receipt
    keyed with the repository as first spelled is found and refused anyway."""
    seed("Owner/Repo#123", {"status": "pr_failed", "branch": BRANCH, "run_id": "run-123"})
    for spelling in SPELLINGS:
        key = delivery.fly_delivery_key(spelling, 123)
        assert "already recorded" in delivery.fly_delivery_blocker(key)
    plan = delivery.FlyDeliveryPlan(REPO, BRANCH, "main", "owner/repo#123", 123)
    assert delivery.fly_delivery_blocker(plan.key)


@pytest.mark.parametrize(
    "status", [s for s in LAUNCHED_STATUSES if s not in {"opened", "updated"}]
)
def test_pipeline_notification_carries_the_recovery_steps(monkeypatch, status):
    monkeypatch.setenv("APIS_FLY_RUNNER_CLASSES", "open_or_merge_pr")

    async def run(*a, **kw):
        return result(status)

    monkeypatch.setattr("swarm_dispatch.run_skill", run)
    d = dispatcher()
    asyncio.run(
        d._hand_off_to_build(
            trigger(),
            _empty_spec_state(),
            completed=[],
            spec_action="review",
            spec_ready_key="spec",
        )
    )
    assert_recovery_text(d.notifier.sent[-1], status)


# ── A dispatch changes only the receipt it reserved ──

KEY = REPO + "#123"


def _plan():
    return delivery.FlyDeliveryPlan(REPO, BRANCH, "main", KEY, 123)


def _hand_the_key_to_another_dispatch():
    """An operator cleared the first dispatch's receipt and a second dispatch
    reserved the same key. Returns the second dispatch's token."""
    ledger = delivery._delivery_ledger()
    assert ledger.clear_delivery(KEY, ledger.delivery_receipts()[KEY])
    token = delivery._reserve_delivery(_plan())
    assert token
    return token


def _interleaved(outcome):
    """Run dispatch A; while it is in flight B takes over the key; then A ends
    with *outcome*. Returns (what A returned or raised, B's token, A's blocked
    reasons)."""
    state, blocked = {}, []

    async def launch():
        state["b"] = _hand_the_key_to_another_dispatch()
        if outcome == "exception":
            raise RuntimeError("the earlier dispatch failed late")
        if outcome == "ineligible":
            return SkillResult("cicada", False, None, "", "", fly_ineligible_before_launch=True)
        if outcome == "refusal":
            return SkillResult("cicada", False, None, "", "", error="fly runner refused")
        return result("unknown")

    def stop(reason):
        blocked.append(reason)
        return "blocked"

    async def go():
        try:
            return await delivery.run_fly_delivery(_plan(), launch, stop)
        except RuntimeError as exc:
            return exc

    return asyncio.run(go()), state["b"], blocked


@pytest.mark.parametrize("outcome", ["ineligible", "refusal", "exception", "late_status"])
def test_an_earlier_dispatch_cannot_change_a_later_dispatchs_receipt(outcome):
    first, token_b, blocked = _interleaved(outcome)
    held = delivery._delivery_ledger().delivery_receipts()[KEY]
    assert held["token"] == token_b and held["status"] == "launching"
    # The later dispatch still owns a live, terminal-for-everyone-else receipt.
    assert "already recorded" in delivery.fly_delivery_blocker(KEY)
    if outcome == "ineligible":
        # No local path beside another dispatch's delivery.
        assert first == "blocked" and "now belongs to another dispatch" in blocked[0]
    elif outcome == "exception":
        assert isinstance(first, RuntimeError) and not blocked
    else:
        assert first is not None and first != "blocked" and not blocked
    # The later dispatch can still settle its own receipt.
    assert delivery._settle_delivery(_plan(), token_b, {"status": "pr_failed", "branch": BRANCH})
    assert delivery._delivery_ledger().delivery_receipts()[KEY]["status"] == "pr_failed"


def test_a_single_dispatchs_prelaunch_cleanup_still_removes_its_own_reservation():
    async def launch():
        return SkillResult("cicada", False, None, "", "", fly_ineligible_before_launch=True)

    got = asyncio.run(delivery.run_fly_delivery(_plan(), launch, lambda r: "blocked"))
    assert got is None  # the local path may run, as for a proven pre-launch outcome
    assert KEY not in delivery._delivery_ledger().delivery_receipts()
    assert delivery.fly_delivery_blocker(KEY) == ""


def test_the_ledger_changes_a_receipt_only_for_its_own_reservation():
    ledger = delivery._delivery_ledger()
    token = delivery._reserve_delivery(_plan())
    assert token and delivery._reserve_delivery(_plan()) == ""
    assert ledger.release_delivery(KEY, "someone-else") is False
    assert ledger.settle_delivery(KEY, "someone-else", {"status": "x", "token": "someone-else"}) is False
    with pytest.raises(fly_runner.FlyLedgerError):
        ledger.settle_delivery(KEY, token, {"status": "unknown"})  # no token carried
    assert ledger.delivery_receipts()[KEY]["token"] == token
    # A receipt that left launching is no longer releasable, even by its owner.
    assert delivery._settle_delivery(_plan(), token, {"status": "unknown", "branch": BRANCH})
    assert ledger.release_delivery(KEY, token) is False
    assert ledger.delivery_receipts()[KEY]["status"] == "unknown"
    assert ledger.release_delivery("owner/repo#999", token) is False


def test_every_reservation_has_its_own_token():
    ledger = delivery._delivery_ledger()
    first = delivery._reserve_delivery(_plan())
    assert ledger.release_delivery(KEY, first)
    second = delivery._reserve_delivery(_plan())
    assert first and second and first != second


# ── A cleanup whose outcome is uncertain never permits the local path ──

LEDGER_ERROR_TEXT = "ledger-error-text-must-not-leak"


def _fail_the_next_ledger_write(monkeypatch, point):
    """Make the next ledger write fail before the replace, or just after it (a
    directory-sync failure). Returns the switch the test arms."""
    real = fly_runner.FlyLedger._save
    armed = []

    def save(self, data):
        if not armed:
            return real(self, data)
        armed.clear()
        if point == "before_replace":
            raise OSError(LEDGER_ERROR_TEXT)
        real(self, data)
        raise OSError(LEDGER_ERROR_TEXT)

    monkeypatch.setattr(fly_runner.FlyLedger, "_save", save)
    return lambda: armed.append(True)


def _ineligible_run(arm):
    async def run(*a, **kw):
        arm()  # the next ledger write is this dispatch's cleanup
        return SkillResult("cicada", False, None, "", "", fly_ineligible_before_launch=True)

    return run


@pytest.mark.parametrize("point", ["before_replace", "after_replace"])
@pytest.mark.parametrize("entry", ["task", "pipeline"])
def test_a_failed_cleanup_never_starts_the_local_builder(monkeypatch, entry, point):
    monkeypatch.setenv("APIS_FLY_RUNNER_CLASSES", "open_or_merge_pr")
    run = _ineligible_run(_fail_the_next_ledger_write(monkeypatch, point))
    local = []

    async def prepare(*a, **kw):
        local.append(True)
        raise AssertionError("the local builder was started")

    monkeypatch.setattr(apis, "run_skill", run)
    monkeypatch.setattr("swarm_dispatch.run_skill", run)
    monkeypatch.setattr(delivery, "prepare_build_worktree", prepare)
    if entry == "task":
        got = asyncio.run(run_task())
        assert not got.ok
        text = got.error
    else:
        with pytest.raises(delivery.FlyDeliveryEnded) as ended:
            asyncio.run(
                dispatcher()._open_implementation_pr(trigger(), _empty_spec_state())
            )
        text = ended.value.text
    assert not local
    assert "could not be confirmed after a failed cleanup (OSError)" in text
    assert "local path was NOT started" in text
    assert f"{delivery.FLY_RECEIPT_COMMAND} list" in text
    receipt = delivery._delivery_ledger().delivery_receipts().get(KEY)
    assert LEDGER_ERROR_TEXT not in text
    assert receipt is None or receipt["token"] not in text
    # Whatever the ledger now holds, a later dispatch cannot assume the key is free:
    # before the replace the reservation is still there; after it, the next
    # dispatch is the one that reserves, never this one's local builder.
    assert (receipt is not None) == (point == "before_replace")


@pytest.mark.parametrize("entry", ["task", "pipeline"])
def test_a_successful_cleanup_still_allows_the_local_builder(monkeypatch, entry):
    monkeypatch.setenv("APIS_FLY_RUNNER_CLASSES", "open_or_merge_pr")
    run = _ineligible_run(lambda: None)
    local = []

    async def prepare(*a, **kw):
        local.append(True)
        raise delivery.BuildWorktreeError("stand-in: the local path ran")

    monkeypatch.setattr(apis, "run_skill", run)
    monkeypatch.setattr("swarm_dispatch.run_skill", run)
    monkeypatch.setattr(delivery, "prepare_build_worktree", prepare)
    if entry == "task":
        got = asyncio.run(run_task())
        assert not got.ok
    else:
        with pytest.raises(delivery.BuildNotStarted):
            asyncio.run(
                dispatcher()._open_implementation_pr(trigger(), _empty_spec_state())
            )
    assert local == [True]
    assert KEY not in delivery._delivery_ledger().delivery_receipts()


def test_fly_instructions_say_the_run_ends_when_the_agent_stops_replying():
    # A Fly delivery is one non-interactive run: an agent that backgrounds the
    # suite and ends its turn "until it reports" commits nothing.
    plan = delivery.FlyDeliveryPlan(REPO, BRANCH, "main", REPO + "#123", 123)
    text = delivery.fly_delivery_instructions(plan)
    assert "RUN MODE:" in text
    assert "never start a background command" in text
    assert "Commit before your final message" in text
    assert text.index("RUN MODE:") < text.index("DELIVERY:")
