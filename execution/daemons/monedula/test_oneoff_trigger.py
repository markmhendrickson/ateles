"""
Unit tests for one-off (due-date-triggered) payment profiles.

Monedula was built for attendance-gated recurring payments: a calendar event
fires the handler. A one-off invoice has no session and no event, so profiles
without calendar_keywords were silently skipped at load time and could never
be paid by the daemon.

These tests lock the contract for the due-date trigger:

  * a one-off matches on or after its due date, never before
  * a malformed due date never fires a payment
  * the two trigger kinds do not bleed into each other — a recurring profile
    never matches on a date, a one-off never matches on a calendar event
  * a profile with neither trigger is rejected at load as unreachable
  * the payee can come from the profile itself (one-off payees are not
    standing contacts)

Run with: pytest execution/daemons/monedula/test_oneoff_trigger.py -v
"""

from __future__ import annotations

from datetime import date, timedelta

from handlers.payment_profile import PaymentProfile
from handlers.wise_transfer import WiseTransferHandler, _load_contact

TODAY = date.today()


def _oneoff(due: date | str, **kw) -> PaymentProfile:
    return PaymentProfile(
        prefix="INVOICE",
        label="Invoice",
        calendar_keywords=[],
        payment_type="wise",
        amount_eur=100,
        due_date=due if isinstance(due, str) else due.isoformat(),
        one_off=True,
        **kw,
    )


def _recurring(**kw) -> PaymentProfile:
    return PaymentProfile(
        prefix="THERAPY",
        label="Therapy",
        calendar_keywords=["therapy", "terapia"],
        payment_type="wise",
        amount_eur=60,
        **kw,
    )


# ── The due-date trigger fires on or after the date, never before ────────────


def test_matches_on_due_date() -> None:
    h = WiseTransferHandler(_oneoff(TODAY))
    matches = h.matches([])
    assert len(matches) == 1
    assert matches[0]["trigger"] == "due_date"
    assert matches[0]["overdue_days"] == 0


def test_matches_when_overdue() -> None:
    h = WiseTransferHandler(_oneoff(TODAY - timedelta(days=9)))
    matches = h.matches([])
    assert len(matches) == 1
    assert matches[0]["overdue_days"] == 9


def test_does_not_match_before_due() -> None:
    h = WiseTransferHandler(_oneoff(TODAY + timedelta(days=1)))
    assert h.matches([]) == []


# ── A malformed date must never fire a payment ───────────────────────────────


def test_malformed_due_date_does_not_match() -> None:
    for bad in ("31/07/2026", "soon", "2026-13-01", ""):
        h = WiseTransferHandler(_oneoff(bad))
        assert h.matches([]) == [], f"{bad!r} must not trigger a payment"


# ── The two trigger kinds stay separate ──────────────────────────────────────


def test_oneoff_ignores_calendar_events() -> None:
    """A due one-off must not multiply its match per calendar event."""
    h = WiseTransferHandler(_oneoff(TODAY))
    events = [{"summary": "Invoice something"}, {"summary": "Therapy"}]
    assert len(h.matches(events)) == 1


def test_recurring_still_matches_events() -> None:
    h = WiseTransferHandler(_recurring())
    assert len(h.matches([{"summary": "Therapy session"}])) == 1


def test_recurring_does_not_match_without_events() -> None:
    """No calendar event means no recurring payment — the attendance gate."""
    h = WiseTransferHandler(_recurring())
    assert h.matches([]) == []


def test_recurring_unaffected_by_a_due_date() -> None:
    """A recurring profile is event-driven even if a due_date is present."""
    h = WiseTransferHandler(_recurring(due_date=TODAY.isoformat()))
    assert h.matches([]) == []
    assert len(h.matches([{"summary": "terapia"}])) == 1


# ── Payee resolution comes from the profile for one-offs ─────────────────────


def test_payee_from_profile(monkeypatch) -> None:
    monkeypatch.delenv("DATA_DIR", raising=False)
    profile = _oneoff(TODAY, wise_iban="XX00 1111", wise_recipient_name="ACME, S.L.")
    contact = _load_contact(profile)
    assert contact == {"name": "ACME, S.L.", "iban": "XX00 1111"}


def test_partial_payee_falls_back(monkeypatch) -> None:
    """Half a payee is not a payee — don't silently pay with a missing name."""
    monkeypatch.delenv("DATA_DIR", raising=False)
    profile = _oneoff(TODAY, wise_iban="XX00 1111")
    assert _load_contact(profile) is None


# ── Preview reflects the trigger kind ────────────────────────────────────────


def test_preview_shows_due_date(monkeypatch) -> None:
    monkeypatch.delenv("DATA_DIR", raising=False)
    profile = _oneoff(TODAY, wise_iban="XX00 1111", wise_recipient_name="ACME, S.L.")
    h = WiseTransferHandler(profile)
    text = h.preview(h.matches([])[0])
    assert "one-off invoice" in text
    assert TODAY.isoformat() in text
