"""Current admitted correction context must match before any owner/job effect."""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest
import apis
import build_delivery as delivery
import fly_runner
import skill_runner
from lib import github_app_token, github_policy_control as control
from lib.daemon_runtime import artifact_resolver as artifacts
from test_existing_pr_owner_reader import normal as normal, admitted_reader as admitted_reader, spawn
from test_fly_run_job_delivery import world as world, existing_correction as existing_correction
import test_fly_run_job_delivery as flow


@pytest.fixture(autouse=True)
def private_context_ledger(monkeypatch, tmp_path):
    ledger = (tmp_path / 'context-binding-ledger.json').resolve()
    monkeypatch.setenv('APIS_FLY_LEDGER_FILE', str(ledger))
    assert delivery._registry_path().resolve() == ledger and not ledger.exists()


@pytest.fixture
def bound_context(normal, monkeypatch):
    row, current, evidence, calls = normal
    # A source-change negative cannot rely on the old fixture's source34
    # assertion accidentally denying before it exposes the initial binding bug.
    def owner(*args):
        calls.append('owner')
        return evidence
    monkeypatch.setattr(control, 'read_owner_policy', owner)
    return row, current, calls


MATERIAL_CHANGES = [
    ('body', 'admitted worker body', 'restricted worker body'),
    ('description', 'admitted description', 'restricted description'),
    ('assigned_to', 'cicada', 'another-role'),
    ('action_type', 'open_or_merge_pr', 'local_edit'),
    ('issue_number', 34, 35),
    ('attempt_count', 3, 9),
    ('notes', 'retained previous round history', 'changed attempt history'),
    ('scope', 'accepted scope', 'narrower scope'),
    ('verification', 'accepted verification', 'different verification'),
    ('extra_scope_constraint', {'preserve': ['prior work']}, {'preserve': ['different work']}),
]


def assert_held(row, calls):
    before = copy.deepcopy(row)
    result = spawn(row)
    assert result.fly_delivery_not_launched and not result.ok, (calls, result.error)
    assert calls == [], 'deny before owner lookup or job'
    assert not delivery._delivery_ledger().delivery_receipts(), 'deny before reservation'
    assert row == before, 'do not adopt current scope by rewriting the dispatched prompt/context'
    assert delivery.active_build('owner/repo', 12) is None


@pytest.mark.parametrize('field,old,new', MATERIAL_CHANGES)
@pytest.mark.parametrize('transition', ['added', 'removed', 'changed'])
def test_initial_material_context_add_remove_change_is_held(bound_context, field, old, new, transition):
    row, current, calls = bound_context
    row[field] = old
    current['snapshot'] = copy.deepcopy(row)
    if transition == 'added':
        row.pop(field)
        current['snapshot'][field] = new
    elif transition == 'removed':
        current['snapshot'].pop(field)
    else:
        current['snapshot'][field] = new
    current['version'] = 'different-current-revision'
    assert_held(row, calls)


@pytest.mark.parametrize('field,value', [
    ('body', None), ('body', ''), ('description', ''), ('assigned_to', None),
    ('action_type', 'open_or_merge_pr'), ('attempt_count', 0), ('attempt_count', None),
    ('issue_number', None), ('notes', ''), ('extra_scope_constraint', None),
])
def test_added_explicit_null_empty_or_default_still_changes_admitted_shape(bound_context, field, value):
    row, current, calls = bound_context
    row.pop(field, None)
    current['snapshot'] = copy.deepcopy(row)
    current['snapshot'][field] = value
    current['version'] = 'new-explicit-current-field'
    assert_held(row, calls)


@pytest.mark.parametrize('field', ['body', 'attempt_count', 'notes', 'extra_scope_constraint'])
def test_removing_explicit_null_is_not_missing_value_equivalence(bound_context, field):
    row, current, calls = bound_context
    row[field] = None
    current['snapshot'] = copy.deepcopy(row)
    current['snapshot'].pop(field)
    assert_held(row, calls)


@pytest.mark.parametrize('field,old,new', [
    ('attempt_count', 1, True), ('attempt_count', 0, False),
    ('extra_scope_constraint', {'count': 1}, {'count': True}),
])
def test_json_types_are_part_of_the_admitted_context(bound_context, field, old, new):
    row, current, calls = bound_context
    row[field] = old
    current['snapshot'] = copy.deepcopy(row)
    current['snapshot'][field] = new
    assert_held(row, calls)


def test_added_source_beside_held_writer_does_not_adopt_new_claim(bound_context):
    row, current, calls = bound_context
    row.pop('issue_number')
    current['snapshot'] = copy.deepcopy(row)
    current['snapshot']['issue_number'] = 35
    with delivery.claim_build('owner/repo', 35, 'existing-source-writer') as held:
        assert_held(row, calls)
        assert delivery.active_build('owner/repo', 35).run_id == held.run_id
    assert delivery.active_build('owner/repo', 35) is None


