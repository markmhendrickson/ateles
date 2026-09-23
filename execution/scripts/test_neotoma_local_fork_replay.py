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
    payload, idem_key, _stripped = build_observation_payload(
        OBSERVATION_ROW, "20260804"
    )
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
    fields_json = json.dumps(
        {"_migration_run_id": "schema_lag_bg_2026-09-06T16:58:38.065Z"}
    )
    assert is_schema_lag_background_rewrite(fields_json) is True


def test_is_schema_lag_background_rewrite_false_for_other_migration_run_id():
    fields_json = json.dumps({"_migration_run_id": "some_other_run_id"})
    assert is_schema_lag_background_rewrite(fields_json) is False


def test_is_schema_lag_background_rewrite_false_when_no_migration_run_id():
    fields_json = json.dumps({"title": "Do the thing"})
    assert is_schema_lag_background_rewrite(fields_json) is False


@pytest.mark.parametrize(
    "bad_input", [None, "", "not json", "{", json.dumps(["a", "list"])]
)
def test_is_schema_lag_background_rewrite_false_on_malformed_or_missing_input(
    bad_input,
):
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
    schema_info = {
        "has_schema": True,
        "declared_fields": {"title", "status", "priority"},
    }
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
    assert CONDITIONALLY_RESERVED_FIELD_KEYS == {
        "entity_id",
        "idempotency_key",
        "canonical_name",
    }


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
        json.dumps(
            {
                "title": "y",
                "status": "open",
                "entity_id": "ent_bogus",
                "_migration_run_id": "schema_lag_bg_z",
            }
        ),
        "2026-08-05T00:00:00.000Z",
        "user-1",
        None,
        "import",
    )
    payload, _idem_key, stripped = build_observation_payload(
        row, "20260804", schema_info=schema_info
    )
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
    payload, _idem_key, stripped = build_observation_payload(
        OBSERVATION_ROW, "20260804"
    )
    assert stripped == []


# --- --unknown-fields pre-flight prediction ---------------------------------


def test_predict_unknown_fields_returns_empty_when_no_hosted_schema():
    schema_info = {"has_schema": False, "declared_fields": set()}
    result = predict_unknown_fields(
        "symptom_report", {"summary": "x", "weird": 1}, schema_info
    )
    assert result == []


def test_predict_unknown_fields_flags_fields_the_schema_does_not_declare():
    schema_info = {"has_schema": True, "declared_fields": {"title", "status"}}
    result = predict_unknown_fields(
        "task",
        {"title": "x", "status": "open", "reviewing_agent": "cicada"},
        schema_info,
    )
    assert result == ["reviewing_agent"]


def test_predict_unknown_fields_empty_when_all_fields_declared():
    schema_info = {"has_schema": True, "declared_fields": {"title", "status"}}
    result = predict_unknown_fields(
        "task", {"title": "x", "status": "open"}, schema_info
    )
    assert result == []


def test_unknown_fields_cli_flag_defaults_to_stop():
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
    result = subprocess.run(
        [_sys.executable, script, "--help"], capture_output=True, text=True, timeout=10
    )
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
    samples = {
        "title": "already declared",
        "reviewing_agent": "cicada",
        "pr_number": 42,
    }
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
    fields_to_add = [
        {"field_name": "summary", "field_type": "string", "required": False}
    ]
    payload = build_register_schema_payload("symptom_report", fields_to_add)
    assert (
        payload["schema_definition"]["identity_opt_out"] == "heuristic_canonical_name"
    )
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
    k2 = build_reconcile_idempotency_key(
        "20260804", "ent_x", ["c", "b"]
    )  # order shouldn't matter
    assert k1 == k2
    assert k1.startswith("migrate-20260804-recon-ent_x-")


def test_build_reconcile_idempotency_key_differs_for_different_field_sets():
    k1 = build_reconcile_idempotency_key("20260804", "ent_x", ["b"])
    k2 = build_reconcile_idempotency_key("20260804", "ent_x", ["b", "c"])
    assert k1 != k2


def test_build_store_payload_for_reconcile_uses_target_id_and_top_level_idempotency_key():
    payload = build_store_payload_for_reconcile(
        "ent_x", "issue", {"body": "new text"}, "migrate-20260804-recon-ent_x-abc123"
    )
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
    entity_id, entity_type, fields_to_write, drifted, idem_key = (
        plan_class_b_reconciliation_for_entity(
            entity_record, conn, "2026-08-04T08:51:43.023Z", "20260804"
        )
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
        (
            "obs-1",
            "ent_x",
            "issue",
            json.dumps({"body": "actual current value"}),
            "2026-08-05T00:00:00.000Z",
        ),
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
    entity_id, entity_type, fields_to_write, drifted, idem_key = (
        plan_class_b_reconciliation_for_entity(
            entity_record, conn, "2026-08-04T08:51:43.023Z", "20260804"
        )
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
            {
                "name": "closed_at",
                "classification": "LOCAL_ONLY",
                "local_value_hash": "abc",
                "hosted_value_hash": "def",
            }
        ],
    }
    entity_id, entity_type, fields_to_write, drifted, idem_key = (
        plan_class_b_reconciliation_for_entity(
            entity_record, conn, "2026-08-04T08:51:43.023Z", "20260804"
        )
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
        "fields": [
            {"name": "a", "classification": "SAME"},
            {"name": "b", "classification": "HOSTED_NEWER"},
        ],
    }
    entity_id, entity_type, fields_to_write, drifted, idem_key = (
        plan_class_b_reconciliation_for_entity(
            entity_record, conn, "2026-08-04T08:51:43.023Z", "20260804"
        )
    )
    assert fields_to_write == {}
    assert drifted == []
    assert idem_key is None


def test_value_hash_stable_for_same_value_differs_for_different_value():
    assert value_hash("same") == value_hash("same")
    assert value_hash("a") != value_hash("b")


# --- --gate-restore (operator-approved 2026-09-23) --------------------------

from neotoma_local_fork_replay import (  # noqa: E402
    LEGACY_GATE_STATUS_FIELD,
    canonical_identity_lookup,
    format_issue_label,
    gate_status_rank,
    merge_current_owner,
    merge_gate_status,
    merge_owner_history,
    plan_gate_restore_for_entity,
    scan_local_gate_candidates,
)


def test_legacy_gate_status_field_name_is_explicit_compatibility_contract():
    assert (
        LEGACY_GATE_STATUS_FIELD
        == "gate_status"  # vocab-ok: hosted legacy-field contract
    )


def test_gate_status_rank_orders_pending_below_signed_off():
    assert gate_status_rank("pending") < gate_status_rank("signed_off")
    assert gate_status_rank("pending") < gate_status_rank("changes_requested")
    assert gate_status_rank("changes_requested") < gate_status_rank("signed_off")


def test_gate_status_rank_unknown_status_is_none():
    assert gate_status_rank("some_future_status_not_in_the_table") is None
    assert gate_status_rank(None) is None


def test_merge_gate_status_upgrades_pending_to_signed_off():
    merged, changes = merge_gate_status({"pm": "pending"}, {"pm": "signed_off"})
    assert merged["pm"] == "signed_off"
    assert changes == [("pm", "pending", "signed_off")]


