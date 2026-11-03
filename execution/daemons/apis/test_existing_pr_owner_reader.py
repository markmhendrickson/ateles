"""Normal current correction entrance with an inert explicit owner counterpart."""
import asyncio
import copy
import json

import pytest
import apis
import build_delivery as delivery
from lib import github_policy_control as control
from lib.daemon_runtime import artifact_resolver as artifacts
from skill_runner import SkillResult
from test_existing_pr_delivery import snapshot, admitted_reader as admitted_reader
from test_fly_delivery_dispatch import _StubNotifier

REPO = 'owner/repo'


@pytest.fixture(autouse=True)
def private_ledger(monkeypatch, tmp_path):
    path = (tmp_path / 'owner-reader-ledger.json').resolve()
    monkeypatch.setenv('APIS_FLY_LEDGER_FILE', str(path))
    assert delivery._registry_path().resolve() == path and not path.exists()


@pytest.fixture
def normal(monkeypatch, admitted_reader):
    row = snapshot(status='executing', issue_number=34, attempt_count=3)
    current = {'snapshot': copy.deepcopy(row), 'version': 'inert-v3'}
    monkeypatch.setenv('APIS_OPERATOR_LOGIN', 'existing-owner')
    monkeypatch.setattr(apis, 'fetch_task_snapshot_and_version', lambda _: (copy.deepcopy(current['snapshot']), current['version']))
    rule = dict(id=42, source_type='Repository', source=REPO, target='branch', enforcement='active',
                conditions={'ref_name': {'include': ['~DEFAULT_BRANCH'], 'exclude': []}}, bypass_actors=[], rules=[{'type': 'non_fast_forward'}])
    evidence = dict(principal='existing-owner', principal_id=7, host='github.com', repository=REPO, default_branch='main', rulesets=[rule], details=[copy.deepcopy(rule)])
    calls = []
    def owner(repo, login):
        assert delivery.active_build(REPO, 12) and delivery.active_build(REPO, 34)
        assert not delivery._delivery_ledger().delivery_receipts()
        calls.append('owner')
        return evidence
    monkeypatch.setattr(control, 'read_owner_policy', owner)
    app_rule = {k: v for k, v in rule.items() if k != 'bypass_actors'}
    monkeypatch.setattr(artifacts, '_gh_rulesets_view', lambda *a: (0, json.dumps([app_rule])))
    monkeypatch.setattr(artifacts, '_gh_ruleset_view', lambda *a: (0, json.dumps(app_rule)))
    async def run(skill, prompt, **kw):
        calls.append('job')
        assert kw['fly_delivery']['pr_number'] == '12'
        assert 'existing-owner' not in prompt + json.dumps(kw['fly_delivery'])
        return SkillResult(skill, False, 0, '', '', fly_delivery_status='no_changes', remote_run_id='inert-run')
    monkeypatch.setattr(apis, 'run_skill', run)
    return row, current, evidence, calls


def spawn(row):
    return asyncio.run(apis._spawn_harness_skill('cicada', 'ent_inert_owner', row, 'approved', _StubNotifier()))


def test_normal_selected_counterpart_allows_exact_same_target(normal):
    row, _, _, calls = normal
    got = spawn(row)
    assert calls == ['owner', 'job'] and not got.ok
    assert list(delivery._delivery_ledger().delivery_receipts()) == [REPO + '#12']


@pytest.mark.parametrize('kind', ['missing_owner', 'sentinel_owner', 'wrong_repo', 'wrong_host', 'wrong_principal', 'omitted_bypass', 'null_bypass', 'nonempty_bypass', 'wrong_rule', 'task_change', 'revoked_task', 'claim_change', 'app_source_change', 'error'])
def test_nonpermitted_context_or_policy_holds_before_reservation(normal, monkeypatch, kind):
    row, current, evidence, calls = normal
    if kind == 'missing_owner':
        monkeypatch.delenv('APIS_OPERATOR_LOGIN')
    elif kind == 'sentinel_owner':
        monkeypatch.setenv('APIS_OPERATOR_LOGIN', 'operator')
    elif kind == 'wrong_repo':
        evidence['repository'] = 'foreign/repo'
    elif kind == 'wrong_host':
        evidence['host'] = 'foreign.invalid'
    elif kind == 'wrong_principal':
        evidence['principal'] = 'foreign-owner'
    elif kind == 'omitted_bypass':
        del evidence['details'][0]['bypass_actors']
    elif kind == 'null_bypass':
        evidence['details'][0]['bypass_actors'] = None
    elif kind == 'nonempty_bypass':
        evidence['details'][0]['bypass_actors'] = [{'actor_id': 99}]
    elif kind == 'wrong_rule':
        evidence['details'][0]['source'] = 'foreign/repo'
    original = control.read_owner_policy
    def changing(repo, login):
        result = original(repo, login)
        if kind == 'task_change':
            current['version'] = 'new-attempt'
        elif kind == 'revoked_task':
            current['snapshot']['status'] = 'blocked'
        elif kind == 'claim_change':
            delivery._inflight.clear()
        elif kind == 'app_source_change':
            monkeypatch.setattr(artifacts, '_gh_pr_view', lambda *a, **kw: (404, '{}', 'HTTP 404'))
        elif kind == 'error':
            raise TimeoutError('inert')
        return result
    monkeypatch.setattr(control, 'read_owner_policy', changing)
    got = spawn(row)
    assert not got.ok and got.fly_delivery_not_launched
    assert 'job' not in calls and not delivery._delivery_ledger().delivery_receipts()


