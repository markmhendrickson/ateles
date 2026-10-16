"""Offline boundary regressions for the existing Fly delivery mechanism."""

import asyncio
import json
import time
from pathlib import Path

import pytest

import build_delivery as delivery
import fly_runner
from test_fly_delivery_dispatch import BRANCH, COMMIT, REPO, isolated_receipt, result
from test_fly_runner import complete_record


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
    with pytest.raises(fly_runner.FlyResultUnreadable):
        fly_runner.FlyExecutor._result_from_record(None, job, record)
