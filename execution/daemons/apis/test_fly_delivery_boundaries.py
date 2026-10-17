"""Offline boundary regressions for the existing Fly delivery mechanism."""

import asyncio
import json
import time
from pathlib import Path

import pytest

import httpx

import build_delivery as delivery
import fly_runner
from test_fly_delivery_dispatch import BRANCH, COMMIT, REPO, result
from test_fly_delivery_dispatch import isolated_receipt as isolated_receipt
from test_fly_delivery_dispatch import component_producer_admission as component_producer_admission
from test_fly_runner import complete_record


@pytest.mark.parametrize("defect", [None, "duplicate", "partial", "boolean_count", "missing_winner", "mixed_winner", "owner", "thumbprint", "grant", "schema", "issuer", "wrong_issuer", "store_alias", "wrong_type", "unknown_bound", "signed_bool_exit", "signed_numeric_probe", "signed_numeric_timeout", "signed_finite_number", "snapshot_bool_exit", "extra_predicate", "contradictory_run", "mixed_predicates", "null_predicates", "duplicate_predicate", "boolean_returned"])
def test_common_canonical_admission_uses_actual_endpoint_shapes(monkeypatch, defect):
    from test_fly_runner import canonical_delivery_fixture

    job = fly_runner.FlyJob("synthetic-run", "cicada", "claude", "owner/repo", [], b"", 60, False,
                           delivery={"branch": "cicada/synthetic"})
    job.machine_id = "synthetic-machine"
    record = complete_record(job.run_id, role=job.role, provider=job.provider,
        repository=job.repository, machine_id=job.machine_id, delivery_status="opened",
        delivery_reason="", delivery_branch=job.delivery["branch"], delivery_commit="a" * 40,
        pr_number=9, pr_url="https://github.com/owner/repo/pull/9")
    binding, responses = canonical_delivery_fixture(record)
    query = responses["/entities/query"]
    observation = responses["/entities/ent_synthetic_result/observations"]
    if defect == "duplicate":
        query["entities"].append(query["entities"][0].copy())
    elif defect == "partial":
        query["read_contract"]["coverage"]["state"] = "partial"
    elif defect == "boolean_count":
        query["total"] = True
    elif defect == "missing_winner":
        query["entities"][0]["provenance"].pop("probe_ok")
    elif defect == "mixed_winner":
        query["entities"][0]["provenance"]["probe_ok"] = "other-observation"
    elif defect == "owner":
        observation["observations"][0]["user_id"] = "foreign-owner"
    elif defect == "thumbprint":
        observation["observations"][0]["provenance"]["agent_thumbprint"] = "foreign-key"
    elif defect == "grant":
        responses["/agents/grants/ent_synthetic_grant"]["grant"]["status"] = "revoked"
    elif defect == "schema":
        responses["/schemas/fly_runner_result"]["schema_definition"]["fields"].pop("delivery_status")
    elif defect in {"issuer", "wrong_issuer"}:
        responses["/agents/grants/ent_synthetic_grant"]["grant"]["match_iss"] = "https://issuer.invalid"
        observation["observations"][0]["provenance"]["agent_iss"] = (
            "https://foreign.invalid" if defect == "wrong_issuer" else "https://issuer.invalid")
    elif defect == "store_alias":
        responses["/agents/grants/ent_synthetic_grant"]["grant"]["capabilities"][0]["op"] = "store_structured"
    elif defect == "wrong_type":
        responses["/agents/grants/ent_synthetic_grant"]["grant"]["capabilities"][0]["entity_types"] = ["unrelated_type"]
    elif defect == "unknown_bound":
        responses["/agents/grants/ent_synthetic_grant"]["grant"]["unrecognized_permission_field"] = True
    elif defect in {"extra_predicate", "contradictory_run", "mixed_predicates", "null_predicates", "duplicate_predicate"}:
        predicates = query["read_contract"]["applied_scope"]["predicates"]
        if defect == "extra_predicate":
            predicates.append({"field": "probe_ok", "op": "eq", "value": {"kind": "sha256", "value": "b" * 64}})
        elif defect == "contradictory_run":
            predicates.append({**predicates[0], "value": {"kind": "sha256", "value": "b" * 64}})
        elif defect == "mixed_predicates":
            predicates.append(None)
        elif defect == "null_predicates":
            query["read_contract"]["applied_scope"]["predicates"] = None
        else:
            predicates.append(dict(predicates[0]))
    elif defect == "boolean_returned":
        query["read_contract"]["coverage"]["returned_count"] = True
    elif defect == "signed_bool_exit":
        observation["observations"][0]["fields"]["exit_code"] = False
    elif defect == "signed_numeric_probe":
        observation["observations"][0]["fields"]["probe_ok"] = 1
    elif defect == "signed_numeric_timeout":
        observation["observations"][0]["fields"]["timed_out"] = 0
    elif defect == "signed_finite_number":
        observation["observations"][0]["fields"]["exit_code"] = 0.0
    elif defect == "snapshot_bool_exit":
        query["entities"][0]["snapshot"]["exit_code"] = False
        observation["observations"][0]["fields"]["exit_code"] = False
    requests = []

    def transport(request):
        requests.append(request)
        assert request.url.host == "canonical.invalid"
        if request.url.path == "/entities/query":
            assert json.loads(request.content)["snapshot_filters"] == {
                "run_id": {"op": "eq", "value": job.run_id}}
        return httpx.Response(200, json=responses[request.url.path])

    async def acquire():
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            return await fly_runner._read_canonical_delivery(client, "https://canonical.invalid", job, binding)

    if defect not in {None, "issuer", "store_alias", "signed_finite_number"}:
        with pytest.raises(fly_runner.FlyResultUnreadable):
            asyncio.run(acquire())
    else:
        got = asyncio.run(acquire())
        assert got["row"]["snapshot"] == record
        assert got["observations"][0]["id"] == "obs_synthetic"
        assert got["grant"]["grant_id"] == binding["grant_id"]
        assert len(requests) == 4


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
    isolated_receipt, monkeypatch, prelaunch, component_producer_admission,
):
    plan = delivery.FlyDeliveryPlan(REPO, BRANCH, "main", REPO + "#123", 123)
    async def launch():
        if prelaunch:
            from skill_runner import SkillResult
            return SkillResult("cicada", False, None, "", "", fly_ineligible_before_launch=True)
        return result()
    record_attempts = []

    def cannot_record(*args, **kwargs):
        record_attempts.append((args, kwargs))
        raise ValueError("synthetic receipt persistence failure")
    monkeypatch.setattr(delivery, "_record", cannot_record)
    outcome = asyncio.run(delivery.run_fly_delivery(plan, launch, lambda reason: reason))
    assert outcome is not None
    assert not getattr(outcome, "ok", False)
    assert record_attempts, "the failing receipt sink was not reached"
    assert outcome == ("delivery receipt release was not confirmed; not falling back" if prelaunch
                       else "delivery receipt persistence was not confirmed; outcome unresolved")
    assert bool(component_producer_admission) is (not prelaunch)


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
    isolated_receipt, monkeypatch, entrypoint, mismatch, component_producer_admission,
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
    artifact_reads = []

    def remote_read(argv, **kwargs):
        assert argv[:3] == ["gh", "pr", "view"]
        artifact_reads.append(argv)
        return subprocess.CompletedProcess(
            argv, 1 if mismatch == "unavailable" else 0, json.dumps(answer), "unavailable")

    monkeypatch.setattr(subprocess, "run", remote_read)
    monkeypatch.setattr(apis, "run_skill", launch)
    monkeypatch.setattr("swarm_dispatch.run_skill", launch)
    if entrypoint == "task":
        got = asyncio.run(run_task())
        assert not got.ok and "pull_request_link:" not in got.stdout
    else:
        with pytest.raises(delivery.FlyDeliveryEnded):
            asyncio.run(dispatcher()._open_implementation_pr(trigger(), _empty_spec_state()))
    assert delivery._delivery_ledger().delivery_receipts()[REPO + "#123"]["status"] == "unknown"
    assert artifact_reads, "the mismatched remote artifact sink was not reached"
    assert component_producer_admission, "the producer component boundary was not reached"
    monkeypatch.delenv("APIS_FLY_RUNNER_CLASSES")
    assert not asyncio.run(run_task()).ok
    with pytest.raises(delivery.FlyDeliveryEnded):
        asyncio.run(dispatcher()._open_implementation_pr(trigger(), _empty_spec_state()))
    assert launches == [True]
