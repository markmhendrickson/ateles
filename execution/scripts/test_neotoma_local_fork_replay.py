"""Payload-shape tests for neotoma_local_fork_replay.py.

These assert the request bodies the script builds match the neotoma repo's
own request schemas, so a schema drift (or a re-introduction of the
"flatten observed_at onto the entity" bug that this script was hardened to
fix) is caught before a live --apply run.

The fixtures below are copied by hand from the neotoma repo source read at
the time this test was written (commit a80340c66):
  - StoreRequestSchema / CreateRelationshipRequestSchema:
    src/shared/action_schemas.ts:120-127, :723-763
  - the /store entity-resolution loop's reserved per-entity keys
    (entity_type, target_id, intent) and everything else being entity field
    data: src/actions.ts:7646-7708
  - relationship_key idempotency (type:source:target):
    src/actions.ts:5790-5791, :9763-9803

There is no local copy of the neotoma zod schemas to import (this is the
ateles repo, a separate package) -- if the two repos are ever co-located in
one Python-importable tree, prefer importing the real schema over this
hand-copied fixture.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from neotoma_local_fork_replay import (  # noqa: E402
    build_entity_record,
    build_observation_payload,
    build_relationship_payload,
)

# --- StoreRequestSchema, action_schemas.ts:723-763 -------------------------
STORE_REQUEST_TOP_LEVEL_KEYS = {
    "user_id",
    "entities",
    "relationships",
    "interpretation",
    "source_priority",
    "observation_source",
    "source_peer_id",
    "external_actor",
    "idempotency_key",
    "file_idempotency_key",
    "file_content",
    "file_path",
    "mime_type",
    "original_filename",
    "source_storage",
    "commit",
    "strict",
    "intake",
}

# Reserved keys the /store entity-resolution loop reads off each entity
# record before treating the remainder as field data (actions.ts:7646-7708).
STORE_ENTITY_RESERVED_KEYS = {"entity_type", "target_id", "intent"}

# --- CreateRelationshipRequestSchema, action_schemas.ts:120-127 ------------
CREATE_RELATIONSHIP_REQUEST_KEYS = {
    "relationship_type",
    "source_entity_id",
    "target_entity_id",
    "source_id",
    "metadata",
    "user_id",
}


OBSERVATION_ROW = (
    "obs-local-1",  # local_id
    "ent_deadbeef00000000000000",  # entity_id
    "task",  # entity_type
    "1.0.0",  # schema_version
    "src-1",  # source_id
    None,  # interpretation_id
    "2026-08-05T00:00:00.000Z",  # observed_at
    100,  # specificity_score
    100,  # source_priority
    json.dumps({"title": "Do the thing", "status": "open"}),  # fields_json
    "2026-08-05T00:00:00.000Z",  # created_at
    "user-1",  # user_id
    "existing-idem",  # existing_idem_key
    "import",  # observation_source
)

RELATIONSHIP_ROW = (
    "rel-local-1",  # local_id
    "PART_OF:ent_a:ent_b",  # relationship_key
    "PART_OF",  # relationship_type
    "ent_a00000000000000000000",  # source_entity_id
    "ent_b00000000000000000000",  # target_entity_id
    "src-1",  # source_id
    None,  # interpretation_id
    "2026-08-05T00:00:00.000Z",  # observed_at
    100,  # specificity_score
    100,  # source_priority
    json.dumps({"note": "linked during replay"}),  # metadata_json
    "2026-08-05T00:00:00.000Z",  # created_at
    "user-1",  # user_id
)


def test_build_entity_record_uses_target_id_not_entity_id():
    record = build_entity_record("task", "ent_deadbeef00000000000000", {"title": "x"})
    assert record["target_id"] == "ent_deadbeef00000000000000"
    assert "entity_id" not in record, (
        "entity_id has no meaning on a /store entity record (actions.ts:7646-7708); "
        "it would be silently treated as field data and flagged UNKNOWN_FIELD."
    )


def test_build_entity_record_flattens_fields_alongside_reserved_keys():
    record = build_entity_record("task", "ent_x", {"title": "x", "status": "open"})
    assert record["entity_type"] == "task"
    assert record["title"] == "x"
    assert record["status"] == "open"


def test_observation_payload_top_level_keys_are_all_declared_on_store_schema():
    payload, idem_key = build_observation_payload(OBSERVATION_ROW, "20260804")
    unknown = set(payload.keys()) - STORE_REQUEST_TOP_LEVEL_KEYS
    assert not unknown, (
        f"payload has keys StoreRequestSchema does not declare: {unknown}"
    )
    assert payload["idempotency_key"] == idem_key
    assert idem_key.startswith("migrate-20260804-obs-")


def test_observation_payload_idempotency_key_is_top_level_not_per_entity():
    payload, _ = build_observation_payload(OBSERVATION_ROW, "20260804")
    assert "idempotency_key" in payload
    for entity in payload["entities"]:
        assert "idempotency_key" not in entity, (
            "idempotency_key belongs at the top level of the /store request "
            "(action_schemas.ts:733), not nested on an entity record -- the "
            "hosted API returns 400 VALIDATION_ERROR otherwise."
        )


def test_observation_payload_entity_record_has_no_reserved_key_collisions_with_fields():
    payload, _ = build_observation_payload(OBSERVATION_ROW, "20260804")
    (entity,) = payload["entities"]
    assert entity["entity_type"] == "task"
    assert entity["target_id"] == "ent_deadbeef00000000000000"
    assert entity["title"] == "Do the thing"
    assert entity["status"] == "open"


def test_observation_payload_never_includes_observed_at_anywhere():
    payload, _ = build_observation_payload(OBSERVATION_ROW, "20260804")
    assert "observed_at" not in payload, (
        "StoreRequestSchema has no observed_at field; the public /store API "
        "server-stamps it (actions.ts:7158/8031)."
    )
    for entity in payload["entities"]:
        assert "observed_at" not in entity, (
            "observed_at must never be flattened onto an entity record -- it "
            "is not a real field of the entity and would corrupt the entity's "
            "own data, exactly the bug this script was hardened to fix."
        )


def test_relationship_payload_matches_create_relationship_schema():
    payload, idem_key = build_relationship_payload(RELATIONSHIP_ROW, "20260804")
    unknown = set(payload.keys()) - CREATE_RELATIONSHIP_REQUEST_KEYS
    assert not unknown, (
        f"payload has keys CreateRelationshipRequestSchema does not declare: {unknown}"
    )
    assert payload["relationship_type"] == "PART_OF"
    assert payload["source_entity_id"] == "ent_a00000000000000000000"
    assert payload["target_entity_id"] == "ent_b00000000000000000000"
    # CreateRelationshipRequestSchema has no idempotency_key field at all;
    # the function still returns one for OUR OWN action log, but it must
    # never appear in the request body sent to hosted.
    assert "idempotency_key" not in payload
    assert idem_key.startswith("migrate-20260804-rel-")


def test_relationship_payload_never_includes_observed_at():
    payload, _ = build_relationship_payload(RELATIONSHIP_ROW, "20260804")
    assert "observed_at" not in payload, (
        "create_relationship has no timestamp override; relationships get a "
        "server-stamped created_at (documented, accepted provenance gap)."
    )


@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"a": 1, "b": "two", "c": None, "d": ["x", "y"], "e": {"nested": True}},
    ],
)
def test_build_entity_record_round_trips_arbitrary_field_shapes(fields):
    record = build_entity_record("note", "ent_x", fields)
    for key, value in fields.items():
        assert record[key] == value
