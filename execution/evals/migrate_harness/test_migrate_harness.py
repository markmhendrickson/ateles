"""Executable synthetic protocol demonstrations, not model-compliance proof.

These establish that the fixture exposes each case the migrate-harness skill's
section 2 check and its close-out and retirement steps must handle, and show the
observable outcome the skill requires. Each case also runs the naive variant the
skill rules out, to show the fixture can tell them apart; a fixture where both
pass would prove nothing. The check functions below encode the skill's pinned
field names and counting rules.
"""
import re


from migrate_harness_sandbox import MigrateHarnessSandbox as Sandbox
from lib.daemon_runtime.task_ownership import SENTINEL_ASSIGNEES

TERMINAL = {'done', 'completed', 'complete', 'cancelled', 'canceled', 'closed', 'dropped',
            'declined', 'superseded'}
ACTIVE = {'in_progress', 'executing'}
SKILL_SENTINELS = {'unassigned', 'none', 'nobody', 'tbd', 'n/a', '-'}
NOTE = re.compile(r'(?m)^2026-11-23 migrate-harness:')
CAP = 2000


def is_open(task):
    return task['status'].strip().lower() not in TERMINAL


def has_owner(task):
    for field in ('assigned_to', 'executor', 'owner'):
        v = (task.get(field) or '').strip().lower()
        if v and v not in SKILL_SENTINELS:
            return True
    return False


def ascent(s, task_id, seen=()):
    """Return (plans reached, problems). One PART_OF edge per hop; chains count once."""
    if task_id in seen:
        return [], ['cycle']
    edges = s.call('retrieve_task', id=task_id)['part_of']
    if len(edges) == 0:
        return [], ['missing_ascent']
    if len(edges) > 1:
        return [], ['duplicate_ascent']
    parent = edges[0]
    if parent in s.plans:
        return [parent], []
    if parent in s.tasks:
        return ascent(s, parent, seen + (task_id,))
    return [], ['missing_ascent']


def plan_reaches_linked_project(s, plan_id):
    edges = s.call('retrieve_plan', id=plan_id)['part_of']
    if len(edges) != 1 or edges[0] not in s.projects:
        return False
    project = s.call('retrieve_project', id=edges[0])
    return (not project['customer_org_expected']) or bool(project['refers_to'])


def section2(s, lead_id):
    """The skill's section 2 table: {check: (n_checked, [offenders])}."""
    lead = s.call('retrieve_lead', id=lead_id)
    open_tasks = [t for t in lead['tasks'] if is_open(s.tasks[t])]
    result = {k: (0, []) for k in ('one_ascent', 'owner', 'cap', 'status_note', 'old_side')}
    bad = []
    for t in open_tasks:
        plans, problems = ascent(s, t)
        if problems or len(plans) != 1 or not plan_reaches_linked_project(s, plans[0]):
            bad.append(t)
    result['one_ascent'] = (len(open_tasks), bad)
    active = [t for t in open_tasks if s.tasks[t]['status'].strip().lower() in ACTIVE]
    result['owner'] = (len(active), [t for t in active if not has_owner(s.tasks[t])])
    result['cap'] = (len(lead['plans']),
                     [p for p in lead['plans'] if len(s.plans[p]['next_steps']) >= CAP])
    result['status_note'] = (len(open_tasks),
                             [t for t in open_tasks if not NOTE.search(s.tasks[t].get('notes') or '')])
    old = s.call('read_old_side', session=lead['session'])
    paused = (old['goal'] or {}).get('status') == 'paused' and \
        all(w['status'] == 'paused' for w in old['watchers'])
    result['old_side'] = (1, [] if paused else [lead['session']])
    return result


def failures(result):
    return {k: v[1] for k, v in result.items() if v[1]}


def close_out_old_side(s, session):
    """Step 1.5 as the skill requires: record, then pause. Never delete."""
    recorded = s.call('read_old_side', session=session)
    s.call('pause_goal', session=session)
    for w in recorded['watchers']:
        s.call('pause_watcher', session=session, id=w['id'])
    return {'goal': recorded['goal']['status'], 'watchers': {w['id']: w['status'] for w in recorded['watchers']}}


def may_retire(s, lead_id):
    """Section 4's gate: new side confirmed, section 2 check passed, pause read back."""
    lead = s.call('retrieve_lead', id=lead_id)
    if not s.call('new_side_confirmation', session=lead['session'])['confirmed']:
        return False
    return not failures(section2(s, lead_id))


def effects(s, method):
    return [e['params'] for e in s.events if e['method'] == method]


# ----------------------------------------------------------------- section 2

