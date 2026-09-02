"""
test_unroutable_ledger.py — the no-owner escalation loop (ateles no-owner fix).

The measured defect these tests pin down (2026-09-01, apis.log):

  - 123 `Task has no owner` escalations from 35 distinct tasks.
  - 218 `task.created` events for those same 35 tasks — 6.2x redelivery.
  - Task ent_c192afd8760fd9f3fbd3c08c has a title, a description and five tags;
    it was escalated three times, logging its real tags at 16:22:05 and
    `tags=[]` at 16:25:25 immediately after a 502 on the hydration GET.

So the tests assert three separable things: an unroutable task escalates once,
suppression is bounded rather than permanent, and a FAILED read is never spelled
the same way as an empty snapshot.

WHY THESE RUN AGAINST A STORAGE FAKE
------------------------------------
PR #666's unit tests were green while its bug was live in production, because
they asserted on in-memory state and the defect lived in what reached storage.
Every persistence test here therefore goes through `FakeNeotoma`, which resolves
identity server-side and writes per-field exactly as prod does, and every
"restart" is a genuinely new ledger object reading the same backing store.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_neotoma import FakeNeotoma  # noqa: E402
from unroutable_ledger import (  # noqa: E402
    LedgerUnavailable,
    UnroutableLedger,
    fingerprint,
)
from unroutable_store import NeotomaLedgerStore  # noqa: E402


@pytest.fixture()
def neotoma(monkeypatch):
    """A fake Neotoma, wired into the store's httpx call sites."""
    fake = FakeNeotoma()
    import unroutable_store as us

    monkeypatch.setattr(us.httpx, "post", fake.post)
    monkeypatch.setattr(us.httpx, "get", fake.get)
    return fake


def _store(cache_seconds: int = 0) -> NeotomaLedgerStore:
    """A store with caching OFF by default.

    Tests assert on what STORAGE holds, so a cache that answered from memory
    would make a lost write look like a successful one — the exact blind spot
    that let PR #666 ship green. Cache behaviour gets its own explicit tests.
    """
    return NeotomaLedgerStore(
        base_url="http://fake", token="t", ledger_key="test", cache_seconds=cache_seconds
    )


def _ledger(cache_seconds: int = 0) -> UnroutableLedger:
    return UnroutableLedger(store=_store(cache_seconds))


@pytest.fixture()
def ledger(neotoma, no_legacy_disk):
    return _ledger()


@pytest.fixture(autouse=True)
def no_legacy_disk(monkeypatch, tmp_path):
    """Point the legacy-migration path at an empty dir unless a test opts in.

    Without this the suite would read the developer's real ledger at
    ~/.local/state/ateles/apis_unroutable.json and inherit live production
    dedup state — tests would pass or fail depending on whose machine ran them.
    """
    monkeypatch.setenv("APIS_UNROUTABLE_LEDGER", str(tmp_path / "absent.json"))


# ── dedup ────────────────────────────────────────────────────────────────────


def test_first_escalation_is_reported(ledger):
    assert ledger.note("ent_a", "Fix the thing", [], None, now=1000.0) is True


def test_repeat_escalation_for_same_task_is_suppressed(ledger):
    assert ledger.note("ent_a", "Fix the thing", [], None, now=1000.0) is True
    for i in range(10):
        assert ledger.note("ent_a", "Fix the thing", [], None, now=1001.0 + i) is False


def test_six_point_two_x_redelivery_yields_one_escalation(ledger):
    """The measured amplification: 218 events / 35 tasks. One page per task."""
    reported = 0
    for task in range(35):
        for _ in range(6):  # ~6.2x redelivery
            if ledger.note(f"ent_{task}", f"task {task}", [], None, now=1000.0):
                reported += 1
    assert reported == 35


def test_changed_routing_inputs_re_escalate(ledger):
    """Acquiring a tag is genuinely new information, so it escalates again."""
    assert ledger.note("ent_a", "t", [], None, now=1000.0) is True
    assert ledger.note("ent_a", "t", ["ops"], None, now=1001.0) is True


def test_title_change_alone_does_not_re_escalate(ledger):
    """Turdus rewrites titles without changing routability — not news."""
    assert ledger.note("ent_a", "old title", ["x"], None, now=1000.0) is True
    assert ledger.note("ent_a", "a totally new title", ["x"], None, now=1001.0) is False