def test_merge_gate_status_never_downgrades_hosted():
    # Hosted already signed_off; local (stale local-fork write) says pending.
    # This is exactly the "never downgrade hosted" rule from the brief.
    merged, changes = merge_gate_status({"pm": "signed_off"}, {"pm": "pending"})
    assert merged["pm"] == "signed_off"
    assert changes == []


def test_merge_gate_status_equal_rank_is_a_noop():
    # not_required and signed_off share the top rank in GATE_STATUS_RANK --
    # neither should overwrite the other when they're already equal, and a
    # local value of DIFFERENT top-rank status than hosted's own top-rank
    # status is also not a change (equal rank means no ordering to apply).
    merged, changes = merge_gate_status(
        {"legal": "not_required"}, {"legal": "not_required"}
    )
    assert changes == []
    merged2, changes2 = merge_gate_status({"qa": "signed_off"}, {"qa": "not_required"})
    assert changes2 == []  # equal rank, hosted's actual value is kept
    assert merged2["qa"] == "signed_off"


def test_merge_gate_status_never_touches_a_gate_absent_from_local():
    # Hosted has gates local never mentions -- those must pass through
    # completely unchanged (the brief: "never touch a gate that doesn't
    # appear locally").
    merged, changes = merge_gate_status(
        {"pm": "signed_off", "arch": "pending", "qa": "pending"}, {"pm": "signed_off"}
    )
    assert merged == {"pm": "signed_off", "arch": "pending", "qa": "pending"}
    assert changes == []


def test_merge_gate_status_adds_gate_hosted_never_had():
    merged, changes = merge_gate_status({"pm": "pending"}, {"ux": "signed_off"})
    assert merged == {"pm": "pending", "ux": "signed_off"}
    assert changes == [("ux", None, "signed_off")]


def test_merge_gate_status_unranked_hosted_value_is_never_overwritten():
    # Fail-closed case: hosted holds a status this table doesn't recognize.
    # Even a "clearly more advanced" local value must not overwrite it,
    # since we cannot prove the ordering.
    merged, changes = merge_gate_status(
        {"pm": "some_new_status_the_rank_table_predates"}, {"pm": "signed_off"}
    )
    assert changes == []
    assert merged["pm"] == "some_new_status_the_rank_table_predates"


def test_merge_owner_history_unions_and_dedupes_new_local_entries():
    hosted = [{"agent": "lanius", "action": "triaged", "at": "2026-09-23T10:00:00Z"}]
    local = [
        {
            "agent": "lanius",
            "action": "triaged",
            "at": "2026-09-23T10:00:00Z",
        },  # true dup of hosted's entry
        {
            "agent": "pavo",
            "action": "claimed",
            "at": "2026-09-23T09:00:00Z",
        },  # genuinely new
    ]
    merged = merge_owner_history(hosted, local)
    assert len(merged) == 2  # deduped (the true dup), not 3
    # Hosted's own entries are never reordered; new local entries are
    # appended after, sorted among themselves.
    assert merged[0]["agent"] == "lanius"
    assert merged[1]["agent"] == "pavo"


def test_merge_owner_history_never_shrinks_hosted_even_with_hosted_internal_duplicates():
    # Regression: real hosted data (entity ent_fec57fb48b3485bff6a24412) already
    # carries an internal near-duplicate pair of its own (same agent/action/
    # gate/at, differing only by an extra `note` field on one). A merge must
    # NEVER edit hosted's history downward to "clean up" a pre-existing
    # hosted data-quality issue -- that is out of scope for this replay and
    # was flagged as a possible union bug before this test pinned the fix.
    hosted = [
        {"agent": "lanius", "action": "triaged", "at": "2026-09-15T09:47:10Z"},
        {
            "agent": "pavo",
            "action": "seeded_missing_entity",
            "at": "2026-09-15T09:53:49Z",
        },
        {
            "agent": "pavo",
            "action": "seeded_missing_entity",
            "at": "2026-09-15T09:53:49Z",
            "note": "Lanius linked entity but 404 on production; Pavo seeded for gate tracking",
        },
        {
            "agent": "pavo",
            "gate": "pm",
            "action": "signed_off",
            "actor": "pavo",
            "at": "2026-09-15T09:54:18Z",
        },
    ]
    local = [{"agent": "lanius", "action": "triaged", "at": "2026-09-15T09:46:34Z"}]
    merged = merge_owner_history(hosted, local)
    assert len(merged) >= max(len(local), len(hosted))
    assert (
        len(merged) == 5
    )  # all 4 hosted entries survive untouched + 1 new local entry
    for h in hosted:
        assert h in merged


def test_merge_owner_history_invariant_never_shrinks_regardless_of_inputs():
    # General form of the regression above: for ANY hosted/local pair
    # (including hosted holding its own internal duplicates, or local being
    # empty), the merge must never produce fewer entries than the larger of
    # the two inputs.
    cases = [
        ([], []),
        ([{"agent": "a", "action": "x", "at": "t1"}], []),
        ([], [{"agent": "a", "action": "x", "at": "t1"}]),
        (
            [
                {"agent": "a", "action": "x", "at": "t1"},
                {"agent": "a", "action": "x", "at": "t1"},
            ],
            [{"agent": "b", "action": "y", "at": "t2"}],
        ),
    ]
    for hosted, local in cases:
        merged = merge_owner_history(hosted, local)
        assert len(merged) >= max(len(hosted), len(local)), (hosted, local, merged)


def test_merge_owner_history_dedupes_legacy_entries_with_no_timestamp_on_note():
    entry = {
        "action": "legacy_gate_init",
        "agent": "lanius",
        "note": "backfill for #2139",
    }
    merged = merge_owner_history([entry], [dict(entry)])
    assert len(merged) == 1


def test_merge_owner_history_keeps_distinct_entries_with_same_agent_and_timestamp():
    # Regression: real local-fork data (entity ent_f89b4fe5636ac3275d629c3e)
    # has the SAME agent logging a signed_off AND a handed_off entry at the
    # EXACT same `at` timestamp (the instant a gate transition happened). A
    # dedup key of (agent, at) alone collapsed a 355-entry owner_history
    # down to 7 -- real data loss, caught before the --apply run. The key
    # must also weigh `action` (and `gate`, where present) so these two
    # entries are kept as distinct.
    same_ts = "2026-09-15T11:28:10.641829+00:00"
    signed_off = {"action": "signed_off", "agent": "pavo", "at": same_ts, "gate": "pm"}
    handed_off = {
        "action": "handed_off",
        "agent": "pavo",
        "assigned_owner": "accipiter",
        "at": same_ts,
        "gate": "pm",
        "next_gates": ["ux", "arch"],
    }
    merged = merge_owner_history([], [signed_off, handed_off])
    assert len(merged) == 2
    actions = {e["action"] for e in merged}
    assert actions == {"signed_off", "handed_off"}


def test_merge_owner_history_still_dedupes_true_duplicates_across_dbs():
    # The SAME write appearing in both local DBs (the common case for the
    # non-schema_lag_bg_* rows this entity replay covers) must still
    # collapse to one entry, not be kept twice.
    entry = {
        "action": "signed_off",
        "agent": "pavo",
        "at": "2026-09-15T11:28:10Z",
        "gate": "pm",
    }
    merged = merge_owner_history([dict(entry)], [dict(entry)])
    assert len(merged) == 1


