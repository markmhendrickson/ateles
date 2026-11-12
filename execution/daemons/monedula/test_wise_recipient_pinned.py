"""
Pinned Wise recipient (wise_recipient_id) and recipient-status fail-closed
tests. All Wise calls are faked; nothing here touches the network.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from handlers import wise_transfer as wt  # noqa: E402
from handlers.payment_profile import PaymentProfile, parse_wise_recipient_id  # noqa: E402

FAKE_IBAN = " ".join(["XX00", "0000", "0000", "0000", "0000", "00"])
NORM = FAKE_IBAN.replace(" ", "")


def _profile(**kw) -> PaymentProfile:
    base = dict(
        prefix="THERAPY",
        label="Therapy",
        calendar_keywords=["therapy"],
        payment_type="wise",
        amount_eur=1,
        wise_reference="ref",
        wise_recipient_name="Test Payee",
    )
    base.update(kw)
    return PaymentProfile(**base)


# ── wise_recipient_id parsing: number or string, int out, else fail closed ──


@pytest.mark.parametrize("raw,want", [(42, 42), ("42", 42), (42.0, 42), (" 42 ", 42)])
def test_recipient_id_normalised_to_int(raw, want):
    assert parse_wise_recipient_id(raw) == want
    assert isinstance(parse_wise_recipient_id(raw), int)


@pytest.mark.parametrize("raw", ["abc", "4a", 1.5, True, -3, 0, "-3"])
def test_unparseable_recipient_id_fails_closed(raw):
    with pytest.raises(ValueError):
        parse_wise_recipient_id(raw)


def test_blank_recipient_id_means_not_set():
    assert parse_wise_recipient_id("") is None
    assert parse_wise_recipient_id(None) is None


# ── pinned recipient: inactive / unknown fail closed and raise ──────────────


def test_inactive_pinned_recipient_fails_closed_and_raises(monkeypatch):
    monkeypatch.setenv("WISE_API_TOKEN", "fake-token")
    h = wt.WiseTransferHandler(_profile(wise_recipient_id=111))
    h.recipient_status_lookup = lambda token, rid: {"id": rid, "active": False}
    monkeypatch.setattr(wt, "_wise_post", lambda *a, **k: pytest.fail("no create/transfer"))
    with pytest.raises(wt.WiseRecipientError, match="inactive"):
        h.preflight({})
    with pytest.raises(wt.WiseRecipientError, match="inactive"):
        h.execute({})


def test_unknown_pinned_recipient_fails_closed_and_raises(monkeypatch):
    monkeypatch.setenv("WISE_API_TOKEN", "fake-token")
    h = wt.WiseTransferHandler(_profile(wise_recipient_id=111))
    h.recipient_status_lookup = lambda token, rid: None
    monkeypatch.setattr(wt, "_wise_post", lambda *a, **k: pytest.fail("no create/transfer"))
    with pytest.raises(wt.WiseRecipientError, match="unknown"):
        h.preflight({})


def test_pinned_recipient_is_used_exactly_and_never_looked_up_by_iban(monkeypatch):
    monkeypatch.setattr(
        wt, "_wise_get", lambda *a, **k: pytest.fail("no IBAN lookup when pinned")
    )
    got = wt._get_or_create_recipient(
        "tok",
        1,
        "",
        "n",
        recipient_id=777,
        recipient_status_lookup=lambda t, r: {"id": r, "active": True},
    )
    assert got == 777


def test_pinned_recipient_happy_path_transfers_to_that_account(monkeypatch):
    monkeypatch.setenv("WISE_API_TOKEN", "fake-token")
    h = wt.WiseTransferHandler(_profile(wise_recipient_id="555"))
    h.recipient_status_lookup = lambda token, rid: {"id": rid, "active": True}
    monkeypatch.setattr(wt, "_get_wise_profile_id", lambda t: 9)
    monkeypatch.setattr(wt, "_create_quote", lambda *a, **k: "q")
    seen = {}

    def create_transfer(token, target, quote, ref):
        seen["target"] = target
        return 1

    monkeypatch.setattr(wt, "_create_transfer", create_transfer)
    monkeypatch.setattr(wt, "_fund_transfer", lambda *a, **k: {"status": "COMPLETED"})
    monkeypatch.setattr(wt, "_update_task", lambda *a, **k: None)
    res = h.execute({})
    assert res["status"] == "sent"
    assert seen["target"] == 555


def test_payee_identity_binds_to_the_pinned_recipient():
    a = wt.WiseTransferHandler(_profile(wise_recipient_id=1)).payee_identity()
    b = wt.WiseTransferHandler(_profile(wise_recipient_id=2)).payee_identity()
    assert a and b and a != b


# ── IBAN fallback: active only, ambiguity fails closed ──────────────────────


def _acct(i, active=True):
    return {"id": i, "active": active, "details": {"iban": NORM}}


def test_stale_inactive_first_match_is_never_chosen(monkeypatch):
    """Live case: an inactive and an active recipient share one IBAN, the
    inactive one first. The old first-match code picked the inactive one."""
    monkeypatch.setattr(
        wt, "_wise_get", lambda *a, **k: [_acct(1, active=False), _acct(2)]
    )
    assert wt._find_existing_recipient("tok", 1, FAKE_IBAN) == 2


def test_only_inactive_match_means_no_match_not_the_inactive_one(monkeypatch):
    monkeypatch.setattr(wt, "_wise_get", lambda *a, **k: [_acct(1, active=False)])
    assert wt._find_existing_recipient("tok", 1, FAKE_IBAN) is None


def test_more_than_one_active_match_fails_closed(monkeypatch):
    monkeypatch.setattr(wt, "_wise_get", lambda *a, **k: [_acct(1), _acct(2)])
    monkeypatch.setattr(wt, "_wise_post", lambda *a, **k: pytest.fail("no create"))
    with pytest.raises(wt.WiseRecipientError, match="ambiguous"):
        wt._get_or_create_recipient("tok", 1, FAKE_IBAN, "n")


# ── partial profile payee fails closed ──────────────────────────────────────


def test_half_filled_profile_does_not_fall_back_to_contacts(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("WISE_API_TOKEN", "fake-token")
    h = wt.WiseTransferHandler(_profile(wise_iban=FAKE_IBAN, wise_recipient_name=""))
    assert wt._load_contact(h.profile) is None
    with pytest.raises(wt.WiseRecipientError, match="payee unresolved"):
        h.preflight({})