def test_tag_order_and_case_do_not_re_escalate(ledger):
    assert ledger.note("ent_a", "t", ["Ops", "Eng"], None, now=1000.0) is True
    assert ledger.note("ent_a", "t", ["eng", "ops"], None, now=1001.0) is False


def test_fingerprint_ignores_title_and_body():
    assert fingerprint(["a"], None) == fingerprint(["A"], "")


# ── suppression is bounded, never permanent (#583 / #636) ────────────────────


def test_still_unroutable_task_is_reasserted_after_the_window(ledger):
    """A standing backlog must not decay into apparent health."""
    assert ledger.note("ent_a", "t", [], None, now=1000.0) is True
    assert ledger.note("ent_a", "t", [], None, now=1000.0 + 3600) is False
    # past REASSERT_SECONDS (default 24h)
    assert ledger.note("ent_a", "t", [], None, now=1000.0 + 86401) is True


def test_suppressed_count_is_reported_not_hidden(ledger):
    ledger.note("ent_a", "first", [], None, now=1000.0)
    for _ in range(12):
        ledger.note("ent_a", "first", [], None, now=1001.0)
    report = ledger.drain(now=1000.0 + ledger.window_seconds, force=True)
    assert "12 duplicate escalation(s) suppressed" in report


# ── persistence across restart (the ateles#636 lesson) ──────────────────────


def test_dedup_survives_a_daemon_restart(neotoma):
    """In-process state would re-page the whole backlog on every restart.

    ateles#636 shipped a digest queue whose flush had zero non-test callers —
    state that looks recorded and is not. This asserts through a genuinely new
    ledger AND a new store reading the same backing Neotoma, not a reset method
    on the same instance.
    """
    assert _ledger().note("ent_a", "t", [], None, now=1000.0) is True
    assert _ledger().note("ent_a", "t", [], None, now=1002.0) is False


def test_ledger_row_is_actually_written(neotoma):
    """Verify by READ-BACK, not by a success return.

    A store call can report success and persist nothing (an undeclared field is
    accepted and dropped). So this asserts on what storage holds.
    """
    _ledger().note("ent_a", "t", [], None, now=1000.0)
    row = neotoma.row_for("test")
    assert "ent_a" in row.get("tasks", {}), "ledger claimed to persist but stored nothing"


def test_one_singleton_row_not_one_per_write(neotoma):
    """Identity resolves on ledger_key, so writers converge rather than fan out."""
    led = _ledger()
    for i in range(5):
        led.note(f"ent_{i}", "t", [], None, now=1000.0 + i)
    led.note_undefined_role("pavo", now=1000.0)
    assert len(neotoma.rows) == 1, f"expected one ledger row, got {len(neotoma.rows)}"


def test_unwritable_ledger_does_not_raise(neotoma):
    """A failed WRITE fails open: noisy-but-alive beats a dead dispatcher."""
    neotoma.fail_writes = True
    assert _ledger().note("ent_a", "t", [], None, now=1000.0) is True


def test_write_failure_still_dedups_within_the_process(neotoma):
    """A lost write must not also lose the in-process decision.

    The page for this task already went out. If the failed write also reset the
    in-memory record, the very next event would page again — turning one
    unwritable ledger into a per-event flood rather than one duplicate later.
    """
    led = _ledger()
    neotoma.fail_writes = True
    assert led.note("ent_a", "t", [], None, now=1000.0) is True
    assert led.note("ent_a", "t", [], None, now=1001.0) is False


# ── an unreadable ledger must never look like an empty one ──────────────────
#
# THE failure mode this whole change turns on. "Empty" means every standing
# unroutable task is new, so treating a failed read as empty re-pages the entire
# backlog the moment Neotoma is slow — 131 pages, rebuilt by the dedup that
# exists to prevent them. Reads therefore fail CLOSED.


def test_failed_read_raises_rather_than_reporting_an_empty_ledger(neotoma):
    neotoma.fail_reads = True
    with pytest.raises(LedgerUnavailable):
        _ledger().note("ent_a", "t", [], None, now=1000.0)


