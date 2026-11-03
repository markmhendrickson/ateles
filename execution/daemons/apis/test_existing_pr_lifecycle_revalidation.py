"""Raw provenance, completed outcomes and truthful lifecycle notices; inert I/O."""
from __future__ import annotations

import asyncio
import copy

import httpx
import pytest

import apis
import build_delivery as delivery
from skill_runner import SkillResult
from test_existing_pr_lifecycle_handoff import joined as joined, release
from test_existing_pr_owner_reader import normal as normal, private_ledger as private_ledger
from test_existing_pr_delivery import admitted_reader as admitted_reader
from test_fly_delivery_dispatch import _StubNotifier


def inject(joined, monkeypatch, change, stage):
    if stage == 'before_clear':
        changed = []
        def before(write):
            if write['field'] == 'blocked_reason' and not changed:
                changed.append(True)
                change()
        joined['before_write'] = before
    elif stage == 'before_reader':
        joined['before_reader'] = change
    elif stage == 'owner_await':
        from lib import github_policy_control as control
        original = control.read_owner_policy
        def owner(*args):
            result = original(*args)
            change()
            return result
        monkeypatch.setattr(control, 'read_owner_policy', owner)
    else:
        from lib.daemon_runtime import artifact_resolver as artifacts
        original, reads = artifacts._gh_pr_view, []
        def source(*args, **kwargs):
            answer = original(*args, **kwargs)
            reads.append(True)
            if len(reads) == 2:
                change()
            return answer
        monkeypatch.setattr(artifacts, '_gh_pr_view', source)


@pytest.mark.parametrize('view', ['envelope_only', 'matching_snapshot'])
@pytest.mark.parametrize('target', ['envelope', 'snapshot'])
@pytest.mark.parametrize('bad', [None, False, 0, '', [], {}])
@pytest.mark.parametrize('stage', ['before_clear', 'before_reader', 'owner_await'])
def test_present_malformed_tenant_cannot_hide_behind_canonical_view(
        joined, monkeypatch, view, target, bad, stage):
    if view == 'matching_snapshot':
        joined['record']['snapshot']['user_id'] = joined['record']['user_id']
        joined['rebind']()
    changed = []
    def change():
        row = joined['record'] if target == 'envelope' else joined['record']['snapshot']
        row['user_id'] = copy.deepcopy(bad)
        changed.append(True)
    inject(joined, monkeypatch, change, stage)
    assert release(joined) is False
    assert changed
    assert joined['calls'] == (['owner'] if stage == 'owner_await' else [])
    assert not delivery._delivery_ledger().delivery_receipts()


@pytest.mark.parametrize('failure', ['known_prelaunch', 'run_provenance'])
def test_completed_refusal_cannot_acknowledge_leftover_executing(joined, monkeypatch, failure):
    notice = _StubNotifier()
    real_status = apis.set_task_status
    def status(entity_id, value, **kwargs):
        if value in {apis.TaskStatus.BLOCKED, apis.TaskStatus.FAILED}:
            return False
        return real_status(entity_id, value, **kwargs)
    monkeypatch.setattr(apis, 'set_task_status', status)
    if failure == 'run_provenance':
        monkeypatch.setattr(apis, 'RUN_CONVERSATIONS', True)
        monkeypatch.setattr(apis, 'create_run_session', lambda **kwargs: None)
    else:
        async def refused(*args, **kwargs):
            joined['calls'].append('prelaunch-refusal')
            return SkillResult('cicada', False, None, '', '', error='inert refusal',
                               fly_delivery_not_launched=True, fly_delivery_cleanup='released')
        monkeypatch.setattr(apis, 'run_skill', refused)
    accepted = asyncio.run(apis.handle_checkpoint_brief(
        joined['checkpoint_id'], copy.deepcopy(joined['checkpoint']['snapshot']), notice))
    assert joined['record']['snapshot']['status'] == 'executing'
    assert joined['record']['snapshot']['blocked_reason'] == ''
    assert not delivery._delivery_ledger().delivery_receipts()
    assert accepted is False
    assert not any('Checkpoint approved:' in message for message in notice.sent)
    assert notice.sent


