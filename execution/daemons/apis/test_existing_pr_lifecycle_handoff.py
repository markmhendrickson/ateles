"""Joined approved-checkpoint lifecycle and strict existing-PR owner binding."""
from __future__ import annotations

import asyncio
import copy
import json
from types import SimpleNamespace

import httpx
import pytest

import apis
import build_delivery as delivery
from lib.daemon_runtime import task_lifecycle as lifecycle
from lib.daemon_runtime.gating import (
    ExecutionPolicy, build_checkpoint_authorization_envelope, evaluate_gate,
)
from test_existing_pr_owner_reader import normal as normal, admitted_reader as admitted_reader
from test_fly_run_job_delivery import world as world, existing_correction as existing_correction
from test_fly_delivery_dispatch import _StubNotifier


@pytest.fixture
def joined(normal, monkeypatch):
    row, _, evidence, calls = normal
    row.update(status='awaiting_approval', blocked_reason='prior synthetic review hold',
               notes='complete original review and attempt history', title='Scoped correction')
    original = copy.deepcopy(row)
    task_id, checkpoint_id = 'ent_joined_task', 'ent_joined_checkpoint'
    owner = '11111111-1111-4111-8111-111111111111'
    record = dict(entity_id=task_id, entity_type='task', user_id=owner,
                  observation_count=1, last_observation_at='2026-01-01T00:00:00Z',
                  entity_version='task-v1', snapshot=copy.deepcopy(row))
    policy = ExecutionPolicy(entity_id='inert-policy', loaded=True,
        low_blast_action_types=frozenset(), high_blast_action_types=frozenset({'open_or_merge_pr'}))
    decision = evaluate_gate(confidence=0.3, action_type='open_or_merge_pr', policy=policy)
    authority = build_checkpoint_authorization_envelope(
        task_record=record, policy=policy, decision=decision,
        action_type='open_or_merge_pr', user_id=owner, required_approver_jkt='A' * 43)
    checkpoint = dict(entity_id=checkpoint_id, entity_type='checkpoint_brief', user_id=owner,
        entity_version='checkpoint-v1', snapshot=dict(status='approved', resolved_dispatched=False,
            task_entity_id=task_id, body=authority, title='Approve scoped correction',
            gate_action=decision.action.value, blast_radius=decision.blast_radius.value))
    writes, seen = [], {}
    state = dict(record=record, checkpoint=checkpoint, original=original, row=row,
                 writes=writes, seen=seen, calls=calls, task_id=task_id,
                 checkpoint_id=checkpoint_id, before_write=None, after_write=None,
                 before_reader=None, reply=None, unreadable_status=None)

    def fetch_record(entity_id):
        assert entity_id == task_id
        return copy.deepcopy(record)

    def fetch_version(entity_id):
        current = fetch_record(entity_id)
        if (current['entity_type'] != 'task'
                or current['snapshot']['status'] == state['unreadable_status']):
            return None, None
        return current['snapshot'], current['entity_version']

    def post(url, *, json: dict, **kwargs):
        assert url == 'https://inert.invalid/correct'
        assert json['entity_id'] == task_id and json['entity_type'] == 'task'
        if state['before_write']:
            state['before_write'](json)
        expected = json.get('expected_version')
        if expected and expected != record['entity_version']:
            return httpx.Response(409, json={'error_code': 'ERR_FIELD_VERSION_CONFLICT'},
                                  request=httpx.Request('POST', url))
        record['snapshot'][json['field']] = copy.deepcopy(json['value'])
        record['observation_count'] += 1
        record['entity_version'] = f"task-v{record['observation_count']}"
        committed_version = record['entity_version']
        writes.append(copy.deepcopy(json))
        if state['after_write']:
            state['after_write'](json)
        if state['reply']:
            response = state['reply'](json, committed_version)
            if response is not None:
                return response
        return httpx.Response(200, json={'entity_version': committed_version},
                              request=httpx.Request('POST', url))

    def stamp(entity_id, *, expected_version, **kwargs):
        assert entity_id == checkpoint_id
        if expected_version != checkpoint['entity_version']:
            return False
        checkpoint['snapshot']['resolved_dispatched'] = True
        checkpoint['entity_version'] = 'checkpoint-v2'
        return True

    real_reader = apis._correction_policy_reader
    def reader(entity_id, snapshot, *args, **kwargs):
        if state['before_reader']:
            state['before_reader']()
        seen['receiving_snapshot'] = copy.deepcopy(snapshot)
        seen['authoritative_snapshot'], seen['version'] = fetch_version(entity_id)
        read, current = real_reader(entity_id, snapshot, *args, **kwargs)
        async def observed_read():
            reason = await read()
            seen['reader_reason'] = reason
            return reason
        return observed_read, current

    def rebind():
        checkpoint['snapshot']['body'] = build_checkpoint_authorization_envelope(
            task_record=record, policy=policy, decision=decision,
            action_type='open_or_merge_pr', user_id=owner, required_approver_jkt='A' * 43)
        state['original'] = copy.deepcopy(record['snapshot'])
        row.clear()
        row.update(copy.deepcopy(state['original']))

    state['rebind'] = rebind

    monkeypatch.setattr(apis, 'fetch_task_record', fetch_record)
    monkeypatch.setattr(apis, 'fetch_task_snapshot', lambda key: fetch_record(key)['snapshot'])
    monkeypatch.setattr(apis, 'fetch_task_snapshot_and_version', fetch_version)
    monkeypatch.setattr(apis, 'fetch_checkpoint_record', lambda key: copy.deepcopy(checkpoint))
    monkeypatch.setattr(apis, 'read_verified_entity_owner', lambda *a, **kw: owner)
    monkeypatch.setattr(apis, 'read_authenticated_checkpoint_authorization',
                        lambda *a: json.loads(checkpoint['snapshot']['body']))
    monkeypatch.setattr(apis, 'read_authenticated_checkpoint_resolution',
                        lambda *a, **kw: {'principal_sub': 'ateles@ateles-swarm'})
    monkeypatch.setattr(apis, 'resolve_policy_for_agent', lambda *a: policy)
    monkeypatch.setattr(apis, 'stamp_checkpoint_dispatched', stamp)
    monkeypatch.setattr(apis, 'set_task_status', lifecycle.set_task_status)
    monkeypatch.setattr(lifecycle, 'NEOTOMA_BASE_URL', 'https://inert.invalid')
    monkeypatch.setattr(lifecycle, 'NEOTOMA_BEARER_TOKEN', 'inert-fixture-token')
    monkeypatch.setattr(lifecycle.httpx, 'post', post)
    monkeypatch.setattr(apis, '_correction_policy_reader', reader)
    monkeypatch.setattr(apis, 'DRY_RUN', False)
    monkeypatch.setattr(apis, 'RUN_CONVERSATIONS', False)
    monkeypatch.setattr(apis, 'RUN_EMAIL', False)
    monkeypatch.setattr(apis._activity, 'started', lambda *a: SimpleNamespace(
        finished=lambda *a: None, failed=lambda *a: None, escalated=lambda *a: None))
    return state


