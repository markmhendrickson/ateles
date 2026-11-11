"""Executable synthetic protocol demonstrations, not model-compliance proof.

A cold evaluator receives SKILL.md + fixture + sandbox without this scripted
walk. These tests establish that the fixture exposes the cases the skill must
handle, and show the observable outcomes the skill requires. Each case also
runs the naive variant the skill rules out, to show the fixture can tell them
apart; a fixture where both pass would prove nothing.
"""
from coordinate_sessions_sandbox import CoordinateSessionsSandbox as Sandbox

CLAUDE_SOURCES = ['claude-registry', 'claude-transcript-search', 'claude-transcript-files']
ALL_SOURCES = CLAUDE_SOURCES + ['codex-threads', 'cursor-composers']


def discover(s, sources):
    """Union discovery: reconcile by session id and keep per-source coverage."""
    found, coverage = set(), {}
    for src in sources:
        page = s.call('list_source', source=src)
        found |= set(page['sessions'])
        coverage[src] = {'covered': page['covered'], 'reason': page['reason'],
                         'count': len(page['sessions'])}
    return found, coverage


def ascents(s, task_id, seen=()):
    """Every plan reachable through valid PART_OF chains (task -> task -> plan)."""
    plans = s.data['plans']
    out = []
    for parent in s.call('retrieve_task', id=task_id)['part_of']:
        if parent in plans:
            out.append(parent)
        elif parent in s.tasks and parent not in seen:
            out += ascents(s, parent, seen + (task_id,))
    return out


def bind(s, task_id):
    """Check ascent; repair only from an authoritative locator, else propose."""
    found = ascents(s, task_id)
    if len(found) == 1:
        return {'bound': found[0]}
    row = s.call('retrieve_task', id=task_id)
    if not found and row.get('locator'):
        s.call('link_part_of', id=task_id, parent=row['locator'])
        assert ascents(s, task_id) == [row['locator']]  # read back
        return {'repaired': row['locator']}
    s.call('message_coordinator', kind='unresolved_ancestry', task=task_id,
           candidates=row.get('candidates', []))
    return {'proposed': task_id}


def recover_archived(s, session_ids, window_start='2026-10-28'):
    """Step 1a: recover remaining dated work from recently archived sessions."""
    recovered = {}
    for sid in sorted(session_ids):
        row = s.call('read_session', id=sid)
        if not row['archived'] or row.get('archived_at', '') < window_start:
            continue
        work = s.call('dated_work', id=sid)
        if not work:
            continue
        hits = s.call('find_tasks', text=work['title'])['ids']
        tid = hits[0] if hits else s.call(
            'create_task', title=work['title'], due=work['due'], part_of=[work['plan']])['id']
        assert ascents(s, tid) == [work['plan']]  # read back
        recovered[sid] = tid
    return recovered


def live_plans_without_session(s, open_sessions, changed_since='2026-10-12', due_by='2026-12-11'):
    """Step 1b: live plans with no open session, and near-dated stuck tasks for the priority list."""
    report, priority = [], []
    for plan in s.call('list_plans')['plans']:
        if plan['status'] not in ('active', 'in_progress'):
            continue
        tasks = s.call('plan_tasks', plan=plan['id'])['tasks']
        dated = [t for t in tasks if t.get('due') and t['due'] <= due_by]
        if plan['changed'] < changed_since and not dated:
            continue
        if set(plan['sessions']) & open_sessions:
            continue
        report.append(plan['id'])
        priority += [t['id'] for t in dated
                     if t['status'] == 'failed' or not t.get('owner')]
    return report, priority


def eligible(s, sid, shared_passes=False):
    """Step 6 archive eligibility. `shared_passes` is the rule the skill replaced."""
    ev = s.call('archive_evidence', id=sid)
    if not (ev.get('approved') and ev.get('capture') and ev.get('task')):
        return False
    row = s.call('read_session', id=sid)
    co = s.call('read_checkout', checkout=row['checkout'])
    if row['checkout'] == 'shared':
        if shared_passes:
            return True
        if row['touched'] is None:
            return False  # ownership cannot be told apart: hold
        return not (set(row['touched']) & set(co['dirty'])) and co['ahead'] == 0
    return not co['dirty'] and co['ahead'] == 0


def effects(s, method):
    return [e['params'] for e in s.events if e['method'] == method]


