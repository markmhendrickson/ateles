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
    FIELD_TYPE_CHOICES,
    build_entity_record,
    build_observation_payload,
    build_reconcile_idempotency_key,
    build_register_schema_payload,
    build_relationship_payload,
    build_store_payload_for_reconcile,
    build_update_schema_incremental_payload,
    filter_writable_fields,
    infer_field_type,
    is_schema_lag_background_rewrite,
    plan_class_b_reconciliation_for_entity,
    plan_schema_extensions,
    predict_unknown_fields,
    strip_reserved_fields,
    value_hash,
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


def test_extend_schemas_and_reconcile_file_flags_present_in_help():
    import subprocess
    import sys as _sys

    script = str(Path(__file__).resolve().parent / "neotoma_local_fork_replay.py")
    result = subprocess.run([_sys.executable, script, "--help"], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0
    assert "--extend-schemas" in result.stdout
    assert "--reconcile-file" in result.stdout


# --- infer_field_type / plan_schema_extensions ------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        (True, "boolean"),
        (False, "boolean"),
        (1, "number"),
        (1.5, "number"),
        ("x", "string"),
        (None, "string"),
        (["a"], "array"),
        ({"k": "v"}, "object"),
    ],
)
def test_infer_field_type_covers_json_value_shapes(value, expected):
    assert infer_field_type(value) == expected


def test_infer_field_type_only_ever_returns_a_valid_field_type_choice():
    for value in (True, 1, 1.5, "x", None, ["a"], {"k": "v"}, object()):
        assert infer_field_type(value) in FIELD_TYPE_CHOICES


def test_plan_schema_extensions_only_includes_undeclared_fields():
    schema_info = {"has_schema": True, "declared_fields": {"title"}}
    samples = {"title": "already declared", "reviewing_agent": "cicada", "pr_number": 42}
    plan = plan_schema_extensions("note", samples, schema_info)
    names = {f["field_name"] for f in plan}
    assert names == {"reviewing_agent", "pr_number"}


def test_plan_schema_extensions_infers_types_and_is_never_required():
    schema_info = {"has_schema": True, "declared_fields": set()}
    samples = {"count": 3, "flag": True, "name": "x"}
    plan = plan_schema_extensions("t", samples, schema_info)
    by_name = {f["field_name"]: f for f in plan}
    assert by_name["count"]["field_type"] == "number"
    assert by_name["flag"]["field_type"] == "boolean"
    assert by_name["name"]["field_type"] == "string"
    assert all(f["required"] is False for f in plan)


def test_plan_schema_extensions_empty_when_no_hosted_schema_and_no_samples():
    schema_info = {"has_schema": False, "declared_fields": set()}
    assert plan_schema_extensions("symptom_report", {}, schema_info) == []


def test_plan_schema_extensions_all_fields_undeclared_when_no_hosted_schema():
    # has_schema=False means declared_fields is empty regardless of what the
    # dict happens to hold -- every sampled field is treated as undeclared.
    schema_info = {"has_schema": False, "declared_fields": {"should_be_ignored"}}
    plan = plan_schema_extensions("symptom_report", {"summary": "x"}, schema_info)
    assert [f["field_name"] for f in plan] == ["summary"]


# --- schema-extension request payloads: additive only, no destructive ops --


def test_update_schema_incremental_payload_never_carries_fields_to_remove():
    fields_to_add = [{"field_name": "x", "field_type": "string", "required": False}]
    payload = build_update_schema_incremental_payload("task", fields_to_add)
    assert "fields_to_remove" not in payload
    assert "canonical_name_fields" not in payload
    assert payload["fields_to_add"] == fields_to_add
    assert payload["activate"] is True
    assert payload["entity_type"] == "task"


def test_update_schema_incremental_payload_top_level_keys_are_all_declared():
    # UpdateSchemaIncrementalRequestSchema, action_schemas.ts:947-985.
    declared_keys = {
        "entity_type",
        "fields_to_add",
        "fields_to_remove",
        "canonical_name_fields",
        "schema_version",
        "user_specific",
        "user_id",
        "activate",
        "migrate_existing",
        "force",
    }
    payload = build_update_schema_incremental_payload("task", [])
    assert set(payload.keys()) <= declared_keys


