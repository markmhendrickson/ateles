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
from lib.daemon_runtime import gating as gating_module
from lib.daemon_runtime.gating import (
    ExecutionPolicy, build_checkpoint_authorization_envelope, evaluate_gate,
)
from test_existing_pr_owner_reader import normal as normal, admitted_reader as admitted_reader
from test_fly_run_job_delivery import world as world, existing_correction as existing_correction
from test_fly_delivery_dispatch import _StubNotifier


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

    def take_release(entity_id, *, kind, owner_token, taken_at, expected_version):
        # The release owner shares the stamp's version precondition; this inert
        # store keeps the version so the stamp's own expectation is unchanged.
        assert entity_id == checkpoint_id
        if expected_version != checkpoint['entity_version']:
            return None
        checkpoint['snapshot']['release_owner'] = (
            gating_module.encode_checkpoint_release_owner(kind, taken_at, owner_token))
        return checkpoint['entity_version']

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
    monkeypatch.setattr(apis, 'take_checkpoint_release', take_release)
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
