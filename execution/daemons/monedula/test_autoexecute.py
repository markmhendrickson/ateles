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
