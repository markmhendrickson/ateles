"""payment_event store payloads carry a declared identity and strict mode. No network."""

import pytest

import payment_approval
import payment_event

FIELDS = {"label": "Synthetic session", "payment_type": "btc", "amount_eur": 10,
          "btc_address": "bc1qsyntheticaddress0000", "status": "sent"}


def test_store_body_carries_approval_id_session_keys_and_strict():
    body = payment_event.store_body(
        FIELDS, payment_date="2030-01-02", approval_id="ent_synthetic_approval",
        session_keys=["synthetic_handler:evt-1", "synthetic_handler:evt-2"])
    assert body["strict"] is True
    (entity,) = body["entities"]
    assert entity["entity_type"] == "payment_event"
    assert entity["approval_id"] == "ent_synthetic_approval"
    assert entity["session_keys"] == ["synthetic_handler:evt-1", "synthetic_handler:evt-2"]
    assert entity["primary_session_key"] == "synthetic_handler:evt-1"
    assert entity["payment_date"] == "2030-01-02"
    assert "btc_address" not in body["idempotency_key"]
    assert "bc1q" not in body["idempotency_key"]


def test_two_payments_to_one_address_get_distinct_identities():
    a = payment_event.store_body(FIELDS, payment_date="2030-01-02", approval_id="ent_a")
    b = payment_event.store_body(FIELDS, payment_date="2030-01-09", approval_id="ent_b")
    assert a["idempotency_key"] != b["idempotency_key"]


def test_session_key_alone_is_enough():
    body = payment_event.store_body(FIELDS, payment_date="2030-01-02",
                                    session_keys=["synthetic_handler:evt-1"])
    assert body["entities"][0]["primary_session_key"] == "synthetic_handler:evt-1"
    assert "approval_id" not in body["entities"][0]


def test_no_identity_is_refused():
    with pytest.raises(payment_event.PaymentEventError):
        payment_event.store_body(FIELDS, payment_date="2030-01-02")
    with pytest.raises(payment_event.PaymentEventError):
        payment_event.store_body(FIELDS, payment_date="", approval_id="ent_a")


def test_store_posts_the_strict_body(monkeypatch):
    calls = []
    monkeypatch.setattr(payment_approval, "_request",
                        lambda m, p, b=None: calls.append((m, p, b)) or {})
    payment_event.store(FIELDS, payment_date="2030-01-02", approval_id="ent_a")
    ((method, path, body),) = calls
    assert (method, path) == ("POST", "/store") and body["strict"] is True
    assert body["entities"][0]["approval_id"] == "ent_a"
