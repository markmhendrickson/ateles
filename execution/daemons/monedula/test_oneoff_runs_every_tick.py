"""
Regression tests: one-off payments must not be gated by the calendar leg.

Monedula's daily guard exists for the CALENDAR leg. It claims the day only
after a successful calendar fetch so a transient outage retries on the next
~15-minute tick (see the 2026-07-30 incident noted in monedula.main).

But the guard sat at the top of main(), returning BEFORE load_handlers() was
ever called. A one-off invoice profile created after the day's calendar run
was therefore invisible until the next calendar day, with no operator-visible
signal — a same-day payment was impossible. Two rental payments (2026-08-15,
2026-08-18) had to be sent by hand for exactly this reason.

The due-date trigger itself was already correct and well covered by
test_oneoff_trigger.py; what was missing was the wiring in main(). These
tests lock that wiring:

  * one-offs are evaluated even when the calendar leg already ran today
  * the calendar leg still runs at most once per day
  * a failed calendar fetch still leaves the day unclaimed for retry
  * a failed calendar fetch does not suppress a due one-off
"""

from __future__ import annotations

import sys
import types
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import monedula  # noqa: E402


class _Handler:
    """Minimal handler double recording whether it was consulted."""

    def __init__(self, name: str, one_off: bool, matches_result: list | None = None):
        self.name = name
        self.consulted_with: list = []
        self._matches = matches_result if matches_result is not None else []
        self.profile = types.SimpleNamespace(
            one_off=one_off,
            due_date=date.today().isoformat() if one_off else "",
            calendar_keywords=[] if one_off else ["therapy"],
            label=name,
        )

    def matches(self, events):
        self.consulted_with.append(events)
        return self._matches

    def preview(self, match):
        return f"preview:{self.name}"


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path, monkeypatch):
    """Point the run-state file at a temp dir so tests never touch real state."""
    monkeypatch.setattr(monedula, "STATE_FILE", tmp_path / ".monedula_last_run")
    # Neutralise outbound effects.
    monkeypatch.setattr(monedula, "_notify", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(monedula, "_fetch_due_tasks", lambda *a, **k: [], raising=False)
    monkeypatch.setattr(
        monedula, "fetch_due_payment_tasks", lambda *a, **k: [], raising=False
    )
    monkeypatch.setattr(monedula, "telegram_send", lambda *a, **k: None, raising=False)
    # A matched payment reaches the operator-approval gate, which long-polls
    # Telegram for 120s. Stub it to decline: these tests assert that a one-off
    # is EVALUATED, never that it is paid — approval stays the operator's.
    monkeypatch.setattr(
        monedula, "telegram_long_poll_once", lambda *a, **k: None, raising=False
    )
    yield


def _install_handlers(monkeypatch, handlers):
    fake_mod = types.ModuleType("handlers")
    fake_mod.load_handlers = lambda strandings=None: handlers
    monkeypatch.setitem(sys.modules, "handlers", fake_mod)


def test_oneoff_evaluated_when_calendar_already_ran(monkeypatch):
    """The core regression: a due one-off must be seen on a later tick."""
    monedula._mark_ran_today()  # calendar leg already done today
    assert monedula._check_already_ran_today() is True

    oneoff = _Handler("invoice", one_off=True, matches_result=[{"trigger": "due_date"}])
    recurring = _Handler("therapy", one_off=False)
    _install_handlers(monkeypatch, [oneoff, recurring])

    fetched = []
    monkeypatch.setattr(
        monedula, "fetch_yesterday_events", lambda: fetched.append(1) or []
    )

    monedula.main()

    assert oneoff.consulted_with, "due one-off must be evaluated on a later tick"
    assert not fetched, "calendar leg must NOT re-run once the day is claimed"


def test_calendar_leg_still_runs_once_per_day(monkeypatch):
    """The guard must keep protecting the calendar leg."""
    oneoff = _Handler("invoice", one_off=True)
    _install_handlers(monkeypatch, [oneoff])

    fetched = []
    monkeypatch.setattr(
        monedula, "fetch_yesterday_events", lambda: fetched.append(1) or []
    )

    monedula.main()  # first tick: claims the day
    monedula.main()  # second tick: must not re-fetch

    assert len(fetched) == 1, f"calendar fetch should run once/day, ran {len(fetched)}x"


def test_failed_calendar_fetch_leaves_day_unclaimed(monkeypatch):
    """A transient outage must not consume the day's calendar attempt."""
    _install_handlers(monkeypatch, [_Handler("invoice", one_off=True)])
    monkeypatch.setattr(monedula, "fetch_yesterday_events", lambda: None)

    monedula.main()

    assert monedula._check_already_ran_today() is False, (
        "failed calendar fetch must leave the day unclaimed so the next tick retries"
    )


def test_failed_calendar_fetch_does_not_suppress_due_oneoff(monkeypatch):
    """A calendar outage is unrelated to an invoice being due."""
    oneoff = _Handler("invoice", one_off=True, matches_result=[{"trigger": "due_date"}])
    _install_handlers(monkeypatch, [oneoff])
    monkeypatch.setattr(monedula, "fetch_yesterday_events", lambda: None)

    monedula.main()

    assert oneoff.consulted_with, (
        "a due one-off must still be evaluated when the calendar fetch fails"
    )
