"""Joined approved-checkpoint lifecycle and strict existing-PR owner binding."""
from __future__ import annotations

import asyncio
import copy
import json
import math
from types import SimpleNamespace

import httpx
import pytest

import apis
import build_delivery as delivery
from lib.daemon_runtime import task_lifecycle as lifecycle
from lib.daemon_runtime import gating as gating_module
from lib.daemon_runtime.gating import (
    ExecutionPolicy, build_checkpoint_authorization_envelope, evaluate_gate,
)
from test_existing_pr_owner_reader import normal as normal, admitted_reader as admitted_reader
from test_fly_run_job_delivery import world as world, existing_correction as existing_correction
from test_fly_delivery_dispatch import _StubNotifier
from test_build_delivery import dispatch_env as dispatch_env


@pytest.fixture
def joined(normal, admitted_reader, monkeypatch, tmp_path):
    denials = (tmp_path / 'joined-checkpoint-denials').resolve()
    assert denials.is_absolute() and not denials.exists()
    denials.mkdir(mode=0o700)
    assert denials.stat().st_mode & 0o777 == 0o700 and not list(denials.iterdir())
    monkeypatch.setenv('APIS_CHECKPOINT_DENIAL_DIR', str(denials))
    row, _, evidence, calls = normal
    row.update(status='awaiting_approval', blocked_reason='prior synthetic review hold',
               notes='complete original review and attempt history', title='Scoped correction',
               verification={'flag': True, 'limit': 1, 'optional': None})
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
    checkpoint = dict(
        entity_id=checkpoint_id, entity_type='checkpoint_brief', user_id=owner,  # vocab-ok: existing authenticated checkpoint wire type
        entity_version='checkpoint-v1', snapshot=dict(status='approved', resolved_dispatched=False,
            task_entity_id=task_id, body=authority, title='Approve scoped correction',
            gate_action=decision.action.value, blast_radius=decision.blast_radius.value))
    writes, seen = [], {}
    state = dict(record=record, checkpoint=checkpoint, original=original, row=row,
                 writes=writes, seen=seen, calls=calls, task_id=task_id,
                 checkpoint_id=checkpoint_id, before_write=None, after_write=None,
                 before_reader=None, reply=None, unreadable_status=None, pr=admitted_reader)

    def fetch_record(entity_id):
        assert entity_id == task_id
        if state['unreadable_status'] is not None and record['snapshot']['status'] == state['unreadable_status']:
            return None
        return copy.deepcopy(record)

    def fetch_version(entity_id):
        current, version = gating_module.fetch_task_snapshot_and_version(entity_id)
        if current is None or current['status'] == state['unreadable_status']:
            return None, None
        return current, version

    def post(url, *, json: dict, **kwargs):
        assert url == 'https://inert.invalid/correct'
        assert json['entity_id'] == task_id and json['entity_type'] == 'task'
        if state['before_write']:
            response = state['before_write'](json)
            if response is not None:
                return response
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

    monkeypatch.setattr(gating_module, '_fetch_entity', fetch_record)
    monkeypatch.setattr(apis, 'fetch_task_record', gating_module.fetch_task_record)
    monkeypatch.setattr(apis, 'fetch_task_snapshot', gating_module.fetch_task_snapshot)
    monkeypatch.setattr(apis, 'fetch_task_snapshot_and_version', fetch_version)
    monkeypatch.setattr(apis, 'fetch_checkpoint_record', lambda key: copy.deepcopy(checkpoint))
    monkeypatch.setattr(gating_module, 'fetch_entity_user_id', lambda key: owner)
    monkeypatch.setattr(apis, 'read_verified_entity_owner', gating_module.read_verified_entity_owner)
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
    if not seen:
        assert joined['calls'] == [] and not delivery._delivery_ledger().delivery_receipts()
        assert joined['row'] == joined['original']
        print('CAUSAL_TENANT_DENIAL: real versioned reader preserves envelope tenant; '
              'raw admitted shape differs; zero owner/job/receipt')
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


def mutate(state, field, value, *, bump_version=True):
    if value is _ABSENT:
        state['record']['snapshot'].pop(field, None)
    else:
        state['record']['snapshot'][field] = copy.deepcopy(value)
    if bump_version:
        state['record']['observation_count'] += 1
        state['record']['entity_version'] = f"task-v{state['record']['observation_count']}"


