"""Inert same-PR delivery: admitted source, single receipt, no replacement PR."""
from __future__ import annotations

import asyncio
import json

import pytest

import apis
import build_delivery as delivery
from skill_runner import SkillResult
from test_fly_delivery_dispatch import _StubNotifier
from test_build_delivery import dispatch_env as dispatch_env
from lib.daemon_runtime import artifact_resolver

REPO = "owner/repo"
BRANCH = "codex/scoped-correction"
HEAD = "a" * 40



@pytest.fixture(autouse=True)
def visible_default_protection(monkeypatch):
    # Historical adapter components use an inert control counterpart. The real
    # current-task/owner binding is exercised through its normal entrance in
    # test_existing_pr_owner_reader, not by this component fixture.
    async def qualified():
        return None
    monkeypatch.setattr(apis, "_correction_policy_reader", lambda *a: (qualified, qualified))
    rule = {"id": 42, "source_type": "Repository", "source": REPO,
            "target": "branch", "enforcement": "active", "bypass_actors": [],
            "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
            "rules": [{"type": "non_fast_forward"}]}
    monkeypatch.setattr(artifact_resolver, "_gh_rulesets_view", lambda *a: (0, json.dumps([rule])))
    monkeypatch.setattr(artifact_resolver, "_gh_ruleset_view", lambda *a: (0, json.dumps(rule)))

def snapshot(**over):
    row = dict(repository=REPO, assigned_to="cicada", action_type="open_or_merge_pr",
               task_type="existing_pr_correction", pr_number=12, head_sha=HEAD,
               context=json.dumps({"branch": BRANCH, "base_branch": "main", "legacy_summary": "preserve scope"}),
               description="Correct the existing PR; retain the original review history.")
    row.update(over)
    return row


def spawn(row):
    return asyncio.run(apis._spawn_harness_skill(
        "cicada", "ent_synthetic_correction", row, "approved", _StubNotifier()))


def test_admitted_target_reaches_existing_fly_adapter_without_new_branch(monkeypatch):
    monkeypatch.setenv("APIS_FLY_RUNNER_CLASSES", "open_or_merge_pr")
    monkeypatch.setattr(apis, "_token_for_agent_on_repo", lambda *a: "inert-reader-token")
    monkeypatch.setattr(artifact_resolver, "_gh_repository_view", lambda *a: (0, json.dumps({"full_name": REPO, "default_branch": "main"})))
    monkeypatch.setattr(artifact_resolver, "_gh_branch_view", lambda *a: (0, json.dumps({"ref": f"refs/heads/{BRANCH}", "object": {"type": "commit", "sha": HEAD}})))
    monkeypatch.setattr(artifact_resolver, "_gh_pr_view", lambda *a, **kw: (0, json.dumps({
        "number": 12, "baseRefName": "main", "headRefName": BRANCH, "headRefOid": HEAD,
        "headRepository": {"nameWithOwner": REPO}, "state": "OPEN", "isCrossRepository": False}), ""))
    seen = []

    async def run(skill, prompt, **kw):
        seen.append((prompt, kw))
        return SkillResult(skill, False, 0, "", "", fly_delivery_status="no_changes",
                           remote_run_id="inert-run")

    monkeypatch.setattr(apis, "run_skill", run)
    got = spawn(snapshot())
    assert len(seen) == 1
    prompt, kw = seen[0]
    assert kw["fly_delivery"]["branch"] == BRANCH
    assert kw["fly_delivery"]["pr_number"] == "12"
    assert kw["fly_delivery"]["head_sha"] == HEAD
    assert "existing pull request #12" in prompt
    assert not got.ok


@pytest.fixture
def admitted_reader(monkeypatch):
    answer = {"number": 12, "baseRefName": "main", "headRefName": BRANCH,
              "headRefOid": HEAD, "headRepository": {"nameWithOwner": REPO},
              "state": "OPEN", "isCrossRepository": False}
    monkeypatch.setenv("APIS_FLY_RUNNER_CLASSES", "open_or_merge_pr")
    monkeypatch.setattr(apis, "_token_for_agent_on_repo", lambda *a: "inert-reader-token")
    monkeypatch.setattr(artifact_resolver, "_gh_repository_view", lambda *a: (0, json.dumps({"full_name": REPO, "default_branch": "main"})))
    monkeypatch.setattr(artifact_resolver, "_gh_branch_view", lambda *a: (0, json.dumps({"ref": f"refs/heads/{BRANCH}", "object": {"type": "commit", "sha": answer["headRefOid"]}})))
    monkeypatch.setattr(artifact_resolver, "_gh_pr_view",
                        lambda *a, **kw: (0, json.dumps(answer), ""))
    return answer


@pytest.mark.parametrize("over", [
    {"task_type": None}, {"repository": None}, {"pr_number": None}, {"pr_number": True},
    {"head_sha": None}, {"head_sha": "abc"}, {"context": None},
    {"context": '{"branch":"codex/main","base_branch":"main"}'},
    {"context": '{"branch":"main","base_branch":"main"}'},
    {"context": '{"branch":"codex/release/v1","base_branch":"main"}'},
    {"context": '{"branch":"codex/a/../b","base_branch":"main"}'},
    {"action_type": "local_edit"},
])
def test_partial_or_nonpermitted_target_never_starts_any_child(monkeypatch, over):
    async def never(*a, **kw):
        raise AssertionError("a malformed target reached a runner")
    monkeypatch.setattr(apis, "run_skill", never)
    assert not spawn(snapshot(**over)).ok