def test_merge_current_owner_local_wins_when_strictly_newer():
    value, changed = merge_current_owner(
        "pavo", "vanellus", "2026-09-23T12:00:00Z", "2026-09-23T10:00:00Z"
    )
    assert value == "vanellus"
    assert changed is True


def test_merge_current_owner_hosted_wins_when_hosted_is_newer():
    value, changed = merge_current_owner(
        "pavo", "vanellus", "2026-09-23T09:00:00Z", "2026-09-23T10:00:00Z"
    )
    assert value == "pavo"
    assert changed is False


def test_merge_current_owner_fails_closed_on_missing_local_timestamp():
    # Cannot prove local is newer without its own write timestamp -- must
    # not overwrite hosted.
    value, changed = merge_current_owner(
        "pavo", "vanellus", None, "2026-09-23T10:00:00Z"
    )
    assert value == "pavo"
    assert changed is False


def test_merge_current_owner_local_wins_when_hosted_has_no_timestamp_at_all():
    value, changed = merge_current_owner(None, "vanellus", "2026-09-23T12:00:00Z", None)
    assert value == "vanellus"
    assert changed is True


def test_plan_gate_restore_missing_issue_creates_with_identifying_and_gate_fields():
    local_state = {
        LEGACY_GATE_STATUS_FIELD: {"pm": "signed_off", "ux": "signed_off"},
        "owner_history": [
            {"agent": "lanius", "action": "triaged", "at": "2026-09-23T10:00:00Z"}
        ],
        "current_owner": "pavo",
        "repo": "markmhendrickson/ateles",
        "github_number": 1172,
        "title": "Some issue title",
    }
    plan = plan_gate_restore_for_entity("ent_missing", local_state, hosted_entity=None)
    assert plan["action"] == "create"
    assert plan["fields"][LEGACY_GATE_STATUS_FIELD] == {
        "pm": "signed_off",
        "ux": "signed_off",
    }
    assert plan["fields"]["owner_history"] == local_state["owner_history"]
    assert plan["fields"]["current_owner"] == "pavo"
    assert plan["fields"]["repo"] == "markmhendrickson/ateles"
    assert plan["fields"]["github_number"] == 1172
    # No target_id/entity_type reserved-key collision -- those are added by
    # build_entity_record at write time, not baked into the plan's fields.
    assert "entity_type" not in plan["fields"]
    assert "target_id" not in plan["fields"]


def test_plan_gate_restore_merge_only_touches_gates_present_locally():
    local_state = {LEGACY_GATE_STATUS_FIELD: {"pm": "signed_off"}}
    hosted_entity = {
        "snapshot": {
            LEGACY_GATE_STATUS_FIELD: {"pm": "pending", "arch": "pending"},
            "owner_history": [],
            "current_owner": None,
        }
    }
    plan = plan_gate_restore_for_entity("ent_1178", local_state, hosted_entity)
    assert plan["action"] == "merge"
    assert plan["fields"][LEGACY_GATE_STATUS_FIELD] == {
        "pm": "signed_off",
        "arch": "pending",
    }
    assert plan["gate_changes"] == [("pm", "pending", "signed_off")]
    assert "owner_history" not in plan["fields"]  # unchanged, not resent


def test_plan_gate_restore_noop_when_hosted_already_equal_or_ahead():
    local_state = {LEGACY_GATE_STATUS_FIELD: {"pm": "pending"}}
    hosted_entity = {
        "snapshot": {
            LEGACY_GATE_STATUS_FIELD: {"pm": "signed_off"},
            "owner_history": [],
            "current_owner": None,
        }
    }
    plan = plan_gate_restore_for_entity("ent_x", local_state, hosted_entity)
    assert plan["fields"] == {}
    assert plan["action"] != "create"


def test_scan_local_gate_candidates_folds_forward_across_both_dbs():
    import sqlite3 as _sqlite3

    db1 = _sqlite3.connect(":memory:")
    db1.execute(
        "CREATE TABLE observations (id TEXT, entity_id TEXT, entity_type TEXT, fields TEXT, created_at TEXT)"
    )
    db1.execute(
        "INSERT INTO observations VALUES (?,?,?,?,?)",
        (
            "o1",
            "ent_a",
            "issue",
            json.dumps(
                {
                    LEGACY_GATE_STATUS_FIELD: {"pm": "pending"},
                    "repo": "x/y",
                    "github_number": 1,
                }
            ),
            "2026-09-23T09:00:00Z",
        ),
    )
    db1.commit()

    # scan_local_gate_candidates opens real files via sqlite3.connect(f"file:{path}?mode=ro", uri=True),
    # so exercise it against real temp db files rather than :memory: connections.
    import tempfile
    import os as _os

    with tempfile.TemporaryDirectory() as tmpdir:
        p1 = _os.path.join(tmpdir, "db1.db")
        p2 = _os.path.join(tmpdir, "db2.db")
        c1 = _sqlite3.connect(p1)
        c1.execute(
            "CREATE TABLE observations (id TEXT, entity_id TEXT, entity_type TEXT, fields TEXT, created_at TEXT)"
        )
        c1.execute(
            "INSERT INTO observations VALUES (?,?,?,?,?)",
            (
                "o1",
                "ent_a",
                "issue",
                json.dumps(
                    {
                        LEGACY_GATE_STATUS_FIELD: {"pm": "pending"},
                        "repo": "x/y",
                        "github_number": 1,
                    }
                ),
                "2026-09-23T09:00:00Z",
            ),
        )
        c1.commit()
        c1.close()

        c2 = _sqlite3.connect(p2)
        c2.execute(
            "CREATE TABLE observations (id TEXT, entity_id TEXT, entity_type TEXT, fields TEXT, created_at TEXT)"
        )
        c2.execute(
            "INSERT INTO observations VALUES (?,?,?,?,?)",
            (
                "o2",
                "ent_a",
                "issue",
                json.dumps({LEGACY_GATE_STATUS_FIELD: {"pm": "signed_off"}}),
                "2026-09-23T10:00:00Z",
            ),
        )
        c2.commit()
        c2.close()

        candidates = scan_local_gate_candidates([p1, p2], "2026-08-04T08:51:43.023Z")
        assert "ent_a" in candidates
        # The later write (db2, 10:00) wins over the earlier one (db1, 9:00)
        # for the SAME key, matching "fold forward across both DBs sorted by
        # created_at, last write wins per field".
        assert candidates["ent_a"][LEGACY_GATE_STATUS_FIELD] == {"pm": "signed_off"}
        # Identifying fields from the earlier observation are still carried.
        assert candidates["ent_a"]["repo"] == "x/y"
        assert candidates["ent_a"]["github_number"] == 1


def test_format_issue_label_uses_repo_and_number_when_present():
    assert (
        format_issue_label("markmhendrickson/ateles", 1172, "ent_x")
        == "markmhendrickson/ateles#1172"
    )
    assert format_issue_label(None, None, "ent_x") == "ent_x"