@pytest.mark.parametrize('phase', ['routed', 'clear', 'executing'])
@pytest.mark.parametrize('failure', ['lost_reply', 'missing_token', 'failed_http', 'nonpersisted',
                                    'unreadable', 'changed', 'conflict'])
def test_unverified_lifecycle_notice_does_not_invent_a_cause(joined, failure, phase):
    notice = _StubNotifier()
    def matches(write):
        return ((phase == 'clear' and write['field'] == 'blocked_reason')
                or (write['field'] == 'status' and write['value'] == phase))
    if failure in {'lost_reply', 'missing_token', 'failed_http', 'nonpersisted'}:
        def reply(write, version):
            if matches(write):
                if failure == 'lost_reply':
                    raise httpx.ReadTimeout('inert ambiguous reply')
                if failure == 'nonpersisted':
                    joined['record']['snapshot'][write['field']] = (
                        'prior synthetic review hold' if phase == 'clear'
                        else 'awaiting_approval' if phase == 'routed' else 'routed')
                return httpx.Response(500 if failure == 'failed_http' else 200,
                                      json={} if failure == 'missing_token' else {'entity_version': version},
                                      request=httpx.Request('POST', 'https://inert.invalid/correct'))
        joined['reply'] = reply
    elif failure == 'unreadable':
        joined['unreadable_status'] = 'executing' if phase == 'executing' else 'routed'
    else:
        def before(write):
            if matches(write):
                joined['record']['snapshot']['notes'] = 'independent retained hold'
                if failure == 'conflict':
                    joined['record']['entity_version'] = 'independent-version'
        joined['before_write'] = before
    assert asyncio.run(apis.handle_checkpoint_brief(
        joined['checkpoint_id'], copy.deepcopy(joined['checkpoint']['snapshot']), notice)) is False
    assert joined['calls'] == [] and not delivery._delivery_ledger().delivery_receipts()
    text = '\n'.join(notice.sent)
    assert 'could not be verified' in text and 'this attempt was not started' in text
    assert 'Reconcile current task/admission' in text
    assert 'changed during' not in text and 'changed before' not in text
    persisted = joined['record']['snapshot']
    if failure in {'lost_reply', 'missing_token', 'failed_http'}:
        assert persisted['status'] == ('executing' if phase == 'executing' else 'routed')
        assert persisted['blocked_reason'] == ('prior synthetic review hold' if phase == 'routed' else '')
    if failure in {'changed', 'conflict'}:
        assert persisted['notes'] == 'independent retained hold'


@pytest.mark.parametrize('view,target,operation', [
    ('envelope_only', 'snapshot', 'add'),
    ('matching_snapshot', 'snapshot', 'remove'),
    ('matching_snapshot', 'envelope', 'remove'),
    ('observations_only', 'snapshot', 'add'),
    ('observations_only', 'envelope', 'add'),
])
@pytest.mark.parametrize('stage', ['before_clear', 'before_reader', 'owner_await', 'final_app_read'])
def test_same_owner_raw_presence_changes_are_not_a_new_admitted_view(
        joined, monkeypatch, view, target, operation, stage):
    owner = joined['record']['user_id']
    if view == 'matching_snapshot':
        joined['record']['snapshot']['user_id'] = owner
    elif view == 'observations_only':
        joined['record'].pop('user_id')
    joined['rebind']()
    changed = []
    def change():
        row = joined['record'] if target == 'envelope' else joined['record']['snapshot']
        if operation == 'add':
            assert 'user_id' not in row
            row['user_id'] = owner
        else:
            assert row.pop('user_id') == owner
        changed.append(True)
    inject(joined, monkeypatch, change, stage)
    assert release(joined) is False
    assert changed
    assert joined['calls'] == (['owner'] if stage in {'owner_await', 'final_app_read'} else [])
    assert not delivery._delivery_ledger().delivery_receipts()


