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


@pytest.mark.parametrize('failure', ['lost_reply', 'missing_token', 'failed_http', 'unreadable', 'changed', 'conflict'])
def test_unverified_lifecycle_notice_does_not_invent_a_cause(joined, failure):
    notice = _StubNotifier()
    if failure in {'lost_reply', 'missing_token', 'failed_http'}:
        def reply(write, version):
            if write['field'] == 'blocked_reason':
                if failure == 'lost_reply':
                    raise httpx.ReadTimeout('inert ambiguous reply')
                return httpx.Response(500 if failure == 'failed_http' else 200,
                                      json={}, request=httpx.Request('POST', 'https://inert.invalid/correct'))
        joined['reply'] = reply
    elif failure == 'unreadable':
        joined['unreadable_status'] = 'routed'
    else:
        def before(write):
            if write['field'] == 'blocked_reason':
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