def test_clean_lead_passes_every_row_after_old_side_is_paused():
    s = Sandbox()
    assert failures(section2(s, 'lead-clean')) == {'old_side': ['old-clean']}  # not yet paused
    close_out_old_side(s, 'old-clean')
    result = section2(s, 'lead-clean')
    assert failures(result) == {}
    # <check>: <n checked>, <n failed> — terminal tasks are not counted as open.
    assert result['one_ascent'][0] == 4 and result['status_note'][0] == 4
    assert result['owner'][0] == 1 and result['cap'] == (1, [])


def test_task_with_zero_ascents_fails_and_blocks_the_move():
    s = Sandbox()
    close_out_old_side(s, 'old-bad')
    assert 't-zero' in failures(section2(s, 'lead-bad'))['one_ascent']
    assert ascent(s, 't-zero')[1] == ['missing_ascent']
    assert may_retire(s, 'lead-bad') is False


def test_task_with_two_ascents_fails_as_duplicate():
    s = Sandbox()
    assert ascent(s, 't-two')[1] == ['duplicate_ascent']
    assert 't-two' in failures(section2(s, 'lead-bad'))['one_ascent']


def test_cycle_fails_instead_of_looping():
    s = Sandbox()
    assert ascent(s, 't-cycle-a')[1] == ['cycle']


def test_task_to_task_chain_counts_as_one_ascent_and_is_not_flattened():
    s = Sandbox()
    assert ascent(s, 't-chain-child') == (['plan-clean'], [])
    assert s.tasks['t-chain-child']['part_of'] == ['t-live']  # no second edge was added
    assert not effects(s, 'correct_task')


def test_active_task_with_no_owner_fails_and_so_does_the_unassigned_sentinel():
    s = Sandbox()
    assert failures(section2(s, 'lead-bad'))['owner'] == ['t-no-owner', 't-sentinel-owner']


def test_owner_may_come_from_any_of_the_three_fields_and_sentinel_in_one_is_ignored():
    s = Sandbox()
    assert has_owner(s.tasks['t-owner-by-executor']) is True  # assigned_to is a sentinel, executor is real
    assert has_owner(s.tasks['t-sentinel-owner']) is False


def test_skill_sentinel_set_equals_the_repo_sentinel_set():
    assert SKILL_SENTINELS == set(SENTINEL_ASSIGNEES)


def test_next_steps_at_the_cap_fails_and_just_under_passes():
    s = Sandbox()
    s.plans['plan-clean']['next_steps'] = 'x' * CAP
    assert failures(section2(s, 'lead-clean'))['cap'] == ['plan-clean']
    s.plans['plan-clean']['next_steps'] = 'x' * (CAP - 1)
    assert 'cap' not in failures(section2(s, 'lead-clean'))


def test_dated_note_needs_the_iso_date_and_the_skill_marker():
    s = Sandbox()
    assert not NOTE.search(s.tasks['t-no-note']['notes'])
    assert not NOTE.search(s.tasks['t-wrong-date-format']['notes'])
    assert NOTE.search(s.tasks['t-stale']['notes'])


def test_ascent_must_reach_a_project_linked_to_its_customer_organization():
    s = Sandbox()
    assert plan_reaches_linked_project(s, 'plan-clean') is True
    assert plan_reaches_linked_project(s, 'plan-other') is True  # internal project: no customer expected
    assert plan_reaches_linked_project(s, 'plan-orphan-project') is False
    s.call('retrieve_lead', id='lead-clean')
    s.data['leads']['lead-clean']['tasks'].append('t-unlinked-project')
    assert failures(section2(s, 'lead-clean'))['one_ascent'] == ['t-unlinked-project']


def test_open_status_is_free_string_so_unknown_spellings_count_as_open():
    s = Sandbox()
    assert is_open(s.tasks['t-terminal']) is False  # "Done " trims and lowercases to done
    s.tasks['t-live']['status'] = 'in-flight-ish'
    assert is_open(s.tasks['t-live']) is True


# ------------------------------------------------------ instrument is proven

def test_the_check_is_proven_on_a_known_negative_and_a_known_positive():
    s = Sandbox()
    close_out_old_side(s, 'old-clean')
    close_out_old_side(s, 'old-bad')
    assert failures(section2(s, 'lead-clean')) == {}          # known clean passes
    bad = failures(section2(s, 'lead-bad'))
    assert set(bad) >= {'one_ascent', 'owner'}
    # An all-zero result is only believed because the same instrument failed the bad lead.
    assert sum(len(v) for v in bad.values()) > 0


# ------------------------------------------------------- close-out variants

def compliant_rewrite(s, plan, text):
    s.call('correct_plan', id=plan, fields={'next_steps': text})
    assert s.call('retrieve_plan', id=plan)['next_steps'] == text  # read back