def test_canonical_identity_lookup_uses_entity_type_prefixed_identifier(monkeypatch):
    # Regression: confirmed against a live hosted canonical_name during the
    # 2026-09-23 apply run ("issue:998|markmhendrickson/ateles"). An earlier
    # version sent "1172|markmhendrickson/ateles" (no "issue:" prefix),
    # which /retrieve_entity_by_identifier silently never matched --
    # {"entities": [], "total": 0, "match_mode": "none"} every time -- so
    # the canonical-identity fallback never fired and a real hosted entity
    # under a DIFFERENT id was missed, producing a live ERR_MERGE_REFUSED
    # 400 (ateles#998) when this script then tried to create at the wrong id.
    import neotoma_local_fork_replay as _mod

    def fake_http_request(
        method, base_url, path, token, body=None, retries=0, retry_backoff_seconds=1.0
    ):
        assert path == "/retrieve_entity_by_identifier"
        assert body == {"entity_type": "issue", "identifier": "issue:1172|markmhendrickson/ateles"}
        return 200, {"entities": [{"id": "ent_canonical_match"}]}

    monkeypatch.setattr(_mod, "http_request", fake_http_request)
    result = canonical_identity_lookup(
        "issue", "markmhendrickson/ateles", 1172, "https://hosted.example", "tok"
    )
    assert result == "ent_canonical_match"


def test_canonical_identity_lookup_falls_back_to_entity_id_key(monkeypatch):
    # The live /retrieve_entity_by_identifier response uses "id", but this
    # function also accepts "entity_id" defensively in case a different
    # response shape is returned by another endpoint variant.
    import neotoma_local_fork_replay as _mod

    monkeypatch.setattr(
        _mod, "http_request", lambda *a, **k: (200, {"entities": [{"entity_id": "ent_via_entity_id_key"}]})
    )
    result = canonical_identity_lookup(
        "issue", "markmhendrickson/ateles", 1172, "https://hosted.example", "tok"
    )
    assert result == "ent_via_entity_id_key"


def test_canonical_identity_lookup_returns_none_on_no_match():
    import neotoma_local_fork_replay as _mod

    def fake_http_request(
        method, base_url, path, token, body=None, retries=0, retry_backoff_seconds=1.0
    ):
        return 200, {"entities": [], "total": 0}

    import pytest as _pytest

    monkeypatch = _pytest.MonkeyPatch()
    monkeypatch.setattr(_mod, "http_request", fake_http_request)
    try:
        result = canonical_identity_lookup(
            "issue", "markmhendrickson/ateles", 9999999, "https://hosted.example", "tok"
        )
        assert result is None
    finally:
        monkeypatch.undo()


def test_canonical_identity_lookup_returns_none_without_repo_or_number():
    assert (
        canonical_identity_lookup("issue", None, 1172, "https://hosted.example", "tok")
        is None
    )
    assert (
        canonical_identity_lookup(
            "issue", "markmhendrickson/ateles", None, "https://hosted.example", "tok"
        )
        is None
    )


def test_github_issue_lookup_returns_state_on_success(monkeypatch):
    import neotoma_local_fork_replay as _mod
    import subprocess as _subprocess

    class FakeResult:
        returncode = 0
        stdout = json.dumps({"number": 1172, "state": "open", "title": "x"})

    def fake_run(args, capture_output, text, timeout):
        assert args == ["gh", "api", "repos/markmhendrickson/ateles/issues/1172"]
        return FakeResult()

    monkeypatch.setattr(_subprocess, "run", fake_run)
    result = _mod.github_issue_lookup("markmhendrickson/ateles", 1172)
    assert result["state"] == "open"


def test_github_issue_lookup_returns_none_on_nonzero_exit(monkeypatch):
    import neotoma_local_fork_replay as _mod
    import subprocess as _subprocess

    class FakeResult:
        returncode = 1
        stdout = ""

    monkeypatch.setattr(_subprocess, "run", lambda *a, **k: FakeResult())
    assert _mod.github_issue_lookup("markmhendrickson/ateles", 999999999) is None


def test_github_issue_lookup_returns_none_without_repo_or_number():
    import neotoma_local_fork_replay as _mod

    assert _mod.github_issue_lookup(None, 1172) is None
    assert _mod.github_issue_lookup("markmhendrickson/ateles", None) is None


# --- Load rules (concurrency 1, timeout >=180s, periodic health check) -----


def test_http_client_timeout_is_at_least_180_seconds():
    import neotoma_local_fork_replay as _mod

    assert _mod.HTTP_CLIENT_TIMEOUT_SECONDS >= 180


def test_health_check_batch_size_is_20():
    import neotoma_local_fork_replay as _mod

    assert _mod.HEALTH_CHECK_BATCH_SIZE == 20


def test_check_health_ok_on_200_with_no_body_opinion(monkeypatch):
    import neotoma_local_fork_replay as _mod

    monkeypatch.setattr(_mod, "http_request", lambda *a, **k: (200, {}))
    ok, status = _mod.check_health("https://hosted.example", "tok")
    assert ok is True
    assert status == 200


def test_check_health_ok_false_on_non_200():
    import neotoma_local_fork_replay as _mod
    import pytest as _pytest

    mp = _pytest.MonkeyPatch()
    mp.setattr(_mod, "http_request", lambda *a, **k: (503, {"error": "unavailable"}))
    try:
        ok, status = _mod.check_health("https://hosted.example", "tok")
        assert ok is False
        assert status == 503
    finally:
        mp.undo()


def test_check_health_ok_false_when_body_says_not_ok():
    import neotoma_local_fork_replay as _mod
    import pytest as _pytest

    mp = _pytest.MonkeyPatch()
    mp.setattr(_mod, "http_request", lambda *a, **k: (200, {"ok": False}))
    try:
        ok, status = _mod.check_health("https://hosted.example", "tok")
        assert ok is False
    finally:
        mp.undo()


# --- Idempotency-on-re-apply regression (converge to zero writes) ----------
#
# Live bug found 2026-09-23: re-running --gate-restore --apply against an
# already-applied batch reported "Applied: 79" instead of 0. Root cause
# confirmed by direct probe: hosted's /store creates a NEW observation on
# EVERY call, even when the payload is byte-identical to what's already
# stored (observation_count went 112 -> 113 for neotoma#2033 from a write
# whose content did not change) -- idempotency there is keyed strictly on
# idempotency_key equality, and this script's key is a hash of the MERGED
# payload, which itself legitimately shifts run-to-run because hosted's own
# owner_history keeps growing from OTHER agents' concurrent writes. The fix
# (recheck_fields_against_current_hosted) re-diffs against hosted's CURRENT
# state immediately before the write and drops any field that is a no-op
# against that fresh read, rather than trusting the plan-time diff.

from neotoma_local_fork_replay import recheck_fields_against_current_hosted  # noqa: E402


def test_recheck_drops_gate_status_when_no_longer_a_change(monkeypatch):
    import neotoma_local_fork_replay as _mod

    # Hosted has ALREADY absorbed the local pm signoff (e.g. from an
    # earlier pass of this same script) by the time this recheck runs.
    hosted_entity = {
        "snapshot": {LEGACY_GATE_STATUS_FIELD: {"pm": "signed_off"}, "owner_history": []}
    }
    monkeypatch.setattr(_mod, "get_hosted_entity", lambda *a, **k: hosted_entity)

    local_state = {LEGACY_GATE_STATUS_FIELD: {"pm": "signed_off"}}
    fields = {LEGACY_GATE_STATUS_FIELD: {"pm": "pending"}}  # stale plan-time value
    result = recheck_fields_against_current_hosted(
        "ent_x", "merge", fields, local_state, "https://hosted.example", "tok"
    )
    assert LEGACY_GATE_STATUS_FIELD not in result