@pytest.mark.parametrize('shape', ['unchanged', 'implicit_action', 'optional_absent', 'explicit_null', 'reordered', 'lifecycle'])
def test_unchanged_context_and_established_lifecycle_remain_permitted(bound_context, shape):
    row, current, calls = bound_context
    if shape == 'implicit_action':
        row.pop('action_type')
    elif shape == 'optional_absent':
        row.pop('attempt_count')
    elif shape == 'explicit_null':
        row['notes'] = None
    elif shape == 'reordered':
        row['extra_scope_constraint'] = {'preserve': True, 'allowed': ['scoped']}
    current['snapshot'] = copy.deepcopy(row)
    if shape == 'reordered':
        current['snapshot'] = dict(reversed(list(current['snapshot'].items())))
        current['snapshot']['extra_scope_constraint'] = {'allowed': ['scoped'], 'preserve': True}
    elif shape == 'lifecycle':
        row['status'] = 'pending'
        row['updated_at'] = 'before-dispatch'
        current['snapshot'].update(status='executing', updated_at='now', updated_date='now', result='lifecycle result')
    before = copy.deepcopy(row)
    result = spawn(row)
    assert calls == ['owner', 'job'] and not result.fly_delivery_not_launched
    assert row == before
    assert list(delivery._delivery_ledger().delivery_receipts()) == ['owner/repo#12']


@pytest.mark.parametrize('login', [None, '', ' ', '\t'])
def test_missing_owner_setting_keeps_actionable_nonsecret_diagnostic(normal, monkeypatch, login):
    row, _, _, calls = normal
    if login is None:
        monkeypatch.delenv('APIS_OPERATOR_LOGIN')
    else:
        monkeypatch.setenv('APIS_OPERATOR_LOGIN', login)
    result = spawn(row)
    assert result.fly_delivery_not_launched and calls == []
    assert not delivery._delivery_ledger().delivery_receipts()
    assert 'APIS_OPERATOR_LOGIN' in result.error and 'configuration' in result.error
    assert 'gates remain held' in result.error


def test_unknown_provider_failure_stays_fixed_not_raw(bound_context, monkeypatch):
    row, _, calls = bound_context
    private_error = 'inert sensitive provider headers must remain private'
    def failed(*args):
        raise RuntimeError(private_error)
    monkeypatch.setattr(control, 'read_owner_policy', failed)
    result = spawn(row)
    assert result.fly_delivery_not_launched and calls == []
    assert private_error not in result.error and 'APIS_OPERATOR_LOGIN' not in result.error
    assert not delivery._delivery_ledger().delivery_receipts()


def test_normal_current_task_owner_runner_receipt_readback_wholeflow(normal, world, existing_correction, monkeypatch):
    row, current, _, calls = normal
    spec, api_calls, _ = existing_correction
    row['head_sha'] = spec['head_sha']
    current['snapshot'] = copy.deepcopy(row)
    branch_ref = 'refs/heads/' + spec['branch']
    def gh_pr(*args, **kwargs):
        return 0, json.dumps(dict(number=12, baseRefName='main', headRefName=spec['branch'],
            headRefOid=flow._remote_ref(world, branch_ref), headRepository={'nameWithOwner': 'owner/repo'},
            state='OPEN', isCrossRepository=False)), ''
    monkeypatch.setattr(artifacts, '_gh_pr_view', gh_pr)
    monkeypatch.setattr(artifacts, '_gh_branch_view', lambda *a: (0, json.dumps(dict(ref=branch_ref,
        object=dict(type='commit', sha=flow._remote_ref(world, branch_ref))))))
    monkeypatch.setattr(github_app_token, 'identity_token', lambda *a: 'inert-read-token')
    model_seen, records, prompts = {}, [], []
    async def launch(skill, prompt, **kwargs):
        calls.append('job')
        prompts.append(prompt)
        assert kwargs['fly_delivery']['head_sha'] == spec['head_sha']
        code, record = flow._run(flow._job(world['tmp'], delivery=kwargs['fly_delivery']), flow._agent(world, seen=model_seen))
        records.append(record)
        assert code == 0
        job = SimpleNamespace(delivery=kwargs['fly_delivery'], delivery_result=fly_runner.delivery_result_from_record(record))
        return skill_runner.SkillResult(skill, True, 0, '', '', remote_run_id='inert-run',
            **skill_runner._fly_delivery_fields(job, unknown=False))
    monkeypatch.setattr(apis, 'run_skill', launch)
    before = copy.deepcopy(row)
    result = spawn(row)
    assert result.ok and result.fly_delivery_readback == 'passed'
    assert calls == ['owner', 'job'] and row == before and row['description'] in prompts[0]
    receipts = delivery._delivery_ledger().delivery_receipts()
    assert list(receipts) == ['owner/repo#12']
    assert receipts['owner/repo#12']['associated_keys'] == ['owner/repo#34']
    assert receipts['owner/repo#12']['existing_pr']['head_sha'] == spec['head_sha']
    assert flow._remote_ref(world, branch_ref) == records[0]['delivery_commit'] != spec['head_sha']
    assert flow.git(world['remote'], 'show', spec['branch'] + ':prior-wip.txt', home=world['home']) == 'preserved original work'
    assert api_calls == [('GET', '/repos/owner/repo'), ('GET', '/repos/owner/repo/pulls/12')] * 3
    assert flow.TOKEN not in json.dumps(model_seen['env'])
    assert delivery.active_build('owner/repo', 12) is None and delivery.active_build('owner/repo', 34) is None