def test_failed_read_does_not_re_escalate_a_known_task(neotoma):
    """The scenario in full: a task already paged, then Neotoma degrades."""
    assert _ledger().note("ent_a", "t", [], None, now=1000.0) is True
    neotoma.fail_reads = True
    with pytest.raises(LedgerUnavailable):
        _ledger().note("ent_a", "t", [], None, now=1001.0)


def test_the_whole_backlog_is_not_re_paged_when_neotoma_is_down(neotoma):
    """35 known tasks + an outage must yield ZERO pages, not 35."""
    led = _ledger()
    for i in range(35):
        led.note(f"ent_{i}", f"task {i}", [], None, now=1000.0)

    neotoma.fail_reads = True
    escalated = 0
    for i in range(35):
        try:
            if _ledger().note(f"ent_{i}", f"task {i}", [], None, now=2000.0):
                escalated += 1
        except LedgerUnavailable:
            pass
    assert escalated == 0, f"an unreadable ledger re-paged {escalated} known task(s)"


def test_missing_token_is_unavailable_not_empty(monkeypatch, neotoma):
    """No token is a read failure, not a fresh ledger."""
    monkeypatch.delenv("NEOTOMA_BEARER_TOKEN", raising=False)
    store = NeotomaLedgerStore(base_url="http://fake", token=None, ledger_key="test")
    with pytest.raises(LedgerUnavailable):
        UnroutableLedger(store=store).note("ent_a", "t", [], None, now=1000.0)


def test_undefined_role_read_failure_also_fails_closed(neotoma):
    neotoma.fail_reads = True
    with pytest.raises(LedgerUnavailable):
        _ledger().note_undefined_role("pavo", now=1000.0)


def test_unreadable_note_read_failure_also_fails_closed(neotoma):
    neotoma.fail_reads = True
    with pytest.raises(LedgerUnavailable):
        _ledger().note_unreadable("ent_a", now=1000.0)


def test_empty_neotoma_is_a_genuine_first_boot(neotoma):
    """The read SUCCEEDED and there is no row — escalating is correct here."""
    assert _ledger().note("ent_a", "t", [], None, now=1000.0) is True


def test_a_path_in_the_store_slot_is_rejected_loudly(neotoma, tmp_path):
    """A str/Path where the store belongs must not silently persist nothing.

    The disk version failed exactly this way: `UnroutableLedger(path="...")`
    broke every save, fail-open swallowed it, and the ledger kept nothing while
    appearing to work.
    """
    with pytest.raises(TypeError):
        UnroutableLedger(store=str(tmp_path / "l.json"))


# ── aggregation ──────────────────────────────────────────────────────────────


def test_drain_aggregates_many_tasks_into_one_report(ledger):
    for i in range(7):
        ledger.note(f"ent_{i}", f"task {i}", [], None, now=1000.0)
    report = ledger.drain(now=1000.0 + ledger.window_seconds, force=True)
    assert report.startswith("7 tasks unroutable")
    for i in range(7):
        assert f"ent_{i}" in report


def test_opening_report_of_a_quiet_period_is_immediate(ledger):
    """A genuinely new unroutable task must not wait out the whole window."""
    ledger.note("ent_a", "t", [], None, now=1000.0)
    assert ledger.drain(now=1000.0) is not None


def test_second_report_in_a_burst_waits_for_the_window(ledger):
    """Coalescing applies to the burst BEHIND the opening event."""
    ledger.note("ent_a", "t", [], None, now=1000.0)
    assert ledger.drain(now=1000.0) is not None  # opening report goes out
    ledger.note("ent_b", "t", [], None, now=1001.0)
    assert ledger.drain(now=1001.0) is None, "second report should be coalesced"
    # …and is delivered once the window has elapsed.
    assert ledger.drain(now=1001.0 + ledger.window_seconds) is not None


def test_drain_is_empty_when_nothing_pending(ledger):
    assert ledger.drain(now=9999.0, force=True) is None


def test_drain_clears_the_buffer(ledger):
    ledger.note("ent_a", "t", [], None, now=1000.0)
    assert ledger.drain(now=2000.0, force=True) is not None
    assert ledger.drain(now=2001.0, force=True) is None


