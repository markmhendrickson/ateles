"""
test_autoexecute.py — Tests for Monedula's task-based auto-execute path.

Covers the four safety properties of execute_approved_tasks():
  1. Approval gate  — only tasks with payment_approved=true execute.
  2. Idempotency    — tasks already done / with a payment_event never re-execute.
  3. Dry-run safety — MONEDULA_DRYRUN on (default) never calls handler.execute().
  4. Task→handler mapping — a task executes via the handler whose profile
     .neotoma_task_id points at it, and nothing else.

No real payment code runs: handlers are fakes that record calls.
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import monedula  # noqa: E402


class _FakeProfile:
    def __init__(self, task_id, amount=100):
        self.neotoma_task_id = task_id
        self.amount_eur = amount


class _FakeHandler:
    """Handler stub that records execute() calls instead of moving money."""

    def __init__(self, name, task_id):
        self.name = name
        self.profile = _FakeProfile(task_id)
        self.executed_with = []

    def execute(self, match):
        self.executed_with.append(match)
        return {"status": "sent", "handler": self.name, "transfer_id": "T123"}

    def format_confirmation(self, result):
        return f"{self.name}: {result.get('status')}"


def _task(entity_id, *, approved=False, status="pending", paid_marker="",
          due_date="", recurrence=""):
    snap = {"title": f"task-{entity_id}", "status": status}
    if approved:
        snap["payment_approved"] = True
    if paid_marker:
        snap["payment_event_id"] = paid_marker
    if due_date:
        snap["due_date"] = due_date
    if recurrence:
        snap["recurrence"] = recurrence
    return {"entity_id": entity_id, "snapshot": snap}


@pytest.fixture(autouse=True)
def _dry_off(monkeypatch):
    # Default each test to REAL mode so execute() is reachable; individual
    # dry-run tests re-enable it. (Keeps the approval/idempotency assertions honest.)
    monkeypatch.setenv("MONEDULA_DRYRUN", "0")


# ── 1. Approval gate ─────────────────────────────────────────────────────────

def test_unapproved_task_does_not_execute():
    h = _FakeHandler("wise", "ent_1")
    results = monedula.execute_approved_tasks([_task("ent_1", approved=False)], [h])
    assert h.executed_with == []
    assert results == []


def test_approved_task_executes():
    h = _FakeHandler("wise", "ent_1")
    results = monedula.execute_approved_tasks([_task("ent_1", approved=True)], [h])
    assert len(h.executed_with) == 1
    assert results and results[0][1]["status"] == "sent"


# ── 2. Idempotency ───────────────────────────────────────────────────────────

def test_done_task_skipped_even_if_approved():
    h = _FakeHandler("wise", "ent_1")
    monedula.execute_approved_tasks(
        [_task("ent_1", approved=True, status="done")], [h])
    assert h.executed_with == []


def test_task_with_payment_event_skipped():
    h = _FakeHandler("wise", "ent_1")
    monedula.execute_approved_tasks(
        [_task("ent_1", approved=True, paid_marker="pe_9")], [h])
    assert h.executed_with == []


# ── 2b. Durable per-session idempotency (double-pay fix) ──────────────────────

def test_session_marker_blocks_repay_for_same_session():
    """A recurring task carrying the session marker for its current due_date is
    already-paid THIS session and must not re-execute."""
    t = _task("ent_1", approved=True, recurrence="weekly", due_date="2026-07-16",
              paid_marker="paid:2026-07-16")
    assert monedula._task_already_paid(t) is True
    h = _FakeHandler("wise", "ent_1")
    monedula.execute_approved_tasks([t], [h])
    assert h.executed_with == []  # no re-pay


def test_stale_session_marker_does_not_block_next_session():
    """After due_date rolls, a marker naming the PREVIOUS session must not block
    the new session (otherwise the payment would never recur)."""
    t = _task("ent_1", approved=True, recurrence="weekly", due_date="2026-07-23",
              paid_marker="paid:2026-07-16")  # stale marker, new session
    assert monedula._task_already_paid(t) is False
    h = _FakeHandler("wise", "ent_1")
    monedula.execute_approved_tasks([t], [h])
    assert len(h.executed_with) == 1  # new session pays


def test_successful_send_stamps_in_memory_marker():
    """Defense-in-depth: a real send stamps the in-memory snapshot so a duplicate
    task row in the SAME run cannot re-enter execute()."""
    t = _task("ent_1", approved=True, recurrence="weekly", due_date="2026-07-16")
    h = _FakeHandler("wise", "ent_1")
    monedula.execute_approved_tasks([t], [h])
    assert t["snapshot"].get("payment_event_id") == "paid:2026-07-16"
    # A second pass over the now-stamped task must not pay again.
    monedula.execute_approved_tasks([t], [h])
    assert len(h.executed_with) == 1  # still only one send


def test_duplicate_task_rows_pay_once_in_one_run():
    """Two rows of the same recurring task in one due_tasks list pay only once."""
    t1 = _task("ent_1", approved=True, recurrence="weekly", due_date="2026-07-16")
    t2 = _task("ent_1", approved=True, recurrence="weekly", due_date="2026-07-16")
    h = _FakeHandler("wise", "ent_1")
    monedula.execute_approved_tasks([t1, t2], [h])
    assert len(h.executed_with) == 1  # the second row sees the stamped marker


def test_non_marker_payment_event_still_blocks():
    """A legacy real payment reference (not a session marker) blocks conservatively."""
    t = _task("ent_1", approved=True, recurrence="weekly", due_date="2026-07-16",
              paid_marker="2251311092")  # a real Wise transfer id
    assert monedula._task_already_paid(t) is True


def test_created_unconfirmed_stamps_marker_and_blocks_reexec():
    """A Wise transfer that was CREATED but returned a non-terminal funding status
    (e.g. transient bounced_back) must not be re-created on the next pass — the
    transfer already exists at the rail. The marker is stamped just like a send."""

    class _UnconfirmedHandler:
        def __init__(self, task_id):
            self.name = "wise"
            self.profile = _FakeProfile(task_id)
            self.calls = 0

        def execute(self, match):
            self.calls += 1
            # A transfer EXISTS but funding status wasn't terminal.
            return {"status": "created_unconfirmed", "handler": self.name,
                    "transfer_id": "T999", "wise_status": "bounced_back"}

        def format_confirmation(self, result):
            return f"{self.name}: {result.get('status')}"

    t = _task("ent_1", approved=True, recurrence="weekly", due_date="2026-07-16")
    h = _UnconfirmedHandler("ent_1")
    monedula.execute_approved_tasks([t], [h])
    assert h.calls == 1
    assert t["snapshot"].get("payment_event_id") == "paid:2026-07-16"
    # Second pass must not re-create the transfer.
    monedula.execute_approved_tasks([t], [h])
    assert h.calls == 1  # no double-create


# ── 3. Dry-run safety ────────────────────────────────────────────────────────

def test_dryrun_never_executes(monkeypatch):
    monkeypatch.setenv("MONEDULA_DRYRUN", "1")
    h = _FakeHandler("wise", "ent_1")
    results = monedula.execute_approved_tasks([_task("ent_1", approved=True)], [h])
    assert h.executed_with == []  # execute() must NOT be called
    assert results and results[0][1]["status"] == "dry_run"


def test_dryrun_is_default_when_unset(monkeypatch):
    monkeypatch.delenv("MONEDULA_DRYRUN", raising=False)
    assert monedula._dryrun_enabled() is True
    h = _FakeHandler("wise", "ent_1")
    results = monedula.execute_approved_tasks([_task("ent_1", approved=True)], [h])
    assert h.executed_with == []
    assert results[0][1]["status"] == "dry_run"


# ── 4. Task→handler mapping ──────────────────────────────────────────────────

def test_task_routed_to_its_own_handler_only():
    h1 = _FakeHandler("wise", "ent_1")
    h2 = _FakeHandler("btc", "ent_2")
    monedula.execute_approved_tasks([_task("ent_2", approved=True)], [h1, h2])
    assert h1.executed_with == []      # unrelated handler untouched
    assert len(h2.executed_with) == 1  # only the linked handler ran


def test_task_with_no_matching_handler_skipped():
    h = _FakeHandler("wise", "ent_1")
    results = monedula.execute_approved_tasks([_task("ent_99", approved=True)], [h])
    assert h.executed_with == []
    assert results == []


def test_handler_exception_does_not_crash_and_is_recorded(monkeypatch):
    h = _FakeHandler("wise", "ent_1")

    def _boom(match):
        raise RuntimeError("wallet down")

    h.execute = _boom
    results = monedula.execute_approved_tasks([_task("ent_1", approved=True)], [h])
    assert results and results[0][1]["status"] == "failed"
    assert "wallet down" in results[0][1]["error"]


# ── 5. _mark_tasks_paid — one-off override clear on recurring obligations ─────
#
# Regression for PR #249 review (qa lens, finding 1): a recurring obligation's
# one-off amount_eur_override must be cleared after it pays, so the next
# session does not silently re-charge the one-off rate. If the clear write
# itself fails, that must surface as an ERROR log (MANUAL CHECK REQUIRED)
# without blocking the marker/approval-reset corrections already applied.

def _recurring_task(entity_id, *, due_date="2026-07-21", override="70"):
    snap = {
        "title": "Private yoga payment — Manel",
        "status": "open",
        "due_date": due_date,
        "recurrence": "weekly",
        "payment_approved": True,
        "amount_eur_override": override,
    }
    return {"entity_id": entity_id, "snapshot": snap}


def test_recurring_task_clears_override_after_paid_send(monkeypatch):
    """A sent recurring-obligation payment must clear amount_eur_override so the
    next session bills the standing rate, not the one-off amount again."""
    import handlers.neotoma_cli as ncli

    calls = []

    def _fake_correct(entity_id, field, value, *, entity_type="task", label="autoexec"):
        calls.append((field, value))
        return True

    monkeypatch.setattr(ncli, "correct_field", _fake_correct)

    h = _FakeHandler("btc", "ent_1")
    task = _recurring_task("ent_1")
    result = {"status": "sent", "handler": "btc", "txid": "deadbeef"}

    monedula._mark_tasks_paid([(h, result)], [task])

    assert ("amount_eur_override", "") in calls, \
        "override must be cleared (set to empty) after the one-off session pays"


def test_recurring_task_override_clear_failure_logs_manual_check(monkeypatch, caplog):
    """If the override-clear write fails, the daemon must log an ERROR telling
    the operator to check manually — silently continuing would let the next
    session re-use the stale one-off amount."""
    import handlers.neotoma_cli as ncli
    import logging

    def _fake_correct(entity_id, field, value, *, entity_type="task", label="autoexec"):
        # Every write succeeds except the override clear.
        return field != "amount_eur_override"

    monkeypatch.setattr(ncli, "correct_field", _fake_correct)

    h = _FakeHandler("btc", "ent_1")
    task = _recurring_task("ent_1")
    result = {"status": "sent", "handler": "btc", "txid": "deadbeef"}

    with caplog.at_level(logging.ERROR, logger=monedula.log.name):
        monedula._mark_tasks_paid([(h, result)], [task])

    assert any("MANUAL CHECK REQUIRED" in rec.message for rec in caplog.records), \
        "a failed override clear must be logged at ERROR for manual follow-up"
    # The other corrections for this session (marker + approval reset) must
    # still have been attempted — override-clear failure must not block them.
