"""
Tests for lookup-first Wise recipient resolution.

`_get_or_create_recipient` used to do no lookup at all: it POSTed to
/v1/accounts unconditionally, so every run created a NEW recipient for a payee
that already existed. Two consequences:

  * duplicate recipients accumulate in the Wise account
  * recipient creation is the step Wise runs Verification of Payee against, so
    a needless create is a needless chance to be blocked on a name mismatch for
    an account that is already known-good

Matching is by IBAN, never by name. The IBAN is the account's identity; the
holder name varies in spacing, accents and abbreviation between what a payee
tells you and what their bank stores: one payee produced four different
renderings of their own name, all rejected by VoP, for one unchanged IBAN.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from handlers import wise_transfer as wt  # noqa: E402

IBAN = "ES91 1234 0418 4502 0005 1332"


def _acct(account_id: int, iban: str) -> dict:
    return {"id": account_id, "details": {"iban": iban}}


# ── IBAN normalisation ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("ES91 1234 0418", "ES9112340418"),
        ("es911234 0418", "ES9112340418"),
        ("  ES91  1234  ", "ES911234"),
        ("", ""),
    ],
)
def test_normalize_iban(raw, expected):
    assert wt._normalize_iban(raw) == expected


# ── Lookup finds an existing recipient ───────────────────────────────────────


def test_reuses_existing_recipient_despite_spacing(monkeypatch):
    """The stored IBAN has no spaces; the profile's does. Same account."""
    monkeypatch.setattr(
        wt, "_wise_get", lambda *a, **k: [_acct(555, "ES9112340418450200051332")]
    )
    posted = []
    monkeypatch.setattr(
        wt, "_wise_post", lambda *a, **k: posted.append(a) or {"id": 999}
    )

    got = wt._get_or_create_recipient("tok", 1, IBAN, "Ana Perez Gomez")

    assert got == 555, "must reuse the existing recipient"
    assert not posted, "must NOT create a duplicate recipient"


def test_reuse_ignores_the_holder_name(monkeypatch):
    """A different name on the same IBAN is still the same account."""
    monkeypatch.setattr(
        wt, "_wise_get", lambda *a, **k: [_acct(555, "ES9112340418450200051332")]
    )
    monkeypatch.setattr(
        wt, "_wise_post", lambda *a, **k: pytest.fail("must not create")
    )
    # Every rendering of the holder name resolves to the same account.
    for name in (
        "AnaPerez Gomez",
        "Ana Perez Gomez",
        "Ana María Pérez Gómez",
        "A Perez Gomez",
    ):
        assert wt._get_or_create_recipient("tok", 1, IBAN, name) == 555


@pytest.mark.parametrize(
    "payload",
    [
        [_acct(555, "ES9112340418450200051332")],
        {"content": [_acct(555, "ES9112340418450200051332")]},
        {"accounts": [_acct(555, "ES9112340418450200051332")]},
    ],
)
def test_handles_list_and_wrapped_response_shapes(monkeypatch, payload):
    monkeypatch.setattr(wt, "_wise_get", lambda *a, **k: payload)
    assert wt._find_existing_recipient("tok", 1, IBAN) == 555


# ── Falling through to create ────────────────────────────────────────────────


def test_creates_when_no_match(monkeypatch):
    monkeypatch.setattr(wt, "_wise_get", lambda *a, **k: [_acct(1, "DE00 1111")])
    monkeypatch.setattr(wt, "_wise_post", lambda *a, **k: {"id": 777})
    assert wt._get_or_create_recipient("tok", 1, IBAN, "Someone") == 777


def test_lookup_failure_falls_back_to_create(monkeypatch):
    """A lookup outage must not block the payment — create as before."""

    def boom(*a, **k):
        raise OSError("wise unreachable")

    monkeypatch.setattr(wt, "_wise_get", boom)
    monkeypatch.setattr(wt, "_wise_post", lambda *a, **k: {"id": 777})
    assert wt._get_or_create_recipient("tok", 1, IBAN, "Someone") == 777


def test_unexpected_shape_falls_back_to_create(monkeypatch):
    monkeypatch.setattr(wt, "_wise_get", lambda *a, **k: "not-a-list")
    monkeypatch.setattr(wt, "_wise_post", lambda *a, **k: {"id": 777})
    assert wt._get_or_create_recipient("tok", 1, IBAN, "Someone") == 777


def test_malformed_entries_are_skipped(monkeypatch):
    monkeypatch.setattr(
        wt,
        "_wise_get",
        lambda *a, **k: [
            None,
            "junk",
            {"no_details": True},
            {"id": None, "details": {"iban": "ES9112340418450200051332"}},
            _acct(555, "ES9112340418450200051332"),
        ],
    )
    assert wt._find_existing_recipient("tok", 1, IBAN) == 555


# ── legalType validation still applies on the create path ────────────────────


def test_invalid_legal_type_still_rejected(monkeypatch):
    monkeypatch.setattr(wt, "_wise_get", lambda *a, **k: [])
    monkeypatch.setattr(wt, "_wise_post", lambda *a, **k: {"id": 777})
    with pytest.raises(ValueError, match="legalType"):
        wt._get_or_create_recipient("tok", 1, IBAN, "X", legal_type="CHARITY")
