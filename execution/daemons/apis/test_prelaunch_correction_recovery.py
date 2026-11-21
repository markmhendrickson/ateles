"""Same-task recovery of an existing-PR correction Fly refused before launch.

Natural path: an approved correction is released by the ordinary checkpoint
consumer, Fly refuses it before anything launches, and the postdispatch branch
holds the task BLOCKED. The producer (`reopen_prelaunch_refused_correction`)
re-holds the SAME task by one version-conditional write and files a fresh
checkpoint through the existing reissue path; only the ordinary consumer then
releases it. Every other shape refuses with zero writes. Synthetic only: no
network, key, signature, token or real task.
"""
from __future__ import annotations

import asyncio
import copy
import json

import httpx
import pytest

import apis
import build_delivery as delivery
from lib.daemon_runtime import gating as gating_module
from lib.daemon_runtime.gating import build_checkpoint_authorization_envelope, evaluate_gate
from skill_runner import SkillResult
from test_existing_pr_lifecycle_handoff import joined as joined, release
from test_existing_pr_owner_reader import normal as normal, admitted_reader as admitted_reader
from test_fly_run_job_delivery import world as world, existing_correction as existing_correction
from test_fly_delivery_dispatch import _StubNotifier

NEW_ID = 'ent_recovery_checkpoint'
OWNER = '11111111-1111-4111-8111-111111111111'


@pytest.fixture
def refused(joined, monkeypatch):
    """The natural refusal: first release refused by Fly before launch."""
    state = joined
    original_run = apis.run_skill
    runs = {'n': 0}

    async def run(*args, **kwargs):
        runs['n'] += 1
        if runs['n'] == 1:
            state['calls'].append('prelaunch-refusal')
            return SkillResult('cicada', False, None, '', 'synthetic diagnostic',
                               error='selected adapter refused before launch', provider='codex',
                               attempted_providers=('codex',), fly_delivery_not_launched=True,
                               fly_delivery_cleanup='released')
        return await original_run(*args, **kwargs)

    monkeypatch.setattr(apis, 'run_skill', run)
    assert release(state) is False
    assert state['record']['snapshot']['status'] == 'blocked'
    assert state['checkpoint']['snapshot']['resolved_dispatched'] is True

    checkpoints = {state['checkpoint_id']: state['checkpoint']}
    state['checkpoints'] = checkpoints
    state['listing'] = lambda: [copy.deepcopy(c) for c in checkpoints.values()]
    real_fetch = gating_module._fetch_entity

    def fetch(entity_id):
        if entity_id in checkpoints and entity_id != state['checkpoint_id']:
            return copy.deepcopy(checkpoints[entity_id])
        return real_fetch(entity_id)

    def stamp(entity_id, *, expected_version, **kwargs):
        cp = checkpoints[entity_id]
        if expected_version != cp['entity_version'] or cp['snapshot'].get('resolved_dispatched'):
            return False
        cp['snapshot']['resolved_dispatched'] = True
        cp['entity_version'] += '-stamped'
        return True

    def file_fresh(*, task_id, task_snapshot, **kwargs):
        # Stand-in for the signed checkpoint write: binds the task exactly as
        # it is now, under the policy the consumer will judge it by.
        record = state['record']
        assert task_id == state['task_id'] and record['snapshot']['status'] == 'awaiting_approval'
        policy = apis.resolve_policy_for_agent('cicada')
        decision = evaluate_gate(confidence=0.3, action_type='open_or_merge_pr', policy=policy)
        body = build_checkpoint_authorization_envelope(
            task_record=record, policy=policy, decision=decision,
            action_type='open_or_merge_pr', user_id=OWNER, required_approver_jkt='A' * 43)
        checkpoints[NEW_ID] = dict(
            entity_id=NEW_ID, entity_type='checkpoint_brief', user_id=OWNER,  # vocab-ok: existing authenticated checkpoint wire type
            entity_version='fresh-v1', snapshot=dict(
                status='awaiting_operator', resolved_dispatched=False, task_entity_id=task_id,
                body=body, title='Approve recovered correction',
                gate_action=decision.action.value, blast_radius=decision.blast_radius.value))
        state['filed'] = state.get('filed', 0) + 1
        return NEW_ID

    monkeypatch.setattr(gating_module, '_fetch_entity', fetch)
    monkeypatch.setattr(apis, 'checkpoints_for_task',
                        lambda task_id, strict=False: state['listing']())
    monkeypatch.setattr(apis, 'fetch_checkpoint_record',
                        lambda key: copy.deepcopy(checkpoints[key]))
    monkeypatch.setattr(apis, 'read_authenticated_checkpoint_authorization',
                        lambda cid, rec, *a: json.loads(rec['snapshot']['body']))
    monkeypatch.setattr(apis, '_checkpoint_authority_state', lambda *a: 'current')
    monkeypatch.setattr(apis, 'stamp_checkpoint_dispatched', stamp)
    monkeypatch.setattr(apis, '_file_fresh_checkpoint', file_fresh)
    state['writes_before'] = len(state['writes'])
    return state