@pytest.mark.parametrize("change", [
    {"headRefOid": "b" * 40}, {"headRefName": "codex/other"}, {"number": 13},
    {"headRepository": {"nameWithOwner": "foreign/repo"}}, {"baseRefName": "other"},
    {"state": "CLOSED"}, {"isCrossRepository": True},
])
def test_stale_or_wrong_live_source_refuses_before_receipt_or_child(monkeypatch, admitted_reader, change):
    admitted_reader.update(change)
    async def never(*a, **kw):
        raise AssertionError("wrong source started a child")
    monkeypatch.setattr(apis, "run_skill", never)
    got = spawn(snapshot())
    assert not got.ok
    assert delivery._delivery_ledger().delivery_receipts() == {}


def test_existing_target_opt_out_does_not_substitute_local(monkeypatch):
    async def never(*a, **kw):
        raise AssertionError("opt-out launched a local substitute")
    monkeypatch.setattr(apis, "run_skill", never)
    assert not spawn(snapshot()).ok


def _noop_run(calls, **extra):
    async def run(skill, *a, **kw):
        calls.append(kw)
        return SkillResult(skill, False, 0, "", "", fly_delivery_status="no_changes",
                           remote_run_id="same-run", **extra)
    return run


def test_noop_correction_makes_way_and_preserves_exhausted_history(monkeypatch, admitted_reader):
    # Red before this fix (observed in CI as `assert 2 == 1` under the old
    # name, inverted here): a settled no_changes receipt with no recorded
    # commit at the same head held the PR, so the second spawn never ran.
    calls = []
    row = snapshot(attempt_count=7, notes="auto-fix:1;auto-fix:2;auto-fix-noop;auto-fix-exhausted")
    original = dict(row)
    monkeypatch.setattr(apis, "run_skill", _noop_run(calls))
    assert not spawn(row).ok
    assert not spawn(row).ok
    assert len(calls) == 2
    assert row == original
    receipt = delivery._delivery_ledger().delivery_receipts()[REPO + "#12"]
    assert receipt["status"] == "no_changes" and receipt["existing_pr"]["head_sha"] == HEAD
    assert apis._fly_receipt_key(row, "ent_synthetic_correction") == REPO + "#12"


def test_noop_correction_with_recorded_commit_keeps_receipt(monkeypatch, admitted_reader):
    # Inferred, not observed: this guards the recorded-no-commit check in
    # _undelivered_correction_settled. Were that check dropped, the second
    # spawn would reach the runner and this would fail with `assert 2 == 1`.
    calls = []
    monkeypatch.setattr(apis, "run_skill", _noop_run(calls, fly_delivery_commit="b" * 40))
    assert not spawn(snapshot()).ok
    assert delivery._delivery_ledger().delivery_receipts()[REPO + "#12"]["commit"] == "b" * 40
    assert not spawn(snapshot()).ok
    assert len(calls) == 1


def test_noop_correction_at_other_head_keeps_receipt(monkeypatch, admitted_reader):
    # Inferred, not observed: this guards the same-admitted-head check in
    # _undelivered_correction_settled. Were that check dropped, a correction
    # declared at another head would reach the runner (`assert 2 == 1`).
    calls = []
    monkeypatch.setattr(apis, "run_skill", _noop_run(calls))
    assert not spawn(snapshot()).ok
    other = "c" * 40
    admitted_reader["headRefOid"] = other  # admission passes; only the receipt refuses
    assert not spawn(snapshot(head_sha=other)).ok
    assert len(calls) == 1


def test_existing_target_prelaunch_ineligibility_does_not_fall_back(monkeypatch, admitted_reader):
    seen = []
    async def run(skill, *a, **kw):
        seen.append(kw)
        return SkillResult(skill, False, None, "", "", fly_ineligible_before_launch=True)
    monkeypatch.setattr(apis, "run_skill", run)
    assert not spawn(snapshot()).ok
    assert len(seen) == 1 and seen[0]["runner"] == "fly"


def test_active_original_source_or_pr_claim_prevents_second_writer(monkeypatch, admitted_reader):
    async def never(*a, **kw):
        raise AssertionError("a competing writer was launched")
    monkeypatch.setattr(apis, "run_skill", never)
    for number in (12, 34):
        with delivery.claim_build(REPO, number, "original-writer"):
            assert not spawn(snapshot(issue_number=34)).ok
            assert delivery.active_build(REPO, number).holder == "original-writer"


def test_custom_default_branch_is_refused_before_reservation(monkeypatch, admitted_reader):
    monkeypatch.setattr(artifact_resolver, "_gh_repository_view", lambda *a: (0, json.dumps({
        "full_name": REPO, "default_branch": BRANCH})))
    async def never(*a, **kw):
        raise AssertionError("repository default branch reached the child")
    monkeypatch.setattr(apis, "run_skill", never)
    assert not spawn(snapshot()).ok
    assert delivery._delivery_ledger().delivery_receipts() == {}


