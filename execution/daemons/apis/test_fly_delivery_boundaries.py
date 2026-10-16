"""Offline boundary regressions for the existing Fly delivery mechanism."""

import asyncio
import json
import time
from pathlib import Path

import pytest

import build_delivery as delivery
import fly_runner
from test_fly_delivery_dispatch import BRANCH, COMMIT, REPO, result
from test_fly_delivery_dispatch import isolated_receipt as isolated_receipt
from test_fly_runner import complete_record


@pytest.mark.parametrize("rows", [
    [{"snapshot": {"run_id": "run-123"}}, {"snapshot": {"run_id": "run-123"}}],
    [{"snapshot": {"run_id": "other-run"}}],
    [{"snapshot": {"run_id": "run-123"}}, {"snapshot": {"run_id": "other-run"}}],
])
def test_result_query_requires_one_unambiguous_matching_record(monkeypatch, rows):
    import httpx
    monkeypatch.setenv("NEOTOMA_BASE_URL", "https://memory.example.invalid")
    monkeypatch.setenv("NEOTOMA_BEARER_TOKEN", "synthetic-parent-token")
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"entities": rows}))
    with pytest.raises(fly_runner.FlyResultUnreadable):
        asyncio.run(fly_runner.read_result_record("run-123", transport=transport, attempts=1))


def receipt(status="unknown", **over):
    return {"status": status, "branch": BRANCH, "run_id": "run-123",
            "recorded_at": time.time(), "commit": "", "reason": "",
            "pr_number": None, "pr_url": "", **over}


def test_both_delivery_entrances_use_one_repository_issue_identity():
    task = delivery.issue_key({"issue_number": 123}, "Owner/Repo")
    assert delivery.fly_delivery_key("Owner/Repo", ("Owner/Repo", 123)) == delivery.fly_delivery_key("owner/repo", task)


def test_legacy_repository_case_receipt_still_blocks_launch(isolated_receipt):
    ledger = delivery._delivery_ledger()
    ledger.record_delivery(REPO + "#123", receipt())
    path = Path(delivery._registry_path())
    document = json.loads(path.read_text())
    document["deliveries"]["Owner/Repo#123"] = document["deliveries"].pop(REPO + "#123")
    path.write_text(json.dumps(document))
    launched = []
    async def launch():
        launched.append(True)
        return result()
    plan = delivery.FlyDeliveryPlan(REPO, BRANCH, "main", REPO + "#123", 123)
    asyncio.run(delivery.run_fly_delivery(plan, launch, lambda reason: reason))
    assert launched == []


@pytest.mark.parametrize("entry", [{"status": "clone_failed"}, {"status": "not_delivered"},
                                    {"status": "opened"}, receipt("unrecognized")])
def test_present_receipts_are_complete_before_retry_admission(isolated_receipt, entry):
    ledger = delivery._delivery_ledger()
    ledger.record_delivery(REPO + "#123", receipt())
    path = Path(delivery._registry_path())
    document = json.loads(path.read_text())
    document["deliveries"][REPO + "#123"] = entry
    path.write_text(json.dumps(document))
    with pytest.raises(fly_runner.FlyLedgerError):
        ledger.delivery_receipts()


@pytest.mark.parametrize("prelaunch", [False, True])
def test_receipt_persistence_failure_never_reports_success_or_local_fallback(
    isolated_receipt, monkeypatch, prelaunch,
):
    plan = delivery.FlyDeliveryPlan(REPO, BRANCH, "main", REPO + "#123", 123)
    async def launch():
        if prelaunch:
            from skill_runner import SkillResult
            return SkillResult("cicada", False, None, "", "", fly_ineligible_before_launch=True)
        return result()
    def cannot_record(*args, **kwargs):
        raise ValueError("synthetic receipt persistence failure")
    monkeypatch.setattr(delivery, "_record", cannot_record)
    outcome = asyncio.run(delivery.run_fly_delivery(plan, launch, lambda reason: reason))
    assert outcome is not None
    assert not getattr(outcome, "ok", False)


@pytest.mark.parametrize("changes", [
    {"exit_code": 1}, {"timed_out": True}, {"probe_ok": False},
    {"role": "other"}, {"provider": "cursor"}, {"repository": "other/repo"},
])
def test_result_job_and_execution_bindings_precede_delivery_acceptance(changes):
    job = fly_runner.FlyJob(run_id="run-123", role="cicada", provider="codex", repository=REPO,
                            argv=[], stdin=b"", timeout=60, needs_github=False,
                            delivery={"branch": BRANCH})
    record = complete_record(job.run_id, role=job.role, provider=job.provider, repository=REPO,
                             delivery_status="opened", delivery_branch=BRANCH,
                             delivery_commit=COMMIT, pr_number=99,
                             pr_url=f"https://github.com/{REPO}/pull/99")
    record.update(changes)
    with pytest.raises(fly_runner.FlyResultUnreadable, match="job identity|credential probe|execution outcome"):
        fly_runner.FlyExecutor._result_from_record(None, job, record)


@pytest.mark.parametrize("entrypoint", ["task", "pipeline"])
@pytest.mark.parametrize("mismatch", ["base", "branch", "commit", "repository", "unavailable"])
def test_remote_delivery_mismatch_is_unconfirmed_and_cannot_replay(
    isolated_receipt, monkeypatch, entrypoint, mismatch,
):
    import subprocess
    import apis
    from test_fly_delivery_dispatch import run_task, dispatcher, trigger, remote_pr
    from test_swarm_dispatch import _empty_spec_state
    monkeypatch.setenv("APIS_FLY_RUNNER_CLASSES", "open_or_merge_pr")
    launches = []

    async def launch(*args, **kwargs):
        launches.append(True)
        return result()

    answer = remote_pr()
    if mismatch == "base":
        answer["baseRefName"] = "other-base"
    elif mismatch == "branch":
        answer["headRefName"] = "other-branch"
    elif mismatch == "commit":
        answer["headRefOid"] = "b" * 40
    elif mismatch == "repository":
        answer["headRepository"] = {"nameWithOwner": "other/repo"}
    monkeypatch.setattr(subprocess, "run", lambda argv, **kwargs: subprocess.CompletedProcess(
        argv, 1 if mismatch == "unavailable" else 0, json.dumps(answer), "unavailable"))
    monkeypatch.setattr(apis, "run_skill", launch)
    monkeypatch.setattr("swarm_dispatch.run_skill", launch)
    if entrypoint == "task":
        got = asyncio.run(run_task())
        assert not got.ok and "pull_request_link:" not in got.stdout
    else:
        with pytest.raises(delivery.FlyDeliveryEnded):
            asyncio.run(dispatcher()._open_implementation_pr(trigger(), _empty_spec_state()))
    assert delivery._delivery_ledger().delivery_receipts()[REPO + "#123"]["status"] == "unknown"
    monkeypatch.delenv("APIS_FLY_RUNNER_CLASSES")
    assert not asyncio.run(run_task()).ok
    with pytest.raises(delivery.FlyDeliveryEnded):
        asyncio.run(dispatcher()._open_implementation_pr(trigger(), _empty_spec_state()))
    assert launches == [True]
