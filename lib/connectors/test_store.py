"""Tests for the Neotoma write path.

These target the 2026 runaway directly: the payload shape it got wrong, the
clock-derived keys that turn re-runs into duplicates, and the success code it
mistook for persistence.
"""

from __future__ import annotations


import pytest

from lib.connectors.base import ConnectorStatus
from lib.connectors.store import (
    observation_payload,
    ConnectorStore,
    NeotomaUnavailable,
    content_hash,
    idempotency_key,
)


class RecordingStore(ConnectorStore):
    """A store whose transport is captured instead of sent."""

    def __init__(self, responses=None, **kw):
        super().__init__(base_url="https://example.invalid", token="t", **kw)
        self.calls: list[tuple[str, dict | None]] = []
        self.responses = responses or {}

    def _request(self, path, body=None):
        self.calls.append((path, body))
        resp = self.responses.get(path, {})
        return resp(body) if callable(resp) else resp


# ── idempotency ─────────────────────────────────────────────────────────────


def test_key_is_stable_across_runs_for_unchanged_content():
    """A re-run over unchanged data must coalesce, not duplicate."""
    payload = {"version": "0.17.0", "image": "deployment-01M1"}
    assert idempotency_key("fly", "v16", payload) == idempotency_key("fly", "v16", payload)


def test_key_changes_when_content_changes():
    a = idempotency_key("fly", "v16", {"version": "0.17.0"})
    b = idempotency_key("fly", "v16", {"version": "0.22.1"})
    assert a != b


def test_key_is_insensitive_to_dict_ordering():
    a = idempotency_key("fly", "v16", {"a": 1, "b": 2})
    b = idempotency_key("fly", "v16", {"b": 2, "a": 1})
    assert a == b


def test_key_contains_no_timestamp():
    """A clock in the key is exactly how a re-run becomes a duplicate."""
    key = idempotency_key("fly", "v16", {"version": "0.17.0"})
    assert key.startswith("connector-fly-v16-")
    # Deterministic given the same inputs — nothing time-varying inside.
    assert key == idempotency_key("fly", "v16", {"version": "0.17.0"})


def test_content_hash_is_short_and_stable():
    h = content_hash({"x": 1})
    assert len(h) == 16 and h == content_hash({"x": 1})


# ── the payload shape the runaway got wrong ─────────────────────────────────


def test_correct_sends_the_flat_field_value_shape():
    """The runaway sent {corrections: map}; Zod rejected it silently."""
    store = RecordingStore()
    store.correct_field("ent_1", "connector_status", "status", "ok", key="k1")

    path, body = store.calls[-1]
    assert path == "/correct"
    assert body == {
        "entity_id": "ent_1",
        "entity_type": "connector_status",
        "field": "status",
        "value": "ok",
        "idempotency_key": "k1",
    }
    assert "corrections" not in body


def test_store_entities_wraps_entities_and_key():
    store = RecordingStore(
        responses={"/store": {"entities": [{"entity_id": "ent_new"}]}}
    )
    ids = store.store_entities([{"entity_type": "x", "a": 1}], key="k2")

    path, body = store.calls[-1]
    assert path == "/store"
    assert body["entities"] == [{"entity_type": "x", "a": 1}]
    assert body["idempotency_key"] == "k2"
    assert ids == ["ent_new"]


def test_store_entities_with_nothing_makes_no_request():
    store = RecordingStore()
    assert store.store_entities([], key="k") == []
    assert store.calls == []


# ── read-back verification ──────────────────────────────────────────────────


def test_verify_stored_confirms_a_field_that_persisted():
    store = RecordingStore(
        responses={
            "/entities/query": {
                "entities": [{"snapshot": {"snapshot": {"version": "0.17.0"}}}]
            }
        }
    )
    assert store.verify_stored("deployment_observation", "version", "0.17.0")


def test_verify_stored_is_false_when_the_field_was_dropped():
    """success: true means the request parsed, not that the data persisted."""
    store = RecordingStore(
        responses={"/entities/query": {"entities": [{"snapshot": {"snapshot": {}}}]}}
    )
    assert not store.verify_stored("task", "body", "some text")


def test_verify_stored_is_false_when_neotoma_is_unreachable():
    class Down(RecordingStore):
        def _request(self, path, body=None):
            raise NeotomaUnavailable("HTTP 502")

    assert not Down().verify_stored("x", "f", "v")


def test_snapshot_unwrapping_handles_all_three_shapes():
    f = ConnectorStore._fields_of
    assert f({"snapshot": {"snapshot": {"a": 1}}}) == {"a": 1}
    assert f({"snapshot": {"a": 1}}) == {"a": 1}
    assert f({"a": 1}) == {"a": 1}


# ── connector_status round-trip ─────────────────────────────────────────────


