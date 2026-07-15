"""
test_share_message.py — every payment confirmation carries a payee-ready message.

Operator standing rule: the confirmation must include a premade message to
copy-paste to the recipient, with a block-explorer link when the payment was
on-chain. The message is in the PAYEE's language, not the operator's.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from handlers import share_message as sm  # noqa: E402


def test_spanish_btc_message_has_explorer_link():
    msg = sm.build_share_message(amount_eur=60, label="Yoga", rail="btc",
                                 explorer_url="https://mempool.space/tx/abc",
                                 language="es")
    assert "60€" in msg
    assert "https://mempool.space/tx/abc" in msg
    assert "Hola" in msg


def test_wise_message_has_no_link_and_no_iban():
    msg = sm.build_share_message(amount_eur=60, label="Terapia", rail="wise",
                                 language="es")
    assert "60€" in msg
    assert "http" not in msg          # Wise has no public explorer
    assert "ES" not in msg.upper().replace("¡HOLA", "")  # never leak an IBAN


def test_language_variants():
    en = sm.build_share_message(amount_eur=60, label="X", rail="wise", language="en")
    ca = sm.build_share_message(amount_eur=60, label="X", rail="wise", language="ca")
    assert "I've just sent you" in en
    assert "Acabo d'enviar-te" in ca


def test_language_name_is_normalized():
    # locale_profile stores names like "Spanish", not codes.
    assert sm._normalize_lang("Spanish") == "es"
    assert sm._normalize_lang("Català") == "ca"
    assert sm._normalize_lang("English") == "en"
    assert sm._normalize_lang("es") == "es"


def test_session_date_included_when_given():
    msg = sm.build_share_message(amount_eur=60, label="Yoga", rail="btc",
                                 explorer_url="https://x/tx/1",
                                 session_date="2026-07-09", language="es")
    assert "2026-07-09" in msg


def test_payee_language_prefers_contact_over_operator(monkeypatch):
    # The payee's own contact wins over the operator's locale.
    monkeypatch.setattr(sm, "_fetch_entity_snapshot", lambda cid: {"language": "Catalan"})
    monkeypatch.setattr(sm, "_locale_field", lambda f: ["Spanish"])
    assert sm.payee_language("ent_contact") == "ca"


def test_payee_language_falls_back_to_secondary_language(monkeypatch):
    # No contact language → operator's SECONDARY language (the local one they
    # transact in), never their primary (English).
    monkeypatch.setattr(sm, "_fetch_entity_snapshot", lambda cid: {})
    monkeypatch.setattr(sm, "_locale_field", lambda f: ["Spanish", "Catalan"])
    assert sm.payee_language("ent_contact") == "es"