_ABSENT = object()
CHANGES = [
    ('blocked_reason', 'a new independent hold'), ('blocked_reason', _ABSENT),
    ('blocked_reason', None), ('blocked_reason', False), ('blocked_reason', []),
    ('blocked_reason', 0), ('blocked_reason', ''),
    ('notes', 'changed complete review history'), ('description', 'changed scope'),
    ('body', 'new worker instructions'), ('task_type', 'different_type'),
    ('action_type', 'local_edit'), ('repository', 'other/repo'),
    ('user_id', '22222222-2222-4222-8222-222222222222'),
    ('assigned_to', 'another-role'), ('attempt_count', 4), ('issue_number', 35),
    ('head_sha', 'b' * 40), ('pr_number', 13), ('context', '{}'),
    ('unknown_scope', {'accepted': False}), ('unknown_scope', {'accepted': 0}),
    ('result', 'new result'), ('updated_at', 'new metadata'), ('status', 'blocked'),
    ('verification', {'flag': 1, 'limit': 1, 'optional': None}),
    ('verification', {'flag': True, 'limit': True, 'optional': None}),
    ('verification', {'flag': True, 'limit': 1}),
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


CURRENT_CHANGES = [(field, value, bump) for field, value in CHANGES for bump in (False, True)
                   if bump or field != 'blocked_reason' or value != '']


@pytest.mark.parametrize('field,value,bump_version', CURRENT_CHANGES)
def test_complete_lifecycle_binding_is_strict_before_first_owner_read(joined, field, value, bump_version):
    joined['before_reader'] = lambda: mutate(joined, field, value, bump_version=bump_version)
    assert release(joined) is False
    assert joined['calls'] == []
    assert_no_delivery(joined)


@pytest.mark.parametrize('field,value,bump_version', CURRENT_CHANGES)
def test_owner_await_rechecks_full_current_shape_and_version(joined, monkeypatch, field, value, bump_version):
    from lib import github_policy_control as control
    original = control.read_owner_policy
    def owner(*args):
        answer = original(*args)
        mutate(joined, field, value, bump_version=bump_version)
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


@pytest.mark.parametrize('stage', ['before_clear', 'before_reader'])
def test_task_record_type_change_is_not_a_task_lifecycle_transition(joined, stage):
    def change():
        joined['record']['entity_type'] = 'different_type'
    if stage == 'before_reader':
        joined['before_reader'] = change
    else:
        def before(write):
            if write['field'] == 'blocked_reason':
                change()
        joined['before_write'] = before
    assert release(joined) is False
    assert joined['calls'] == [] and joined['record']['entity_type'] == 'different_type'
    assert_no_delivery(joined)


@pytest.mark.parametrize('target', ['task_envelope', 'task_snapshot', 'checkpoint_envelope',
                                   'checkpoint_snapshot', 'spoofed_task_proof'])
@pytest.mark.parametrize('tenant', [None, '', '22222222-2222-4222-8222-222222222222'])
def test_initial_tenant_conflict_cannot_create_checkpoint_lifecycle_context(joined, target, tenant):
    record = joined['record'] if target.startswith('task') or target == 'spoofed_task_proof' else joined['checkpoint']
    row = record['snapshot'] if target.endswith('snapshot') else record
    row['user_id'] = tenant
    if target == 'spoofed_task_proof':
        record['snapshot'].update(owner_policy_approved=True, approved_user_id=record['user_id'],
                                  controller_reason_cleared=True)
    assert release(joined) is False
    assert joined['writes'] == [] and joined['calls'] == []
    assert_no_delivery(joined)


@pytest.mark.parametrize('stage', ['before_clear', 'before_reader', 'owner_await'])
def test_envelope_tenant_change_during_lifecycle_or_await_never_rebinds(joined, monkeypatch, stage):
    foreign = '22222222-2222-4222-8222-222222222222'
    def change():
        joined['record']['user_id'] = foreign
    if stage == 'before_clear':
        def before(write):
            if write['field'] == 'blocked_reason':
                change()
        joined['before_write'] = before
    elif stage == 'before_reader':
        joined['before_reader'] = change
    else:
        from lib import github_policy_control as control
        original = control.read_owner_policy
        def owner(*args):
            result = original(*args)
            change()
            return result
        monkeypatch.setattr(control, 'read_owner_policy', owner)
    assert release(joined) is False
    assert joined['calls'] == (['owner'] if stage == 'owner_await' else [])
    assert joined['record']['user_id'] == foreign
    assert_no_delivery(joined)


@pytest.mark.parametrize('tenant_shape', ['envelope_only', 'matching_snapshot', 'observations_only'])
def test_existing_verified_tenant_views_preserve_admitted_source(joined, tenant_shape):
    if tenant_shape == 'matching_snapshot':
        joined['record']['snapshot']['user_id'] = joined['record']['user_id']
    elif tenant_shape == 'observations_only':
        joined['record'].pop('user_id')
    joined['rebind']()
    release(joined)
    assert joined['calls'] == ['owner', 'job']
    assert joined['seen']['receiving_snapshot'] == joined['seen']['authoritative_snapshot']
    assert joined['row'] == joined['original']
    assert list(delivery._delivery_ledger().delivery_receipts()) == ['owner/repo#12']


@pytest.mark.parametrize('reason', [_ABSENT, None, False, [], 0])
def test_missing_or_nonstring_admitted_reason_is_not_a_clearance(joined, reason):
    mutate(joined, 'blocked_reason', reason)
    joined['rebind']()
    assert release(joined) is False
    assert joined['writes'] == [] and joined['calls'] == []
    assert_no_delivery(joined)


def test_already_empty_admitted_reason_retains_exact_normal_path(joined):
    mutate(joined, 'blocked_reason', '')
    joined['rebind']()
    release(joined)
    assert joined['calls'] == ['owner', 'job']
    assert joined['seen']['receiving_snapshot'] == joined['seen']['authoritative_snapshot']
    assert joined['row'] == joined['original']
    assert list(delivery._delivery_ledger().delivery_receipts()) == ['owner/repo#12']


def test_detached_request_accepts_qualified_lifecycle_then_finishes_once(joined):
    async def consume():
        accepted = await apis.handle_checkpoint_brief(
            joined['checkpoint_id'], copy.deepcopy(joined['checkpoint']['snapshot']),
            _StubNotifier(), detach_after_accept=True)
        assert accepted is True and joined['record']['snapshot']['status'] == 'executing'
        background = list(apis._background_dispatch_tasks)
        assert len(background) == 1
        await asyncio.gather(*background)
    asyncio.run(consume())
    assert joined['calls'] == ['owner', 'job']
    assert list(delivery._delivery_ledger().delivery_receipts()) == ['owner/repo#12']
    assert release(joined) is False


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


@pytest.mark.parametrize('phase', ['routed', 'clear', 'executing'])
@pytest.mark.parametrize('status', [409, 500])
def test_rejected_lifecycle_write_never_continues_or_starts_delivery(joined, phase, status):
    rejected = []
    def before(write):
        if ((phase == 'clear' and write['field'] == 'blocked_reason')
                or (write['field'] == 'status' and write['value'] == phase)):
            rejected.append(copy.deepcopy(write))
            return httpx.Response(status, json={'error_code': 'ERR_FIELD_VERSION_CONFLICT'},
                                  request=httpx.Request('POST', 'https://inert.invalid/correct'))
    joined['before_write'] = before
    assert release(joined) is False
    assert len(rejected) == 1 and joined['calls'] == []
    assert len(joined['writes']) == {'routed': 0, 'clear': 1, 'executing': 2}[phase]
    assert joined['record']['snapshot']['status'] == ('awaiting_approval' if phase == 'routed' else 'routed')
    assert_no_delivery(joined)


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


def test_normal_checkpoint_preserves_known_prelaunch_diagnostic_hold(joined, monkeypatch):
    from skill_runner import SkillResult
    async def refused(*args, **kwargs):
        joined['calls'].append('prelaunch-refusal')
        return SkillResult('cicada', False, None, 'kept output', 'kept diagnostic',
                           error='selected adapter refused before launch', provider='codex',
                           attempted_providers=('codex',), configuration_repair='repair existing setting',
                           fly_delivery_not_launched=True, fly_delivery_cleanup='released')
    monkeypatch.setattr(apis, 'run_skill', refused)
    assert release(joined) is False
    assert joined['calls'] == ['owner', 'prelaunch-refusal']
    saved = joined['record']['snapshot']
    assert saved['status'] == 'blocked'
    assert 'not launched' in saved['result'] and 'kept diagnostic' in saved['result']
    assert 'repair existing setting' in saved['result'] and 'codex' in saved['result']
    assert 'UNKNOWN' not in saved['result']
    assert not delivery._delivery_ledger().delivery_receipts()
    assert release(joined) is False
    assert joined['calls'] == ['owner', 'prelaunch-refusal']


@pytest.mark.parametrize('field,value', CHANGES)
def test_final_app_read_keeps_complete_bound_current_context(joined, monkeypatch, field, value):
    from lib.daemon_runtime import artifact_resolver as artifacts
    original, reads = artifacts._gh_pr_view, []
    def source(*args, **kwargs):
        answer = original(*args, **kwargs)
        reads.append(True)
        if len(reads) == 2:
            mutate(joined, field, value)
        return answer
    monkeypatch.setattr(artifacts, '_gh_pr_view', source)
    assert release(joined) is False
    assert len(reads) == 2 and joined['calls'] == ['owner']
    assert_no_delivery(joined)


@pytest.mark.parametrize('closed_after_push', [False, True])
def test_normal_checkpoint_private_git_receipt_and_daemon_readback(
        joined, world, existing_correction, monkeypatch, closed_after_push):
    import fly_runner
    import skill_runner
    import test_fly_run_job_delivery as flow
    from lib import github_app_token
    spec, api_calls, _ = existing_correction
    joined['record']['snapshot']['head_sha'] = spec['head_sha']
    joined['rebind']()
    joined['pr']['headRefOid'] = spec['head_sha']
    monkeypatch.setattr(github_app_token, 'identity_token', lambda *a: 'inert-readback-token')
    records, resolved = [], []
    async def runner(skill, prompt, **kwargs):
        joined['calls'].append('private-git-runner')
        selected = kwargs['fly_delivery']
        assert selected['branch'] == spec['branch'] and selected['head_sha'] == spec['head_sha']
        code, record = flow._run(flow._job(world['tmp'], delivery=selected), flow._agent(world))
        assert code == 0 and record['delivery_status'] == 'updated'
        records.append(record)
        joined['pr']['headRefOid'] = record['delivery_commit']
        if closed_after_push:
            joined['pr']['state'] = 'CLOSED'
        job = SimpleNamespace(delivery=selected,
            delivery_result=fly_runner.delivery_result_from_record(record))
        return skill_runner.SkillResult(skill, True, 0, '', '', remote_run_id='inert-private-git-run',
            **skill_runner._fly_delivery_fields(job, unknown=False))
    def resolve_ref(parsed, *, repo):
        assert repo == 'owner/repo' and parsed.canonical == 'owner/repo#12'
        assert len(records) == 1 and joined['pr']['state'] == 'OPEN'
        assert flow._remote_ref(world, 'refs/heads/' + spec['branch']) == records[0]['delivery_commit']
        resolved.append(parsed.canonical)
        return 'exists'
    monkeypatch.setattr(apis, 'run_skill', runner)
    monkeypatch.setattr(apis, 'resolve_artifact_ref', resolve_ref)
    assert release(joined) is (not closed_after_push)
    assert joined['calls'] == ['owner', 'private-git-runner'] and len(records) == 1
    remote = flow._remote_ref(world, 'refs/heads/' + spec['branch'])
    assert remote == records[0]['delivery_commit'] != spec['head_sha']
    assert flow.git(world['remote'], 'show', spec['branch'] + ':prior-wip.txt', home=world['home']) == 'preserved original work'
    assert flow.git(world['remote'], 'merge-base', spec['head_sha'], remote, home=world['home']) == spec['head_sha']
    assert all(method == 'GET' for method, _ in api_calls)
    receipts = delivery._delivery_ledger().delivery_receipts()
    assert list(receipts) == ['owner/repo#12']
    receipt = receipts['owner/repo#12']
    assert receipt['associated_keys'] == ['owner/repo#34']
    assert receipt['commit'] == remote and receipt['existing_pr']['head_sha'] == spec['head_sha']
    assert receipt['readback'] == ('failed' if closed_after_push else 'passed')
    task = joined['record']['snapshot']
    assert task['status'] == ('blocked' if closed_after_push else 'done')
    assert bool(resolved) is (not closed_after_push)
    if closed_after_push:
        assert 'Fly delivery not verified' in task['blocked_reason']
        assert 'not a confirmed open same-repository PR' in task['blocked_reason']
    else:
        assert 'Fly delivery updated pull request #12' in task['result']
    assert delivery.active_build('owner/repo', 12) is None
    assert delivery.active_build('owner/repo', 34) is None
    assert release(joined) is False
    assert len(records) == 1 and joined['calls'] == ['owner', 'private-git-runner']


class _RecordingNotifier:
    def __init__(self):
        self.sent = []

    def send(self, message, priority=None, handler=None, **kwargs):
        self.sent.append((message, priority))


def test_executing_version_conflict_sends_one_blocker_and_writes_nothing(joined):
    # Another writer moves the task's version just before the executing write
    # (e.g. a note lands at that moment): the versioned write is refused.
    def before(write):
        if write['field'] == 'status' and write['value'] == 'executing':
            mutate(joined, 'notes', 'a note written at that moment')
    joined['before_write'] = before
    notifier = _RecordingNotifier()
    released = asyncio.run(apis.handle_checkpoint_brief(
        joined['checkpoint_id'], copy.deepcopy(joined['checkpoint']['snapshot']), notifier))
    assert released is False
    assert joined['calls'] == []
    assert_no_delivery(joined)
    # Only the routed and reason-clear writes happened; the conflict branch
    # wrote nothing, and the task is left routed.
    assert [(w['field'], w['value']) for w in joined['writes']] == [
        ('status', 'routed'), ('blocked_reason', '')]
    assert joined['record']['snapshot']['status'] == 'routed'
    blockers = [m for m, p in notifier.sent if p == apis.Priority.BLOCKER]
    assert len(blockers) == 1, notifier.sent
    (notice,) = blockers
    assert joined['task_id'] in notice
    assert 'could not be verified' in notice and 'last read as routed' in notice
    assert 'refused' not in notice and 'changed under it' not in notice
    assert 'Nothing was started' in notice
    assert 'cannot be released again' in notice and 'refile' in notice


def _fly_admission_seam(monkeypatch, tmp_path, on_attempt, *, retries='2', backoff='1',
                        build=None):
    """The real run_skill Fly path, with fly_runner.build_job refusing on a
    Neotoma outage: every provider binary pinned and no local run possible.

    Returns (attempts, notices): the delivery receipts held at each build_job
    call, and every operator notice sent. A number *on_attempt* returns is the
    wait Neotoma stated (Retry-After) on that refusal. *build*, when given,
    replaces the refusal on an attempt: ``build(n, kw)`` returns the job or
    raises. The dispatcher's waits are recorded in ``apis.SLEEPS`` instead of
    slept (a zero backoff is rejected as unbounded-retry config)."""
    import fly_runner
    import skill_runner
    from lib.daemon_runtime import AgentDefinition
    monkeypatch.setenv('APIS_NEOTOMA_ADMISSION_RETRIES', retries)
    monkeypatch.setenv('APIS_NEOTOMA_ADMISSION_BACKOFF_S', backoff)
    skill_dir = tmp_path / '.claude' / 'skills' / 'cicada'
    skill_dir.mkdir(parents=True)
    (skill_dir / 'SKILL.md').write_text('# cicada\n')
    monkeypatch.setattr(skill_runner, 'ATELES_REPO', tmp_path)
    # Like test_fly_runner's `seam` fixture: pin every provider binary, so
    # provider selection never depends on which CLIs the host has on PATH
    # (with none, run_skill returned before the Fly path and a test passed
    # vacuously).
    monkeypatch.setattr(skill_runner, 'CLAUDE_BIN', '/usr/bin/claude-test')
    monkeypatch.setattr(skill_runner, 'CODEX_BIN', '/usr/bin/codex-test')
    monkeypatch.setattr(skill_runner, 'CURSOR_BIN', '/usr/bin/cursor-agent-test')
    monkeypatch.setattr(skill_runner, '_load_agent_def', lambda role: AgentDefinition(
        entity_id='e', name=role, prompt_markdown='You are cicada.', tool_allowlist='*',
        aauth_sub=f'{role}@ateles-swarm'))
    monkeypatch.setattr(skill_runner, '_load_active_policy_prompt', lambda role, **kw: '')
    monkeypatch.setattr(skill_runner, '_write_harness_event', lambda **kw: None)
    monkeypatch.setattr(skill_runner, 'neotoma_token_for_agent', lambda role: ('inert-role-token', 'inert'))
    monkeypatch.setenv('NEOTOMA_BASE_URL', 'https://neotoma.example.test')
    async def no_local(*a, **kw):
        pytest.fail('a Neotoma deferral must never run the delivery locally')
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', no_local)
    sleeps = []
    real_sleep = asyncio.sleep
    async def record_sleep(delay, *a, **kw):
        sleeps.append(delay)
        await real_sleep(0)
    monkeypatch.setattr(apis.asyncio, 'sleep', record_sleep)
    monkeypatch.setattr(apis, 'SLEEPS', sleeps, raising=False)
    attempts = []
    def build_job(**kw):
        attempts.append(list(delivery._delivery_ledger().delivery_receipts()))
        if build is not None:
            built = build(len(attempts), kw)
            if built is not None:
                return built
        stated = on_attempt(len(attempts))
        raise fly_runner.FlyIneligible('Neotoma /session answered 429', neotoma_unavailable=True,
                                       retry_after_seconds=stated)
    monkeypatch.setattr(fly_runner, 'build_job', build_job)
    monkeypatch.setattr(apis, 'run_skill', skill_runner.run_skill)
    notices = []
    original_send = _StubNotifier.send
    def send(self, message, *a, **kw):
        notices.append(message)
        return original_send(self, message, *a, **kw)
    monkeypatch.setattr(_StubNotifier, 'send', send)
    return attempts, notices


def _decision_queue(monkeypatch, mode, task_snapshot):
    """Stand-in for the existing decision queue (write_checkpoint_brief_outcome
    and its read-back). ``mode``: 'ok', 'write_absent' (the writer returned
    before sending any store request), 'write_post_raised' (the store request
    raised after it was sent: unconfirmed, no id), 'write_unconfirmed' (the
    writer's own read-back failed: unconfirmed, id kept),
    'readback_fails' (the written checkpoint does not read back),
    'readback_raises' (the read-back cannot run after the write) or
    'readback_mismatch' (the read-back returns a different checkpoint)."""
    briefs = {}
    def write(**kw):
        briefs.setdefault('calls', []).append(kw)
        if mode == 'write_absent':
            return 'absent', None
        if mode == 'write_post_raised':
            return 'unconfirmed', None
        key = 'ent_exhausted_' + kw['idempotency_context']
        briefs[key] = dict(entity_id=key, entity_type='checkpoint_brief', snapshot=dict(  # vocab-ok: existing authenticated checkpoint wire type
            task_entity_id=kw['task_entity_id'], reason=kw['decision'].reason,
            plan_summary=kw['plan_summary'], status='awaiting_operator'))
        if mode == 'write_unconfirmed':
            return 'unconfirmed', key
        return 'confirmed', key
    earlier = apis.fetch_checkpoint_record
    def read(key):
        if key in briefs:
            if mode == 'readback_raises':
                raise httpx.ConnectError('Neotoma went away after the write')
            if mode == 'readback_fails':
                return None
            record = copy.deepcopy(briefs[key])
            if mode == 'readback_mismatch':
                record['snapshot']['reason'] = 'some_other_reason'
            return record
        return earlier(key)
    task_reads = []
    def read_task(entity_id):
        if briefs.get('calls'):  # read-backs after the checkpoint write only
            task_reads.append(entity_id)
        return task_snapshot()
    monkeypatch.setattr(apis, 'write_checkpoint_brief_outcome', write)
    monkeypatch.setattr(apis, 'fetch_checkpoint_record', read)
    monkeypatch.setattr(apis, 'fetch_task_snapshot', read_task)
    monkeypatch.setattr(apis, 'fetch_entity_user_id', lambda key: 'inert-tenant')
    return briefs, task_reads


def _assert_exhaustion(mode, briefs, task_reads, notices, task_id, saved, statuses):
    """failure_posture.md rule 5 at the ceiling: the checkpoint is the hold."""
    assert not any('marked FAILED' in n for n in notices)
    calls = briefs.get('calls', [])
    assert len(calls) == 1, calls  # exactly one checkpoint, never a second one
    assert calls[0]['decision'].reason == 'rounds_exhausted'
    assert calls[0]['task_entity_id'] == task_id
    assert calls[0]['idempotency_context']
    assert "Neotoma's admission check" in calls[0]['plan_summary']
    assert 'Nothing was launched' in calls[0]['plan_summary']
    neotoma = [n for n in notices if "Neotoma's admission check did not answer" in n]
    assert len(neotoma) == 1, notices
    (notice,) = neotoma
    assert 'nothing was launched' in notice
    if mode == 'ok':
        assert 'failed' not in statuses
        assert saved['status'] == 'blocked'
        assert "Neotoma's admission check did not answer" in saved['blocked_reason']
        assert 'rounds_exhausted checkpoint releases it again' in saved['blocked_reason']
        # Both the checkpoint and the held status were read back.
        assert task_id in task_reads
        assert 'task is held with checkpoint' in notice and 'rounds_exhausted' in notice
        # The exact action: which checkpoint to approve to release it again.
        (brief_id,) = [k for k in briefs if k != 'calls']
        assert f'Approve checkpoint {brief_id} to release the task again.' in notice
    elif mode in UNCONFIRMED_NO_ID_MODES:
        # A store request was sent but no id came back: the checkpoint may
        # exist, so the task is held and the notice says to look for one.
        assert 'failed' not in statuses, statuses
        assert saved['status'] == 'blocked'
        assert [k for k in briefs if k != 'calls'] == []
        assert task_id in notice
        assert 'checkpoint may have been written but could not be confirmed' in notice
        assert f'Look for a checkpoint for task {task_id}' in notice
        assert 'if one exists, approve it to release the task' in notice
        assert 'file a new task with a different title for the same work' in notice
        assert 'No checkpoint' not in notice and 'no checkpoint' not in notice
    elif mode in UNCONFIRMED_MODES:
        # Written, but the read-back failed, raised or did not match: the
        # checkpoint may exist, so the task is held (no independent recovery
        # runs while it may be open), the id is kept, and the notice never
        # says that no checkpoint exists.
        assert 'failed' not in statuses, statuses
        assert saved['status'] == 'blocked'
        (brief_id,) = [k for k in briefs if k != 'calls']
        assert brief_id in saved['blocked_reason']
        assert task_id in notice
        assert f'Checkpoint {brief_id} (rounds_exhausted) was written but could not be confirmed' in notice
        assert f'approve checkpoint {brief_id} to release the task' in notice
        assert 'file a new task with a different title for the same work' in notice
        assert 'No checkpoint' not in notice and 'no checkpoint' not in notice
    else:
        # No checkpoint was created (no store request was sent):
        # no held status at all, one notice naming the task and saying no
        # checkpoint was created, and the failed-dispatch status so the
        # existing recovery handles it.
        assert 'blocked' not in statuses, statuses
        assert saved['status'] == 'failed'
        assert statuses[-1] == 'failed'
        assert task_id in notice and 'No checkpoint was created' in notice
        assert 'task is held' not in notice
        assert not task_reads  # no held status was written to read back


ABSENT_MODES = ('write_absent',)
UNCONFIRMED_NO_ID_MODES = ('write_post_raised',)
UNCONFIRMED_MODES = ('write_unconfirmed', 'readback_fails', 'readback_raises',
                     'readback_mismatch')


@pytest.mark.parametrize('mode,recovers', [('ok', False), ('ok', True),
                                           ('write_absent', False), ('write_post_raised', False),
                                           ('write_unconfirmed', False),
                                           ('readback_fails', False), ('readback_raises', False),
                                           ('readback_mismatch', False)])
def test_neotoma_unanswered_admission_is_a_bounded_deferral_never_failed(
        joined, monkeypatch, tmp_path, mode, recovers):
    # Red before the deferral (observed): the real run_skill returned
    # fly_neotoma_unavailable without fly_delivery_not_launched, the general
    # failure branch wrote status 'failed' and sent "task marked FAILED", and
    # build_job was called once (no retry). Red before the exhaustion
    # escalation (observed): no write_checkpoint_brief call (len(calls) == 0)
    # and the notice carried neither 'is held with checkpoint' nor
    # 'could not be recorded'.
    import fly_runner
    answered = []
    def on_attempt(n):
        joined['calls'].append('admission')
        if recovers and n == 3:
            answered.append(True)
            raise fly_runner.FlyIneligible('a later, real refusal')
    attempts, notices = _fly_admission_seam(monkeypatch, tmp_path, on_attempt)
    briefs, task_reads = _decision_queue(
        monkeypatch, mode, lambda: copy.deepcopy(joined['record']['snapshot']))
    release(joined)
    # The Fly path was really taken; every assertion below depends on it.
    assert attempts, 'run_skill never reached fly_runner.build_job'
    statuses = [w.get('value') for w in joined['writes'] if w.get('field') == 'status']
    # Never FAILED while an attempt can still answer or a checkpoint holds it;
    # without a checkpoint the failed-dispatch status is checked below.
    assert 'failed' not in statuses or (mode != 'ok' and not recovers)
    # One first attempt plus the two bounded retries, each with its own
    # receipt that was reserved for the attempt and released after it.
    assert len(attempts) == 3
    assert all(r == ['owner/repo#12'] for r in attempts), attempts
    assert not delivery._delivery_ledger().delivery_receipts()
    saved = joined['record']['snapshot']
    if recovers:
        # The third attempt was no longer an outage: it is handled as before.
        assert answered and 'Neotoma' not in saved.get('blocked_reason', '')
        assert not briefs.get('calls')
        return
    assert 're-released once Neotoma answers' not in saved.get('blocked_reason', '')
    _assert_exhaustion(mode, briefs, task_reads, notices, joined['task_id'], saved, statuses)


@pytest.mark.parametrize('mode', ['ok', 'write_absent', 'write_post_raised', 'readback_fails', 'readback_raises'])
def test_first_delivery_neotoma_outage_is_bounded_then_escalated(
        monkeypatch, tmp_path, mode, dispatch_env):
    # The natural first-delivery entry: dispatch_task -> run_fly_delivery ->
    # run_skill -> fly_runner.build_job. Red before the deferral (observed for
    # the shared run_skill path): one build_job call and a 'failed' write. Red
    # before the exhaustion escalation (observed): no checkpoint call.
    import test_fly_delivery_dispatch as dispatch
    from lib.daemon_runtime.gating import ExecutionPolicy
    monkeypatch.setenv(delivery.FLY_DELIVERY_REGISTRY_ENV, str(tmp_path / 'receipts.json'))
    _writes, _spawns, store = dispatch_env
    monkeypatch.setenv('APIS_FLY_RUNNER_CLASSES', 'open_or_merge_pr')
    eid = 'ent_first_delivery_task'
    snapshot = dispatch.task_snapshot()
    store.rows[eid] = snapshot.copy()
    statuses = []
    def save(entity_id, state, **kw):
        statuses.append(getattr(state, 'value', state))
        store.status(entity_id, state, **kw)
        if kw.get('result') is not None:
            store.field(entity_id, 'result', kw['result'])
        return True
    monkeypatch.setattr(apis, 'set_task_status', save)
    monkeypatch.setattr(apis, '_spawn_harness_skill', dispatch._REAL_SPAWN)
    monkeypatch.setattr(apis, 'resolve_policy_for_agent', lambda *a: ExecutionPolicy(
        entity_id='inert-policy', loaded=True, low_blast_action_types=frozenset(),
        high_blast_action_types=frozenset({'open_or_merge_pr'})))
    monkeypatch.setattr(apis, 'fetch_task_record', lambda key: dict(
        entity_id=key, entity_type='task', snapshot=copy.deepcopy(store.rows[key])))
    # The pre-retry re-read of a first delivery (unchanged record: it retries).
    monkeypatch.setattr(apis, 'fetch_task_snapshot_and_version',
                        lambda key: (copy.deepcopy(store.rows[key]), 'task-v1'))
    attempts, notices = _fly_admission_seam(monkeypatch, tmp_path, lambda n: None)
    briefs, task_reads = _decision_queue(
        monkeypatch, mode, lambda: copy.deepcopy(store.rows[eid]))
    asyncio.run(apis.dispatch_task(eid, snapshot, 'approved', _StubNotifier(), gate_override=True))
    assert attempts, 'run_skill never reached fly_runner.build_job'
    # One first attempt plus two bounded retries; each reserved its own
    # receipt and released it, and nothing launched or ran locally.
    assert len(attempts) == 3
    assert all(r == ['owner/repo#123'] for r in attempts), attempts
    assert not delivery._delivery_ledger().delivery_receipts()
    saved = store.rows[eid]
    assert 're-released once Neotoma answers' not in saved.get('blocked_reason', '')
    _assert_exhaustion(mode, briefs, task_reads, notices, eid, saved, statuses)



def _first_delivery_dispatch(monkeypatch, tmp_path, dispatch_env, on_attempt, *,
                             retries='2', backoff='10', read=None, build=None,
                             gate_override=True, release_authorization=None,
                             policy=None, queue_mode='ok'):
    """The natural first-delivery entry (as in the test above), with the
    dispatcher's waits recorded instead of slept. *read* replaces the
    pre-retry task re-read (default: the stored row, unchanged).

    Returns a dict: attempts, notices, sleeps, store row, statuses, briefs."""
    import test_fly_delivery_dispatch as dispatch
    from lib.daemon_runtime.gating import ExecutionPolicy
    monkeypatch.setenv(delivery.FLY_DELIVERY_REGISTRY_ENV, str(tmp_path / 'receipts.json'))
    _writes, _spawns, store = dispatch_env
    monkeypatch.setenv('APIS_FLY_RUNNER_CLASSES', 'open_or_merge_pr')
    eid = 'ent_first_delivery_task'
    snapshot = dispatch.task_snapshot()
    store.rows[eid] = snapshot.copy()
    statuses = []
    def save(entity_id, state, **kw):
        statuses.append(getattr(state, 'value', state))
        store.status(entity_id, state, **kw)
        if kw.get('result') is not None:
            store.field(entity_id, 'result', kw['result'])
        return True
    monkeypatch.setattr(apis, 'set_task_status', save)
    monkeypatch.setattr(apis, '_spawn_harness_skill', dispatch._REAL_SPAWN)
    monkeypatch.setattr(apis, 'resolve_policy_for_agent', policy or (lambda *a: ExecutionPolicy(
        entity_id='inert-policy', loaded=True, low_blast_action_types=frozenset(),
        high_blast_action_types=frozenset({'open_or_merge_pr'}))))
    # observation_count stands for the task revision: a test bumps
    # ``store.observations`` to change the revision without changing content.
    monkeypatch.setattr(apis, 'fetch_task_record', lambda key: dict(
        entity_id=key, entity_type='task', observation_count=getattr(store, 'observations', 1),
        snapshot=copy.deepcopy(store.rows[key])))
    reads = []
    def fetch(key):
        reads.append(key)
        if read is not None:
            return read(key, store)
        return copy.deepcopy(store.rows[key]), 'task-v1'
    monkeypatch.setattr(apis, 'fetch_task_snapshot_and_version', fetch)
    attempts, notices = _fly_admission_seam(
        monkeypatch, tmp_path, lambda n: on_attempt(n, store, eid),
        retries=retries, backoff=backoff, build=build)
    briefs, _task_reads = _decision_queue(monkeypatch, queue_mode, lambda: copy.deepcopy(store.rows[eid]))
    sleeps = []
    real_sleep = asyncio.sleep
    async def record_sleep(delay, *a, **kw):
        sleeps.append(delay)
        await real_sleep(0)
    monkeypatch.setattr(apis.asyncio, 'sleep', record_sleep)
    authorization = release_authorization(eid, snapshot) if release_authorization else None
    asyncio.run(apis.dispatch_task(eid, snapshot, 'approved', _StubNotifier(),
                                   gate_override=gate_override,
                                   release_authorization=authorization))
    assert attempts, 'run_skill never reached fly_runner.build_job'
    return dict(attempts=attempts, notices=notices, sleeps=sleeps, saved=store.rows[eid],
                statuses=statuses, briefs=briefs, reads=reads, eid=eid)


def _assert_failed_dispatch_not_held(run):
    """A refused retry writes no checkpoint and no held status: one notice and
    the failed-dispatch status, left to the existing recovery."""
    assert 'blocked' not in run['statuses'], run['statuses']
    assert run['statuses'][-1] == 'failed', run['statuses']
    assert run['saved']['status'] == 'failed'
    assert not run['briefs'].get('calls')  # no checkpoint, so nothing to approve
    assert 'failed-dispatch recovery' in run['saved']['blocked_reason']
    assert len([n for n in run['notices'] if 'retry not started' in n]) == 1, run['notices']


def _stated_then_refused(stated):
    """Attempt 1: a 429 stating *stated*; attempt 2: a real refusal (ends it)."""
    import fly_runner
    def on_attempt(n, store, eid):
        if n == 1:
            return stated
        raise fly_runner.FlyIneligible('a later, real refusal')
    return on_attempt


def test_a_stated_retry_after_in_seconds_is_waited_out(monkeypatch, tmp_path, dispatch_env):
    # Red before (observed): the dispatcher slept the configured 10s only,
    # sleeps == [10.0], under a 429 that said Retry-After: 25.
    import fly_runner
    stated = fly_runner.parse_retry_after('25', now=0.0)
    run = _first_delivery_dispatch(monkeypatch, tmp_path, dispatch_env, _stated_then_refused(stated))
    assert len(run['attempts']) == 2
    assert run['sleeps'] == [25.0]


def test_a_stated_retry_after_http_date_is_waited_out(monkeypatch, tmp_path, dispatch_env):
    # Red before (observed): sleeps == [10.0] under a Retry-After date ~28s out.
    import email.utils
    import time
    import fly_runner
    stated = fly_runner.parse_retry_after(
        email.utils.formatdate(time.time() + 28, usegmt=True), now=time.time())
    assert stated is not None and 20 < stated <= 30  # inside the 30s ceiling
    run = _first_delivery_dispatch(monkeypatch, tmp_path, dispatch_env, _stated_then_refused(stated))
    assert len(run['attempts']) == 2
    (waited,) = run['sleeps']
    assert waited >= stated and waited > 10


def test_no_stated_retry_after_keeps_the_configured_delay(monkeypatch, tmp_path, dispatch_env):
    run = _first_delivery_dispatch(monkeypatch, tmp_path, dispatch_env, _stated_then_refused(None))
    assert len(run['attempts']) == 2
    assert run['sleeps'] == [10.0]


def test_a_stated_wait_past_the_ceiling_ends_as_exhaustion(monkeypatch, tmp_path, dispatch_env):
    # Ceiling for 2 retries at 10s: 10 + 20 = 30s. Neotoma asks for 1000s.
    # Red before (observed): the dispatcher slept 10s and attempted again
    # (len(attempts) == 3, sleeps == [10.0, 20.0]).
    run = _first_delivery_dispatch(monkeypatch, tmp_path, dispatch_env,
                                   lambda n, store, eid: 1000.0)
    assert len(run['attempts']) == 1
    assert run['sleeps'] == []
    saved = run['saved']
    assert 'failed' not in run['statuses']
    assert saved['status'] == 'blocked'
    assert 'asked to wait 1000s, past the retry' in saved['blocked_reason']
    calls = run['briefs'].get('calls', [])
    assert len(calls) == 1 and calls[0]['decision'].reason == 'rounds_exhausted'
    (brief_id,) = [k for k in run['briefs'] if k != 'calls']
    (notice,) = [n for n in run['notices'] if "admission check did not answer" in n]
    assert f'Approve checkpoint {brief_id} to release the task again.' in notice


def test_a_task_changed_between_attempts_is_not_launched_again(monkeypatch, tmp_path, dispatch_env):
    # Red before (observed): attempt 2 launched under the stale decision
    # (len(attempts) == 3, the changed title never re-read).
    def on_attempt(n, store, eid):
        if n == 1:
            store.rows[eid]['title'] = 'a different task now'
        return None
    run = _first_delivery_dispatch(monkeypatch, tmp_path, dispatch_env, on_attempt)
    assert len(run['attempts']) == 1
    assert run['reads'] == [run['eid']]
    saved = run['saved']
    _assert_failed_dispatch_not_held(run)
    assert 'changed since it was dispatched' in saved['blocked_reason']
    assert not run['briefs'].get('calls')  # not an exhaustion: no rounds_exhausted
    assert any('retry not started' in n and 'changed since' in n for n in run['notices'])


def test_a_failed_task_read_launches_nothing_and_is_not_held(monkeypatch, tmp_path, dispatch_env):
    # Red before (observed): no re-read at all; len(attempts) == 3. Red before
    # round 5 (observed): statuses ended 'blocked' with no checkpoint written.
    def read(key, store):
        raise httpx.ConnectError('Neotoma went away')
    run = _first_delivery_dispatch(monkeypatch, tmp_path, dispatch_env,
                                   lambda n, store, eid: None, read=read)
    assert len(run['attempts']) == 1
    saved = run['saved']
    _assert_failed_dispatch_not_held(run)
    assert 'could not be re-read before the retry (ConnectError)' in saved['blocked_reason']


def test_a_task_no_longer_executing_is_not_launched_or_overwritten(
        monkeypatch, tmp_path, dispatch_env):
    def on_attempt(n, store, eid):
        if n == 1:
            store.rows[eid]['status'] = 'cancelled'
        return None
    run = _first_delivery_dispatch(monkeypatch, tmp_path, dispatch_env, on_attempt)
    assert len(run['attempts']) == 1
    assert run['saved']['status'] == 'cancelled'  # not written over
    assert any('no longer in its admitted executing state' in n for n in run['notices'])


def test_an_unchanged_task_is_launched_again(monkeypatch, tmp_path, dispatch_env):
    run = _first_delivery_dispatch(monkeypatch, tmp_path, dispatch_env, _stated_then_refused(None))
    assert len(run['attempts']) == 2
    assert run['reads'] == [run['eid']]


# ── Round 3: bounded settings, the delivery effect, and retry authority ──

@pytest.mark.parametrize('value', ['inf', 'nan', '0', '-1', 'x'])
def test_a_non_finite_or_non_positive_backoff_uses_the_default(monkeypatch, caplog, value):
    # Red before (observed): 'inf' -> backoff inf, 'nan', '0' and '-1' -> 0.0, and
    # no setting name was logged for any value.
    monkeypatch.setenv('APIS_NEOTOMA_ADMISSION_BACKOFF_S', value)
    # A retry count of 0 is valid (no retries); any other of these is not.
    monkeypatch.setenv('APIS_NEOTOMA_ADMISSION_RETRIES', '1.5' if value == '0' else value)
    caplog.set_level('WARNING')
    retries, backoff = apis._neotoma_admission_retry_bounds()
    assert backoff == apis.NEOTOMA_ADMISSION_BACKOFF_S_DEFAULT
    assert retries == apis.NEOTOMA_ADMISSION_RETRIES_DEFAULT
    logged = caplog.text
    assert 'APIS_NEOTOMA_ADMISSION_BACKOFF_S' in logged
    assert 'APIS_NEOTOMA_ADMISSION_RETRIES' in logged
    # The setting's name, never its value.
    assert f'{value!r}' not in logged and f' {value} ' not in logged


def test_valid_bounds_are_kept(monkeypatch):
    monkeypatch.setenv('APIS_NEOTOMA_ADMISSION_BACKOFF_S', '7.5')
    monkeypatch.setenv('APIS_NEOTOMA_ADMISSION_RETRIES', '0')
    assert apis._neotoma_admission_retry_bounds() == (0, 7.5)


def test_an_infinite_backoff_still_ends_in_one_rounds_exhausted_checkpoint(
        monkeypatch, tmp_path, dispatch_env):
    # Red before: the old bounds returned backoff inf for 'inf' (observed in
    # the test above), so the first wait was asyncio.sleep(inf) and the
    # rounds_exhausted checkpoint was never reached.
    run = _first_delivery_dispatch(monkeypatch, tmp_path, dispatch_env,
                                   lambda n, store, eid: None, backoff='inf')
    default = apis.NEOTOMA_ADMISSION_BACKOFF_S_DEFAULT
    assert run['sleeps'] == [default, 2 * default]
    assert all(math.isfinite(s) for s in run['sleeps'])
    assert len(run['attempts']) == 3
    calls = run['briefs'].get('calls', [])
    assert len(calls) == 1 and calls[0]['decision'].reason == 'rounds_exhausted'
    assert run['saved']['status'] == 'blocked'


def _admitted_job(kw):
    """What fly_runner.build_job returns once Neotoma answers: the delivery job."""
    import fly_runner
    return fly_runner.FlyJob(
        run_id='run-123', role=kw['role'], provider=kw['provider'],
        repository=kw['repository'], argv=list(kw['cmd']), stdin=kw['stdin'],
        timeout=int(kw['timeout']), needs_github=False,
        github_delivery_contract=bool(kw.get('github_delivery_contract')),
        delivery=kw['delivery'])


def test_unanswered_then_admitted_delivers_once_and_completes(
        monkeypatch, tmp_path, dispatch_env):
    # The delivery effect: attempt 1 is deferred (Neotoma did not answer),
    # attempt 2 is admitted and the launch runs once with the delivery spec and
    # opens the PR. Red with the retry removed (observed, retries=0): one
    # build_job call (len(attempts) == 1) and no launch.
    import fly_runner
    launches = []
    class Executor:
        async def run(self, job):
            launches.append(job)
            job.launch_requested = True
            job.delivery_result = dict(
                delivery_status='opened', delivery_branch=job.delivery['branch'],
                delivery_commit='a' * 40, pr_number=99,
                pr_url='https://github.com/owner/repo/pull/99')
            return 0, b'', b''
    monkeypatch.setattr(fly_runner, 'get_executor', lambda: Executor())
    # The real pull request as GitHub returns it to the runner's read-back.
    from lib import github_app_token
    from lib.daemon_runtime import artifact_resolver
    def view(repo, number, token=None):
        return 0, json.dumps(dict(
            number=99, baseRefName='main', headRefName=launches[0].delivery['branch'],
            headRefOid='a' * 40, headRepository={'nameWithOwner': 'owner/repo'})), ''
    monkeypatch.setattr(github_app_token, 'identity_token', lambda identity, repo=None: 'app-token')
    monkeypatch.setattr(artifact_resolver, '_gh_pr_view', view)
    monkeypatch.setattr(artifact_resolver.time, 'sleep', lambda s: None)
    monkeypatch.setattr(apis, 'resolve_artifact_ref', lambda *a, **kw: 'exists')
    monkeypatch.setattr('lib.daemon_runtime.task_lifecycle._correct', dispatch_env[2].field)
    run = _first_delivery_dispatch(
        monkeypatch, tmp_path, dispatch_env, lambda n, store, eid: None,
        build=lambda n, kw: _admitted_job(kw) if n == 2 else None)
    assert len(run['attempts']) == 2
    assert len(launches) == 1
    (job,) = launches
    assert set(job.delivery) == set(fly_runner.DELIVERY_SPEC_KEYS)
    saved = run['saved']
    assert 'failed' not in run['statuses'] and 'blocked' not in run['statuses'], (
        saved.get('blocked_reason'), run['notices'])
    assert saved['status'] == 'done', saved
    assert 'https://github.com/owner/repo/pull/99' in saved['result']
    assert not run['briefs'].get('calls')


def _policy(revision):
    from lib.daemon_runtime.gating import ExecutionPolicy
    return ExecutionPolicy(
        entity_id='inert-policy', loaded=True, low_blast_action_types=frozenset(),
        high_blast_action_types=frozenset({'open_or_merge_pr'}),
        authorization_revision=revision)


def _authorization(eid, snapshot):
    """The authenticated release authorization an approved checkpoint carries,
    bound to policy revision rev-1 and the gate decision for the task."""
    from lib.daemon_runtime.gating import execution_policy_revision
    _skill, action_type, policy, decision = apis._task_gate_context(snapshot)
    return dict(task_entity_id=eid, user_id='inert-tenant', action_type=action_type,
                policy_entity_id=policy.entity_id,
                policy_revision=execution_policy_revision(_policy('rev-1')),
                gate_action=decision.action.value, blast_radius=decision.blast_radius.value)


def _switchable_policy():
    box = {'revision': 'rev-1'}
    return box, (lambda *a: _policy(box['revision']))


def test_an_approved_release_unchanged_between_attempts_is_launched_again(
        monkeypatch, tmp_path, dispatch_env):
    # Positive control for the two tests below: the authority still holds.
    _box, policy = _switchable_policy()
    run = _first_delivery_dispatch(monkeypatch, tmp_path, dispatch_env,
                                   _stated_then_refused(None), policy=policy,
                                   release_authorization=_authorization)
    assert len(run['attempts']) == 2


def test_a_policy_revision_change_between_attempts_launches_nothing_and_is_not_held(
        monkeypatch, tmp_path, dispatch_env):
    # Same policy entity, same action type and blast tier; only its revision
    # changed. Red with the authority check removed, leaving the earlier
    # snapshot comparison (observed): every retry launched (len(attempts) == 3)
    # and no hold was written.
    box, policy = _switchable_policy()
    def on_attempt(n, store, eid):
        if n == 1:
            box['revision'] = 'rev-2'
        return None
    run = _first_delivery_dispatch(monkeypatch, tmp_path, dispatch_env, on_attempt,
                                   policy=policy, release_authorization=_authorization)
    assert len(run['attempts']) == 1
    saved = run['saved']
    _assert_failed_dispatch_not_held(run)
    assert 'execution policy changed since the first attempt' in saved['blocked_reason']
    assert any('retry not started' in n and 'execution policy changed' in n
               for n in run['notices'])


def test_an_approved_release_whose_task_revision_changed_is_not_relaunched(
        monkeypatch, tmp_path, dispatch_env):
    # The task's content is unchanged (so the snapshot comparison passes) but
    # its revision moved. Red with the authority check removed (observed):
    # every retry launched (len(attempts) == 3).
    _box, policy = _switchable_policy()
    def on_attempt(n, store, eid):
        if n == 1:
            store.observations = 2
        return None
    run = _first_delivery_dispatch(monkeypatch, tmp_path, dispatch_env, on_attempt,
                                   policy=policy, release_authorization=_authorization)
    assert len(run['attempts']) == 1
    saved = run['saved']
    _assert_failed_dispatch_not_held(run)
    assert 'authority it was released under changed' in saved['blocked_reason']
    assert any('retry not started' in n for n in run['notices'])


@pytest.mark.parametrize('failure', ['unloaded', 'raises'])
def test_a_failed_policy_read_between_attempts_launches_nothing_and_is_not_held(
        monkeypatch, tmp_path, dispatch_env, failure):
    # Red before round 5 (observed): the retry branch wrote 'blocked' with no
    # checkpoint behind it.
    from lib.daemon_runtime.gating import ExecutionPolicy
    box = {'fail': False}
    def policy(*a):
        if not box['fail']:
            return _policy('rev-1')
        if failure == 'raises':
            raise httpx.ConnectError('Neotoma went away')
        return ExecutionPolicy(entity_id='inert-policy', loaded=False)
    def on_attempt(n, store, eid):
        if n == 1:
            box['fail'] = True
        return None
    run = _first_delivery_dispatch(monkeypatch, tmp_path, dispatch_env, on_attempt,
                                   policy=policy, release_authorization=_authorization)
    assert len(run['attempts']) == 1
    _assert_failed_dispatch_not_held(run)
    assert 'could not be re-read before the retry' in run['saved']['blocked_reason'] or \
        'could not be read before the retry' in run['saved']['blocked_reason']


# ── Round 4: the whole schedule is bounded; the checkpoint is the hold ──

def _bounds_with(monkeypatch, caplog, retries, backoff):
    monkeypatch.setenv('APIS_NEOTOMA_ADMISSION_RETRIES', retries)
    monkeypatch.setenv('APIS_NEOTOMA_ADMISSION_BACKOFF_S', backoff)
    caplog.set_level('WARNING')
    return apis._neotoma_admission_retry_bounds(), caplog.text


DEFAULT_BOUNDS = (apis.NEOTOMA_ADMISSION_RETRIES_DEFAULT, apis.NEOTOMA_ADMISSION_BACKOFF_S_DEFAULT)


def test_a_backoff_of_1e308_falls_back_to_the_defaults(monkeypatch, caplog):
    # Red before (observed): (3, 1e308); its schedule total overflowed to inf.
    bounds, logged = _bounds_with(monkeypatch, caplog, '3', '1e308')
    assert bounds == DEFAULT_BOUNDS
    assert 'APIS_NEOTOMA_ADMISSION_BACKOFF_S' in logged and '1e308' not in logged
    assert math.isfinite(apis._neotoma_admission_wait_ceiling(*bounds))


def test_a_retry_count_of_1000_falls_back_to_the_defaults(monkeypatch, caplog):
    # Red before (observed): (1000, 120.0).
    bounds, logged = _bounds_with(monkeypatch, caplog, '1000', '120')
    assert bounds == DEFAULT_BOUNDS
    assert 'APIS_NEOTOMA_ADMISSION_RETRIES' in logged and '1000' not in logged


def test_settings_each_inside_their_cap_but_too_long_in_total_fall_back(monkeypatch, caplog):
    # 3600s x (1+..+10) = 198000s, past the 6h total. Red before (observed):
    # AttributeError, no caps existed; the old bounds returned (10, 3600.0).
    assert apis.NEOTOMA_ADMISSION_BACKOFF_S_MAX >= 3600
    assert apis.NEOTOMA_ADMISSION_RETRIES_MAX >= 10
    bounds, logged = _bounds_with(monkeypatch, caplog, '10', '3600')
    assert bounds == DEFAULT_BOUNDS
    assert 'APIS_NEOTOMA_ADMISSION_RETRIES' in logged
    assert 'APIS_NEOTOMA_ADMISSION_BACKOFF_S' in logged
    assert '198000' not in logged  # names, never the values or the total


def test_the_largest_allowed_schedule_is_kept_and_finite(monkeypatch, caplog):
    # 392s x 55 = 21560s, just inside the 6h total. Red before: AttributeError.
    bounds, _logged = _bounds_with(monkeypatch, caplog, '10', '392')
    assert bounds == (10, 392.0)
    assert apis._neotoma_admission_wait_ceiling(*bounds) <= apis.NEOTOMA_ADMISSION_TOTAL_WAIT_S_MAX


def test_an_overflowing_schedule_runs_the_defaults_to_one_rounds_exhausted_checkpoint(
        monkeypatch, tmp_path, dispatch_env):
    # Red before (observed): backoff 1e308 was kept and the recorded sleeps
    # were 1e308 and then inf; with the real asyncio.sleep the dispatch would
    # never have reached its checkpoint.
    run = _first_delivery_dispatch(monkeypatch, tmp_path, dispatch_env,
                                   lambda n, store, eid: None, retries='3', backoff='1e308')
    default = apis.NEOTOMA_ADMISSION_BACKOFF_S_DEFAULT
    assert run['sleeps'] == [default, 2 * default, 3 * default]
    assert all(math.isfinite(s) for s in run['sleeps'])
    assert len(run['attempts']) == apis.NEOTOMA_ADMISSION_RETRIES_DEFAULT + 1
    calls = run['briefs'].get('calls', [])
    assert len(calls) == 1 and calls[0]['decision'].reason == 'rounds_exhausted'
    assert run['saved']['status'] == 'blocked'


def test_a_non_finite_stated_wait_is_never_slept(monkeypatch, tmp_path, dispatch_env):
    # A Retry-After that is not finite never reaches asyncio.sleep. A guard:
    # green before too (inf exceeded the old finite ceiling).
    run = _first_delivery_dispatch(monkeypatch, tmp_path, dispatch_env,
                                   lambda n, store, eid: math.inf)
    assert all(math.isfinite(s) for s in run['sleeps'])
    calls = run['briefs'].get('calls', [])
    assert len(calls) == 1 and calls[0]['decision'].reason == 'rounds_exhausted'


@pytest.mark.parametrize('digits', [11, 400])
def test_a_stated_delta_too_long_for_the_schedule_ends_without_a_retry(
        monkeypatch, tmp_path, dispatch_env, digits):
    # Red before round 5 (observed): an 11-digit or 400-digit Retry-After parsed
    # to None, so the configured 10s backoff applied and every retry ran
    # (len(attempts) == 3, sleeps == [10.0, 20.0]).
    import fly_runner
    stated = fly_runner.parse_retry_after('9' * digits, now=0.0)
    run = _first_delivery_dispatch(monkeypatch, tmp_path, dispatch_env,
                                   lambda n, store, eid: stated)
    assert len(run['attempts']) == 1
    assert run['sleeps'] == []
    calls = run['briefs'].get('calls', [])
    assert len(calls) == 1 and calls[0]['decision'].reason == 'rounds_exhausted'
    assert 'past the retry schedule' in run['saved']['blocked_reason']


@pytest.mark.parametrize('queue_mode', ABSENT_MODES)
def test_no_checkpoint_means_no_hold(monkeypatch, tmp_path, dispatch_env, queue_mode):
    # Red before (observed): BLOCKED was written before the checkpoint call, so
    # the task ended 'blocked' with nothing to approve, and no notice said that
    # no checkpoint was created.
    run = _first_delivery_dispatch(monkeypatch, tmp_path, dispatch_env,
                                   lambda n, store, eid: None, queue_mode=queue_mode)
    assert 'blocked' not in run['statuses'], run['statuses']
    assert run['statuses'][-1] == 'failed'
    assert run['saved']['status'] == 'failed'
    calls = run['briefs'].get('calls', [])
    assert len(calls) == 1, calls  # no second checkpoint in the same dispatch
    notices = [n for n in run['notices'] if 'No checkpoint was created' in n]
    assert len(notices) == 1, run['notices']
    assert run['eid'] in notices[0]


@pytest.mark.parametrize('queue_mode', UNCONFIRMED_MODES)
def test_an_unconfirmed_checkpoint_holds_and_is_never_reported_absent(
        monkeypatch, tmp_path, dispatch_env, queue_mode):
    # Red before (observed): a checkpoint written with an id whose read-back
    # failed, raised or mismatched was reported as "No checkpoint exists" and
    # the task was set failed for independent recovery while it may be open.
    run = _first_delivery_dispatch(monkeypatch, tmp_path, dispatch_env,
                                   lambda n, store, eid: None, queue_mode=queue_mode)
    assert 'failed' not in run['statuses'], run['statuses']
    assert run['saved']['status'] == 'blocked'
    calls = run['briefs'].get('calls', [])
    assert len(calls) == 1, calls  # no second checkpoint in the same dispatch
    (brief_id,) = [k for k in run['briefs'] if k != 'calls']
    assert not any('No checkpoint' in n for n in run['notices']), run['notices']
    notices = [n for n in run['notices'] if 'could not be confirmed' in n]
    assert len(notices) == 1, run['notices']
    assert run['eid'] in notices[0] and brief_id in notices[0]
    assert 'file a new task with a different title' in notices[0]


def test_an_unconfirmed_checkpoint_with_no_id_holds_and_names_the_task(
        monkeypatch, tmp_path, dispatch_env):
    # Red before (observed): the store request raised after it was sent, the
    # writer returned no id, and the task was set failed with "No checkpoint
    # was created" while a checkpoint may have landed.
    run = _first_delivery_dispatch(monkeypatch, tmp_path, dispatch_env,
                                   lambda n, store, eid: None,
                                   queue_mode='write_post_raised')
    assert 'failed' not in run['statuses'], run['statuses']
    assert run['saved']['status'] == 'blocked'
    calls = run['briefs'].get('calls', [])
    assert len(calls) == 1, calls  # one checkpoint attempt, never a second
    assert not any('No checkpoint' in n or 'no checkpoint' in n
                   for n in run['notices']), run['notices']
    notices = [n for n in run['notices'] if 'could not be confirmed' in n]
    assert len(notices) == 1, run['notices']
    assert f"Look for a checkpoint for task {run['eid']}" in notices[0]
    assert 'if one exists, approve it to release the task' in notices[0]
    assert 'file a new task with a different title' in notices[0]