@pytest.mark.parametrize('view', ['envelope_only', 'matching_snapshot'])
@pytest.mark.parametrize('target', ['envelope', 'snapshot'])
@pytest.mark.parametrize('bad', [None, False, 0, '', [], {}])
def test_final_app_read_revalidates_raw_tenant_before_receipt(joined, monkeypatch, view, target, bad):
    if view == 'matching_snapshot':
        joined['record']['snapshot']['user_id'] = joined['record']['user_id']
        joined['rebind']()
    changed = []
    def change():
        row = joined['record'] if target == 'envelope' else joined['record']['snapshot']
        row['user_id'] = copy.deepcopy(bad)
        changed.append(True)
    inject(joined, monkeypatch, change, 'final_app_read')
    assert release(joined) is False
    assert changed and joined['calls'] == ['owner']
    assert not delivery._delivery_ledger().delivery_receipts()


@pytest.mark.parametrize('stage', ['before_clear', 'before_reader', 'owner_await', 'final_app_read'])
def test_changed_verified_observation_owner_is_not_hidden_by_same_raw_view(joined, monkeypatch, stage):
    from lib.daemon_runtime import gating
    changed = []
    def change():
        monkeypatch.setattr(gating, 'fetch_entity_user_id', lambda key: '22222222-2222-4222-8222-222222222222')
        changed.append(True)
    inject(joined, monkeypatch, change, stage)
    assert release(joined) is False
    assert changed
    assert joined['calls'] == (['owner'] if stage in {'owner_await', 'final_app_read'} else [])
    assert not delivery._delivery_ledger().delivery_receipts()


@pytest.mark.parametrize('view', ['envelope_only', 'matching_snapshot', 'observations_only'])
@pytest.mark.parametrize('bump_version', [False, True])
def test_same_owner_same_presence_is_unchanged_but_new_version_is_not(joined, view, bump_version):
    owner = joined['record']['user_id']
    if view == 'matching_snapshot':
        joined['record']['snapshot']['user_id'] = owner
    elif view == 'observations_only':
        joined['record'].pop('user_id')
    joined['rebind']()
    def change():
        if view != 'observations_only':
            joined['record']['user_id'] = owner
        if bump_version:
            joined['record']['entity_version'] = 'same-shape-new-version'
    joined['before_reader'] = change
    release(joined)
    assert joined['calls'] == ([] if bump_version else ['owner', 'job'])
    assert bool(delivery._delivery_ledger().delivery_receipts()) is (not bump_version)


@pytest.mark.parametrize('kind', ['missing_header', 'empty_header', 'blocked_header', 'wrong_body',
                                  'absent_ref', 'unavailable_ref', 'gate_error', 'record_not_saved',
                                  'done_other_result', 'uncontracted_write',
                                  'session_terminal', 'unknown', 'failed'])