def test_new_status_is_created_not_corrected():
    status_fields = ConnectorStatus(connector_name="fly", status="ok").to_entity_fields()

    def query_response(_body):
        if any(path == "/store" for path, _ in store.calls):
            return {
                "entities": [
                    {"entity_id": "ent_fly", "snapshot": {"snapshot": status_fields}}
                ]
            }
        return {"entities": []}

    store = RecordingStore(
        responses={
            "/entities/query": query_response,
            "/store": {"entities": [{"entity_id": "ent_fly"}]},
        }
    )
    store.write_status(ConnectorStatus(connector_name="fly", status="ok"))

    assert any(path == "/store" for path, _ in store.calls)
    assert not any(path == "/correct" for path, _ in store.calls)


def test_existing_status_is_corrected_not_recreated():
    """Creating a second entity per run is how duplicates accumulate."""
    snapshot = {"connector_name": "fly"}

    def query_response(_body):
        return {
            "entities": [
                {
                    "entity_id": "ent_fly",
                    "snapshot": {"snapshot": dict(snapshot)},
                }
            ]
        }

    def correct_response(body):
        snapshot[body["field"]] = body["value"]
        return {}

    store = RecordingStore(
        responses={
            "/entities/query": query_response,
            "/correct": correct_response,
        }
    )
    store.write_status(
        ConnectorStatus(
            connector_name="fly", status="ok", last_attempt_at="2026-09-01T12:00:00+00:00"
        )
    )

    assert not any(path == "/store" for path, _ in store.calls)
    corrections = [b for p, b in store.calls if p == "/correct"]
    assert corrections
    assert all(b["entity_id"] == "ent_fly" for b in corrections)
    # connector_name is the identity and must never be corrected.
    assert not any(b["field"] == "connector_name" for b in corrections)


def test_read_status_returns_none_when_absent():
    store = RecordingStore(responses={"/entities/query": {"entities": []}})
    assert store.read_status("fly") is None


def test_read_status_ignores_a_different_connectors_row():
    store = RecordingStore(
        responses={
            "/entities/query": {
                "entities": [{"snapshot": {"snapshot": {"connector_name": "github"}}}]
            }
        }
    )
    assert store.read_status("fly") is None


# ── transport safety ────────────────────────────────────────────────────────


def test_unconfigured_store_refuses_rather_than_silently_skipping():
    store = ConnectorStore(base_url="https://x.invalid", token="")
    assert not store.configured
    with pytest.raises(NeotomaUnavailable, match="no NEOTOMA_BEARER_TOKEN"):
        store.query("task")


# ── the ERR_IDEMPOTENCY_MISMATCH trap ───────────────────────────────────────
#
# Neotoma hashes the FULL entity payload server-side. A wall-clock field inside
# the entity therefore changes the content every run under a stable key, and the
# store is rejected forever. This froze every synced issue in
# sync_issues_from_github.ts and needed a one-time migration token to escape.


def test_observed_at_does_not_change_the_key():
    """The load-bearing property: our clock must not poison the content hash."""
    a = idempotency_key("fly", "v16", {"version": "0.17.0", "observed_at": "2026-09-01T00:00:00Z"})
    b = idempotency_key("fly", "v16", {"version": "0.17.0", "observed_at": "2026-09-02T12:34:56Z"})
    assert a == b


def test_a_real_source_change_still_changes_the_key():
    """Stripping volatile fields must not make genuine changes invisible."""
    a = idempotency_key("fly", "v16", {"version": "0.17.0", "observed_at": "2026-09-01T00:00:00Z"})
    b = idempotency_key("fly", "v16", {"version": "0.22.1", "observed_at": "2026-09-01T00:00:00Z"})
    assert a != b


def test_observation_payload_strips_only_our_own_clock():
    payload = observation_payload(
        {
            "version": "0.17.0",
            "deployed_at": "2026-08-27T14:46:00Z",  # the SOURCE's timestamp — kept
            "observed_at": "2026-09-02T00:00:00Z",  # OUR clock — stripped
            "observed_by": "fly",
            "connector_name": "fly",
        }
    )
    assert payload == {"version": "0.17.0", "deployed_at": "2026-08-27T14:46:00Z"}


def test_unchanged_source_is_byte_stable_across_runs():
    """An unchanged record re-observed must hash identically, or the no-op fails."""
    record = {"version": "0.17.0", "image_ref": "deployment-01M1", "deployed_at": "2026-08-27T14:46:00Z"}
    run1 = content_hash(observation_payload({**record, "observed_at": "2026-09-01T08:00:00Z"}))
    run2 = content_hash(observation_payload({**record, "observed_at": "2026-09-02T09:15:00Z"}))
    assert run1 == run2