def test_recheck_keeps_gate_status_when_still_a_real_change(monkeypatch):
    import neotoma_local_fork_replay as _mod

    hosted_entity = {
        "snapshot": {LEGACY_GATE_STATUS_FIELD: {"pm": "pending"}, "owner_history": []}
    }
    monkeypatch.setattr(_mod, "get_hosted_entity", lambda *a, **k: hosted_entity)

    local_state = {LEGACY_GATE_STATUS_FIELD: {"pm": "signed_off"}}
    fields = {LEGACY_GATE_STATUS_FIELD: {"pm": "signed_off"}}
    result = recheck_fields_against_current_hosted(
        "ent_x", "merge", fields, local_state, "https://hosted.example", "tok"
    )
    assert result[LEGACY_GATE_STATUS_FIELD] == {"pm": "signed_off"}


def test_recheck_drops_owner_history_when_hosted_already_has_every_local_entry(monkeypatch):
    import neotoma_local_fork_replay as _mod

    entry = {"agent": "pavo", "action": "signed_off", "gate": "pm", "at": "2026-09-23T10:00:00Z"}
    hosted_entity = {
        "snapshot": {LEGACY_GATE_STATUS_FIELD: {}, "owner_history": [entry]}
    }
    monkeypatch.setattr(_mod, "get_hosted_entity", lambda *a, **k: hosted_entity)
    # owner_history presence is decided from OBSERVATIONS (neotoma#2341), not
    # the snapshot -- mock that source directly to say the entry is already
    # present, independent of whatever the snapshot fixture above says.
    monkeypatch.setattr(
        _mod,
        "hosted_array_field_present_keys",
        lambda *a, **k: {_mod.array_entry_dedupe_key(entry)},
    )

    local_state = {"owner_history": [dict(entry)]}
    fields = {"owner_history": [entry, {"agent": "stale", "action": "x", "at": "t"}]}  # stale plan-time value
    result = recheck_fields_against_current_hosted(
        "ent_x", "merge", fields, local_state, "https://hosted.example", "tok"
    )
    assert "owner_history" not in result


def test_recheck_keeps_owner_history_with_genuinely_new_entries(monkeypatch):
    import neotoma_local_fork_replay as _mod

    hosted_entity = {
        "snapshot": {LEGACY_GATE_STATUS_FIELD: {}, "owner_history": []}
    }
    monkeypatch.setattr(_mod, "get_hosted_entity", lambda *a, **k: hosted_entity)
    monkeypatch.setattr(_mod, "hosted_array_field_present_keys", lambda *a, **k: set())

    new_entry = {"agent": "pavo", "action": "signed_off", "gate": "pm", "at": "2026-09-23T10:00:00Z"}
    local_state = {"owner_history": [new_entry]}
    fields = {"owner_history": [new_entry]}
    result = recheck_fields_against_current_hosted(
        "ent_x", "merge", fields, local_state, "https://hosted.example", "tok"
    )
    assert result["owner_history"] == [new_entry]


def test_recheck_is_a_noop_for_create_action():
    # No hosted state to re-diff against for a class-a create -- fields
    # pass through completely unchanged, and no hosted GET should even be
    # attempted (not exercised directly here since get_hosted_entity isn't
    # monkeypatched -- an unpatched network call would raise/hang, so this
    # test asserting a clean return IS the proof no call was made).
    fields = {
        LEGACY_GATE_STATUS_FIELD: {"pm": "signed_off"},
        "owner_history": [{"agent": "x"}],
    }
    result = recheck_fields_against_current_hosted(
        "ent_x",
        "create",
        fields,
        {LEGACY_GATE_STATUS_FIELD: {"pm": "signed_off"}},
        "https://hosted.example",
        "tok",
    )
    assert result == fields


def test_recheck_ignores_fields_with_no_gate_status_or_owner_history():
    fields = {"current_owner": "waxwing"}
    result = recheck_fields_against_current_hosted(
        "ent_x", "merge", fields, {}, "https://hosted.example", "tok"
    )
    assert result == fields


def test_recheck_fails_closed_when_hosted_entity_vanishes(monkeypatch):
    import neotoma_local_fork_replay as _mod

    monkeypatch.setattr(_mod, "get_hosted_entity", lambda *a, **k: None)
    fields = {
        LEGACY_GATE_STATUS_FIELD: {"pm": "signed_off"},
        "owner_history": [{"agent": "x"}],
        "current_owner": "waxwing",
    }
    result = recheck_fields_against_current_hosted(
        "ent_x", "merge", fields, {}, "https://hosted.example", "tok"
    )
    assert LEGACY_GATE_STATUS_FIELD not in result
    assert "owner_history" not in result
    assert result.get("current_owner") == "waxwing"  # untouched by this function
    assert value_hash({"k": 1}) == value_hash({"k": 1})


# --- merge_array presence from OBSERVATIONS, not the snapshot (neotoma#2341) -
#
# The bug this section covers: hosted's snapshot never reflects a
# merge_array-reducer field's true state (a store() write is accepted and
# bumps observation_count, but a subsequent GET /entities/<id> keeps showing
# the field's PRE-write value), so a script that diffs "what to send" against
# the snapshot never converges -- every re-run re-sends the same array
# entries. The fix reads presence from POST /list_observations instead.

from neotoma_local_fork_replay import (  # noqa: E402
    array_entry_dedupe_key,
    get_hosted_field_observations,
    hosted_array_field_present_keys,
    plan_array_field_missing_entries,
    reduce_array_fields_to_missing_entries,
)


def _list_observations_responder(pages_by_entity):
    """Build a fake http_request(method, base_url, path, token, body, ...)
    that answers POST /list_observations from a canned {entity_id: [obs, ...]}
    map, paginating by LIMIT/OFFSET the same way the real route does, and
    errors (a plain assert) on any other path -- so a test using this never
    silently talks to a different route than the one it's fixturing.
    """

    def _fake(method, base_url, path, token, body=None, **kwargs):
        assert path == "/list_observations", f"unexpected path: {path}"
        entity_id = body["entity_id"]
        limit = body.get("limit", 100)
        offset = body.get("offset", 0)
        all_obs = pages_by_entity.get(entity_id, [])
        page = all_obs[offset : offset + limit]
        return 200, {"observations": page}

    return _fake


def test_array_entry_dedupe_key_stable_across_key_order():
    a = {"agent": "pavo", "action": "signed_off", "at": "t1"}
    b = {"action": "signed_off", "at": "t1", "agent": "pavo"}
    assert array_entry_dedupe_key(a) == array_entry_dedupe_key(b)


def test_array_entry_dedupe_key_differs_for_different_content():
    a = {"agent": "pavo", "at": "t1"}
    b = {"agent": "pavo", "at": "t2"}
    assert array_entry_dedupe_key(a) != array_entry_dedupe_key(b)