@pytest.mark.parametrize('change', ['owner_context', 'attempt', 'description', 'no_version', 'held_before_read'])
def test_current_context_cannot_change_or_be_filled_by_task_proof(normal, monkeypatch, change):
    row, current, evidence, calls = normal
    row['owner_policy_approved'] = True  # undeclared task assertion has no authority
    row['owner_policy_file'] = '/model/forged-proof.json'
    current['snapshot'] = copy.deepcopy(row)
    original = control.read_owner_policy
    def changing(repo, login):
        result = original(repo, login)
        if change == 'owner_context':
            monkeypatch.setenv('APIS_OPERATOR_LOGIN', 'other-owner')
        elif change == 'attempt':
            current['snapshot']['attempt_count'] += 1
        elif change == 'description':
            current['snapshot']['description'] = 'different admitted scope'
        return result
    monkeypatch.setattr(control, 'read_owner_policy', changing)
    if change == 'no_version':
        current['version'] = ''
    elif change == 'held_before_read':
        current['snapshot']['status'] = 'blocked'
    got = spawn(row)
    assert not got.ok and got.fly_delivery_not_launched and 'job' not in calls
    assert not delivery._delivery_ledger().delivery_receipts()
    if change in {'no_version', 'held_before_read'}:
        assert not calls


def test_owner_failure_cannot_be_replaced_by_app_or_task_success(normal, monkeypatch):
    row, _, evidence, calls = normal
    row['owner_policy_approved'] = True
    monkeypatch.setattr(artifacts, 'read_back_default_protection', lambda *a: pytest.fail('no App downgrade'))
    monkeypatch.setattr(control, 'read_owner_policy', lambda *a: (_ for _ in ()).throw(TimeoutError('inert')))
    got = spawn(row)
    assert got.fly_delivery_not_launched and 'job' not in calls
    assert not delivery._delivery_ledger().delivery_receipts()


@pytest.mark.parametrize('other', ['source_task', 'source_pipeline', 'same_pr'])
def test_awaited_actual_owner_read_retains_source_and_pr_exclusion(normal, monkeypatch, other):
    import threading
    import dataclasses
    from test_fly_delivery_dispatch import dispatcher, trigger
    from test_swarm_dispatch import _empty_spec_state
    row, _, _, calls = normal
    entered, resume = threading.Event(), threading.Event()
    original = control.read_owner_policy
    def waiting(repo, login):
        entered.set()
        assert resume.wait(5), 'inert owner read was not resumed'
        return original(repo, login)
    monkeypatch.setattr(control, 'read_owner_policy', waiting)
    async def go():
        task = asyncio.create_task(apis._spawn_harness_skill('cicada', 'ent_inert_owner', row, 'approved', _StubNotifier()))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            if other == 'source_pipeline':
                with pytest.raises(delivery.BuildNotStarted):
                    await dispatcher()._open_implementation_pr(dataclasses.replace(trigger(), number=34), _empty_spec_state())
            else:
                other_row = snapshot(issue_number=35) if other == 'same_pr' else dict(
                    repository=REPO, issue_number=34, assigned_to='cicada', action_type='open_or_merge_pr')
                result = await apis._spawn_harness_skill('cicada', 'ent_inert_competitor', other_row, 'approved', _StubNotifier())
                assert not result.ok
            assert calls == [] and not delivery._delivery_ledger().delivery_receipts()
        finally:
            resume.set()
            await task
    asyncio.run(go())
    assert calls == ['owner', 'job']


def test_change_during_final_app_source_read_holds_before_reservation(normal, monkeypatch):
    row, current, _, calls = normal
    original, reads = artifacts._gh_pr_view, []
    def source(*a, **kw):
        result = original(*a, **kw)
        reads.append(True)
        if len(reads) == 2:
            current['version'] = 'revoked-during-final-source-read'
        return result
    monkeypatch.setattr(artifacts, '_gh_pr_view', source)
    result = spawn(row)
    assert result.fly_delivery_not_launched and calls == ['owner'] and len(reads) == 2
    assert not delivery._delivery_ledger().delivery_receipts()


def test_ordinary_new_delivery_never_selects_owner_counterpart(normal, monkeypatch):
    _, _, _, calls = normal
    monkeypatch.setattr(control, 'read_owner_policy', lambda *a: pytest.fail('ordinary new delivery must not read owner policy'))
    async def run(skill, *a, **kw):
        assert 'pr_number' not in kw['fly_delivery']
        calls.append('ordinary-job')
        return SkillResult(skill, False, 0, '', '', fly_delivery_status='no_changes', remote_run_id='inert-new-run')
    monkeypatch.setattr(apis, 'run_skill', run)
    result = spawn(dict(repository=REPO, assigned_to='cicada', action_type='open_or_merge_pr', issue_number=35))
    assert not result.ok and calls == ['ordinary-job']


@pytest.mark.parametrize('principal_id', [None, '7', True, 0, -1])
def test_normal_entrance_requires_verified_nonsecret_numeric_identity(normal, principal_id):
    row, _, evidence, calls = normal
    evidence['principal_id'] = principal_id
    result = spawn(row)
    assert result.fly_delivery_not_launched and calls == ['owner']
    assert not delivery._delivery_ledger().delivery_receipts()