def test_large_backlog_truncates_titles_but_never_drops_ids(ledger):
    """The operator must be able to act on the TAIL of a backlog, not just its head."""
    for i in range(50):
        ledger.note(f"ent_{i}", f"task {i}", [], None, now=1000.0)
    report = ledger.drain(now=2000.0, force=True)
    assert report.startswith("50 tasks unroutable")
    assert "and 30 more" in report
    for i in range(50):
        assert f"ent_{i}" in report, f"ent_{i} was dropped from the report"


def test_singular_phrasing_for_one_task(ledger):
    ledger.note("ent_a", "t", [], None, now=1000.0)
    assert ledger.drain(now=2000.0, force=True).startswith("1 task unroutable")


# ── undefined roles are a per-ROLE fact, not a per-task one ──────────────────


def test_undefined_role_reports_once_per_role(ledger):
    assert ledger.note_undefined_role("pavo", now=1000.0) is True
    for _ in range(20):  # 20 tasks that would route to pavo
        assert ledger.note_undefined_role("pavo", now=1001.0) is False


def test_distinct_undefined_roles_each_report(ledger):
    assert ledger.note_undefined_role("pavo", now=1000.0) is True
    assert ledger.note_undefined_role("vanellus", now=1000.0) is True


def test_undefined_role_is_reasserted_after_the_window(ledger):
    assert ledger.note_undefined_role("pavo", now=1000.0) is True
    assert ledger.note_undefined_role("pavo", now=1000.0 + 86401) is True


def test_undefined_role_dedup_survives_restart(neotoma):
    assert _ledger().note_undefined_role("pavo", now=1000.0) is True
    assert _ledger().note_undefined_role("pavo", now=1002.0) is False


# ── the two writers must not clobber each other (Loxia review, ateles#656) ───
#
# apis.dispatch_task records unroutable TASKS; skill_runner records undefined
# ROLES. On disk, each writer held its own instance whose `_loaded` latch froze
# a stale view, and every save() wrote all three fields back from that memory —
# silently dropping the other writer's records. Measured in prod: 2 of 4 roles
# and 1 of 2 unreadable records lost in ~11 minutes.
#
# These assert against STORAGE after interleaved writes, because in-memory
# assertions are precisely what missed it the first two times.


def test_two_instances_do_not_clobber(neotoma):
    """The raw hazard, stated directly against two independent ledgers.

    On disk this failed. Here each writer corrects only its own field, so
    neither can express an opinion about the other's.
    """
    a, b = _ledger(), _ledger()
    b.load()                                    # b latches a view at T0
    a.note("ent_a", "t", [], None, now=1000.0)  # a writes a task
    b.note_undefined_role("pavo", now=1001.0)   # b writes a role

    row = neotoma.row_for("test")
    assert "pavo" in row["roles"]
    assert "ent_a" in row["tasks"], "the role write dropped the task record"


def test_stale_writer_cannot_erase_a_newer_record(neotoma):
    """The exact prod shape: a writer holding an OLD view writes last.

    `b` loaded before `a` recorded anything, so `b`'s view of `tasks` is empty.
    A whole-row write from `b` would erase `ent_a`. A per-field write cannot.
    """
    b = _ledger()
    b.load()
    _ledger().note("ent_a", "t", [], None, now=1000.0)
    b.note_undefined_role("pavo", now=2000.0)  # newest write, oldest view

    assert "ent_a" in neotoma.row_for("test")["tasks"]


def test_all_three_fields_survive_interleaved_writers(neotoma):
    """Roles dropped 2 of 4 and unreadable 1 of 2 in the measured incident."""
    a, b, c = _ledger(), _ledger(), _ledger()
    for led in (a, b, c):
        led.load()
    for i in range(4):
        a.note(f"ent_{i}", "t", [], None, now=1000.0 + i)
        b.note_undefined_role(f"role_{i}", now=1000.0 + i)
        for _ in range(2):
            c.note_unreadable(f"ent_u{i}", now=1000.0 + i)

    row = neotoma.row_for("test")
    assert len(row["tasks"]) == 4, f"lost tasks: {row['tasks']}"
    assert len(row["roles"]) == 4, f"lost roles: {row['roles']}"
    assert len(row["unreadable"]) == 4, f"lost unreadable: {row['unreadable']}"