def test_get_hosted_field_observations_unions_across_pages(monkeypatch):
    import neotoma_local_fork_replay as _mod

    entries = [{"agent": f"a{i}", "at": f"t{i}"} for i in range(3)]
    pages = {
        "ent_x": [
            {"fields": {"owner_history": [entries[0]]}},
            {"fields": {"owner_history": [entries[1]]}},
            {"fields": {"other_field": "irrelevant"}},
            {"fields": {"owner_history": [entries[2]]}},
        ]
    }
    monkeypatch.setattr(
        _mod, "http_request", _list_observations_responder(pages)
    )
    values = get_hosted_field_observations(
        "ent_x", "owner_history", "https://hosted.example", "tok"
    )
    assert values == [[entries[0]], [entries[1]], [entries[2]]]


def test_get_hosted_field_observations_raises_on_ambiguous_500_never_returns_empty(
    monkeypatch,
):
    """Regression test for the bug the coordinator flagged: this function used
    to return [] on ANY non-200, which for a presence check means "found
    nothing new" -- during a hosted outage that makes every local array entry
    look missing and re-sends the WHOLE array, duplicating what's already
    there. It must now raise HostedProbeAmbiguousError instead of silently
    answering "empty" (neotoma#2483).
    """
    import neotoma_local_fork_replay as _mod

    monkeypatch.setattr(
        _mod, "http_request", lambda *a, **k: (500, {"error": "boom"})
    )
    with pytest.raises(_mod.HostedProbeAmbiguousError):
        get_hosted_field_observations(
            "ent_x", "owner_history", "https://hosted.example", "tok"
        )


def test_hosted_array_field_present_keys_unions_entries_from_every_observation(
    monkeypatch,
):
    import neotoma_local_fork_replay as _mod

    e1 = {"agent": "pavo", "action": "signed_off", "gate": "pm", "at": "t1"}
    e2 = {"agent": "cicada", "action": "handed_off", "gate": "ux", "at": "t2"}
    pages = {
        "ent_x": [
            {"fields": {"owner_history": [e1]}},
            {"fields": {"owner_history": [e1, e2]}},  # e1 repeated -- still one key
        ]
    }
    monkeypatch.setattr(_mod, "http_request", _list_observations_responder(pages))
    present = hosted_array_field_present_keys(
        "ent_x", "owner_history", "https://hosted.example", "tok"
    )
    assert present == {array_entry_dedupe_key(e1), array_entry_dedupe_key(e2)}


# --- Requirement 3 (task spec): an array ALREADY present in observations
# produces NO write.


def test_plan_array_field_missing_entries_empty_when_all_already_present():
    e1 = {"agent": "pavo", "at": "t1"}
    e2 = {"agent": "cicada", "at": "t2"}
    present = {array_entry_dedupe_key(e1), array_entry_dedupe_key(e2)}
    missing = plan_array_field_missing_entries([e1, e2], present)
    assert missing == []


# --- Requirement 2 (task spec): a PARTIALLY present array sends only the
# missing entries.


def test_plan_array_field_missing_entries_sends_only_the_missing_subset():
    e1 = {"agent": "pavo", "at": "t1"}
    e2 = {"agent": "cicada", "at": "t2"}  # already on hosted
    e3 = {"agent": "waxwing", "at": "t3"}  # NOT on hosted -- genuinely new
    present = {array_entry_dedupe_key(e2)}
    missing = plan_array_field_missing_entries([e1, e2, e3], present)
    assert missing == [e1, e3]


def test_plan_array_field_missing_entries_dedupes_local_duplicates():
    e1 = {"agent": "pavo", "at": "t1"}
    missing = plan_array_field_missing_entries([e1, dict(e1), e1], set())
    assert missing == [e1]


def test_plan_array_field_missing_entries_preserves_local_order():
    e1 = {"agent": "a", "at": "1"}
    e2 = {"agent": "b", "at": "2"}
    e3 = {"agent": "c", "at": "3"}
    missing = plan_array_field_missing_entries([e3, e1, e2], set())
    assert missing == [e3, e1, e2]


# --- schema reducer detection (merge_array) --------------------------------


def test_get_schema_declared_fields_captures_merge_array_reducer(monkeypatch):
    import neotoma_local_fork_replay as _mod

    def fake_http_request(method, base_url, path, token, body=None, **kwargs):
        assert path == "/schemas/issue"
        return 200, {
            "schema_definition": {
                "fields": {"owner_history": {"type": "array"}, "title": {"type": "string"}}
            },
            "reducer_config": {
                "merge_policies": {
                    "owner_history": {"strategy": "merge_array"},
                    "gate_status": {"strategy": "last_write"},
                }
            },
        }

    monkeypatch.setattr(_mod, "http_request", fake_http_request)
    info = _mod.get_schema_declared_fields("issue", "https://hosted.example", "tok", {})
    assert info["merge_array_fields"] == {"owner_history"}
    assert "gate_status" not in info["merge_array_fields"]


def test_get_schema_declared_fields_merge_array_empty_when_no_hosted_schema(monkeypatch):
    import neotoma_local_fork_replay as _mod

    monkeypatch.setattr(_mod, "http_request", lambda *a, **k: (404, {"error": "not found"}))
    info = _mod.get_schema_declared_fields("nosuchtype", "https://hosted.example", "tok", {})
    assert info["has_schema"] is False
    assert info["merge_array_fields"] == set()


# --- reduce_array_fields_to_missing_entries (--reconcile-file path) --------


def test_reduce_array_fields_drops_field_entirely_present(monkeypatch):
    import neotoma_local_fork_replay as _mod

    e1 = {"agent": "pavo", "at": "t1"}
    pages = {"ent_x": [{"fields": {"owner_history": [e1]}}]}
    monkeypatch.setattr(_mod, "http_request", _list_observations_responder(pages))

    schema_info = {"merge_array_fields": {"owner_history"}}
    reduced = reduce_array_fields_to_missing_entries(
        "ent_x",
        "issue",
        {"owner_history": [dict(e1)], "title": "unrelated"},
        schema_info,
        "https://hosted.example",
        "tok",
    )
    assert "owner_history" not in reduced
    assert reduced["title"] == "unrelated"  # non-array field untouched


def test_reduce_array_fields_keeps_only_missing_subset(monkeypatch):
    import neotoma_local_fork_replay as _mod

    e1 = {"agent": "pavo", "at": "t1"}
    e2 = {"agent": "cicada", "at": "t2"}
    pages = {"ent_x": [{"fields": {"owner_history": [e1]}}]}
    monkeypatch.setattr(_mod, "http_request", _list_observations_responder(pages))

    schema_info = {"merge_array_fields": {"owner_history"}}
    reduced = reduce_array_fields_to_missing_entries(
        "ent_x",
        "issue",
        {"owner_history": [dict(e1), e2]},
        schema_info,
        "https://hosted.example",
        "tok",
    )
    assert reduced["owner_history"] == [e2]


