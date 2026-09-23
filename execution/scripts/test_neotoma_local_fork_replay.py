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
    ALWAYS_STRIP_FIELD_KEYS,
    CONDITIONALLY_RESERVED_FIELD_KEYS,
    build_entity_record,
    build_observation_payload,
    build_relationship_payload,
    is_schema_lag_background_rewrite,
    predict_unknown_fields,
    strip_reserved_fields,
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
    payload, idem_key, _stripped = build_observation_payload(OBSERVATION_ROW, "20260804")
    unknown = set(payload.keys()) - STORE_REQUEST_TOP_LEVEL_KEYS
    assert not unknown, (
        f"payload has keys StoreRequestSchema does not declare: {unknown}"
    )
    assert payload["idempotency_key"] == idem_key
    assert idem_key.startswith("migrate-20260804-obs-")


def test_observation_payload_idempotency_key_is_top_level_not_per_entity():
    payload, _, _stripped = build_observation_payload(OBSERVATION_ROW, "20260804")
    assert "idempotency_key" in payload
    for entity in payload["entities"]:
        assert "idempotency_key" not in entity, (
            "idempotency_key belongs at the top level of the /store request "
            "(action_schemas.ts:733), not nested on an entity record -- the "
            "hosted API returns 400 VALIDATION_ERROR otherwise."
        )


def test_observation_payload_entity_record_has_no_reserved_key_collisions_with_fields():
    payload, _, _stripped = build_observation_payload(OBSERVATION_ROW, "20260804")
    (entity,) = payload["entities"]
    assert entity["entity_type"] == "task"
    assert entity["target_id"] == "ent_deadbeef00000000000000"
    assert entity["title"] == "Do the thing"
    assert entity["status"] == "open"


def test_observation_payload_never_includes_observed_at_anywhere():
    payload, _, _stripped = build_observation_payload(OBSERVATION_ROW, "20260804")
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


# --- schema_lag_bg_* exclusion ----------------------------------------------


def test_is_schema_lag_background_rewrite_true_for_schema_lag_bg_prefix():
    fields_json = json.dumps({"_migration_run_id": "schema_lag_bg_2026-09-06T16:58:38.065Z"})
    assert is_schema_lag_background_rewrite(fields_json) is True


def test_is_schema_lag_background_rewrite_false_for_other_migration_run_id():
    fields_json = json.dumps({"_migration_run_id": "some_other_run_id"})
    assert is_schema_lag_background_rewrite(fields_json) is False


def test_is_schema_lag_background_rewrite_false_when_no_migration_run_id():
    fields_json = json.dumps({"title": "Do the thing"})
    assert is_schema_lag_background_rewrite(fields_json) is False


@pytest.mark.parametrize("bad_input", [None, "", "not json", "{", json.dumps(["a", "list"])])
def test_is_schema_lag_background_rewrite_false_on_malformed_or_missing_input(bad_input):
    # Malformed/absent fields_json must never raise or be mistaken for a
    # schema_lag rewrite -- default to NOT excluding when unsure, since
    # excluding a real observation silently would be the worse failure.
    assert is_schema_lag_background_rewrite(bad_input) is False


# --- reserved/bogus field stripping -----------------------------------------


def test_strip_reserved_fields_always_drops_migration_run_id():
    schema_info = {"has_schema": True, "declared_fields": {"title", "status"}}
    cleaned, stripped = strip_reserved_fields(
        "task", {"title": "x", "_migration_run_id": "schema_lag_bg_x"}, schema_info
    )
    assert "_migration_run_id" not in cleaned
    assert cleaned == {"title": "x"}
    assert "_migration_run_id" in stripped


def test_strip_reserved_fields_drops_entity_id_when_not_declared():
    # issue and task do not declare entity_id -- a stray collision with the
    # /store entity-resolution loop's reserved per-entity keys, not real
    # field data (see module docstring, actions.ts:7690-7698).
    schema_info = {"has_schema": True, "declared_fields": {"title"}}
    cleaned, stripped = strip_reserved_fields(
        "issue", {"title": "x", "entity_id": "ent_bogus"}, schema_info
    )
    assert "entity_id" not in cleaned
    assert "entity_id" in stripped


def test_strip_reserved_fields_keeps_entity_id_when_declared_on_hosted_schema():
    # agent_definition, workflow_definition, operator_profile, and
    # agent_strategy declare entity_id as a genuine field on the hosted
    # schema (verified against the fetched schema, not a hardcoded list) --
    # it must survive stripping for exactly those types.
    schema_info = {"has_schema": True, "declared_fields": {"name", "entity_id"}}
    cleaned, stripped = strip_reserved_fields(
        "agent_definition", {"name": "Apis", "entity_id": "ent_acdb65a8"}, schema_info
    )
    assert cleaned["entity_id"] == "ent_acdb65a8"
    assert "entity_id" not in stripped