def test_shared_ledger_returns_one_instance():
    from unroutable_ledger import shared_ledger

    assert shared_ledger() is shared_ledger()


def test_shared_ledger_keeps_both_writers_records(neotoma, monkeypatch):
    """Through the SHARED accessor both writers actually use, then reloaded."""
    import unroutable_ledger as ul

    monkeypatch.setattr(ul, "_SHARED", None)
    monkeypatch.setattr(ul, "UnroutableLedger", lambda: _ledger())

    ul.shared_ledger().note("ent_a", "t", [], None, now=1000.0)
    ul.shared_ledger().note_undefined_role("pavo", now=1001.0)

    reloaded = _ledger()
    assert reloaded.note("ent_a", "t", [], None, now=1002.0) is False
    assert reloaded.note_undefined_role("pavo", now=1002.0) is False


def test_both_writers_survive_repeated_restart_cycles(neotoma):
    """Restart survival with BOTH writers active — the deployed shape.

    Apis restarted twice on the day this bug shipped and daemons run for months,
    so the invariant is: after N restarts with task-writes and role-writes
    interleaved, nothing either writer recorded has been lost. A clobbered
    ledger is indistinguishable from one that legitimately had no entry, so this
    is asserted by re-noting and requiring suppression.
    """
    for cycle in range(3):
        led = _ledger()  # a genuinely new process
        led.note(f"ent_{cycle}", "t", [], None, now=1000.0 + cycle)
        led.note_undefined_role(f"role_{cycle}", now=1000.0 + cycle)

    final = _ledger()
    for cycle in range(3):
        assert final.note(f"ent_{cycle}", "t", [], None, now=2000.0) is False, (
            f"task from cycle {cycle} was lost across restarts"
        )
        assert final.note_undefined_role(f"role_{cycle}", now=2000.0) is False, (
            f"role from cycle {cycle} was lost across restarts"
        )


def test_concurrent_writers_across_a_restart(neotoma):
    """Concurrency AND restart together — the trace-replay shape.

    The prod defect needed both: two writers interleaved, then a restart to make
    the loss observable. Three writers advance in lockstep, the process restarts
    mid-stream, and afterwards EVERY record either writer made must still
    suppress.
    """
    written_tasks, written_roles = [], []
    for cycle in range(4):
        a, b = _ledger(), _ledger()  # fresh process each cycle
        a.load()
        b.load()
        for step in range(3):
            tid = f"ent_{cycle}_{step}"
            role = f"role_{cycle}_{step}"
            a.note(tid, "t", [], None, now=1000.0 + cycle * 10 + step)
            b.note_undefined_role(role, now=1000.0 + cycle * 10 + step)
            written_tasks.append(tid)
            written_roles.append(role)

    final = _ledger()
    lost = [t for t in written_tasks if final.note(t, "t", [], None, now=5000.0)]
    assert not lost, f"{len(lost)} task record(s) lost: {lost[:5]}"
    lost_roles = [r for r in written_roles if final.note_undefined_role(r, now=5000.0)]
    assert not lost_roles, f"{len(lost_roles)} role record(s) lost: {lost_roles[:5]}"


def test_unreadable_records_also_survive_the_other_writer(neotoma):
    """The third field. A whole-row overwrite drops it as easily as the others."""
    a, b = _ledger(), _ledger()
    b.load()
    for _ in range(5):
        a.note_unreadable("ent_u", now=1000.0)
    b.note_undefined_role("pavo", now=1001.0)
    row = neotoma.row_for("test")
    assert "ent_u" in row["unreadable"], "the role write dropped the unreadable record"
    assert "pavo" in row["roles"]


# ── deletion must be representable (Loxia review, ateles#666) ────────────────
#
# The disk version's merge-on-write unioned the prior file back in, which cannot
# express a DELETE, so `clear_unreadable` never persisted and needed per-field
# tombstones. Writing the whole map as one observation makes a removed key
# simply absent. These tests outlive the tombstones and pin the BEHAVIOUR.


def test_clear_unreadable_actually_persists(neotoma):
    led = _ledger()
    for _ in range(3):
        led.note_unreadable("ent_x", now=1000.0)
    assert "ent_x" in neotoma.row_for("test")["unreadable"]

    led.clear_unreadable("ent_x")
    assert "ent_x" not in neotoma.row_for("test")["unreadable"], (
        "a deliberately cleared entry survived the write"
    )