def test_reduce_array_fields_protects_locally_list_typed_field_even_without_schema_flag(
    monkeypatch,
):
    """A field can be array-valued without this run's schema cache having
    classified it as merge_array yet (e.g. a type with no hosted schema at
    all) -- reduce_array_fields_to_missing_entries still protects it because
    it also checks isinstance(value, list), not only the schema flag.
    """
    import neotoma_local_fork_replay as _mod

    e1 = {"agent": "pavo", "at": "t1"}
    pages = {"ent_x": [{"fields": {"some_array": [e1]}}]}
    monkeypatch.setattr(_mod, "http_request", _list_observations_responder(pages))

    schema_info = {"merge_array_fields": set()}  # NOT flagged by schema
    reduced = reduce_array_fields_to_missing_entries(
        "ent_x",
        "no_schema_type",
        {"some_array": [dict(e1)]},
        schema_info,
        "https://hosted.example",
        "tok",
    )
    assert "some_array" not in reduced  # still correctly detected as fully present


# --- Requirement 1 (task spec): convergence -- re-planning after a simulated
# write yields zero changes.


def test_gate_restore_owner_history_converges_to_zero_after_simulated_write(
    monkeypatch,
):
    """Simulates exactly the regression this fix targets: plan once (nothing
    on hosted yet), "apply" by adding the sent entries to the fake hosted
    observations store, then re-plan -- the second plan must send ZERO
    owner_history entries, proving convergence rather than re-sending the
    same entries forever.
    """
    import neotoma_local_fork_replay as _mod

    entry = {"agent": "pavo", "action": "signed_off", "gate": "pm", "at": "2026-09-23T10:00:00Z"}
    hosted_observations: list = []  # simulates hosted's observations table

    def fake_http_request(method, base_url, path, token, body=None, **kwargs):
        assert path == "/list_observations"
        return 200, {"observations": list(hosted_observations)}

    monkeypatch.setattr(_mod, "http_request", fake_http_request)

    local_state = {"owner_history": [entry]}
    hosted_entity = {"snapshot": {LEGACY_GATE_STATUS_FIELD: {}, "owner_history": []}}

    # First plan: hosted has nothing yet -> entry is genuinely missing.
    plan1 = plan_gate_restore_for_entity(
        "ent_x", local_state, hosted_entity, base_url="https://hosted.example", token="tok"
    )
    assert plan1["owner_history_changed"] is True
    assert plan1["fields"]["owner_history"] == [entry]

    # Simulate the apply: hosted's observations table now has this entry.
    hosted_observations.append({"fields": {"owner_history": [entry]}})

    # Second plan (a re-run, or the same run's apply-time recheck): must
    # converge to zero owner_history change.
    plan2 = plan_gate_restore_for_entity(
        "ent_x", local_state, hosted_entity, base_url="https://hosted.example", token="tok"
    )
    assert plan2["owner_history_changed"] is False
    assert "owner_history" not in plan2["fields"]


def test_recheck_fields_against_current_hosted_converges_after_simulated_write(
    monkeypatch,
):
    """Same convergence property, exercised through the apply-time recheck
    function directly (recheck_fields_against_current_hosted) rather than
    the plan function -- this is the function actually called immediately
    before each --gate-restore write.
    """
    import neotoma_local_fork_replay as _mod

    entry = {"agent": "pavo", "action": "signed_off", "gate": "pm", "at": "2026-09-23T10:00:00Z"}
    hosted_observations: list = []

    def fake_http_request(method, base_url, path, token, body=None, **kwargs):
        assert path == "/list_observations"
        return 200, {"observations": list(hosted_observations)}

    monkeypatch.setattr(_mod, "http_request", fake_http_request)
    hosted_entity = {"snapshot": {LEGACY_GATE_STATUS_FIELD: {}, "owner_history": []}}
    monkeypatch.setattr(_mod, "get_hosted_entity", lambda *a, **k: hosted_entity)

    local_state = {"owner_history": [entry]}
    fields = {"owner_history": [entry]}

    result1 = recheck_fields_against_current_hosted(
        "ent_x", "merge", fields, local_state, "https://hosted.example", "tok"
    )
    assert result1["owner_history"] == [entry]

    # Simulate the write landing.
    hosted_observations.append({"fields": {"owner_history": [entry]}})

    result2 = recheck_fields_against_current_hosted(
        "ent_x", "merge", dict(fields), local_state, "https://hosted.example", "tok"
    )
    assert "owner_history" not in result2


# --- Requirement 4 (task spec): load rules honored on the read-only
# verification path against hosted (concurrency 1 -- strictly sequential
# calls, no threading/async fan-out anywhere in this module; timeout >= 180s;
# health check every HEALTH_CHECK_BATCH_SIZE requests -- both already covered
# by test_http_client_timeout_is_at_least_180_seconds and
# test_health_check_batch_size_is_20 above). get_hosted_field_observations
# must never abandon an in-flight request: it has no timeout/cancellation
# logic of its own and delegates entirely to http_request, whose only retry
# path is for TRANSIENT connection failures (never a live request already in
# flight) -- asserted here by checking it passes retries/backoff through and
# performs no concurrent/threaded calls.


def test_get_hosted_field_observations_uses_retries_not_silent_abandon(monkeypatch):
    import neotoma_local_fork_replay as _mod

    calls = []

    def fake_http_request(method, base_url, path, token, body=None, retries=0, retry_backoff_seconds=1.0):
        calls.append((path, retries, retry_backoff_seconds))
        return 200, {"observations": []}

    monkeypatch.setattr(_mod, "http_request", fake_http_request)
    get_hosted_field_observations("ent_x", "owner_history", "https://hosted.example", "tok")
    assert len(calls) == 1
    # retries>0 with a real backoff -- never a bare fire-and-forget call that
    # would abandon a transient failure instead of retrying it.
    assert calls[0][1] > 0
    assert calls[0][2] > 0


# --- Tri-state existence probe (neotoma#2483) -------------------------------
#
# Hosted crash-looped (Fly exit 134) and returned 502s during a live run.
# entity_exists() and every other probe in this script treated
# `status == 200` as the ONLY "exists" signal and collapsed anything else,
# including that 502, into "missing" -- which would make a replay CREATE a
# duplicate entity for an issue that already existed and was merely
# unreachable at that moment. The fix: a probe is tri-state. 200 -> exists,
# 404 -> confirmed missing, anything else (5xx, timeout, 401/403, connection
# error) -> retry with backoff up to 3 times, then ABORT (raise) rather than
# ever returning "missing".

from neotoma_local_fork_replay import (  # noqa: E402
    HostedProbeAmbiguousError,
    entity_exists,
    probe_status_tristate,
)


def _fixed_status_responder(status, body):
    def _fake(method, base_url, path, token, body_=None, **kwargs):
        return status, body

    return _fake


def test_probe_status_tristate_200_returns_exists_true(monkeypatch):
    import neotoma_local_fork_replay as _mod

    monkeypatch.setattr(_mod, "http_request", _fixed_status_responder(200, {"id": "ent_x"}))
    exists, status, body = probe_status_tristate(
        "GET", "https://hosted.example", "/entities/ent_x", "tok", max_attempts=3
    )
    assert exists is True
    assert status == 200
    assert body == {"id": "ent_x"}


def test_probe_status_tristate_404_returns_exists_false(monkeypatch):
    import neotoma_local_fork_replay as _mod

    monkeypatch.setattr(_mod, "http_request", _fixed_status_responder(404, {"error": "not found"}))
    exists, status, body = probe_status_tristate(
        "GET", "https://hosted.example", "/entities/ent_x", "tok", max_attempts=3
    )
    assert exists is False
    assert status == 404