def test_all_completed_refusal_exits_cannot_ack_from_failed_terminal_state(joined, monkeypatch, kind):
    from types import SimpleNamespace
    from lib.daemon_runtime.task_lifecycle import CompletionOutcome
    notice = _StubNotifier()
    header = '[cicada] pull_request_link: https://github.com/owner/repo/pull/12'
    result = SkillResult('cicada', True, 0, header, '')
    if kind == 'missing_header':
        result.stdout = 'no required header'
    elif kind == 'empty_header':
        result.stdout = '[cicada] pull_request_link:'
    elif kind == 'blocked_header':
        result.stdout = '[cicada] pull_request_link: BLOCKED synthetic input required'
    elif kind == 'wrong_body':
        result.stdout = '[cicada] pull_request_link: ENG_SPEC_SECTION synthetic content'
    elif kind in {'unknown', 'failed'}:
        result.ok = False
        result.remote_outcome_unknown = kind == 'unknown'
        result.error = 'inert completed failure'
    async def finished_spawn(*args, **kwargs):
        joined['calls'].append('inert-finish-result')
        return result
    monkeypatch.setattr(apis, '_spawn_harness_skill', finished_spawn)
    def resolve(*args, **kwargs):
        if kind == 'gate_error':
            raise ValueError('inert resolver failure')
        return {'absent_ref': 'absent', 'unavailable_ref': 'unavailable'}.get(kind, 'exists')
    monkeypatch.setattr(apis, 'resolve_artifact_ref', resolve)
    real_status = apis.set_task_status
    def deny_terminal(entity_id, value, **kwargs):
        if value in {apis.TaskStatus.FAILED, apis.TaskStatus.BLOCKED, apis.TaskStatus.VERIFIED}:
            return False
        if kind == 'uncontracted_write' and value == apis.TaskStatus.DONE:
            return False
        return real_status(entity_id, value, **kwargs)
    monkeypatch.setattr(apis, 'set_task_status', deny_terminal)
    if kind == 'record_not_saved':
        monkeypatch.setattr(apis, 'complete_task_with_result', lambda *a, **kw: CompletionOutcome(False, 'result_write', 'inert failure'))
    if kind == 'done_other_result':
        def other_result(*args, **kwargs):
            joined['record']['snapshot'].update(status='done', result='different immutable result')
            return CompletionOutcome(False, 'status_readback', 'inert competing result')
        monkeypatch.setattr(apis, 'complete_task_with_result', other_result)
    if kind == 'uncontracted_write':
        monkeypatch.setattr(apis, 'role_required_artifact', lambda: {})
    if kind == 'session_terminal':
        monkeypatch.setattr(apis, 'RUN_CONVERSATIONS', True)
        monkeypatch.setattr(apis, 'create_run_session', lambda **kw: SimpleNamespace(
            conversation_id='inert-conversation', agent_session_id='inert-session', native_session_id='inert-native'))
        monkeypatch.setattr(apis, 'append_turn', lambda **kw: None)
        monkeypatch.setattr(apis, 'update_run_session_status', lambda *a, **kw: False)
    accepted = asyncio.run(apis.handle_checkpoint_brief(
        joined['checkpoint_id'], copy.deepcopy(joined['checkpoint']['snapshot']), notice))
    assert joined['calls'] == ['inert-finish-result']
    assert joined['record']['snapshot']['status'] == ('done' if kind == 'done_other_result' else 'executing')
    assert joined['record']['snapshot']['blocked_reason'] == ''
    assert accepted is False
    assert not any('Checkpoint approved:' in message for message in notice.sent)
    assert notice.sent and not delivery._delivery_ledger().delivery_receipts()
    assert release(joined) is False
    assert joined['calls'] == ['inert-finish-result']


@pytest.mark.parametrize('detach', [False, True])
def test_noop_completed_failure_and_detached_acceptance_keep_receipt_and_replay(joined, monkeypatch, detach):
    notice = _StubNotifier()
    real_status = apis.set_task_status
    def deny_hold(entity_id, value, **kwargs):
        return False if value == apis.TaskStatus.BLOCKED else real_status(entity_id, value, **kwargs)
    monkeypatch.setattr(apis, 'set_task_status', deny_hold)
    async def consume():
        accepted = await apis.handle_checkpoint_brief(
            joined['checkpoint_id'], copy.deepcopy(joined['checkpoint']['snapshot']), notice,
            detach_after_accept=detach)
        if detach:
            assert joined['calls'] == [] and joined['record']['snapshot']['status'] == 'executing'
        background = list(apis._background_dispatch_tasks)
        if background:
            await asyncio.gather(*background)
        return accepted
    assert asyncio.run(consume()) is detach
    assert joined['calls'] == ['owner', 'job']
    assert joined['record']['snapshot']['status'] == 'executing'
    receipts = delivery._delivery_ledger().delivery_receipts()
    assert list(receipts) == ['owner/repo#12'] and receipts['owner/repo#12']['status'] == 'no_changes'
    assert any('Task result/status could not be verified' in message for message in notice.sent)
    assert any('Checkpoint approved:' in message for message in notice.sent) is detach
    assert release(joined) is False and joined['calls'] == ['owner', 'job']
