"""A Fly delivery is verified only when the real pull request, read back from
GitHub, matches the runner's own report.

Offline entrypoint tests: the runner's result is a stub and GitHub's answer is
canned. Both delivery entries (task and issue pipeline) are driven.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import apis
import build_delivery as delivery
import test_fly_delivery_dispatch as base
from test_fly_delivery_dispatch import (
    REPO,
    _empty_spec_state,
    dispatcher,
    pr_answer,
    result,
    run_task,
    trigger,
)

# Fixtures are found by module attribute: re-exported here so the same canned
# GitHub and isolated receipt ledger apply to this file.
github_pr = base.github_pr
isolated_receipt = base.isolated_receipt

KEY = REPO + "#123"
MISMATCHES = {
    "head commit": pr_answer(headRefOid="b" * 40),
    "base branch": pr_answer(baseRefName="release"),
    "head branch": pr_answer(headRefName="someone/else"),
    "repository": pr_answer(headRepository={"nameWithOwner": "fork/repo"}),
}
UNREADABLE = {
    "not found": (1, "", "HTTP 404: Not Found"),
    "outage": (1, "", "HTTP 503: Service Unavailable"),
    "malformed": (0, "<html>", ""),
}


def launch(monkeypatch, **fields):
    monkeypatch.setenv("APIS_FLY_RUNNER_CLASSES", "open_or_merge_pr")

    async def run(skill, prompt, **kw):
        return result(**fields)

    monkeypatch.setattr(apis, "run_skill", run)
    monkeypatch.setattr("swarm_dispatch.run_skill", run)


def pipeline():
    return asyncio.run(
        dispatcher()._open_implementation_pr(trigger(), _empty_spec_state())
    )


def test_a_matching_pull_request_is_verified_for_a_task(monkeypatch):
    launch(monkeypatch)
    got = asyncio.run(run_task())
    assert got.ok and "pull_request_link" in got.stdout


def test_a_matching_pull_request_is_handed_on_by_the_pipeline(monkeypatch):
    launch(monkeypatch)
    assert pipeline() == f"https://github.com/{REPO}/pull/99"


@pytest.mark.parametrize("field", list(MISMATCHES))
def test_task_delivery_is_not_verified_when_the_pull_request_differs(
    monkeypatch, github_pr, field
):
    launch(monkeypatch)
    github_pr["answer"] = (0, json.dumps(MISMATCHES[field]), "")
    got = asyncio.run(run_task())
    assert not got.ok
    assert "not verified" in got.error and field in got.error
    # No artifact header, so the artifact gate has nothing to mark verified.
    assert "pull_request_link" not in got.stdout


@pytest.mark.parametrize("field", list(MISMATCHES))
def test_pipeline_delivery_hands_on_no_pull_request_that_differs(
    monkeypatch, github_pr, field
):
    launch(monkeypatch)
    github_pr["answer"] = (0, json.dumps(MISMATCHES[field]), "")
    with pytest.raises(delivery.FlyDeliveryEnded) as ended:
        pipeline()
    assert "not verified" in ended.value.text and field in ended.value.text


@pytest.mark.parametrize("kind", list(UNREADABLE))
def test_an_unreadable_pull_request_is_not_verified_for_a_task(
    monkeypatch, github_pr, kind
):
    launch(monkeypatch)
    github_pr["answer"] = UNREADABLE[kind]
    got = asyncio.run(run_task())
    assert not got.ok and "not verified" in got.error


@pytest.mark.parametrize("kind", list(UNREADABLE))
def test_an_unreadable_pull_request_is_not_verified_for_the_pipeline(
    monkeypatch, github_pr, kind
):
    launch(monkeypatch)
    github_pr["answer"] = UNREADABLE[kind]
    with pytest.raises(delivery.FlyDeliveryEnded) as ended:
        pipeline()
    assert "not verified" in ended.value.text


@pytest.mark.parametrize(
    "missing, blank",
    [("fly_delivery_branch", ""), ("fly_delivery_commit", ""), ("fly_pr_number", None)],
)
def test_a_field_the_runner_did_not_report_is_not_verified(monkeypatch, missing, blank):
    launch(monkeypatch, **{missing: blank})
    got = asyncio.run(run_task())
    assert not got.ok and "did not report" in got.error


def test_a_failed_readback_keeps_the_receipt_so_nothing_is_redelivered(
    monkeypatch, github_pr
):
    launch(monkeypatch)
    github_pr["answer"] = (0, json.dumps(MISMATCHES["head commit"]), "")
    assert not asyncio.run(run_task()).ok
    assert delivery.fly_delivery_blocker(KEY)


def _plan():
    return delivery.FlyDeliveryPlan(REPO, base.BRANCH, "main", KEY, 123)


def _deliver(monkeypatch, status="opened", verdict=None):
    calls = []

    async def verify(res, plan):
        calls.append(res.fly_pr_url)
        return verdict

    monkeypatch.setattr(delivery, "verify_fly_delivery", verify)

    async def launch():
        return result(status)

    got = asyncio.run(delivery.run_fly_delivery(_plan(), launch, lambda r: r))
    return got, delivery._delivery_ledger().delivery_receipts()[KEY], calls


def test_a_failed_readback_is_recorded_on_the_receipt(monkeypatch):
    got, receipt, calls = _deliver(monkeypatch, verdict="commit differs")
    assert calls and got.fly_delivery_readback == "failed"
    assert receipt["readback"] == "failed" and receipt["readback_reason"] == "commit differs"
    # The runner's own status stays as it reported it.
    assert receipt["status"] == "opened"


def test_a_passing_readback_is_recorded_on_the_receipt(monkeypatch):
    got, receipt, _ = _deliver(monkeypatch)
    assert got.fly_delivery_readback == "passed"
    assert receipt["readback"] == "passed" and receipt["readback_reason"] == ""


def test_no_pull_request_means_no_readback(monkeypatch):
    got, receipt, calls = _deliver(monkeypatch, status="no_changes")
    assert calls == [] and got.fly_delivery_readback == "not_applicable"
    assert receipt["readback"] == "not_applicable" and receipt["status"] == "no_changes"


def test_a_readback_that_raises_is_a_failed_readback(monkeypatch):
    async def boom(res, plan):
        raise RuntimeError("gh exploded")

    monkeypatch.setattr(delivery, "verify_fly_delivery", boom)

    async def launch():
        return result()

    got = asyncio.run(delivery.run_fly_delivery(_plan(), launch, lambda r: r))
    assert got.fly_delivery_readback == "failed"
    assert got.fly_delivery_readback_reason == "read-back error: RuntimeError"


@pytest.mark.parametrize("verdict", [None, "commit differs"])
def test_the_task_summary_is_logged_only_after_the_readback(monkeypatch, caplog, verdict):
    launch(monkeypatch)
    seen = []

    async def verify(res, plan):
        seen.append([r.getMessage() for r in caplog.records
                     if "opened pull request" in r.getMessage()])
        return verdict

    monkeypatch.setattr(delivery, "verify_fly_delivery", verify)
    with caplog.at_level(logging.INFO):
        got = asyncio.run(run_task())
    assert seen == [[]]  # nothing claimed the PR before it was read back
    lines = [r.getMessage() for r in caplog.records if "opened pull request" in r.getMessage()]
    assert len(lines) == 1
    if verdict:
        assert not got.ok and "Read-back FAILED: commit differs." in lines[0]
    else:
        assert got.ok and lines[0].endswith("Read-back: passed.")


def test_the_readback_is_made_with_the_pr_opener_app_for_that_repository(
    monkeypatch, github_pr
):
    launch(monkeypatch)
    got = asyncio.run(run_task())
    assert got.ok
    assert github_pr["mints"] == [("pr_opener", REPO)]
    assert github_pr["tokens"] and all(
        t == f"app-token-for-{REPO}" for t in github_pr["tokens"]
    )


def test_no_pr_opener_token_means_the_delivery_is_not_verified(
    monkeypatch, github_pr
):
    from lib import github_app_token

    def refuse(identity, repo=None):
        raise github_app_token.AppTokenError("GitHub App not configured")

    monkeypatch.setattr(github_app_token, "identity_token", refuse)
    launch(monkeypatch)
    got = asyncio.run(run_task())
    assert not got.ok and "read-back credential is unavailable" in got.error
    assert github_pr["tokens"] == []
