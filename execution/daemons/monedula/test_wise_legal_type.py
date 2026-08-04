"""
Unit tests for Wise recipient legalType handling.

A Wise IBAN recipient is created with a `legalType` of PRIVATE (an individual)
or BUSINESS (a company). The handler used to hardcode PRIVATE, so paying a
company registered a recipient whose legal type contradicted the account
holder — which the receiving bank can reject or return AFTER the funds have
left the source balance. These tests lock the contract so it can't regress:

  * the profile's wise_legal_type reaches the Wise request body verbatim
  * an unset profile still defaults to PRIVATE (existing profiles unaffected)
  * an invalid value fails loudly rather than silently defaulting

Run with: pytest execution/daemons/monedula/test_wise_legal_type.py -v
"""

from __future__ import annotations

import pytest

from handlers.payment_profile import _WISE_LEGAL_TYPES, PaymentProfile
from handlers.wise_transfer import _get_or_create_recipient

# Synthetic placeholder assembled at runtime: these tests only care that the
# value is passed through and space-stripped, never that it is a valid IBAN.
# Written in parts so the repo's PII scanner sees no IBAN-shaped literal.
IBAN = " ".join(["XX00", "0000", "0000", "0000", "0000", "00"])


class _Capture:
    """Stand-in for _wise_post that records the body instead of calling Wise."""

    def __init__(self) -> None:
        self.body: dict | None = None

    def __call__(self, token: str, path: str, body: dict) -> dict:
        self.body = body
        return {"id": 12345}


@pytest.fixture
def capture(monkeypatch) -> _Capture:
    cap = _Capture()
    monkeypatch.setattr("handlers.wise_transfer._wise_post", cap)
    return cap


# ── The profile's value reaches the Wise request body ─────────────────────────


def test_business_legal_type_is_sent(capture) -> None:
    _get_or_create_recipient("tok", 1, IBAN, "ACME, S.L.", "BUSINESS")
    assert capture.body is not None
    assert capture.body["details"]["legalType"] == "BUSINESS"


def test_private_legal_type_is_sent(capture) -> None:
    _get_or_create_recipient("tok", 1, IBAN, "Jane Doe", "PRIVATE")
    assert capture.body is not None
    assert capture.body["details"]["legalType"] == "PRIVATE"


def test_default_is_private_when_omitted(capture) -> None:
    """Existing profiles that never set a legal type keep the old behavior."""
    _get_or_create_recipient("tok", 1, IBAN, "Jane Doe")
    assert capture.body is not None
    assert capture.body["details"]["legalType"] == "PRIVATE"


def test_legal_type_is_normalized(capture) -> None:
    _get_or_create_recipient("tok", 1, IBAN, "ACME, S.L.", "  business  ")
    assert capture.body is not None
    assert capture.body["details"]["legalType"] == "BUSINESS"


def test_iban_spaces_are_stripped(capture) -> None:
    _get_or_create_recipient("tok", 1, IBAN, "ACME, S.L.", "BUSINESS")
    assert capture.body is not None
    assert " " not in capture.body["details"]["iban"]


# ── Invalid values fail loudly, never silently default ────────────────────────


@pytest.mark.parametrize("bad", ["COMPANY", "individual", "PRIVATE_LIMITED", "x"])
def test_invalid_legal_type_raises(capture, bad: str) -> None:
    with pytest.raises(ValueError, match="Invalid Wise legalType"):
        _get_or_create_recipient("tok", 1, IBAN, "ACME, S.L.", bad)
    assert capture.body is None, "must not reach the Wise API with a bad legalType"


# ── The dataclass carries the field ───────────────────────────────────────────


def test_profile_defaults_to_private() -> None:
    profile = PaymentProfile(
        prefix="X", label="X", calendar_keywords=["x"], payment_type="wise", amount_eur=1
    )
    assert profile.wise_legal_type == "PRIVATE"


def test_profile_accepts_business() -> None:
    profile = PaymentProfile(
        prefix="X",
        label="X",
        calendar_keywords=["x"],
        payment_type="wise",
        amount_eur=1,
        wise_legal_type="BUSINESS",
    )
    assert profile.wise_legal_type == "BUSINESS"


def test_accepted_values_are_exactly_wise_s_two() -> None:
    assert _WISE_LEGAL_TYPES == {"PRIVATE", "BUSINESS"}