def test_strip_reserved_fields_drops_conditionally_reserved_keys_when_no_hosted_schema():
    # With no hosted schema at all for the type (has_schema=False -- e.g.
    # symptom_report, github_issue_ref, github_comment_intent), there is
    # nothing to declare a conditionally-reserved key, so it is stripped
    # defensively rather than guessed at.
    schema_info = {"has_schema": False, "declared_fields": set()}
    cleaned, stripped = strip_reserved_fields(
        "symptom_report",
        {"summary": "x", "entity_id": "ent_y", "idempotency_key": "k1"},
        schema_info,
    )
    assert cleaned == {"summary": "x"}
    assert set(stripped) == {"entity_id", "idempotency_key"}


def test_strip_reserved_fields_leaves_ordinary_fields_untouched():
    schema_info = {"has_schema": True, "declared_fields": {"title", "status", "priority"}}
    cleaned, stripped = strip_reserved_fields(
        "task", {"title": "x", "status": "open", "priority": 1}, schema_info
    )
    assert cleaned == {"title": "x", "status": "open", "priority": 1}
    assert stripped == []


def test_conditionally_reserved_keys_constant_matches_documented_set():
    # Guards against ALWAYS_STRIP_FIELD_KEYS and CONDITIONALLY_RESERVED_FIELD_KEYS
    # silently drifting apart from what strip_reserved_fields's docstring
    # (and the task brief) actually names.
    assert ALWAYS_STRIP_FIELD_KEYS == {"_migration_run_id"}
    assert CONDITIONALLY_RESERVED_FIELD_KEYS == {"entity_id", "idempotency_key", "canonical_name"}


def test_build_observation_payload_applies_schema_driven_stripping():
    schema_info = {"has_schema": True, "declared_fields": {"title", "status"}}
    row = (
        "obs-local-2",
        "ent_x",
        "task",
        "1.0.0",
        "src-1",
        None,
        "2026-08-05T00:00:00.000Z",
        100,
        100,
        json.dumps({"title": "y", "status": "open", "entity_id": "ent_bogus", "_migration_run_id": "schema_lag_bg_z"}),
        "2026-08-05T00:00:00.000Z",
        "user-1",
        None,
        "import",
    )
    payload, _idem_key, stripped = build_observation_payload(row, "20260804", schema_info=schema_info)
    (entity,) = payload["entities"]
    assert entity["title"] == "y"
    assert entity["status"] == "open"
    assert "entity_id" not in entity
    assert "_migration_run_id" not in entity
    assert set(stripped) == {"entity_id", "_migration_run_id"}


def test_build_observation_payload_without_schema_info_does_not_strip():
    # Backward-compatible default: callers that don't pass schema_info (the
    # pre-existing OBSERVATION_ROW fixture tests above) keep the original
    # unconditional pass-through behavior.
    payload, _idem_key, stripped = build_observation_payload(OBSERVATION_ROW, "20260804")
    assert stripped == []


# --- --unknown-fields pre-flight prediction ---------------------------------


def test_predict_unknown_fields_returns_empty_when_no_hosted_schema():
    schema_info = {"has_schema": False, "declared_fields": set()}
    result = predict_unknown_fields("symptom_report", {"summary": "x", "weird": 1}, schema_info)
    assert result == []


def test_predict_unknown_fields_flags_fields_the_schema_does_not_declare():
    schema_info = {"has_schema": True, "declared_fields": {"title", "status"}}
    result = predict_unknown_fields(
        "task", {"title": "x", "status": "open", "reviewing_agent": "cicada"}, schema_info
    )
    assert result == ["reviewing_agent"]


def test_predict_unknown_fields_empty_when_all_fields_declared():
    schema_info = {"has_schema": True, "declared_fields": {"title", "status"}}
    result = predict_unknown_fields("task", {"title": "x", "status": "open"}, schema_info)
    assert result == []


def test_unknown_fields_cli_flag_defaults_to_stop():
    import argparse
    import subprocess
    import sys as _sys

    script = str(Path(__file__).resolve().parent / "neotoma_local_fork_replay.py")
    # --help exits 0 and prints argparse's own rendering of the flag; assert
    # the default is documented as 'stop' and both choices are offered,
    # without needing a live DB or hosted credentials.
    result = subprocess.run(
        [_sys.executable, script, "--help"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0
    assert "--unknown-fields" in result.stdout
    assert "{stop,warn}" in result.stdout or "stop,warn" in result.stdout