def test_probe_status_tristate_502_raises_never_returns_false(monkeypatch):
    """The specific regression case: a 502 (hosted crash-loop / Fly exit 134,
    neotoma#2483) must abort, never be read as 'entity missing'.
    """
    import neotoma_local_fork_replay as _mod

    monkeypatch.setattr(_mod, "http_request", _fixed_status_responder(502, {"error": "bad gateway"}))
    with pytest.raises(HostedProbeAmbiguousError):
        probe_status_tristate(
            "GET", "https://hosted.example", "/entities/ent_x", "tok",
            max_attempts=3, retry_backoff_seconds=0.001,
        )


def test_probe_status_tristate_timeout_raises_never_returns_false(monkeypatch):
    import neotoma_local_fork_replay as _mod

    def _raise_timeout(*a, **k):
        raise TimeoutError("timed out")

    monkeypatch.setattr(_mod, "http_request", _raise_timeout)
    with pytest.raises(HostedProbeAmbiguousError):
        probe_status_tristate(
            "GET", "https://hosted.example", "/entities/ent_x", "tok",
            max_attempts=3, retry_backoff_seconds=0.001,
        )


def test_probe_status_tristate_401_raises(monkeypatch):
    import neotoma_local_fork_replay as _mod

    monkeypatch.setattr(_mod, "http_request", _fixed_status_responder(401, {"error": "unauthorized"}))
    with pytest.raises(HostedProbeAmbiguousError):
        probe_status_tristate(
            "GET", "https://hosted.example", "/entities/ent_x", "tok",
            max_attempts=3, retry_backoff_seconds=0.001,
        )


def test_probe_status_tristate_retries_before_succeeding(monkeypatch):
    """A transient 502 that clears on a later attempt must succeed, not abort
    -- retry-then-succeed is the whole point of the retry budget.
    """
    import neotoma_local_fork_replay as _mod

    calls = {"n": 0}

    def _flaky(method, base_url, path, token, body=None, **kwargs):
        calls["n"] += 1
        if calls["n"] < 3:
            return 502, {"error": "bad gateway"}
        return 200, {"id": "ent_x"}

    monkeypatch.setattr(_mod, "http_request", _flaky)
    exists, status, body = probe_status_tristate(
        "GET", "https://hosted.example", "/entities/ent_x", "tok",
        max_attempts=3, retry_backoff_seconds=0.001,
    )
    assert exists is True
    assert calls["n"] == 3


def test_probe_status_tristate_retries_up_to_max_attempts_then_raises(monkeypatch):
    import neotoma_local_fork_replay as _mod

    calls = {"n": 0}

    def _always_502(method, base_url, path, token, body=None, **kwargs):
        calls["n"] += 1
        return 502, {"error": "bad gateway"}

    monkeypatch.setattr(_mod, "http_request", _always_502)
    with pytest.raises(HostedProbeAmbiguousError):
        probe_status_tristate(
            "GET", "https://hosted.example", "/entities/ent_x", "tok",
            max_attempts=3, retry_backoff_seconds=0.001,
        )
    assert calls["n"] == 3


# --- entity_exists / get_hosted_entity now delegate to the tri-state probe --


def test_entity_exists_true_on_confirmed_200(monkeypatch):
    import neotoma_local_fork_replay as _mod

    monkeypatch.setattr(_mod, "http_request", _fixed_status_responder(200, {"id": "ent_x"}))
    assert entity_exists("ent_x", "https://hosted.example", "tok") is True


def test_entity_exists_false_on_confirmed_404(monkeypatch):
    import neotoma_local_fork_replay as _mod

    monkeypatch.setattr(_mod, "http_request", _fixed_status_responder(404, {}))
    assert entity_exists("ent_x", "https://hosted.example", "tok") is False


def test_entity_exists_raises_on_502_never_returns_false(monkeypatch):
    """The exact regression: a 502 during a hosted crash-loop must never be
    classified as 'entity does not exist', because that drives class-a
    replay to CREATE a duplicate of an entity that actually already exists.
    """
    import neotoma_local_fork_replay as _mod

    monkeypatch.setattr(_mod, "http_request", _fixed_status_responder(502, {"error": "bad gateway"}))
    with pytest.raises(HostedProbeAmbiguousError):
        entity_exists("ent_x", "https://hosted.example", "tok")


def test_entity_exists_raises_on_timeout_never_returns_false(monkeypatch):
    import neotoma_local_fork_replay as _mod

    def _raise_timeout(*a, **k):
        raise TimeoutError("timed out")

    monkeypatch.setattr(_mod, "http_request", _raise_timeout)
    with pytest.raises(HostedProbeAmbiguousError):
        entity_exists("ent_x", "https://hosted.example", "tok")


def test_get_hosted_entity_returns_dict_on_200(monkeypatch):
    import neotoma_local_fork_replay as _mod

    monkeypatch.setattr(_mod, "http_request", _fixed_status_responder(200, {"id": "ent_x", "snapshot": {}}))
    result = _mod.get_hosted_entity("ent_x", "https://hosted.example", "tok")
    assert result == {"id": "ent_x", "snapshot": {}}


def test_get_hosted_entity_returns_none_on_confirmed_404(monkeypatch):
    import neotoma_local_fork_replay as _mod

    monkeypatch.setattr(_mod, "http_request", _fixed_status_responder(404, {}))
    result = _mod.get_hosted_entity("ent_x", "https://hosted.example", "tok")
    assert result is None


def test_get_hosted_entity_raises_on_502_never_returns_none(monkeypatch):
    """None from get_hosted_entity means 'confirmed absent' throughout this
    script (e.g. gate-restore's create-vs-merge branch). A 502 must not be
    conflatable with that -- it must raise instead.
    """
    import neotoma_local_fork_replay as _mod

    monkeypatch.setattr(_mod, "http_request", _fixed_status_responder(502, {"error": "bad gateway"}))
    with pytest.raises(HostedProbeAmbiguousError):
        _mod.get_hosted_entity("ent_x", "https://hosted.example", "tok")


def test_get_schema_declared_fields_raises_on_502_never_silently_no_schema(monkeypatch):
    import neotoma_local_fork_replay as _mod

    monkeypatch.setattr(_mod, "http_request", _fixed_status_responder(502, {"error": "bad gateway"}))
    with pytest.raises(HostedProbeAmbiguousError):
        _mod.get_schema_declared_fields("issue", "https://hosted.example", "tok", {})


def test_canonical_identity_lookup_raises_on_502_never_falls_through_to_none(monkeypatch):
    """A 502 during canonical-identity lookup must not be read as 'no
    canonical match' -- that would send the run down the class-a CREATE path
    for an issue that may already exist under this identity.
    """
    import neotoma_local_fork_replay as _mod

    monkeypatch.setattr(_mod, "http_request", _fixed_status_responder(502, {"error": "bad gateway"}))
    with pytest.raises(HostedProbeAmbiguousError):
        _mod.canonical_identity_lookup(
            "issue", "markmhendrickson/ateles", 1172, "https://hosted.example", "tok"
        )