@pytest.mark.parametrize("over", [{}, {"fly_pr_number": 13}, {"fly_delivery_branch": "codex/other"},
                                  {"fly_delivery_status": "opened"}, {"fly_delivery_commit": HEAD}])
def test_correction_result_must_read_back_the_same_pr_and_a_new_head(monkeypatch, admitted_reader, over):
    from lib import github_app_token
    monkeypatch.setattr(github_app_token, "identity_token", lambda *a: "inert-opener-token")
    async def run(skill, *a, **kw):
        data = dict(fly_delivery_status="updated", remote_run_id="inert-run",
                    fly_pr_number=12, fly_pr_url=f"https://github.com/{REPO}/pull/12",
                    fly_delivery_branch=BRANCH, fly_delivery_commit="b" * 40)
        data.update(over)
        admitted_reader["headRefOid"] = data["fly_delivery_commit"]
        return SkillResult(skill, True, 0, "", "", **data)
    monkeypatch.setattr(apis, "run_skill", run)
    got = spawn(snapshot())
    assert got.ok is (not over)
    assert got.fly_delivery_readback == ("passed" if not over else "failed")


def test_single_provider_correction_preserves_normal_selection_without_failover(monkeypatch):
    import skill_runner
    monkeypatch.setattr(skill_runner, "provider_candidates", lambda *a, **kw: ["codex", "cursor"])
    monkeypatch.setattr(skill_runner, "_refresh_usage_snapshot", lambda *a: None)
    seen = []
    async def attempt(provider):
        seen.append(provider)
        return SkillResult("cicada", False, None, "", "", configuration_repair="inert config refusal")
    asyncio.run(skill_runner._run_provider_attempts(
        "cicada", attempt, binaries={"codex": "fake", "cursor": "fake"}, single_provider=True))
    assert seen == ["codex"]


@pytest.mark.parametrize("response", [(404, "{}"), (0, '{"ref":"refs/heads/codex/scoped-correction","object":{"type":"commit","sha":"wrong"}}')])
def test_missing_or_changed_branch_ref_refuses_before_job(monkeypatch, admitted_reader, response):
    monkeypatch.setattr(artifact_resolver, "_gh_branch_view", lambda *a: response)
    async def never(*a, **kw):
        raise AssertionError("a missing or changed remote ref started a child")
    monkeypatch.setattr(apis, "run_skill", never)
    assert not spawn(snapshot()).ok
    assert delivery._delivery_ledger().delivery_receipts() == {}


def test_deferral_marker_does_not_replace_admitted_target_placement(monkeypatch, admitted_reader):
    seen = []
    async def run(skill, *a, **kw):
        seen.append(kw["fly_delivery"])
        return SkillResult(skill, False, 0, "", "", fly_delivery_status="no_changes", remote_run_id="inert-run")
    monkeypatch.setattr(apis, "run_skill", run)
    row = snapshot(dispatch_context="deferred-behind:owner/repo#34:0123456789abcdef")
    original = dict(row)
    assert not spawn(row).ok  # noop remains held
    assert seen[0]["branch"] == BRANCH and seen[0]["head_sha"] == HEAD
    assert row == original



def test_existing_pr_target_survives_real_deferral_and_fresh_recovery(dispatch_env, monkeypatch):
    """Use the normal versioned marker writer and fresh recovery consumer.

    Stop at the dispatch boundary: recovery does not clear the correction's
    checkpoint, bypass admission, or start a real child.
    """
    _, _, store = dispatch_env
    eid = "ent_synthetic_existing_correction"
    original = snapshot(status="pending", issue_number=34, attempt_count=7,
                        notes="round1;round2;noop;exhausted;current checkpoint remains held")
    store.put(eid, original)
    notifier = _StubNotifier()
    recovered = []

    async def boundary(entity_id, fresh, **kw):
        recovered.append((entity_id, dict(fresh), kw))

    monkeypatch.setattr(apis, "dispatch_task", boundary)

    async def go():
        with delivery.claim_build(REPO, 34, "original-writer") as claim:
            outcome = await apis._record_deferral(eid, original, claim, notifier)
            assert outcome.sweep_owns_it
            stored = dict(store.rows[eid])
            marker = stored["dispatch_context"]
            assert marker != stored["context"] and stored["context"] == original["context"]
            assert delivery.parse_deferral_context(marker) == (REPO.lower(), 34, claim.run_id)
        await apis._redispatch_deferred(eid, marker, notifier)

    asyncio.run(go())
    assert len(recovered) == 1
    got_id, fresh, kwargs = recovered[0]
    assert got_id == eid and kwargs["trigger"] == "build_released"
    for field in ("context", "pr_number", "head_sha", "attempt_count", "notes", "description"):
        assert fresh[field] == original[field]
    target = delivery.existing_pr_target(fresh, REPO)
    assert target.branch == BRANCH and target.number == 12
    assert store.rows[eid]["notes"] == original["notes"]
