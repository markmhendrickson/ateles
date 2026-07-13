"""
test_wise_neotoma_recipient.py — Wise recipient resolution is Neotoma-sourced.

Verifies the parquet dependency is gone and the payee resolves from:
  1. profile.wise_recipient_id (direct, preferred)
  2. profile.wise_iban
  3. Neotoma contact entity (profile.contact_id) — via a mocked fetch
And that a missing recipient degrades to status="manual_required".
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from handlers import wise_transfer as wt  # noqa: E402
from handlers.payment_profile import PaymentProfile  # noqa: E402


def _profile(**kw):
    base = dict(prefix="X", label="X", calendar_keywords=[], payment_type="wise",
                amount_eur=100)
    base.update(kw)
    return PaymentProfile(**base)


# ── No parquet dependency ────────────────────────────────────────────────────

def test_module_has_no_parquet_imports():
    src = (Path(__file__).parent / "handlers" / "wise_transfer.py").read_text()
    for banned in ("pyarrow", "pandas", "read_parquet", "contacts.parquet", "DATA_DIR"):
        # allow the word inside comments that explicitly say it's removed
        offending = [
            ln for ln in src.splitlines()
            if banned in ln and "no parquet" not in ln.lower()
            and "parquet removal" not in ln.lower()
        ]
        assert not offending, f"{banned!r} still referenced: {offending}"


# ── Neotoma contact lookup ───────────────────────────────────────────────────

def test_load_contact_reads_neotoma_snapshot(monkeypatch):
    monkeypatch.setattr(wt, "_fetch_contact_snapshot", lambda cid: {
        "name": "Jane Payee", "iban": "ES1234", "wise_recipient_id": "999",
        "btc_address": "bc1xyz",
    })
    c = wt._load_contact(_profile(contact_id="ent_abc"))
    assert c["name"] == "Jane Payee"
    assert c["iban"] == "ES1234"
    assert c["wise_recipient_id"] == "999"


def test_load_contact_none_without_contact_id():
    assert wt._load_contact(_profile(contact_id="")) is None


def test_load_contact_none_when_neotoma_misses(monkeypatch):
    monkeypatch.setattr(wt, "_fetch_contact_snapshot", lambda cid: None)
    assert wt._load_contact(_profile(contact_id="ent_missing")) is None


# ── Recipient resolution precedence in execute() ─────────────────────────────

def test_execute_prefers_profile_recipient_id(monkeypatch):
    captured = {}

    def _fake_exec(token, iban, name, amount, ref, label="", recipient_id="", dry_run=False):
        captured.update(recipient_id=recipient_id, iban=iban, dry_run=dry_run)
        return {"status": "dry_run"}

    monkeypatch.setenv("WISE_API_TOKEN", "t")
    monkeypatch.setenv("MONEDULA_DRYRUN", "1")
    monkeypatch.setattr(wt, "_execute_wise_transfer", _fake_exec)
    # Contact lookup must NOT be needed when recipient_id is on the profile.
    monkeypatch.setattr(wt, "_load_contact", lambda p: pytest.fail("should not hit Neotoma"))

    h = wt.WiseTransferHandler(_profile(wise_recipient_id="1468664388"))
    h.execute({"summary": "t"})
    assert captured["recipient_id"] == "1468664388"
    assert captured["dry_run"] is True


def test_execute_falls_back_to_neotoma_contact(monkeypatch):
    captured = {}

    def _fake_exec(token, iban, name, amount, ref, label="", recipient_id="", dry_run=False):
        captured.update(recipient_id=recipient_id, iban=iban)
        return {"status": "dry_run"}

    monkeypatch.setenv("WISE_API_TOKEN", "t")
    monkeypatch.setenv("MONEDULA_DRYRUN", "1")
    monkeypatch.setattr(wt, "_execute_wise_transfer", _fake_exec)
    monkeypatch.setattr(wt, "_load_contact", lambda p: {
        "name": "Jane", "iban": "ES99", "wise_recipient_id": "555"})

    h = wt.WiseTransferHandler(_profile(contact_id="ent_abc"))
    h.execute({"summary": "t"})
    assert captured["recipient_id"] == "555"  # contact's verified id preferred


def test_execute_manual_required_when_nothing_resolves(monkeypatch):
    monkeypatch.setattr(wt, "_load_contact", lambda p: None)
    h = wt.WiseTransferHandler(_profile(contact_id="ent_missing"))
    result = h.execute({"summary": "t"})
    assert result["status"] == "manual_required"