def consume(state, checkpoint_id):
    return asyncio.run(apis.handle_checkpoint_brief(
        checkpoint_id, copy.deepcopy(state['checkpoints'][checkpoint_id]['snapshot']),
        _StubNotifier()))


def approve(state):
    state['checkpoints'][NEW_ID]['snapshot']['status'] = 'approved'


def assert_untouched(state, outcome, result):
    assert result.outcome == outcome, result
    assert len(state['writes']) == state['writes_before']
    assert state['record']['snapshot']['status'] == 'blocked'
    assert NEW_ID not in state['checkpoints']
    assert state['calls'] == ['owner', 'prelaunch-refusal']
    assert not delivery._delivery_ledger().delivery_receipts()


def test_recovery_reaches_exactly_one_fly_correction_on_the_same_task(refused):
    state = refused
    history = copy.deepcopy(state['checkpoints'][state['checkpoint_id']])
    task_id = state['task_id']
    result = apis.reopen_prelaunch_refused_correction(task_id, apply=True)
    assert result.outcome == 'reissued' and result.checkpoint_id == NEW_ID
    # One conditional status write; the hold text and attempt record are kept.
    new_writes = state['writes'][state['writes_before']:]
    assert [(w['field'], w['value']) for w in new_writes] == [('status', 'awaiting_approval')]
    assert new_writes[0]['expected_version']
    saved = state['record']['snapshot']
    assert saved['blocked_reason'].startswith(delivery.PRELAUNCH_REFUSAL_OPENINGS)
    assert saved['result'] == saved['blocked_reason']
    # The old approval stays consumed history; a pending checkpoint releases nothing.
    assert state['checkpoints'][state['checkpoint_id']] == history
    assert consume(state, state['checkpoint_id']) is False
    assert consume(state, NEW_ID) is False
    assert state['calls'] == ['owner', 'prelaunch-refusal']

    approve(state)
    # The shared fixture's synthetic Fly run reports an unverified PR, so the
    # consumer's return is False there too; the effect asserted is the run.
    consume(state, NEW_ID)
    assert state['calls'] == ['owner', 'prelaunch-refusal', 'owner', 'job']
    assert list(delivery._delivery_ledger().delivery_receipts()) == ['owner/repo#12']
    assert state['record']['entity_id'] == task_id
    # The ordinary signed lifecycle cleared the refusal hold before launch.
    lifecycle = [(w['field'], w['value']) for w in state['writes'][state['writes_before'] + 1:]]
    assert lifecycle[:3] == [('status', 'routed'), ('blocked_reason', ''), ('status', 'executing')]

    # Replays: neither checkpoint, nor the producer, can start a second run.
    assert consume(state, NEW_ID) is False
    assert consume(state, state['checkpoint_id']) is False
    again = apis.reopen_prelaunch_refused_correction(task_id, apply=True)
    assert again.outcome == 'refused'
    assert state['calls'].count('job') == 1
    assert state['filed'] == 1
    print('CAUSAL: refusal->re-hold->fresh checkpoint->one consumer->jobs=1 on original task')


def test_dry_run_writes_and_spawns_nothing(refused):
    result = apis.reopen_prelaunch_refused_correction(refused['task_id'], apply=False)
    assert_untouched(refused, 'would_reopen', result)


def test_concurrent_producer_loses_the_version_race_with_zero_effects(refused):
    def race(write):
        refused['record']['entity_version'] = 'task-v-other'
        return None
    refused['before_write'] = race
    result = apis.reopen_prelaunch_refused_correction(refused['task_id'], apply=True)
    assert_untouched(refused, 'failed', result)


def test_concurrent_consumers_release_once(refused):
    apis.reopen_prelaunch_refused_correction(refused['task_id'], apply=True)
    approve(refused)

    async def both():
        snap = copy.deepcopy(refused['checkpoints'][NEW_ID]['snapshot'])
        return await asyncio.gather(
            apis.handle_checkpoint_brief(NEW_ID, snap, _StubNotifier()),
            apis.handle_checkpoint_brief(NEW_ID, copy.deepcopy(snap), _StubNotifier()))
    asyncio.run(both())
    assert refused['calls'].count('job') == 1


def test_stale_revision_after_reissue_is_not_released(refused):
    apis.reopen_prelaunch_refused_correction(refused['task_id'], apply=True)
    approve(refused)
    refused['record']['snapshot']['notes'] = 'changed after the fresh checkpoint'
    refused['record']['observation_count'] += 1
    refused['record']['entity_version'] = 'task-v-changed'
    assert consume(refused, NEW_ID) is False
    assert 'job' not in refused['calls']


