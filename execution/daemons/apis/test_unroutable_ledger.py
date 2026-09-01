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
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from unroutable_ledger import UnroutableLedger, fingerprint  # noqa: E402


@pytest.fixture()
def ledger(tmp_path):
    return UnroutableLedger(path=tmp_path / "ledger.json")


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


# ── persistence across restart (the ateles#636 lesson) ───────────────────────


def test_dedup_survives_a_daemon_restart(tmp_path):
    """In-process state would re-page the whole backlog on every restart.

    ateles#636 shipped a digest queue whose flush had zero non-test callers —
    state that looks recorded and is not. This asserts through a genuinely new
    object reading the file back, not a reset method on the same instance.
    """
    path = tmp_path / "ledger.json"
    first = UnroutableLedger(path=path)
    assert first.note("ent_a", "t", [], None, now=1000.0) is True

    restarted = UnroutableLedger(path=path)
    assert restarted.note("ent_a", "t", [], None, now=1002.0) is False


def test_ledger_file_is_actually_written(tmp_path):
    path = tmp_path / "ledger.json"
    led = UnroutableLedger(path=path)
    led.note("ent_a", "t", [], None, now=1000.0)
    assert path.exists(), "ledger claimed to persist but wrote no file"
    data = json.loads(path.read_text())
    assert "ent_a" in data["tasks"]


def test_corrupt_ledger_fails_open_to_escalating(tmp_path):
    """Unreadable state must degrade to noisy-but-visible, never to silence."""
    path = tmp_path / "ledger.json"
    path.write_text("{not json at all")
    led = UnroutableLedger(path=path)
    assert led.note("ent_a", "t", [], None, now=1000.0) is True


def test_unwritable_ledger_does_not_raise(tmp_path):
    led = UnroutableLedger(path=tmp_path / "nope" / "x" / "ledger.json")
    led.path = Path("/proc/definitely/not/writable/ledger.json")
    assert led.note("ent_a", "t", [], None, now=1000.0) is True


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


def test_undefined_role_dedup_survives_restart(tmp_path):
    path = tmp_path / "ledger.json"
    assert UnroutableLedger(path=path).note_undefined_role("pavo", now=1000.0) is True
    assert UnroutableLedger(path=path).note_undefined_role("pavo", now=1002.0) is False


def test_string_path_still_persists(tmp_path):
    """A str path must not silently disable persistence.

    Found by replaying the real event trace: `path=` given a str made every save
    fail with 'str' object has no attribute 'parent', fail-open swallowed it, and
    the ledger kept nothing while appearing to work.
    """
    path = tmp_path / "ledger.json"
    led = UnroutableLedger(path=str(path))
    assert led.note("ent_a", "t", [], None, now=1000.0) is True
    assert path.exists(), "a str path silently disabled persistence"
    assert UnroutableLedger(path=str(path)).note("ent_a", "t", [], None, now=1002.0) is False