def test_cleared_entry_stays_gone_after_reload(neotoma):
    """The consequence that actually bites: a stale streak reloaded on restart
    reports on the first later blip instead of starting from zero."""
    led = _ledger()
    for _ in range(3):
        led.note_unreadable("ent_x", now=1000.0)
    led.clear_unreadable("ent_x")

    restarted = _ledger()
    # A single fresh failure must NOT immediately report (streak restarts at 1).
    assert restarted.note_unreadable("ent_x", now=2000.0) is False


def test_a_new_streak_after_a_clear_is_recorded_again(neotoma):
    """Clearing must not permanently blacklist the key."""
    led = _ledger()
    for _ in range(3):
        led.note_unreadable("ent_x", now=1000.0)
    led.clear_unreadable("ent_x")
    for _ in range(2):
        led.note_unreadable("ent_x", now=2000.0)
    assert "ent_x" in neotoma.row_for("test")["unreadable"]


def test_clearing_one_entry_does_not_disturb_another(neotoma):
    led = _ledger()
    for _ in range(3):
        led.note_unreadable("ent_keep", now=1000.0)
        led.note_unreadable("ent_drop", now=1000.0)
    led.clear_unreadable("ent_drop")
    unreadable = neotoma.row_for("test")["unreadable"]
    assert "ent_keep" in unreadable and "ent_drop" not in unreadable


def test_clear_does_not_disturb_the_other_writers_fields(neotoma):
    """A delete must stay scoped to its own field."""
    led = _ledger()
    led.note("ent_a", "t", [], None, now=1000.0)
    led.note_undefined_role("pavo", now=1000.0)
    for _ in range(3):
        led.note_unreadable("ent_u", now=1000.0)
    led.clear_unreadable("ent_u")
    row = neotoma.row_for("test")
    assert "ent_a" in row["tasks"] and "pavo" in row["roles"]
    assert "ent_u" not in row["unreadable"]


def test_clear_on_an_unreadable_ledger_does_not_raise(neotoma):
    """Forgetting is not urgent and pages nothing, so it degrades quietly.

    Called on EVERY readable snapshot, so raising here would turn a Neotoma blip
    into a crash on the dispatch happy path.
    """
    neotoma.fail_reads = True
    _ledger().clear_unreadable("ent_x")  # must not raise


# ── idempotency keys carry no clock ─────────────────────────────────────────
#
# Neotoma hashes the full payload server-side, so a wall-clock value inside a
# written entity permanently poisons that row's key — a prior sync froze every
# affected row that way. Timestamps are real state and ARE written; they are
# excluded from the DIGEST that forms the key.


def test_identical_logical_state_reuses_one_idempotency_key(neotoma):
    """Re-writing the same membership must be a replay, not a new row."""
    from unroutable_store import _digest, _keyable

    state = {"ent_a": {"fp": "x", "last_escalated": 1000.0, "count": 1}}
    later = {"ent_a": {"fp": "x", "last_escalated": 9999.0, "count": 1}}
    assert _digest(_keyable(state)) == _digest(_keyable(later))


def test_changed_membership_changes_the_key(neotoma):
    from unroutable_store import _digest, _keyable

    one = {"ent_a": {"fp": "x", "last_escalated": 1000.0}}
    two = {"ent_a": {"fp": "x", "last_escalated": 1000.0}, "ent_b": {"fp": "y"}}
    assert _digest(_keyable(one)) != _digest(_keyable(two))


def test_changed_fingerprint_changes_the_key(neotoma):
    from unroutable_store import _digest, _keyable

    before = {"ent_a": {"fp": "no-tags", "last_escalated": 1000.0}}
    after = {"ent_a": {"fp": "has-tags", "last_escalated": 1000.0}}
    assert _digest(_keyable(before)) != _digest(_keyable(after))


def test_no_idempotency_key_contains_a_wall_clock(neotoma):
    """The regression guard: a clock in the key freezes the row."""
    led = _ledger()
    led.note("ent_a", "t", [], None, now=1756000000.0)
    led.note_undefined_role("pavo", now=1756000001.0)
    for key in neotoma.idempotency_keys:
        assert "1756" not in key, f"idempotency key carries a clock: {key}"


