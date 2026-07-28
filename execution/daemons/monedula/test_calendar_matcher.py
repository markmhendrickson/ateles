"""
test_calendar_matcher.py — Tests for the shared calendar matcher in
PaymentHandler.match_events() (handler_base.py).

Regression guard for the double-pay bug: profile "Yoga con Manel" carried
calendar_keywords ["yoga", "manel"] matched with ANY, so an unrelated event
titled "Manel work session" triggered a second €60 BTC payment. The matcher now
keys on the recurring-series id first, falls back to requiring ALL keywords, and
returns at most one match per obligation.

Fixture titles/ids mirror the real Google Calendar payload observed 2026-07-16:
  '🧘 Yoga with Manel' -> recurringEventId ee0dad5f7f5d4879949ffdbbf65d9374
  'Manel work session' -> recurringEventId None
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from handler_base import PaymentHandler  # noqa: E402


class _Profile:
    def __init__(self, recurring_id="", keywords=None):
        self.calendar_recurring_event_id = recurring_id
        self.calendar_keywords = keywords or []


class _Handler(PaymentHandler):
    name = "yoga"

    def __init__(self, profile):
        self.profile = profile

    def matches(self, events):
        return self.match_events(events)

    def preview(self, match):
        return ""

    def execute(self, match):
        return {"status": "sent", "handler": self.name}


_YOGA_ID = "ee0dad5f7f5d4879949ffdbbf65d9374"
_YOGA = {"summary": "🧘 Yoga with Manel", "recurringEventId": _YOGA_ID}
_WORK = {"summary": "Manel work session", "recurringEventId": None}
_UNRELATED = {"summary": "Dentist", "recurringEventId": None}


def test_recurring_id_matches_only_the_series_instance():
    h = _Handler(_Profile(recurring_id=_YOGA_ID, keywords=["yoga", "manel"]))
    out = h.matches([_YOGA, _WORK, _UNRELATED])
    assert len(out) == 1
    assert out[0]["summary"] == "🧘 Yoga with Manel"


def test_work_session_no_longer_triggers_a_payment():
    # The exact double-pay case: with a recurring id set, an event that merely
    # mentions the payee but is not the series instance must NOT match.
    h = _Handler(_Profile(recurring_id=_YOGA_ID, keywords=["yoga", "manel"]))
    out = h.matches([_WORK])
    assert out == []


def test_single_obligation_never_yields_two_payments():
    # Two events both carrying the series id (e.g. a duplicated calendar entry)
    # still collapse to one payment.
    dup = {"summary": "🧘 Yoga with Manel (dup)", "recurringEventId": _YOGA_ID}
    h = _Handler(_Profile(recurring_id=_YOGA_ID, keywords=["yoga", "manel"]))
    out = h.matches([_YOGA, dup])
    assert len(out) == 1


def test_keyword_fallback_requires_all_keywords():
    # No recurring id -> keyword fallback. "Manel work session" is missing
    # "yoga", so ALL-keywords fails it; the real yoga title passes.
    h = _Handler(_Profile(recurring_id="", keywords=["yoga", "manel"]))
    assert h.matches([_WORK]) == []
    assert len(h.matches([_YOGA])) == 1


def test_keyword_fallback_single_keyword_still_works():
    h = _Handler(_Profile(recurring_id="", keywords=["yoga"]))
    assert len(h.matches([_YOGA])) == 1
    assert h.matches([_UNRELATED]) == []


def test_no_triggers_when_profile_has_neither_signal():
    h = _Handler(_Profile(recurring_id="", keywords=[]))
    assert h.matches([_YOGA, _WORK]) == []
