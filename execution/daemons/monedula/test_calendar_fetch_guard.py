"""
Regression tests for the calendar-fetch failure contract.

The calendar leg runs at most once per day, gated by `.monedula_last_run`.
Marking the day BEFORE fetching meant a failed fetch still consumed the only
attempt, with no retry — so a transient outage silently skipped that day's
payments. The daily run had drifted to just after midnight (host asleep, no
network) and every attempt failed for a week; the 2026-07-30 therapy session
went unnotified and unpaid as a result.

Two invariants lock that in:
  1. A FAILED fetch returns None — distinct from [] ("yesterday had no
     events") — so callers can tell the two apart.
  2. A failed fetch leaves the day UNCLAIMED, so the next launchd tick retries.

Run with: pytest execution/daemons/monedula/test_calendar_fetch_guard.py -v
"""

from __future__ import annotations

import shutil
import subprocess
from datetime import date

import handlers
import monedula


class _Result:
    """Stand-in for subprocess.CompletedProcess."""

    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


# ── Invariant 1: failure is None, empty day is [] ────────────────────────────


def test_nonzero_exit_returns_none(monkeypatch) -> None:
    """A gws failure (the live DNS error) must be None, not an empty day."""
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/gws")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: _Result(1, stderr="dns error: failed to lookup address information"),
    )
    assert monedula.fetch_yesterday_events() is None


def test_unparseable_output_returns_none(monkeypatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/gws")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Result(0, stdout="not json"))
    assert monedula.fetch_yesterday_events() is None


def test_missing_gws_returns_none(monkeypatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda _: None)
    assert monedula.fetch_yesterday_events() is None


def test_genuinely_empty_day_returns_empty_list_not_none(monkeypatch) -> None:
    """The distinction that matters: a real quiet day is [], never None."""
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/gws")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Result(0, stdout='{"items": []}'))
    assert monedula.fetch_yesterday_events() == []


# ── Invariant 2: a failed fetch does not burn the day ────────────────────────


def test_failed_fetch_leaves_day_unclaimed(monkeypatch, tmp_path) -> None:
    """
    The core regression. On fetch failure main() must return WITHOUT writing
    the run-state marker, so the next ~15-min tick retries instead of the
    obligation being skipped until tomorrow.
    """
    state = tmp_path / ".monedula_last_run"
    monkeypatch.setattr(monedula, "STATE_FILE", state)
    monkeypatch.setattr(monedula, "fetch_yesterday_events", lambda: None)
    monkeypatch.setattr(handlers, "load_handlers", lambda strandings=None: [])

    monedula.main()

    assert not state.exists(), "failed fetch must NOT claim the day — it blocks the retry"


def test_successful_fetch_claims_the_day(monkeypatch, tmp_path) -> None:
    """The idempotency guard must still hold on the success path."""
    state = tmp_path / ".monedula_last_run"
    monkeypatch.setattr(monedula, "STATE_FILE", state)
    monkeypatch.setattr(monedula, "fetch_yesterday_events", lambda: [])
    monkeypatch.setattr(handlers, "load_handlers", lambda strandings=None: [])
    monkeypatch.setattr(monedula, "fetch_due_payment_tasks", lambda *a, **k: [])

    monedula.main()

    assert state.exists(), "a successful fetch must claim the day"
    assert state.read_text().strip() == date.today().isoformat()