def _mutate(state, field, value):
    state['record']['snapshot'][field] = value


REFUSALS = {
    'person_hold': lambda s, m: _mutate(s, 'blocked_reason', 'held by a person'),
    'result_mismatch': lambda s, m: _mutate(s, 'result', 'other'),
    'not_correction': lambda s, m: _mutate(s, 'task_type', 'code'),
    'not_blocked': lambda s, m: _mutate(s, 'status', 'failed'),
    'target_gone': lambda s, m: s['record']['snapshot'].pop('pr_number', None),
    'no_version': lambda s, m: s['record'].__setitem__('entity_version', ''),
    'wrong_tenant': lambda s, m: m.setattr(apis, 'read_verified_entity_owner', lambda *a, **k: None),
    'operator_only': lambda s, m: m.setattr(apis, '_approval_cannot_release', lambda snap: 'operator_only'),
    'unknown_job': lambda s, m: m.setattr(apis, '_fly_run_state', lambda *a: (None, '')),
    'live_job': lambda s, m: m.setattr(apis, '_fly_run_state', lambda *a: (True, '')),
    'receipt_exists': lambda s, m: m.setattr(delivery, '_receipt_for', lambda *a: {'status': 'done'}),
    'ledger_unreadable': lambda s, m: m.setattr(delivery, '_delivery_ledger', _raise),
    'in_dispatch': lambda s, m: m.setitem(apis._tasks_in_dispatch, s['task_id'], 1),
    'history_unreadable': lambda s, m: s.__setitem__('listing', lambda: None),
    'history_empty': lambda s, m: s.__setitem__('listing', lambda: []),
    'history_malformed': lambda s, m: s.__setitem__('listing', lambda: [{'entity_id': 'x'}]),
    'approval_unconsumed': lambda s, m: s['checkpoint']['snapshot'].__setitem__('resolved_dispatched', False),
    'history_declined': lambda s, m: s['checkpoint']['snapshot'].__setitem__('status', 'rejected'),
    'history_pending': lambda s, m: s['checkpoint']['snapshot'].__setitem__('status', 'awaiting_operator'),
    'task_unreadable': lambda s, m: m.setattr(apis, 'fetch_task_record', lambda *a: None),
    'read_error': lambda s, m: m.setattr(apis, 'fetch_task_record', _raise),
}


def _raise(*args, **kwargs):
    raise OSError('synthetic read failure')


@pytest.mark.parametrize('case', sorted(REFUSALS))
def test_unqualified_shapes_refuse_with_zero_effects(refused, monkeypatch, case):
    REFUSALS[case](refused, monkeypatch)
    result = apis.reopen_prelaunch_refused_correction(refused['task_id'], apply=True)
    assert result.outcome == 'refused', result
    assert len(refused['writes']) == refused['writes_before']
    assert NEW_ID not in refused['checkpoints']
    assert refused['calls'] == ['owner', 'prelaunch-refusal']


def test_reissue_alone_still_never_touches_a_blocked_task(refused):
    # The existing held-task path keeps its contract: blocked is not held.
    result = apis.reissue_held_task_checkpoint(refused['task_id'], apply=True)
    assert_untouched(refused, 'skipped', result)


def test_unproven_readback_stops_before_any_checkpoint(refused):
    def reply(write, version):
        refused['record']['snapshot']['notes'] = 'concurrent change'
        return None
    refused['reply'] = reply
    result = apis.reopen_prelaunch_refused_correction(refused['task_id'], apply=True)
    assert result.outcome == 'incomplete'
    assert NEW_ID not in refused['checkpoints'] and 'job' not in refused['calls']


def test_cli_dry_run_requires_named_task_and_writes_nothing(refused, monkeypatch, capsys):
    import reissue_held_checkpoints as cli
    monkeypatch.setattr(cli, 'reopen_prelaunch_refused_correction',
                        apis.reopen_prelaunch_refused_correction)
    monkeypatch.setattr('sys.argv', ['x', '--prelaunch-correction', '--task', refused['task_id']])
    assert cli.main() == 0
    assert '"would_reopen"' in capsys.readouterr().out
    assert len(refused['writes']) == refused['writes_before']
    monkeypatch.setattr('sys.argv', ['x', '--prelaunch-correction'])
    with pytest.raises(SystemExit):
        cli.main()


def test_prelaunch_summary_opens_with_the_recognized_text():
    for cleanup, opening in zip(('released', 'not_reserved'), delivery.PRELAUNCH_REFUSAL_OPENINGS):
        result = SkillResult('cicada', False, None, '', '', error='e', fly_delivery_cleanup=cleanup)
        assert delivery.fly_prelaunch_failure_summary(result).startswith(opening)
