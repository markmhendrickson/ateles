"""Desired Round2 exclusion and truthful outcome invariants; inert sources only."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import apis
import build_delivery as delivery
from skill_runner import SkillResult
from test_existing_pr_delivery import snapshot
from test_build_delivery import dispatch_env as dispatch_env
from test_fly_delivery_dispatch import _StubNotifier, dispatcher, trigger
from test_swarm_dispatch import _empty_spec_state
from test_fly_run_job_delivery import world as world, existing_correction as existing_correction
import test_fly_run_job_delivery as flow

REPO = "owner/repo"
_REAL_SPAWN = apis._spawn_harness_skill


@pytest.fixture(autouse=True)
def private_ledger(monkeypatch, tmp_path):
    path = (tmp_path / "round2-ledger.json").resolve()
    monkeypatch.setenv("APIS_FLY_LEDGER_FILE", str(path))
    assert Path(delivery._registry_path()).resolve() == path
    assert not path.exists()
    monkeypatch.setenv("APIS_FLY_RUNNER_CLASSES", "open_or_merge_pr")


async def correct():
    return await apis._spawn_harness_skill(
        "cicada", "ent_inert_correction", snapshot(issue_number=34), "approved", _StubNotifier())


async def source(number=34, pipeline=False):
    if pipeline:
        import dataclasses
        return await dispatcher()._open_implementation_pr(
            dataclasses.replace(trigger(), number=number), _empty_spec_state())
    return await apis._spawn_harness_skill(
        "cicada", "ent_inert_source", dict(repository=REPO, issue_number=number,
        action_type="open_or_merge_pr", assigned_to="cicada"), "approved", _StubNotifier())


def noop(skill):
    return SkillResult(skill, False, 0, "", "", fly_delivery_status="no_changes", remote_run_id="inert-run")


@pytest.mark.parametrize("pipeline", [False, True])
def test_correction_excludes_normal_source_for_entire_lifetime(monkeypatch, pipeline):
    seen = []
    async def verify(*a, **kw):
        return None
    async def run(skill, prompt, **kw):
        seen.append("correction" if "pr_number" in kw["fly_delivery"] else "source")
        if seen[-1] == "correction":
            try:
                await source(pipeline=pipeline)
            except (delivery.FlyDeliveryEnded, delivery.BuildWorktreeError):
                pass
        return noop(skill)
    monkeypatch.setattr(delivery, "verify_existing_pr_target", verify)
    monkeypatch.setattr(apis, "run_skill", run)
    monkeypatch.setattr("swarm_dispatch.run_skill", run)
    asyncio.run(correct())
    assert seen == ["correction"]
    receipts = delivery._delivery_ledger().delivery_receipts()
    assert list(receipts) == [REPO + "#12"]
    assert REPO + "#34" in receipts[REPO + "#12"]["associated_keys"]
    assert delivery.active_build(REPO, 12) is None and delivery.active_build(REPO, 34) is None


def test_legacy_claim_cannot_enter_during_awaited_correction_preflight(monkeypatch):
    observed = []
    async def verify(*a, **kw):
        try:
            with delivery.claim_build(REPO, 34, "legacy-source", exclusive=False):
                observed.append("second writer")
        except delivery.BuildWorktreeError:
            observed.append("excluded")
        return None
    async def run(skill, *a, **kw):
        observed.append("correction")
        return noop(skill)
    monkeypatch.setattr(delivery, "verify_existing_pr_target", verify)
    monkeypatch.setattr(apis, "run_skill", run)
    asyncio.run(correct())
    assert observed == ["excluded", "correction"]


def test_confirmed_prelaunch_refusal_preserves_diagnostics_and_no_launch(monkeypatch):
    original = SkillResult("cicada", False, None, "inert output", "inert stderr",
                           error="inert selected configuration refusal", provider="codex",
                           attempted_providers=("codex",), configuration_repair="repair existing configuration")
    async def verify(*a, **kw):
        return None
    async def run(*a, **kw):
        return original
    monkeypatch.setattr(delivery, "verify_existing_pr_target", verify)
    monkeypatch.setattr(apis, "run_skill", run)
    got = asyncio.run(correct())
    assert got is original
    assert got.error == "inert selected configuration refusal" and got.stderr == "inert stderr"
    assert got.configuration_repair == "repair existing configuration" and got.attempted_providers == ("codex",)
    assert got.fly_delivery_status == "" and got.fly_delivery_not_launched
    assert got.fly_delivery_cleanup == "released"
    assert delivery._delivery_ledger().delivery_receipts() == {}


@pytest.mark.parametrize("pipeline", [False, True])
def test_source_first_keeps_correction_out(monkeypatch, pipeline):
    seen = []
    async def verify(*a, **kw):
        return None
    async def run(skill, *a, **kw):
        seen.append("source" if "pr_number" not in kw["fly_delivery"] else "correction")
        if seen[-1] == "source":
            assert not (await correct()).ok
        return noop(skill)
    monkeypatch.setattr(delivery, "verify_existing_pr_target", verify)
    monkeypatch.setattr(apis, "run_skill", run)
    monkeypatch.setattr("swarm_dispatch.run_skill", run)
    async def go():
        try:
            await source(pipeline=pipeline)
        except delivery.FlyDeliveryEnded:
            pass
    asyncio.run(go())
    assert seen == ["source"]


@pytest.mark.parametrize("other", ["task", "pipeline", "same_pr", "unrelated"])
def test_awaited_preflight_keeps_both_associations_owned(monkeypatch, other):
    seen = []
    async def run(skill, *a, **kw):
        seen.append("correction" if "pr_number" in kw["fly_delivery"] else "source")
        return noop(skill)
    monkeypatch.setattr(apis, "run_skill", run)
    monkeypatch.setattr("swarm_dispatch.run_skill", run)
    async def go():
        entered, resume = asyncio.Event(), asyncio.Event()
        async def verify(*a, **kw):
            entered.set()
            await resume.wait()
            return None
        monkeypatch.setattr(delivery, "verify_existing_pr_target", verify)
        task = asyncio.create_task(correct())
        await entered.wait()
        try:
            if other == "same_pr":
                result = await asyncio.wait_for(apis._spawn_harness_skill(
                    "cicada", "ent_inert_other", snapshot(issue_number=35), "approved", _StubNotifier()), 1)
                assert not result.ok
            else:
                try:
                    await source(35 if other == "unrelated" else 34, pipeline=other == "pipeline")
                except (delivery.FlyDeliveryEnded, delivery.BuildNotStarted):
                    pass
            assert seen == (["source"] if other == "unrelated" else [])
        finally:
            resume.set()
            await task
    asyncio.run(go())
    assert seen == (["source", "correction"] if other == "unrelated" else ["correction"])
    assert delivery.active_build(REPO, 34) is None and delivery.active_build(REPO, 12) is None


def test_associations_survive_settlement_restart_and_token_owned_recovery(monkeypatch):
    ledger = delivery._delivery_ledger()
    primary, alias = REPO + "#12", REPO + "#34"
    entry = {"status": "launching", "token": "inert-owner", "associated_keys": [alias]}
    assert ledger.reserve_delivery(primary, entry)
    assert not ledger.reserve_delivery(alias, {"status": "launching", "token": "competitor"})
    assert not ledger.release_delivery(primary, "competitor")
    assert ledger.settle_delivery(primary, "inert-owner", {"status": "unknown", "token": "inert-owner"})
    assert ledger.delivery_receipts()[primary]["associated_keys"] == [alias]
    assert not ledger.release_delivery(primary, "inert-owner")  # terminal/unknown is never auto-cleared
    monkeypatch.setattr(delivery, "_inflight", {})
    assert "already recorded" in delivery.fly_delivery_blocker(alias)
    import fly_delivery_receipt
    assert fly_delivery_receipt._find(ledger.delivery_receipts(), alias)[0] == primary
    held = ledger.delivery_receipts()[primary]
    assert ledger.clear_delivery(primary, held)  # exact private fixture receipt, no live recovery
    assert ledger.reserve_delivery(alias, {"status": "launching", "token": "new-inert-owner"})


@pytest.mark.parametrize("source_first", [False, True])
def test_ledger_reservation_blocks_the_other_association_in_both_orders(source_first):
    ledger = delivery._delivery_ledger()
    primary, alias = REPO + "#12", REPO + "#34"
    correction = {"status": "launching", "token": "correction", "associated_keys": [alias]}
    original = {"status": "launching", "token": "source"}
    if source_first:
        assert ledger.reserve_delivery(alias, original)
        assert not ledger.reserve_delivery(primary, correction)
    else:
        assert ledger.reserve_delivery(primary, correction)
        assert not ledger.reserve_delivery(alias, original)
    assert ledger.reserve_delivery(REPO + "#35", {"status": "launching", "token": "unrelated"})


@pytest.mark.parametrize("cleanup", ["not_owned", "unconfirmed"])
def test_prelaunch_unconfirmed_cleanup_preserves_cause_and_unknown_hold(monkeypatch, cleanup):
    original = SkillResult("cicada", False, None, "kept output", "kept stderr",
                           error="kept cause", configuration_repair="kept repair",
                           fly_ineligible_before_launch=True, provider="codex", attempted_providers=("codex",))
    async def verify(*a, **kw):
        return None
    async def run(*a, **kw):
        return original
    def release(*a):
        if cleanup == "unconfirmed":
            raise OSError("inert interrupted cleanup")
        return False
    monkeypatch.setattr(delivery, "verify_existing_pr_target", verify)
    monkeypatch.setattr(apis, "run_skill", run)
    monkeypatch.setattr(delivery, "_release_delivery", release)
    got = asyncio.run(correct())
    assert got is original and got.error == "kept cause" and got.stderr == "kept stderr"
    assert got.configuration_repair == "kept repair" and got.fly_delivery_status == "unknown"
    assert got.fly_delivery_cleanup == cleanup
    assert "already recorded" in delivery.fly_delivery_blocker(REPO + "#34")


def test_release_wakes_both_original_recovery_associations(monkeypatch):
    released = []
    monkeypatch.setattr(delivery, "_release_hook", released.append)
    with delivery.claim_build(REPO, 12, "correction", exclusive=True, aliases=((REPO, 34),)):
        primary = delivery.active_build(REPO, 12)
        original = delivery.active_build(REPO, 34)
        assert primary.run_id == original.run_id and primary.holder == original.holder
        assert primary.number == 12 and original.number == 34
        with pytest.raises(delivery.BuildWorktreeError):
            with delivery.claim_build(REPO, 34, "legacy", exclusive=False):
                pytest.fail("legacy option bypassed the correction")
    assert set(released) == {(REPO, 12), (REPO, 34)}


def test_receipt_display_exposes_target_effect_and_current_qualification():
    from fly_delivery_receipt import describe
    text = describe(REPO + "#12", {"status": "no_time", "branch": "codex/scoped-correction",
        "existing_pr": {"pr_number": "12", "branch": "codex/scoped-correction", "base_branch": "main", "head_sha": "a" * 40},
        "associated_keys": [REPO + "#34"], "reason": "[push=not_started] inert timeout", "readback": "failed", "readback_reason": "inert target divergence"})
    for expected in ("#12", "a" * 40, "main", "inert timeout", "failed", "inert target divergence", "checkpoint", "Clearing this receipt alone"):
        assert expected in text


@pytest.mark.parametrize("phase", ["before_import", "after_import", "uncertain_applied", "uncertain_not_applied", "after_push"])
def test_timeout_reports_actual_push_evidence_and_existing_target(world, existing_correction, monkeypatch, phase):
    from types import SimpleNamespace
    import fly_runner
    import skill_runner
    spec, _, _ = existing_correction
    actual_import = flow.rd.import_agent_work
    actual_check = flow.rd.require_existing_pr
    actual_run = flow.rd._run
    checks = []
    def no_time():
        raise flow.rd.RepoWorkError(flow.rd.NO_TIME, flow.rd.NO_TIME_REASON)
    def importing(*a, **kw):
        if phase == "before_import":
            no_time()
        return actual_import(*a, **kw)
    def checking(*a, **kw):
        checks.append(True)
        if (phase == "after_import" and len(checks) == 2) or (phase == "after_push" and len(checks) == 3):
            no_time()
        return actual_check(*a, **kw)
    def running(args, **kw):
        if "push" in args and phase.startswith("uncertain"):
            if phase == "uncertain_applied":
                assert actual_run(args, **kw).returncode == 0
            return "timed_out"
        return actual_run(args, **kw)
    monkeypatch.setattr(flow.rd, "import_agent_work", importing)
    monkeypatch.setattr(flow.rd, "require_existing_pr", checking)
    monkeypatch.setattr(flow.rd, "_run", running)
    _, record = flow._run(flow._job(world["tmp"], delivery=spec), flow._agent(world))
    job = SimpleNamespace(delivery=spec, delivery_result=fly_runner.delivery_result_from_record(record))
    result = SkillResult("cicada", False, 0, "", "", **skill_runner._fly_delivery_fields(job, unknown=False))
    text = delivery.fly_delivery_summary(result, REPO + "#12")
    remote = flow._remote_ref(world, f"refs/heads/{spec['branch']}")
    assert "existing PR #12" in text
    if phase in {"before_import", "after_import", "uncertain_not_applied"}:
        assert remote == spec["head_sha"]
    else:
        assert remote == record["delivery_commit"] != spec["head_sha"]
    if phase.startswith("uncertain"):
        assert "push outcome is unknown" in text and "was pushed" not in text
    elif phase == "after_push":
        assert "was pushed" in text and "confirmation did not complete" in text
        assert "no pull request was opened" not in text
    else:
        assert "no push was started" in text and "was pushed" not in text
        if phase == "after_import":
            assert record["delivery_commit"] and "imported locally only" in text
    assert record["delivery_status"] in {"no_time", "push_failed"}


def test_single_provider_keeps_original_adapter_diagnostics(monkeypatch):
    import skill_runner
    monkeypatch.setattr(skill_runner, "provider_candidates", lambda *a, **kw: ["codex", "cursor"])
    monkeypatch.setattr(skill_runner, "_refresh_usage_snapshot", lambda *a: None)
    original = SkillResult("cicada", False, None, "original output", "original stderr",
                           error="original cause", configuration_repair="original repair", provider="codex")
    seen = []
    async def attempt(name):
        seen.append(name)
        return original
    got = asyncio.run(skill_runner._run_provider_attempts(
        "cicada", attempt, binaries={"codex": "fake", "cursor": "fake"}, single_provider=True))
    assert got is original and got.error == "original cause" and got.stderr == "original stderr"
    assert got.configuration_repair == "original repair" and got.attempted_providers == ("codex",)
    assert seen == ["codex"]


def test_normal_dispatch_keeps_known_no_launch_as_technical_hold(dispatch_env, monkeypatch):
    from lib.notify import Priority
    from task_watchdog import TaskWatchdog, WatchdogAction
    _, _, store = dispatch_env
    eid = "ent_inert_prelaunch_hold"
    row = snapshot(status="pending", blocked_reason="", confidence=0.95, issue_number=34)
    store.put(eid, row)
    result = SkillResult("cicada", False, None, "kept output", "kept diagnostic",
                         error="selected adapter refused before launch", provider="codex",
                         attempted_providers=("codex",), configuration_repair="repair existing setting",
                         fly_delivery_not_launched=True, fly_delivery_cleanup="released")
    async def refused(*a, **kw):
        return result
    def save(entity_id, state, **kw):
        ok = store.status(entity_id, state, **kw)
        if kw.get("result") is not None:
            ok = store.field(entity_id, "result", kw["result"]) and ok
        return ok
    monkeypatch.setattr(apis, "set_task_status", save)
    monkeypatch.setattr(apis, "_spawn_harness_skill", refused)
    monkeypatch.setattr(apis, "RUN_EMAIL", False)
    notice = _StubNotifier()
    # Seed the trusted invocation context for this downstream diagnostic unit.
    # The joined handoff suite exercises its authenticated consumer origin;
    # task-supplied fields/approved triggers cannot create this context.
    current, version = store.read_versioned(eid)
    binding = apis._ApprovedCorrectionLifecycle(
        eid, apis._correction_lifecycle_shape(current), version, "inert-release")
    asyncio.run(apis.dispatch_task(eid, row, "approved", notice, gate_override=True,
                                 _approved_correction_lifecycle=binding))
    saved = store.rows[eid]
    assert saved["status"] == "blocked"
    assert "not launched" in saved["result"] and "kept diagnostic" in saved["result"]
    assert "repair existing setting" in saved["result"] and "codex" in saved["result"]
    assert "UNKNOWN" not in saved["result"]
    assert Priority.OPERATOR_DECISION not in notice.priorities
    assert TaskWatchdog().classify(eid, "blocked", None) == WatchdogAction.NONE



def test_normal_dispatch_defers_original_source_behind_correction(dispatch_env, monkeypatch):
    _, _, store = dispatch_env
    notice = _StubNotifier()
    row = dict(repository=REPO, issue_number=34, assigned_to="cicada", action_type="open_or_merge_pr",
               confidence=0.95, status="pending", description="Original source work", context="retained context", notes="retained history")
    eid = "ent_inert_deferred_source"
    store.put(eid, row)
    seen = []
    async def verify(*a, **kw):
        return None
    async def run(skill, *a, **kw):
        seen.append("correction" if "pr_number" in kw["fly_delivery"] else "source")
        if seen[-1] == "correction":
            await apis.dispatch_task(eid, row, "created", notice)
        return noop(skill)
    monkeypatch.setattr(delivery, "verify_existing_pr_target", verify)
    monkeypatch.setattr(apis, "_spawn_harness_skill", _REAL_SPAWN)
    monkeypatch.setattr(apis, "run_skill", run)
    monkeypatch.setattr(apis, "RUN_EMAIL", False)
    asyncio.run(correct())
    assert seen == ["correction"]
    saved = store.rows[eid]
    assert saved["disposition"] == delivery.DEFERRED_DISPOSITION
    assert delivery.parse_deferral_context(saved["dispatch_context"])[:2] == (REPO, 34)
    assert saved["context"] == row["context"] and saved["notes"] == row["notes"]


@pytest.mark.parametrize("change", ["closed", "base"])
def test_accepted_metadata_divergence_reports_same_ref_post_effect_hold(world, existing_correction, monkeypatch, change):
    from types import SimpleNamespace
    import fly_runner
    import skill_runner
    spec, calls, state = existing_correction
    push = flow.rd.push_branch
    def racing(work, **kw):
        push(work, **kw)
        if change == "closed":
            state["state"] = "closed"
        else:
            state["base"] = {"ref": "other", "repo": {"full_name": REPO}}
    monkeypatch.setattr(flow.rd, "push_branch", racing)
    _, record = flow._run(flow._job(world["tmp"], delivery=spec), flow._agent(world))
    assert record["delivery_status"] == "pr_failed"
    assert flow._remote_ref(world, f"refs/heads/{spec['branch']}") == record["delivery_commit"] != spec["head_sha"]
    job = SimpleNamespace(delivery=spec, delivery_result=fly_runner.delivery_result_from_record(record))
    result = SkillResult("cicada", False, 0, "", "", **skill_runner._fly_delivery_fields(job, unknown=False))
    text = delivery.fly_delivery_summary(result, REPO + "#12")
    assert "was pushed" in text and "existing PR #12 could not be confirmed" in text
    assert "no push was started" not in text and all(method == "GET" for method, _ in calls)


@pytest.mark.parametrize("aliases", [None, "owner/repo#34", {}, [None], [""], [7]])
@pytest.mark.parametrize("persisted", [False, True])
def test_malformed_present_associations_never_become_legacy_absence(aliases, persisted):
    import json
    import fly_runner
    ledger = delivery._delivery_ledger()
    key = REPO + "#12"
    # A genuine legacy receipt remains a durable exclusion after a fresh read.
    assert ledger.reserve_delivery(key, {"status": "unknown", "token": "inert-old"})
    assert not ledger.reserve_delivery(key, {"status": "launching", "token": "competitor"})
    before = ledger.path.read_bytes()
    entry = {"status": "launching", "token": "inert-new", "associated_keys": aliases}
    if persisted:
        document = json.loads(before)
        document["deliveries"][key]["associated_keys"] = aliases
        ledger.path.write_text(json.dumps(document))
        before = ledger.path.read_bytes()
        with pytest.raises(fly_runner.FlyLedgerError):
            ledger.reserve_delivery(REPO + "#35", {"status": "launching"})
    else:
        with pytest.raises(fly_runner.FlyLedgerError):
            ledger.reserve_delivery(REPO + "#35", entry)
    assert ledger.path.read_bytes() == before