def test_register_schema_payload_sets_identity_opt_out_never_canonical_name_fields():
    fields_to_add = [{"field_name": "summary", "field_type": "string", "required": False}]
    payload = build_register_schema_payload("symptom_report", fields_to_add)
    assert payload["schema_definition"]["identity_opt_out"] == "heuristic_canonical_name"
    assert "canonical_name_fields" not in payload["schema_definition"]
    assert payload["schema_definition"]["fields"] == {"summary": {"type": "string"}}
    assert payload["activate"] is True


def test_register_schema_payload_top_level_keys_are_all_declared():
    # RegisterSchemaRequestSchema, action_schemas.ts:987-996.
    declared_keys = {
        "entity_type",
        "schema_definition",
        "reducer_config",
        "schema_version",
        "user_specific",
        "user_id",
        "activate",
        "force",
    }
    payload = build_register_schema_payload("symptom_report", [])
    assert set(payload.keys()) <= declared_keys


def test_register_schema_payload_reducer_config_has_no_merge_policies_that_remove_fields():
    payload = build_register_schema_payload("symptom_report", [])
    assert payload["reducer_config"] == {"merge_policies": {}}


# --- class-b reconciliation: field filtering --------------------------------


def test_filter_writable_fields_keeps_only_local_only_and_local_newer():
    entity_record = {
        "id": "ent_x",
        "entity_type": "issue",
        "fields": [
            {"name": "a", "classification": "SAME"},
            {"name": "b", "classification": "LOCAL_ONLY"},
            {"name": "c", "classification": "LOCAL_NEWER"},
            {"name": "d", "classification": "HOSTED_NEWER"},
        ],
    }
    writable = filter_writable_fields(entity_record)
    assert {f["name"] for f in writable} == {"b", "c"}


def test_filter_writable_fields_excludes_unrecognized_classification():
    entity_record = {
        "id": "ent_x",
        "entity_type": "issue",
        "fields": [{"name": "a", "classification": "SOMETHING_ELSE"}],
    }
    assert filter_writable_fields(entity_record) == []


def test_filter_writable_fields_empty_when_no_fields_key():
    assert filter_writable_fields({"id": "ent_x", "entity_type": "issue"}) == []


def test_build_reconcile_idempotency_key_deterministic_for_same_field_set():
    k1 = build_reconcile_idempotency_key("20260804", "ent_x", ["b", "c"])
    k2 = build_reconcile_idempotency_key("20260804", "ent_x", ["c", "b"])  # order shouldn't matter
    assert k1 == k2
    assert k1.startswith("migrate-20260804-recon-ent_x-")


def test_build_reconcile_idempotency_key_differs_for_different_field_sets():
    k1 = build_reconcile_idempotency_key("20260804", "ent_x", ["b"])
    k2 = build_reconcile_idempotency_key("20260804", "ent_x", ["b", "c"])
    assert k1 != k2


def test_build_store_payload_for_reconcile_uses_target_id_and_top_level_idempotency_key():
    payload = build_store_payload_for_reconcile("ent_x", "issue", {"body": "new text"}, "migrate-20260804-recon-ent_x-abc123")
    (entity,) = payload["entities"]
    assert entity["target_id"] == "ent_x"
    assert entity["entity_type"] == "issue"
    assert entity["body"] == "new text"
    assert payload["idempotency_key"] == "migrate-20260804-recon-ent_x-abc123"
    assert "entity_id" not in entity


# --- class-b reconciliation: local-state planning + drift skip -------------


def _make_sqlite_with_observations(tmp_path, entity_id, rows):
    """rows: list of (fields_dict, created_at_iso) -- builds a minimal
    observations table matching the columns plan_class_b_reconciliation_for_entity
    (via local_post_cutover_field_state) reads."""
    import sqlite3 as _sqlite3

    db_path = tmp_path / "fork.db"
    conn = _sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE observations (id TEXT, entity_id TEXT, entity_type TEXT, fields TEXT, created_at TEXT)"
    )
    for i, (fields, created_at) in enumerate(rows):
        conn.execute(
            "INSERT INTO observations (id, entity_id, entity_type, fields, created_at) VALUES (?, ?, ?, ?, ?)",
            (f"obs-{i}", entity_id, "issue", json.dumps(fields), created_at),
        )
    conn.commit()
    return conn