def release(state):
    return asyncio.run(apis.handle_checkpoint_brief(
        state['checkpoint_id'], copy.deepcopy(state['checkpoint']['snapshot']), _StubNotifier()))


def test_normal_checkpoint_reason_clear_reaches_bound_reader_and_one_receipt(joined):
    release(joined)
    seen = joined['seen']
    assert seen['authoritative_snapshot']['status'] == 'executing'
    assert seen['authoritative_snapshot']['blocked_reason'] == ''
    if seen.get('reader_reason'):
        assert joined['calls'] == []
        assert not delivery._delivery_ledger().delivery_receipts()
        assert joined['row'] == joined['original']
        print('CAUSAL_DENIAL: immutable prior reason; persisted executing/empty; '
              'zero owner/job/receipt; exact initial context mismatch')
    # The old path actually receives the immutable pre-clear hold, explaining
    # its initial strict-shape denial. The candidate must hand off only the
    # controller-qualified lifecycle projection, not ignore arbitrary reason.
    assert seen['receiving_snapshot']['blocked_reason'] == '', seen.get('reader_reason')
    assert joined['row'] == joined['original']
    assert joined['calls'] == ['owner', 'job']
    receipts = delivery._delivery_ledger().delivery_receipts()
    assert list(receipts) == ['owner/repo#12']
    assert receipts['owner/repo#12']['associated_keys'] == ['owner/repo#34']
    assert release(joined) is False
    assert joined['calls'] == ['owner', 'job']


def mutate(state, field, value):
    if value is _ABSENT:
        state['record']['snapshot'].pop(field, None)
    else:
        state['record']['snapshot'][field] = copy.deepcopy(value)
    state['record']['observation_count'] += 1
    state['record']['entity_version'] = f"task-v{state['record']['observation_count']}"


_ABSENT = object()
CHANGES = [
    ('blocked_reason', 'a new independent hold'), ('blocked_reason', _ABSENT),
    ('blocked_reason', None), ('blocked_reason', False), ('blocked_reason', []),
    ('notes', 'changed complete review history'), ('description', 'changed scope'),
    ('assigned_to', 'another-role'), ('attempt_count', 4), ('issue_number', 35),
    ('head_sha', 'b' * 40), ('pr_number', 13), ('context', '{}'),
    ('unknown_scope', {'accepted': False}), ('unknown_scope', {'accepted': 0}),
    ('result', 'new result'), ('updated_at', 'new metadata'), ('status', 'blocked'),
]


def assert_no_delivery(state):
    assert 'job' not in state['calls']
    assert not delivery._delivery_ledger().delivery_receipts()