def test_next_steps_is_rewritten_not_appended():
    s = Sandbox()
    old = 'o' * 1800
    s.plans['plan-clean']['next_steps'] = old
    new = 'Current state: deploy live; next action is the review.'
    compliant_rewrite(s, 'plan-clean', new)
    assert s.plans['plan-clean']['next_steps'] == new
    assert failures(section2(s, 'lead-clean')).get('cap') is None


def test_naive_append_breaks_the_cap_and_keeps_stale_text():
    s = Sandbox()
    old = 'o' * 1800
    s.plans['plan-clean']['next_steps'] = old
    new = 'Current state: ' + 'n' * 300
    s.call('correct_plan', id='plan-clean',
           fields={'next_steps': s.plans['plan-clean']['next_steps'] + '\n' + new})
    assert 'cap' in failures(section2(s, 'lead-clean'))
    assert old in s.plans['plan-clean']['next_steps']


def close_out_task(s, task_id, verified_artifact):
    """Step 1.2: close only with an artifact; otherwise keep status and add the dated note."""
    t = s.tasks[task_id]
    if verified_artifact:
        s.call('correct_task', id=task_id, fields={'status': 'done'})
    return t['status']


def test_stale_task_keeps_its_status_and_verified_task_is_closed_with_evidence():
    s = Sandbox()
    assert close_out_task(s, 't-stale', verified_artifact=False) == 'pending'
    assert close_out_task(s, 't-done-with-artifact', verified_artifact=True) == 'done'
    assert [p['id'] for p in effects(s, 'correct_task')] == ['t-done-with-artifact']


def test_naive_bulk_close_changes_a_task_nobody_verified():
    s = Sandbox()
    for t in ('t-stale', 't-done-with-artifact'):
        close_out_task(s, t, verified_artifact=True)
    assert s.tasks['t-stale']['status'] == 'done'  # the bulk change the skill forbids
    assert len(effects(s, 'correct_task')) == 2


# ------------------------------------------------- old side and retirement

def test_goal_and_watchers_are_recorded_then_paused_never_deleted():
    s = Sandbox()
    recorded = close_out_old_side(s, 'old-clean')
    assert recorded == {'goal': 'active', 'watchers': {'watch-1': 'active'}}
    old = s.call('read_old_side', session='old-clean')
    assert old['goal']['status'] == 'paused' and old['watchers'][0]['status'] == 'paused'
    assert not effects(s, 'delete_goal') and not effects(s, 'delete_watcher')


def test_naive_deletion_is_distinguishable_and_loses_the_record():
    s = Sandbox()
    s.call('delete_goal', session='old-clean')
    s.call('delete_watcher', session='old-clean', id='watch-1')
    assert effects(s, 'delete_goal') and effects(s, 'delete_watcher')
    assert s.old['old-clean']['goal'] is None and s.old['old-clean']['watchers'] == []


def retire(s, lead_id):
    lead = s.call('retrieve_lead', id=lead_id)
    if not may_retire(s, lead_id):
        return False
    s.call('archive_session', session=lead['session'])
    assert s.call('read_old_side', session=lead['session'])['archived'] is True  # read back
    return True


def test_retirement_waits_for_new_side_check_and_confirmed_pause():
    s = Sandbox()
    # Pause only "listed for the operator": goal still active, so no retirement.
    assert retire(s, 'lead-clean') is False
    assert not effects(s, 'archive_session')
    close_out_old_side(s, 'old-clean')
    assert retire(s, 'lead-clean') is True
    assert effects(s, 'archive_session') == [{'session': 'old-clean'}]


def test_naive_archive_before_confirmation_is_distinguishable():
    s = Sandbox()
    s.call('archive_session', session='old-bad')  # new side never confirmed, check not run
    assert effects(s, 'archive_session') == [{'session': 'old-bad'}]
    assert may_retire(s, 'lead-bad') is False  # the compliant gate would have refused


def test_failed_migration_restores_the_recorded_old_side():
    s = Sandbox()
    recorded = close_out_old_side(s, 'old-bad')
    assert s.call('new_side_confirmation', session='old-bad')['confirmed'] is False
    assert retire(s, 'lead-bad') is False
    s.call('resume_goal', session='old-bad', status=recorded['goal'])
    for wid, status in recorded['watchers'].items():
        s.call('resume_watcher', session='old-bad', id=wid, status=status)
    old = s.call('read_old_side', session='old-bad')
    assert old['goal']['status'] == recorded['goal'] and old['archived'] is False


def test_naive_pause_without_resume_leaves_no_working_lead():
    s = Sandbox()
    close_out_old_side(s, 'old-bad')
    assert retire(s, 'lead-bad') is False
    assert s.call('read_old_side', session='old-bad')['goal']['status'] == 'paused'  # nobody drives it