def test_union_discovery_finds_aged_out_session_and_reports_gaps():
    s = Sandbox()
    found, coverage = discover(s, ALL_SOURCES)
    assert 'c-aged' in found
    assert coverage['claude-registry']['covered'] == 'partial'
    assert coverage['cursor-composers']['covered'] == 'none'
    assert coverage['cursor-composers']['reason']
    # Discovery is read-only and messages no session.
    assert not [e for e in s.events if e['method'] != 'list_source']


def test_registry_only_discovery_misses_the_aged_out_session():
    s = Sandbox()
    found, _ = discover(s, ['claude-registry'])
    assert 'c-aged' not in found


def test_archived_dated_work_is_recovered_with_one_ascent():
    s = Sandbox()
    found, _ = discover(s, ALL_SOURCES)
    recovered = recover_archived(s, found)
    assert list(recovered) == ['x-archived-dated']
    tid = recovered['x-archived-dated']
    assert s.tasks[tid]['due'] == '2026-11-20'
    assert ascents(s, tid) == ['plan-filing']
    # A finished archived session yields nothing, and nothing is unarchived.
    assert not effects(s, 'archive_session')


def test_recovery_reuses_the_recovered_task_on_a_rerun():
    s = Sandbox()
    found, _ = discover(s, ALL_SOURCES)
    first = recover_archived(s, found)
    second = recover_archived(s, found)
    assert second == first
    assert len(effects(s, 'create_task')) == 1


def test_live_plan_without_open_session_is_reported_with_dated_task():
    s = Sandbox()
    found, _ = discover(s, ALL_SOURCES)
    open_sessions = {sid for sid in found if not s.call('read_session', id=sid)['archived']}
    report, priority = live_plans_without_session(s, open_sessions)
    # plan-filing's only session is archived; plan-hiring has none. plan-platform has
    # an open session, plan-old is stale with no near date, plan-closed is not live.
    assert sorted(report) == ['plan-filing', 'plan-hiring']
    assert priority == ['task-hiring-offer']
    assert not [e for e in s.events if e['method'] not in
                {'list_source', 'read_session', 'list_plans', 'plan_tasks'}]


def test_plan_sweep_that_trusts_any_mapped_session_misses_the_archived_one():
    s = Sandbox()
    report, _ = live_plans_without_session(s, set(s.data['sessions']))
    assert 'plan-filing' not in report


def test_parent_task_chain_counts_as_one_ascent_and_is_not_flattened():
    s = Sandbox()
    assert bind(s, 'task-chain-child') == {'bound': 'plan-platform'}
    assert s.tasks['task-chain-child']['part_of'] == ['task-chain-parent']
    assert not effects(s, 'link_part_of')


def test_orphan_with_locator_is_repaired_and_read_back():
    s = Sandbox()
    assert bind(s, 'task-orphan-located') == {'repaired': 'plan-platform'}
    assert effects(s, 'link_part_of') == [{'id': 'task-orphan-located', 'parent': 'plan-platform'}]


def test_orphan_without_locator_is_proposed_not_linked():
    s = Sandbox()
    assert bind(s, 'task-orphan-unlocated') == {'proposed': 'task-orphan-unlocated'}
    assert not effects(s, 'link_part_of')
    assert s.tasks['task-orphan-unlocated']['part_of'] == []
    assert effects(s, 'message_coordinator')[0]['kind'] == 'unresolved_ancestry'


def test_eligibility_refuses_each_missing_condition():
    s = Sandbox()
    for sid in ['c-dirty-shared', 'c-unknown-shared', 'c-unapproved', 'c-no-capture', 'c-no-task']:
        assert eligible(s, sid) is False, sid


def test_old_shared_checkout_rule_would_wrongly_pass_dirty_sessions():
    s = Sandbox()
    assert eligible(s, 'c-dirty-shared', shared_passes=True) is True
    assert eligible(s, 'c-unknown-shared', shared_passes=True) is True


def test_clean_approved_session_passes_and_is_archived_once():
    s = Sandbox()
    assert eligible(s, 'c-clean') is True
    s.call('archive_session', id='c-clean')
    assert s.call('read_session', id='c-clean')['archived'] is True
    assert effects(s, 'archive_session') == [{'id': 'c-clean'}]
    # Checkout reads were read-only: no checkout-mutating method exists in the trace.
    assert all(e['method'] in {'archive_evidence', 'read_session', 'read_checkout', 'archive_session'}
               for e in s.events)