@pytest.mark.parametrize('field,value', CHANGES)
@pytest.mark.parametrize('stage', ['before_routed', 'after_routed', 'before_clear', 'after_clear', 'after_executing'])
def test_independent_change_cannot_be_erased_or_adopted(joined, field, value, stage):
    changed = []
    def inject(write):
        matches = {
            'before_routed': write['field'] == 'status' and write['value'] == 'routed',
            'after_routed': write['field'] == 'status' and write['value'] == 'routed',
            'before_clear': write['field'] == 'blocked_reason',
            'after_clear': write['field'] == 'blocked_reason',
            'after_executing': write['field'] == 'status' and write['value'] == 'executing',
        }
        if not changed and matches[stage]:
            changed.append(True)
            mutate(joined, field, value)
    joined['before_write' if stage in {'before_routed', 'before_clear'} else 'after_write'] = inject
    assert release(joined) is False
    assert changed and joined['calls'] == []
    assert_no_delivery(joined)
    actual = joined['record']['snapshot']
    assert (field not in actual) if value is _ABSENT else actual[field] == value
    assert joined['row'] == joined['original']


@pytest.mark.parametrize('field,value', CHANGES)
def test_complete_lifecycle_binding_is_strict_before_first_owner_read(joined, field, value):
    joined['before_reader'] = lambda: mutate(joined, field, value)
    assert release(joined) is False
    assert joined['calls'] == []
    assert_no_delivery(joined)


@pytest.mark.parametrize('field,value', CHANGES)
def test_owner_await_rechecks_full_current_shape_and_version(joined, monkeypatch, field, value):
    from lib import github_policy_control as control
    original = control.read_owner_policy
    def owner(*args):
        answer = original(*args)
        mutate(joined, field, value)
        return answer
    monkeypatch.setattr(control, 'read_owner_policy', owner)
    assert release(joined) is False
    assert joined['calls'] == ['owner']
    assert_no_delivery(joined)


@pytest.mark.parametrize('stage', ['before_routed', 'before_clear', 'after_executing', 'before_reader'])
def test_same_shape_new_version_is_not_adopted(joined, stage):
    def change():
        mutate(joined, 'notes', joined['record']['snapshot']['notes'])
    if stage == 'before_reader':
        joined['before_reader'] = change
    else:
        fired = []
        def hook(write):
            match = ((stage == 'before_routed' and write['field'] == 'status' and write['value'] == 'routed')
                     or (stage == 'before_clear' and write['field'] == 'blocked_reason')
                     or (stage == 'after_executing' and write['field'] == 'status' and write['value'] == 'executing'))
            if match and not fired:
                fired.append(True)
                change()
        joined['after_write' if stage == 'after_executing' else 'before_write'] = hook
    assert release(joined) is False
    assert joined['calls'] == []
    assert_no_delivery(joined)


@pytest.mark.parametrize('reason', [_ABSENT, None, False, [], 0])
def test_missing_or_nonstring_admitted_reason_is_not_a_clearance(joined, reason):
    mutate(joined, 'blocked_reason', reason)
    joined['rebind']()
    assert release(joined) is False
    assert joined['writes'] == [] and joined['calls'] == []
    assert_no_delivery(joined)


@pytest.mark.parametrize('version', [None, '', False, 1])
def test_no_admitted_version_never_starts_lifecycle_writes(joined, version):
    joined['record']['entity_version'] = version
    assert release(joined) is False
    assert joined['writes'] == [] and joined['calls'] == []
    assert_no_delivery(joined)


@pytest.mark.parametrize('phase', ['routed', 'clear', 'executing'])
@pytest.mark.parametrize('failure', ['missing_token', 'lost_reply', 'did_not_persist'])
def test_partial_or_ambiguous_lifecycle_write_stops_sequence(joined, phase, failure):
    fired = []
    def reply(write, version):
        matches = ((phase == 'clear' and write['field'] == 'blocked_reason')
                   or (write['field'] == 'status' and write['value'] == phase))
        if not fired and matches:
            fired.append(True)
            if failure == 'lost_reply':
                raise httpx.ReadTimeout('inert lost reply')
            if failure == 'did_not_persist':
                value = ('prior synthetic review hold' if phase == 'clear'
                         else 'awaiting_approval' if phase == 'routed' else 'routed')
                joined['record']['snapshot'][write['field']] = value
            return httpx.Response(200, json={} if failure == 'missing_token' else {'entity_version': version},
                                  request=httpx.Request('POST', 'https://inert.invalid/correct'))
    joined['reply'] = reply
    assert release(joined) is False
    assert fired and joined['calls'] == []
    assert_no_delivery(joined)
    assert len(joined['writes']) <= {'routed': 1, 'clear': 2, 'executing': 3}[phase]


@pytest.mark.parametrize('status', ['routed', 'executing'])
def test_unreadable_lifecycle_readback_holds(joined, status):
    joined['unreadable_status'] = status
    assert release(joined) is False
    assert joined['calls'] == []
    assert_no_delivery(joined)


def test_task_proof_and_trigger_cannot_forge_normal_release_context(joined):
    row = copy.deepcopy(joined['original'])
    row['controller_reason_cleared'] = True
    row['_approved_correction_lifecycle'] = {'version': 'task-v1', 'approved': True}
    asyncio.run(apis.dispatch_task(joined['task_id'], row, 'approved', _StubNotifier(), gate_override=True))
    assert joined['writes'] == [] and joined['calls'] == []
    assert_no_delivery(joined)