# ── migration from the legacy disk ledger ───────────────────────────────────
#
# A record left on disk is not a cosmetic loss: every dropped entry is one
# re-page of a task the operator has already seen.


def test_disk_state_migrates_into_neotoma(neotoma, monkeypatch, tmp_path):
    import json as _json

    path = tmp_path / "legacy.json"
    path.write_text(_json.dumps({
        "version": 1,
        "tasks": {"ent_old": {"fp": fingerprint([], None), "last_escalated": 500.0, "count": 3}},
        "roles": {"pavo": 500.0},
        "unreadable": {"ent_u": {"n": 2, "reported": 500.0}},
    }))
    monkeypatch.setenv("APIS_UNROUTABLE_LEDGER", str(path))

    led = _ledger()
    # The migrated task is already known, so it must NOT re-page.
    assert led.note("ent_old", "t", [], None, now=1000.0) is False
    assert led.note_undefined_role("pavo", now=1000.0) is False

    row = neotoma.row_for("test")
    assert "ent_old" in row["tasks"]
    assert "pavo" in row["roles"]
    assert "ent_u" in row["unreadable"]


def test_migration_does_not_overwrite_newer_neotoma_state(neotoma, monkeypatch, tmp_path):
    """Neotoma is the newer decision; disk only fills gaps."""
    import json as _json

    _ledger().note("ent_a", "t", ["ops"], None, now=9000.0)
    new_fp = neotoma.row_for("test")["tasks"]["ent_a"]["fp"]

    path = tmp_path / "legacy.json"
    path.write_text(_json.dumps({"tasks": {"ent_a": {"fp": "STALE", "last_escalated": 1.0}}}))
    monkeypatch.setenv("APIS_UNROUTABLE_LEDGER", str(path))

    _ledger().load()
    assert neotoma.row_for("test")["tasks"]["ent_a"]["fp"] == new_fp


def test_migration_is_idempotent(neotoma, monkeypatch, tmp_path):
    import json as _json

    path = tmp_path / "legacy.json"
    path.write_text(_json.dumps({"roles": {"pavo": 500.0}}))
    monkeypatch.setenv("APIS_UNROUTABLE_LEDGER", str(path))

    for _ in range(3):
        assert _ledger().note_undefined_role("pavo", now=1000.0) is False
    assert len(neotoma.rows) == 1


def test_migration_never_writes_the_legacy_file(neotoma, monkeypatch, tmp_path):
    """The file stays as a manual fallback, untouched."""
    import json as _json

    path = tmp_path / "legacy.json"
    original = _json.dumps({"roles": {"pavo": 500.0}})
    path.write_text(original)
    monkeypatch.setenv("APIS_UNROUTABLE_LEDGER", str(path))

    _ledger().note_undefined_role("vanellus", now=1000.0)
    assert path.read_text() == original


def test_a_corrupt_legacy_file_does_not_block_startup(neotoma, monkeypatch, tmp_path):
    path = tmp_path / "legacy.json"
    path.write_text("{not json at all")
    monkeypatch.setenv("APIS_UNROUTABLE_LEDGER", str(path))
    assert _ledger().note("ent_a", "t", [], None, now=1000.0) is True


def test_absent_legacy_file_is_normal(neotoma, monkeypatch, tmp_path):
    monkeypatch.setenv("APIS_UNROUTABLE_LEDGER", str(tmp_path / "nope.json"))
    assert _ledger().note("ent_a", "t", [], None, now=1000.0) is True


# ── the read cache keeps the hot path off the network ───────────────────────


def test_a_cached_ledger_does_not_re_read_per_dispatch(neotoma):
    """Dedup is consulted on every task.created; it must not cost a read each time."""
    led = _ledger(cache_seconds=300)
    led.note("ent_a", "t", [], None, now=1000.0)
    reads_after_first = sum(1 for path, _ in neotoma.calls if path == "query")
    for i in range(20):
        led.note(f"ent_{i}", "t", [], None, now=1001.0 + i)
    reads_now = sum(1 for path, _ in neotoma.calls if path == "query")
    assert reads_now == reads_after_first, "the cache is not preventing per-dispatch reads"
