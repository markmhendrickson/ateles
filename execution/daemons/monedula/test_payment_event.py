"""payment_event store payloads carry a declared identity and strict mode. No network."""

import json
from pathlib import Path

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
    def request(method, path, body=None):
        calls.append((method, path, body))
        if method == "POST":
            return {"entity_ids": ["ent_synthetic_event"]}
        return {"snapshot": {"snapshot": {
            "approval_id": "ent_a", "payment_date": "2030-01-02", "status": "sent"
        }}}

    monkeypatch.setattr(payment_approval, "_request", request)
    payment_event.store(FIELDS, payment_date="2030-01-02", approval_id="ent_a")
    (method, path, body), (read_method, read_path, _) = calls
    assert (method, path) == ("POST", "/store") and body["strict"] is True
    assert body["entities"][0]["approval_id"] == "ent_a"
    assert (read_method, read_path) == ("GET", "/entities/ent_synthetic_event")


def test_session_keys_are_trimmed_before_they_become_identity():
    body = payment_event.store_body(FIELDS, payment_date="2030-01-02",
                                    session_keys=[" synthetic_handler:evt-1 "])
    assert body["entities"][0]["primary_session_key"] == "synthetic_handler:evt-1"
    assert body["entities"][0]["session_keys"] == ["synthetic_handler:evt-1"]


def test_changed_terms_for_one_calendar_session_store_distinct_events(monkeypatch):
    schema = json.loads(
        (Path(__file__).resolve().parents[2] / "schemas" / "payment_event.json").read_text()
    )
    rules = schema["schema_definition"]["canonical_name_fields"]
    assert rules[:2] == [
        {"composite": ["approval_id", "obligation_key", "payment_date"]},
        {"composite": ["primary_session_key", "obligation_key", "payment_date"]},
    ]

    rows: dict[str, dict] = {}
    keys: dict[str, str] = {}

    def request(method, path, body=None):
        if method == "POST":
            entity = body["entities"][0]
            identity = (entity["primary_session_key"], entity["obligation_key"], entity["payment_date"])
            name = "|".join(identity)
            entity_id = keys.setdefault(name, f"ent_synthetic_{len(keys)}")
            rows[entity_id] = entity
            return {"entity_ids": [entity_id]}
        return {"snapshot": {"snapshot": rows[path.rsplit("/", 1)[1]]}}

    monkeypatch.setattr(payment_approval, "_request", request)
    identity = {"payment_date": "2030-01-02", "session_keys": ["synthetic_handler:evt-1"]}
    first = payment_event.store_body(FIELDS, **identity, obligation_key="terms_a")
    changed = payment_event.store_body(FIELDS, **identity, obligation_key="terms_b")
    retried = payment_event.store_body(FIELDS, **identity, obligation_key="terms_a")
    assert first["idempotency_key"] != changed["idempotency_key"]
    assert first["idempotency_key"] == retried["idempotency_key"]
    for obligation in ("terms_a", "terms_b", "terms_a"):
        payment_event.store(FIELDS, **identity, obligation_key=obligation)
    assert len(rows) == 2
    assert {row["obligation_key"] for row in rows.values()} == {"terms_a", "terms_b"}


def test_approval_id_identity_also_includes_obligation_key():
    base = {"payment_date": "2030-01-02", "approval_id": "ent_approval"}
    first = payment_event.store_body(FIELDS, **base, obligation_key="terms_a")
    changed = payment_event.store_body(FIELDS, **base, obligation_key="terms_b")
    assert first["entities"][0]["approval_id"] == "ent_approval"
    assert first["idempotency_key"] != changed["idempotency_key"]


def test_store_rejects_obligation_key_readback_mismatch(monkeypatch):
    def request(method, path, body=None):
        if method == "POST":
            return {"entity_ids": ["ent_synthetic_event"]}
        return {"snapshot": {"snapshot": {
            "primary_session_key": "synthetic_handler:evt-1",
            "obligation_key": "stale_terms",
            "payment_date": "2030-01-02",
            "status": "sent",
        }}}

    monkeypatch.setattr(payment_approval, "_request", request)
    with pytest.raises(payment_event.PaymentEventError, match="obligation_key"):
        payment_event.store(
            FIELDS, payment_date="2030-01-02", session_keys=["synthetic_handler:evt-1"],
            obligation_key="current_terms",
        )