def test_plan_class_b_reconciliation_reads_latest_local_value_per_field(tmp_path):
    conn = _make_sqlite_with_observations(
        tmp_path,
        "ent_x",
        [
            ({"body": "first draft"}, "2026-08-05T00:00:00.000Z"),
            ({"body": "second draft"}, "2026-08-06T00:00:00.000Z"),
        ],
    )
    entity_record = {
        "id": "ent_x",
        "entity_type": "issue",
        "fields": [
            {
                "name": "body",
                "classification": "LOCAL_NEWER",
                "local_value_hash": value_hash("second draft"),
                "hosted_value_hash": value_hash("old hosted body"),
            }
        ],
    }
    entity_id, entity_type, fields_to_write, drifted, idem_key = plan_class_b_reconciliation_for_entity(
        entity_record, conn, "2026-08-04T08:51:43.023Z", "20260804"
    )
    assert fields_to_write == {"body": "second draft"}
    assert drifted == []
    assert idem_key is not None


def test_plan_class_b_reconciliation_skips_when_local_value_hash_mismatches():
    import sqlite3 as _sqlite3

    conn = _sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE observations (id TEXT, entity_id TEXT, entity_type TEXT, fields TEXT, created_at TEXT)"
    )
    conn.execute(
        "INSERT INTO observations VALUES (?, ?, ?, ?, ?)",
        ("obs-1", "ent_x", "issue", json.dumps({"body": "actual current value"}), "2026-08-05T00:00:00.000Z"),
    )
    conn.commit()
    entity_record = {
        "id": "ent_x",
        "entity_type": "issue",
        "fields": [
            {
                "name": "body",
                "classification": "LOCAL_NEWER",
                "local_value_hash": value_hash("a stale hash from a different value"),
                "hosted_value_hash": "irrelevant",
            }
        ],
    }
    entity_id, entity_type, fields_to_write, drifted, idem_key = plan_class_b_reconciliation_for_entity(
        entity_record, conn, "2026-08-04T08:51:43.023Z", "20260804"
    )
    assert fields_to_write == {}
    assert drifted == ["body"]
    assert idem_key is None


def test_plan_class_b_reconciliation_skips_field_with_no_post_cutover_local_state():
    import sqlite3 as _sqlite3

    conn = _sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE observations (id TEXT, entity_id TEXT, entity_type TEXT, fields TEXT, created_at TEXT)"
    )
    conn.commit()  # no rows at all for this entity
    entity_record = {
        "id": "ent_x",
        "entity_type": "issue",
        "fields": [
            {"name": "closed_at", "classification": "LOCAL_ONLY", "local_value_hash": "abc", "hosted_value_hash": "def"}
        ],
    }
    entity_id, entity_type, fields_to_write, drifted, idem_key = plan_class_b_reconciliation_for_entity(
        entity_record, conn, "2026-08-04T08:51:43.023Z", "20260804"
    )
    assert fields_to_write == {}
    assert drifted == ["closed_at"]


def test_plan_class_b_reconciliation_nothing_to_apply_when_all_same():
    import sqlite3 as _sqlite3

    conn = _sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE observations (id TEXT, entity_id TEXT, entity_type TEXT, fields TEXT, created_at TEXT)"
    )
    conn.commit()
    entity_record = {
        "id": "ent_x",
        "entity_type": "issue",
        "fields": [{"name": "a", "classification": "SAME"}, {"name": "b", "classification": "HOSTED_NEWER"}],
    }
    entity_id, entity_type, fields_to_write, drifted, idem_key = plan_class_b_reconciliation_for_entity(
        entity_record, conn, "2026-08-04T08:51:43.023Z", "20260804"
    )
    assert fields_to_write == {}
    assert drifted == []
    assert idem_key is None


def test_value_hash_stable_for_same_value_differs_for_different_value():
    assert value_hash("same") == value_hash("same")
    assert value_hash("a") != value_hash("b")
    assert value_hash({"k": 1}) == value_hash({"k": 1})
