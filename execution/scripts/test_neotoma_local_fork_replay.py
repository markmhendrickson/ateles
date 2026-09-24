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


SCRIPT_PATH = str(Path(__file__).resolve().parent / "neotoma_local_fork_replay.py")


def _run_cli(args_list, env=None, timeout=10):
    import os as _os
    import subprocess
    import sys as _sys

    if env is None:
        env = dict(_os.environ)
        # A fake but present base_url/token so dry-run paths (which still
        # call get_base_url()/get_token() before doing any real work) don't
        # fail on missing env for tests that aren't exercising that check
        # specifically. No real HTTP call is made in these tests.
        env.setdefault("NEOTOMA_BASE_URL", "https://hosted.example.invalid")
        env.setdefault("NEOTOMA_BEARER_TOKEN", "test-token")
    return subprocess.run(
        [_sys.executable, SCRIPT_PATH, *args_list],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )


def test_unknown_fields_cli_flag_defaults_to_stop():
    # --help exits 0 and prints argparse's own rendering of the flag; assert
    # the default is documented as 'stop' and both choices are offered,
    # without needing a live DB or hosted credentials.
    result = _run_cli(["replay", "--help"])
    assert result.returncode == 0
    assert "--unknown-fields" in result.stdout
    assert "{stop,warn}" in result.stdout or "stop,warn" in result.stdout


def test_extend_schemas_and_reconcile_file_flags_present_in_help():
    replay_result = _run_cli(["replay", "--help"])
    assert replay_result.returncode == 0
    assert "--extend-schemas" in replay_result.stdout

    reconcile_result = _run_cli(["reconcile", "--help"])
    assert reconcile_result.returncode == 0
    assert "--reconcile-file" in reconcile_result.stdout


def test_apply_without_confirm_env_refuses_before_touching_hosted():
    """The double guard: --apply alone must refuse, even with no --db given --
    it must fail closed before reaching any hosted call or the --db check."""
    import os as _os

    env = dict(_os.environ)
    env.pop("NEOTOMA_REPLAY_CONFIRM_APPLY", None)
    env.pop("MIGRATE_CONFIRM_APPLY", None)
    result = _run_cli(
        ["replay", "--db", "/tmp/does-not-exist.db", "--cutover", "2026-01-01T00:00:00Z", "--apply"],
        env=env,
    )
    assert result.returncode == 1
    assert "code=E_CONFIRMATION_REQUIRED" in result.stderr
    assert "NEOTOMA_REPLAY_CONFIRM_APPLY=yes" in result.stderr


def test_apply_with_wrong_confirm_value_still_refuses():
    import os as _os

    env = dict(_os.environ)
    env.pop("MIGRATE_CONFIRM_APPLY", None)
    env["NEOTOMA_REPLAY_CONFIRM_APPLY"] = "true"  # anything other than the literal "yes"
    result = _run_cli(
        ["replay", "--db", "/tmp/does-not-exist.db", "--cutover", "2026-01-01T00:00:00Z", "--apply"],
        env=env,
    )
    assert result.returncode == 1
    assert "code=E_CONFIRMATION_REQUIRED" in result.stderr
    assert "NEOTOMA_REPLAY_CONFIRM_APPLY=yes" in result.stderr


def test_apply_refuses_without_a_signing_identity():
    """ateles#1223: --apply must fail closed (E_SIGNING_UNAVAILABLE) before
    any hosted call when the --sign-as identity has no AAuth JWK key on
    disk, rather than silently falling back to the bearer token for a
    write. ATELES_AAUTH_KEYS_DIR points at an empty dir so this is
    deterministic regardless of what keys the running machine happens to
    have under ~/repos/ateles-private/keys."""
    import os as _os

    env = dict(_os.environ)
    env["NEOTOMA_REPLAY_CONFIRM_APPLY"] = "yes"
    env["ATELES_AAUTH_KEYS_DIR"] = "/tmp/neotoma-local-fork-replay-test-no-such-keys-dir"
    result = _run_cli(
        ["replay", "--db", "/tmp/does-not-exist.db", "--cutover", "2026-01-01T00:00:00Z", "--apply"],
        env=env,
    )
    assert result.returncode == 1
    assert "code=E_SIGNING_UNAVAILABLE" in result.stderr
    assert "ateles@ateles-swarm" in result.stderr
    assert "NEOTOMA_BEARER_TOKEN" in result.stderr  # "never falls back" language present


def test_apply_refuses_for_an_explicit_sign_as_with_no_key_either():
    """Same guard, but exercised via an explicit --sign-as rather than the
    default, confirming the flag is actually read (not just the default
    constant)."""
    import os as _os

    env = dict(_os.environ)
    env["NEOTOMA_REPLAY_CONFIRM_APPLY"] = "yes"
    result = _run_cli(
        [
            "replay", "--db", "/tmp/does-not-exist.db",
            "--cutover", "2026-01-01T00:00:00Z", "--apply",
            "--sign-as", "nosuchagent@ateles-swarm",
        ],
        env=env,
    )
    assert result.returncode == 1
    assert "code=E_SIGNING_UNAVAILABLE" in result.stderr
    assert "nosuchagent@ateles-swarm" in result.stderr


def test_dry_run_does_not_require_a_signing_identity():
    """Dry-run must NOT be gated on signing availability -- only --apply is.
    Confirms the signing check is skipped entirely in dry-run (the run
    proceeds to its normal dry-run NO_CHANGES path rather than refusing)."""
    import os as _os

    env = dict(_os.environ)
    env["ATELES_AAUTH_KEYS_DIR"] = "/tmp/neotoma-local-fork-replay-test-no-such-keys-dir"
    result = _run_cli(
        ["replay", "--db", "/tmp/does-not-exist.db", "--cutover", "2026-01-01T00:00:00Z"],
        env=env,
    )
    assert "code=E_SIGNING_UNAVAILABLE" not in result.stderr


def test_signed_write_shells_out_via_neotoma_signed_with_no_bearer_header(monkeypatch, tmp_path):
    """Requests carry signatures, and never a bearer header, on the write
    path (ateles#1223). signed_write must route through
    neotoma_signed.signed_request rather than constructing an
    Authorization: Bearer header itself -- asserted here by monkeypatching
    neotoma_signed.signed_request and inspecting exactly what it was called
    with (method/url/body/agent_name), with NO token/bearer argument
    anywhere in that call's signature or kwargs."""
    import inspect
    import neotoma_local_fork_replay as _mod

    calls = []

    def fake_signed_request(method, url, body=None, agent_name="", timeout=20):
        calls.append(
            {"method": method, "url": url, "body": body, "agent_name": agent_name}
        )
        return 200, {"success": True}

    # Confirm the real signed_request signature has no bearer/token
    # parameter at all -- the write path has no way to smuggle one in.
    real_sig = inspect.signature(_mod._neotoma_signed.signed_request)
    assert "token" not in real_sig.parameters
    assert "bearer" not in real_sig.parameters
    assert "authorization" not in {p.lower() for p in real_sig.parameters}

    monkeypatch.setattr(_mod._neotoma_signed, "signed_request", fake_signed_request)

    status, resp = _mod.signed_write(
        "POST", "https://hosted.example.invalid", "/store",
        "ateles@ateles-swarm", {"entities": [{"entity_type": "task"}]},
    )

    assert status == 200
    assert resp == {"success": True}
    assert len(calls) == 1
    call = calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "https://hosted.example.invalid/store"
    assert call["agent_name"] == "ateles"
    assert call["body"] == {"entities": [{"entity_type": "task"}]}
    # No bearer/Authorization header was ever constructed by signed_write
    # itself -- it delegates entirely to neotoma_signed, which signs with
    # the agent's own key rather than a shared token.
    assert "token" not in call
    assert "Authorization" not in call
    assert "bearer" not in json.dumps(call).lower()


def test_signed_write_enables_via_cli_flag_only_for_the_call_duration(monkeypatch):
    """signed_write flips NEOTOMA_AAUTH_VIA_CLI on for the duration of the
    call and restores whatever value (or absence) preceded it -- so it
    can't leave the flag globally enabled for unrelated code running later
    in the same process."""
    import os as _os
    import neotoma_local_fork_replay as _mod

    _os.environ.pop("NEOTOMA_AAUTH_VIA_CLI", None)
    seen_during_call = {}

    def fake_signed_request(method, url, body=None, agent_name="", timeout=20):
        seen_during_call["value"] = _os.environ.get("NEOTOMA_AAUTH_VIA_CLI")
        return 200, {}

    monkeypatch.setattr(_mod._neotoma_signed, "signed_request", fake_signed_request)

    _mod.signed_write("POST", "https://hosted.example.invalid", "/store", "ateles@ateles-swarm", {})

    assert seen_during_call["value"] == "1"
    assert "NEOTOMA_AAUTH_VIA_CLI" not in _os.environ


def test_apply_with_deprecated_migrate_confirm_apply_still_works_with_warning():
    """MIGRATE_CONFIRM_APPLY is kept as a deprecated fallback (only honored
    when NEOTOMA_REPLAY_CONFIRM_APPLY is absent) -- confirms it still passes
    the double-guard and prints a deprecation warning rather than silently
    dropping an in-flight invocation."""
    import os as _os

    env = dict(_os.environ)
    env.pop("NEOTOMA_REPLAY_CONFIRM_APPLY", None)
    env["MIGRATE_CONFIRM_APPLY"] = "yes"
    env["NEOTOMA_BASE_URL"] = "https://example.invalid"
    env["NEOTOMA_BEARER_TOKEN"] = "test-token"
    # Deterministic regardless of what AAuth keys the running machine has:
    # point at an empty keys dir so this exercises "passes confirm gate,
    # then fails on the next guard (signing)" rather than depending on a
    # real ateles.jwk.json being present.
    env["ATELES_AAUTH_KEYS_DIR"] = "/tmp/neotoma-local-fork-replay-test-no-such-keys-dir"
    result = _run_cli(
        ["replay", "--db", "/tmp/does-not-exist-for-deprecated-alias-test.db",
         "--cutover", "2026-01-01T00:00:00Z", "--apply"],
        env=env,
    )
    # Passes the confirm gate (no E_CONFIRMATION_REQUIRED) and prints the
    # deprecation warning; it then fails on the next guard down the chain
    # (signing identity unavailable, since ATELES_AAUTH_KEYS_DIR above is
    # empty) rather than the confirm gate -- confirming MIGRATE_CONFIRM_APPLY
    # genuinely unblocked --apply rather than the run never reaching that
    # far at all.
    assert "code=E_CONFIRMATION_REQUIRED" not in result.stderr
    assert "MIGRATE_CONFIRM_APPLY is deprecated" in result.stderr
    assert "code=E_SIGNING_UNAVAILABLE" in result.stderr


def test_zero_references_to_deprecated_confirm_var_remain_as_the_primary_name():
    """The deprecated alias is kept ONLY as a fallback -- help text and the
    apply-hint/documentation strings must reference NEOTOMA_REPLAY_CONFIRM_APPLY,
    never MIGRATE_CONFIRM_APPLY, as the name to use."""
    result = _run_cli(["--help"])
    assert "MIGRATE_CONFIRM_APPLY" not in result.stdout
    assert "NEOTOMA_REPLAY_CONFIRM_APPLY" in result.stdout


def test_cli_apply_and_dry_run_conflict_any_mode():
    for mode, extra in (
        ("replay", ["--db", "/tmp/x.db"]),
        ("reconcile", ["--db", "/tmp/x.db", "--reconcile-file", "/tmp/r.json"]),
        ("restore-gates", ["--db", "/tmp/x.db"]),
    ):
        result = _run_cli(
            [mode, *extra, "--cutover", "2026-01-01T00:00:00Z", "--apply", "--dry-run"]
        )
        assert result.returncode == 1, mode
        assert "code=E_ARGUMENT_CONFLICT" in result.stderr, mode
        assert "writes_occurred=no" in result.stderr, mode


def test_cli_no_subcommand_is_argument_conflict():
    result = _run_cli([])
    assert result.returncode == 1
    assert "code=E_ARGUMENT_CONFLICT" in result.stderr


def test_cli_restore_gates_requires_explicit_db():
    result = _run_cli(["restore-gates", "--cutover", "2026-01-01T00:00:00Z"])
    assert result.returncode != 0
    assert "--db" in result.stderr

    help_result = _run_cli(["restore-gates", "--help"])
    assert "~/data/neotoma" not in help_result.stdout


def test_cli_replay_requires_explicit_db():
    result = _run_cli(["replay", "--cutover", "2026-01-01T00:00:00Z"])
    assert result.returncode != 0
    assert "--db" in result.stderr


def test_cli_omitting_both_apply_flags_is_dry_run_no_confirm_env_required():
    import os as _os

    env = dict(_os.environ)
    env.pop("NEOTOMA_REPLAY_CONFIRM_APPLY", None)
    env.pop("MIGRATE_CONFIRM_APPLY", None)
    result = _run_cli(
        ["replay", "--db", "/tmp/does-not-exist.db", "--cutover", "2026-01-01T00:00:00Z"],
        env=env,
    )
    # Fails downstream (no such DB), but NOT on the confirm gate -- proves
    # dry-run never checks the confirm env at all.
    assert "code=E_CONFIRMATION_REQUIRED" not in result.stderr


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
                    "gate_status": {"strategy": "last_write"},  # vocab-ok: hosted legacy-field contract
                }
            },
        }

    monkeypatch.setattr(_mod, "http_request", fake_http_request)
    info = _mod.get_schema_declared_fields("issue", "https://hosted.example", "tok", {})
    assert info["merge_array_fields"] == {"owner_history"}
    assert "gate_status" not in info["merge_array_fields"]  # vocab-ok: hosted legacy-field contract


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


# --- CLI end-to-end: NO_CHANGES / apply-hint reproduction -------------------


def _make_empty_fork_db(tmp_path):
    """A real, on-disk local-fork DB with the full three-table shape
    load_candidates() reads (observations, relationship_observations,
    sources), but zero rows -- for a genuine CLI-level NO_CHANGES run with
    no mocking of load_candidates itself."""
    import sqlite3 as _sqlite3

    db_path = tmp_path / "empty_fork.db"
    conn = _sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE observations (id TEXT, entity_id TEXT, entity_type TEXT, "
        "schema_version TEXT, source_id TEXT, interpretation_id TEXT, observed_at TEXT, "
        "specificity_score REAL, source_priority TEXT, fields TEXT, created_at TEXT, "
        "user_id TEXT, idempotency_key TEXT, observation_source TEXT)"
    )
    conn.execute(
        "CREATE TABLE relationship_observations (id TEXT, relationship_key TEXT, "
        "relationship_type TEXT, source_entity_id TEXT, target_entity_id TEXT, "
        "source_id TEXT, interpretation_id TEXT, observed_at TEXT, specificity_score REAL, "
        "source_priority TEXT, metadata TEXT, created_at TEXT, user_id TEXT)"
    )
    conn.execute(
        "CREATE TABLE sources (id TEXT, user_id TEXT, content_hash TEXT, mime_type TEXT, "
        "storage_url TEXT, file_size INTEGER, original_filename TEXT, provenance TEXT, "
        "created_at TEXT, idempotency_key TEXT, source_type TEXT, storage_mode TEXT, "
        "reference_path TEXT)"
    )
    conn.commit()
    conn.close()
    return str(db_path)


def test_cli_replay_empty_db_dry_run_reports_no_changes(tmp_path):
    db_path = _make_empty_fork_db(tmp_path)
    log_path = tmp_path / "action.jsonl"
    result = _run_cli(
        ["replay", "--db", db_path, "--cutover", "2026-01-01T00:00:00Z", "--log", str(log_path)]
    )
    assert result.returncode == 0
    assert "NO_CHANGES" in result.stdout
    assert "To apply:" not in result.stdout


def test_cli_reconcile_empty_file_dry_run_reports_no_changes(tmp_path):
    db_path = _make_empty_fork_db(tmp_path)
    reconcile_file = tmp_path / "reconciliation.json"
    reconcile_file.write_text("[]", encoding="utf-8")
    log_path = tmp_path / "action.jsonl"
    result = _run_cli(
        [
            "reconcile",
            "--db", db_path,
            "--cutover", "2026-01-01T00:00:00Z",
            "--reconcile-file", str(reconcile_file),
            "--log", str(log_path),
        ]
    )
    assert result.returncode == 0
    assert "NO_CHANGES" in result.stdout


def test_build_apply_hint_reproduces_mode_and_safety_flags_appends_only_apply():
    """Accipiter P1 fix: the apply hint must reprint the exact resolved argv
    (mode + safety flags) and append only --apply -- never drop the mode,
    filters, or actual db/cutover paths. Unit-level (no subprocess/network):
    the CLI-level NO_CHANGES tests above already exercise the real process
    boundary; this isolates the hint-construction logic itself, since a live
    replay run also does an (unrelated) hosted schema GET that would need
    mocking to keep this fast and network-free."""
    import neotoma_local_fork_replay as _mod

    argv = [
        "replay",
        "--db", "/path/to/fork.db",
        "--cutover", "2026-01-01T00:00:00Z",
        "--no-only-missing",
        "--log", "/path/to/action.jsonl",
    ]
    hint = _mod.build_apply_hint("replay", argv)
    assert "replay" in hint
    assert "/path/to/fork.db" in hint
    assert "2026-01-01T00:00:00Z" in hint
    assert hint.count("--apply") == 1
    assert "--dry-run" not in hint


def test_build_apply_hint_strips_dry_run_token_if_present():
    import neotoma_local_fork_replay as _mod

    argv = ["replay", "--db", "x.db", "--cutover", "2026-01-01T00:00:00Z", "--dry-run"]
    hint = _mod.build_apply_hint("replay", argv)
    assert "--dry-run" not in hint
    assert "--apply" in hint


# =============================================================================
# QA (Phoenicurus) findings on PR #1167, 2026-09-23/24 -----------------------
# =============================================================================
#
# The tests below close out the QA lens's REQUEST_CHANGES on this PR:
#   - eval-coverage: restore-gates' 150-issue sanity threshold had no
#     boundary test (150 proceeds, 151 refuses before any write).
#   - test-coverage: run_gate_restore / run_reconciliation, the two
#     apply-mode orchestrators, were never invoked end to end by any test.
#   - NEOTOMA_REPLAY_CONFIRM_APPLY vs the deprecated MIGRATE_CONFIRM_APPLY
#     disagreeing (the new var always wins).
#   - the documented canary filters (--limit / --entity-ids /
#     --exclude-entity-ids) lacked direct behavioural tests.
#   - the JSONL action log's "never logs field contents" claim was asserted
#     nowhere.
#
# All of these drive run_gate_restore()/run_reconciliation() directly
# in-process (real argparse.Namespace via build_arg_parser().parse_args) with
# http_request monkeypatched -- no live HTTP, matching every other apply-mode
# test in this file (see test_gate_restore_owner_history_converges_to_zero_
# after_simulated_write above).


def _gate_restore_args(tmp_path, db_paths, *, apply=False, cutover="2026-01-01T00:00:00Z", **extra):
    """Build a real argparse.Namespace for the restore-gates subcommand via
    the script's own parser, so these tests exercise the exact flags/defaults
    main() would pass to run_gate_restore rather than a hand-rolled stand-in
    that could silently drift from the real CLI surface."""
    import neotoma_local_fork_replay as _mod

    argv = [
        "restore-gates",
        "--db", ",".join(db_paths),
        "--cutover", cutover,
        "--log", str(tmp_path / "action.jsonl"),
    ]
    if apply:
        argv.append("--apply")
    for flag, value in extra.items():
        argv.extend([flag, value])
    ap = _mod.build_arg_parser()
    return ap.parse_args(argv)


def _make_gate_candidate_db(tmp_path, name, entities):
    """entities: dict of entity_id -> (repo, github_number, gate_status_dict).
    Builds a minimal observations table with one post-cutover row per entity,
    matching the columns scan_local_gate_candidates reads."""
    import sqlite3 as _sqlite3

    db_path = tmp_path / name
    conn = _sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE observations (id TEXT, entity_id TEXT, entity_type TEXT, fields TEXT, created_at TEXT)"
    )
    for i, (entity_id, (repo, github_number, gate_status)) in enumerate(entities.items()):
        conn.execute(
            "INSERT INTO observations VALUES (?,?,?,?,?)",
            (
                f"o{i}",
                entity_id,
                "issue",
                json.dumps(
                    {
                        LEGACY_GATE_STATUS_FIELD: gate_status,
                        "repo": repo,
                        "github_number": github_number,
                    }
                ),
                "2026-09-23T09:00:00Z",
            ),
        )
    conn.commit()
    conn.close()
    return str(db_path)


def _fake_github_open(monkeypatch):
    """Every candidate resolves as OPEN on GitHub -- isolates these tests
    from the identity filter's own behavior (covered separately above) so
    they can focus on the sanity threshold / orchestration under test."""
    import neotoma_local_fork_replay as _mod

    monkeypatch.setattr(
        _mod, "github_issue_lookup", lambda repo, number: {"state": "open"}
    )


def _permissive_grant_entity(sub="ateles@ateles-swarm"):
    """A hosted agent_grant list entry covering everything (op="*"-style via
    explicit "*" entity_types on every op this script's writes use), for
    fake routers that don't care about the pre-flight grant-coverage check
    itself (see test_preflight_grant_check_* below for the ones that do)."""
    return {
        "entity_id": "ent_test_grant",
        "entity_type": "agent_grant",
        "snapshot": {
            "match_sub": sub,
            "status": "active",
            "capabilities": [
                {"op": "store_structured", "entity_types": ["*"]},
                {"op": "create_relationship", "entity_types": ["*"]},
                {"op": "retrieve", "entity_types": ["*"]},
                {"op": "correct", "entity_types": ["*"]},
            ],
        },
    }


def _fake_hosted_gate_restore_router(monkeypatch, *, store_status=200, grant_entities=None):
    """A minimal hosted double for run_gate_restore's HTTP surface:
    GET /entities/<id> -> confirmed 404 (no hosted issue yet, so every
    candidate plans as a 'create'), GET /health -> 200 ok, POST /store ->
    store_status. Returns the list of POST /store bodies sent, so callers
    can assert on idempotency keys / no-writes-in-dry-run / log contents.

    POST /store is a WRITE (ateles#1223): the real code path now sends it
    via signed_write, not http_request, so both are faked here -- the fake
    http_request covers the read-only probes/health as before, and
    signed_write is monkeypatched separately to route into the same
    store_calls list rather than shelling out to a real AAuth signer.

    GET /entities?entity_type=agent_grant is the pre-flight grant-coverage
    check's read (ateles#1223 follow-up) -- defaults to a permissive grant
    so existing apply-mode tests aren't newly gated by it; pass
    grant_entities to exercise the gap-detection path itself.
    """
    import neotoma_local_fork_replay as _mod

    if grant_entities is None:
        grant_entities = [_permissive_grant_entity()]
    store_calls: list[dict] = []

    def fake_http_request(method, base_url, path, token, body=None, **kwargs):
        if path == "/health":
            return 200, {"ok": True}
        if path.startswith("/entities?entity_type=agent_grant"):
            return 200, {"entities": grant_entities}
        if path.startswith("/schemas/"):
            return 404, {"error": "not found"}
        if path.startswith("/entities/"):
            return 404, {"error": "not found"}
        if path == "/retrieve_entity_by_identifier":
            # No canonical hosted entity either -- every candidate plans as
            # a genuine 'create'.
            return 404, {"error": "not found"}
        if path == "/store":
            store_calls.append(body)
            return store_status, {"success": True}
        raise AssertionError(f"unexpected call: {method} {path}")

    def fake_signed_write(method, base_url, path, sign_as, body=None, **kwargs):
        assert sign_as, "signed_write called with no signing identity"
        if path == "/store":
            store_calls.append(body)
            return store_status, {"success": True}
        raise AssertionError(f"unexpected signed write: {method} {path}")

    monkeypatch.setattr(_mod, "http_request", fake_http_request)
    monkeypatch.setattr(_mod, "signed_write", fake_signed_write)
    return store_calls


# --- 150-issue restore-gates sanity threshold: boundary test ---------------


def test_restore_gates_150_planned_changes_proceeds(tmp_path, monkeypatch, capsys):
    import neotoma_local_fork_replay as _mod

    _fake_github_open(monkeypatch)
    store_calls = _fake_hosted_gate_restore_router(monkeypatch)

    entities = {
        f"ent_{i:04d}": (f"owner/repo{i}", i, {"pm": "signed_off"})
        for i in range(150)
    }
    db_path = _make_gate_candidate_db(tmp_path, "db150.db", entities)
    args = _gate_restore_args(tmp_path, [db_path], apply=False)

    _mod.run_gate_restore(args, "https://hosted.example.invalid", "tok", apply_mode=False)

    out = capsys.readouterr().out
    assert "E_SANITY_THRESHOLD" not in out
    assert "150" in out
    assert store_calls == []  # dry run: no writes regardless of threshold


def test_restore_gates_151_planned_changes_refuses_before_any_write(
    tmp_path, monkeypatch, capsys
):
    import neotoma_local_fork_replay as _mod

    _fake_github_open(monkeypatch)
    store_calls = _fake_hosted_gate_restore_router(monkeypatch)

    entities = {
        f"ent_{i:04d}": (f"owner/repo{i}", i, {"pm": "signed_off"})
        for i in range(151)
    }
    db_path = _make_gate_candidate_db(tmp_path, "db151.db", entities)
    args = _gate_restore_args(tmp_path, [db_path], apply=False)

    with pytest.raises(SystemExit) as exc_info:
        _mod.run_gate_restore(args, "https://hosted.example.invalid", "tok", apply_mode=False)

    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert "code=E_SANITY_THRESHOLD" in err
    assert "151" in err
    assert "writes_occurred=no" in err
    assert store_calls == []  # the whole point of the gate: refuses before any write


# --- run_gate_restore end to end (dry-run and apply), mocked HTTP ----------


def test_run_gate_restore_dry_run_issues_no_store_writes(tmp_path, monkeypatch, capsys):
    import neotoma_local_fork_replay as _mod

    _fake_github_open(monkeypatch)
    store_calls = _fake_hosted_gate_restore_router(monkeypatch)

    db_path = _make_gate_candidate_db(
        tmp_path, "dry.db", {"ent_a": ("owner/repo", 42, {"pm": "signed_off"})}
    )
    args = _gate_restore_args(tmp_path, [db_path], apply=False)

    _mod.run_gate_restore(args, "https://hosted.example.invalid", "tok", apply_mode=False)

    assert store_calls == []
    out = capsys.readouterr().out
    assert "This was a DRY RUN. No data was written to hosted Neotoma." in out
    assert "owner/repo#42" in out


def test_run_gate_restore_apply_writes_expected_store_payload_with_idempotency_key(
    tmp_path, monkeypatch, capsys
):
    import neotoma_local_fork_replay as _mod

    _fake_github_open(monkeypatch)
    store_calls = _fake_hosted_gate_restore_router(monkeypatch)

    db_path = _make_gate_candidate_db(
        tmp_path, "apply.db", {"ent_a": ("owner/repo", 42, {"pm": "signed_off"})}
    )
    args = _gate_restore_args(tmp_path, [db_path], apply=True)

    _mod.run_gate_restore(args, "https://hosted.example.invalid", "tok", apply_mode=True)

    assert len(store_calls) == 1
    (payload,) = store_calls
    assert payload["idempotency_key"]
    (entity,) = payload["entities"]
    assert entity["target_id"] == "ent_a"
    assert entity["entity_type"] == "issue"
    assert entity[LEGACY_GATE_STATUS_FIELD] == {"pm": "signed_off"}

    out = capsys.readouterr().out
    assert "Applied: 1" in out
    assert "Failed:  0" in out


def test_run_gate_restore_apply_refuses_before_any_write_when_grant_lacks_issue(
    tmp_path, monkeypatch, capsys
):
    """ateles#1223 follow-up: the pre-flight grant-coverage check must abort
    BEFORE any /store write when the signing identity's agent_grant does not
    cover (store_structured, issue) -- restore-gates only ever writes
    `issue` entities, so a grant missing that type must refuse the whole
    run rather than fail partway through a batch."""
    import neotoma_local_fork_replay as _mod

    _fake_github_open(monkeypatch)
    grant_missing_issue = {
        "entity_id": "ent_test_grant",
        "entity_type": "agent_grant",
        "snapshot": {
            "match_sub": "ateles@ateles-swarm",
            "status": "active",
            "capabilities": [
                {"op": "store_structured", "entity_types": ["task", "note"]},
            ],
        },
    }
    store_calls = _fake_hosted_gate_restore_router(
        monkeypatch, grant_entities=[grant_missing_issue]
    )

    db_path = _make_gate_candidate_db(
        tmp_path, "apply.db", {"ent_a": ("owner/repo", 42, {"pm": "signed_off"})}
    )
    args = _gate_restore_args(tmp_path, [db_path], apply=True)

    with pytest.raises(SystemExit) as exc_info:
        _mod.run_gate_restore(args, "https://hosted.example.invalid", "tok", apply_mode=True)
    assert exc_info.value.code == 1

    assert store_calls == []  # no write reached hosted
    err = capsys.readouterr().err
    assert "code=E_SIGNING_UNAVAILABLE" in err
    assert "store_structured:issue" in err


def test_find_grant_gaps_treats_store_and_store_structured_as_the_same_family():
    """Mirrors hosted's grantOpMatchesRequested (agent_capabilities.ts): a
    grant entry for op="store" also covers a requested "store_structured",
    and vice versa -- these are the same family on hosted's admission
    path, so the pre-flight check must not report a false gap for it."""
    import neotoma_local_fork_replay as _mod

    caps = [{"op": "store", "entity_types": ["issue"]}]
    gaps = _mod.find_grant_gaps(caps, {("store_structured", "issue")})
    assert gaps == []

    caps2 = [{"op": "store_structured", "entity_types": ["issue"]}]
    gaps2 = _mod.find_grant_gaps(caps2, {("store", "issue")})
    assert gaps2 == []


def test_find_grant_gaps_reports_every_missing_pair_and_respects_wildcards():
    import neotoma_local_fork_replay as _mod

    caps = [
        {"op": "store_structured", "entity_types": ["task"]},
        {"op": "retrieve", "entity_types": ["*"]},
    ]
    needed = {
        ("store_structured", "task"),  # covered
        ("store_structured", "issue"),  # gap
        ("retrieve", "anything"),  # covered by "*"
        ("create_relationship", "task"),  # gap -- no create_relationship entry at all
    }
    gaps = _mod.find_grant_gaps(caps, needed)
    assert set(gaps) == {("store_structured", "issue"), ("create_relationship", "task")}


def test_find_grant_gaps_none_capabilities_means_every_pair_is_a_gap():
    """No active grant found for the sub at all -- every needed pair is
    reported, never silently treated as covered."""
    import neotoma_local_fork_replay as _mod

    gaps = _mod.find_grant_gaps(None, {("store_structured", "issue"), ("retrieve", "task")})
    assert set(gaps) == {("store_structured", "issue"), ("retrieve", "task")}


def test_run_gate_restore_apply_aborts_on_ambiguous_5xx_probe(tmp_path, monkeypatch):
    """An ambiguous (non-200/404) hosted GET /entities/<id> must raise
    HostedProbeAmbiguousError -- never be silently treated as 'missing' and
    planned as a create (neotoma#2483). run_gate_restore does not catch
    this itself, so it propagates out of the apply run entirely rather than
    proceeding to any /store write."""
    import neotoma_local_fork_replay as _mod

    _fake_github_open(monkeypatch)

    def fake_http_request(method, base_url, path, token, body=None, **kwargs):
        if path == "/health":
            return 200, {"ok": True}
        if path.startswith("/schemas/"):
            return 404, {"error": "not found"}
        if path.startswith("/entities/"):
            return 502, {"error": "bad gateway"}
        raise AssertionError(f"unexpected call reached /store on an ambiguous probe: {path}")

    monkeypatch.setattr(_mod, "http_request", fake_http_request)

    db_path = _make_gate_candidate_db(
        tmp_path, "ambiguous.db", {"ent_a": ("owner/repo", 42, {"pm": "signed_off"})}
    )
    args = _gate_restore_args(tmp_path, [db_path], apply=True)

    with pytest.raises(_mod.HostedProbeAmbiguousError):
        _mod.run_gate_restore(args, "https://hosted.example.invalid", "tok", apply_mode=True)


# --- run_reconciliation end to end (dry-run and apply), mocked HTTP -------


def _fake_hosted_reconcile_router(monkeypatch, *, store_status=200, hosted_fields=None, grant_entities=None):
    """A minimal hosted double for run_reconciliation's HTTP surface:
    GET /schemas/<type> -> confirmed 404 (no schema, so no merge_array
    reduction applies and nothing is stripped), GET /entities/<id> -> a
    hosted snapshot carrying `hosted_fields` (used by recheck_hosted_drift's
    apply-time hash re-check -- defaults to a `body` value whose hash
    matches the "stale hosted text" fixture the tests below use as
    hosted_value_hash, so the drift check finds no drift), POST /store ->
    store_status. Returns the list of POST /store bodies sent.

    POST /store is a WRITE (ateles#1223): the real code path now sends it
    via signed_write, not http_request -- both are faked here, matching
    _fake_hosted_gate_restore_router's approach. GET
    /entities?entity_type=agent_grant is the pre-flight grant-coverage
    check's read -- defaults to a permissive grant.
    """
    import neotoma_local_fork_replay as _mod

    if hosted_fields is None:
        hosted_fields = {"body": "stale hosted text"}
    if grant_entities is None:
        grant_entities = [_permissive_grant_entity()]
    store_calls: list[dict] = []

    def fake_http_request(method, base_url, path, token, body=None, **kwargs):
        if path.startswith("/entities?entity_type=agent_grant"):
            return 200, {"entities": grant_entities}
        if path.startswith("/schemas/"):
            return 404, {"error": "not found"}
        if path.startswith("/entities/"):
            return 200, {"fields": dict(hosted_fields)}
        if path == "/store":
            store_calls.append(body)
            return store_status, {"success": True}
        raise AssertionError(f"unexpected call: {method} {path}")

    def fake_signed_write(method, base_url, path, sign_as, body=None, **kwargs):
        assert sign_as, "signed_write called with no signing identity"
        if path == "/store":
            store_calls.append(body)
            return store_status, {"success": True}
        raise AssertionError(f"unexpected signed write: {method} {path}")

    monkeypatch.setattr(_mod, "http_request", fake_http_request)
    monkeypatch.setattr(_mod, "signed_write", fake_signed_write)
    return store_calls


def _reconcile_args(tmp_path, db_path, reconcile_file, *, apply=False, cutover="2026-01-01T00:00:00Z"):
    import neotoma_local_fork_replay as _mod

    argv = [
        "reconcile",
        "--db", db_path,
        "--cutover", cutover,
        "--reconcile-file", str(reconcile_file),
        "--log", str(tmp_path / "action.jsonl"),
    ]
    if apply:
        argv.append("--apply")
    ap = _mod.build_arg_parser()
    return ap.parse_args(argv)


def test_run_reconciliation_dry_run_issues_no_store_writes(tmp_path, monkeypatch, capsys):
    import neotoma_local_fork_replay as _mod

    store_calls = _fake_hosted_reconcile_router(monkeypatch)

    conn = _make_sqlite_with_observations(
        tmp_path, "ent_x", [({"body": "current text"}, "2026-08-05T00:00:00.000Z")]
    )
    reconcile_file = tmp_path / "reconciliation.json"
    reconcile_file.write_text(
        json.dumps(
            [
                {
                    "id": "ent_x",
                    "entity_type": "issue",
                    "fields": [
                        {
                            "name": "body",
                            "classification": "LOCAL_NEWER",
                            "local_value_hash": value_hash("current text"),
                            "hosted_value_hash": value_hash("stale hosted text"),
                        }
                    ],
                }
            ]
        ),
        encoding="utf-8",
    )
    args = _reconcile_args(tmp_path, str(tmp_path / "fork.db"), reconcile_file, apply=False)

    _mod.run_reconciliation(
        args, conn, "https://hosted.example.invalid", "tok", "20260101",
        apply_mode=False, entity_ids=None,
    )

    assert store_calls == []
    out = capsys.readouterr().out
    assert "This was a DRY RUN. No data was written to hosted Neotoma." in out


def test_run_reconciliation_apply_writes_expected_store_payload(tmp_path, monkeypatch, capsys):
    import neotoma_local_fork_replay as _mod

    store_calls = _fake_hosted_reconcile_router(monkeypatch)

    conn = _make_sqlite_with_observations(
        tmp_path, "ent_x", [({"body": "current text"}, "2026-08-05T00:00:00.000Z")]
    )
    reconcile_file = tmp_path / "reconciliation.json"
    reconcile_file.write_text(
        json.dumps(
            [
                {
                    "id": "ent_x",
                    "entity_type": "issue",
                    "fields": [
                        {
                            "name": "body",
                            "classification": "LOCAL_NEWER",
                            "local_value_hash": value_hash("current text"),
                            "hosted_value_hash": value_hash("stale hosted text"),
                        }
                    ],
                }
            ]
        ),
        encoding="utf-8",
    )
    args = _reconcile_args(tmp_path, str(tmp_path / "fork.db"), reconcile_file, apply=True)

    _mod.run_reconciliation(
        args, conn, "https://hosted.example.invalid", "tok", "20260101",
        apply_mode=True, entity_ids=None,
    )

    assert len(store_calls) == 1
    (payload,) = store_calls
    (entity,) = payload["entities"]
    assert entity["target_id"] == "ent_x"
    assert entity["body"] == "current text"
    assert payload["idempotency_key"]


def test_run_reconciliation_apply_aborts_on_ambiguous_5xx_schema_probe(tmp_path, monkeypatch):
    """A 5xx on GET /schemas/<type> must raise HostedProbeAmbiguousError, not
    be silently treated as 'no schema' -- see get_schema_declared_fields's
    own docstring (neotoma#2483). This propagates out of run_reconciliation
    before any /store call is attempted."""
    import neotoma_local_fork_replay as _mod

    def fake_http_request(method, base_url, path, token, body=None, **kwargs):
        if path.startswith("/schemas/"):
            return 500, {"error": "internal"}
        raise AssertionError(f"unexpected call reached /store on an ambiguous schema probe: {path}")

    monkeypatch.setattr(_mod, "http_request", fake_http_request)

    conn = _make_sqlite_with_observations(
        tmp_path, "ent_x", [({"body": "current text"}, "2026-08-05T00:00:00.000Z")]
    )
    reconcile_file = tmp_path / "reconciliation.json"
    reconcile_file.write_text(
        json.dumps(
            [
                {
                    "id": "ent_x",
                    "entity_type": "issue",
                    "fields": [
                        {
                            "name": "body",
                            "classification": "LOCAL_NEWER",
                            "local_value_hash": value_hash("current text"),
                            "hosted_value_hash": value_hash("stale hosted text"),
                        }
                    ],
                }
            ]
        ),
        encoding="utf-8",
    )
    args = _reconcile_args(tmp_path, str(tmp_path / "fork.db"), reconcile_file, apply=True)

    with pytest.raises(_mod.HostedProbeAmbiguousError):
        _mod.run_reconciliation(
            args, conn, "https://hosted.example.invalid", "tok", "20260101",
            apply_mode=True, entity_ids=None,
        )


# --- NEOTOMA_REPLAY_CONFIRM_APPLY vs deprecated MIGRATE_CONFIRM_APPLY ------
# conflict resolution: the new var always wins when both are set and they
# disagree, rather than either being ignored or an ambiguous outcome.


def test_new_confirm_var_wins_when_both_set_and_agree_yes():
    import os as _os

    env = dict(_os.environ)
    env["NEOTOMA_REPLAY_CONFIRM_APPLY"] = "yes"
    env["MIGRATE_CONFIRM_APPLY"] = "yes"
    env["NEOTOMA_BASE_URL"] = "https://example.invalid"
    env["NEOTOMA_BEARER_TOKEN"] = "test-token"
    result = _run_cli(
        ["replay", "--db", "/tmp/does-not-exist-both-agree.db",
         "--cutover", "2026-01-01T00:00:00Z", "--apply"],
        env=env,
    )
    assert "code=E_CONFIRMATION_REQUIRED" not in result.stderr
    # No deprecation warning: the new var being present is what satisfied
    # the gate, so the deprecated-alias code path was never reached.
    assert "MIGRATE_CONFIRM_APPLY is deprecated" not in result.stderr


def test_new_confirm_var_no_wins_over_deprecated_yes_when_they_disagree():
    """The conflict case QA flagged as unpinned: NEOTOMA_REPLAY_CONFIRM_APPLY
    explicitly set to something other than 'yes' (e.g. accidentally 'no')
    while the deprecated MIGRATE_CONFIRM_APPLY=yes is also present. The new
    var is checked first and, once present, is authoritative -- it must
    still refuse, never fall back to honoring the deprecated yes."""
    import os as _os

    env = dict(_os.environ)
    env["NEOTOMA_REPLAY_CONFIRM_APPLY"] = "no"
    env["MIGRATE_CONFIRM_APPLY"] = "yes"
    env["NEOTOMA_BASE_URL"] = "https://example.invalid"
    env["NEOTOMA_BEARER_TOKEN"] = "test-token"
    result = _run_cli(
        ["replay", "--db", "/tmp/does-not-exist-disagree.db",
         "--cutover", "2026-01-01T00:00:00Z", "--apply"],
        env=env,
    )
    assert result.returncode == 1
    assert "code=E_CONFIRMATION_REQUIRED" in result.stderr


def test_resolve_confirm_apply_env_new_var_present_never_reports_deprecated_used(monkeypatch):
    """Unit-level check on resolve_confirm_apply_env's own return contract:
    the second element (used_deprecated) must be False whenever the new var
    is present at all, regardless of the deprecated var, since the new var
    being present is what's authoritative."""
    import neotoma_local_fork_replay as _mod

    monkeypatch.setenv("NEOTOMA_REPLAY_CONFIRM_APPLY", "no")
    monkeypatch.setenv("MIGRATE_CONFIRM_APPLY", "yes")
    confirmed, used_deprecated = _mod.resolve_confirm_apply_env()
    assert confirmed is False
    assert used_deprecated is False


# --- Documented canary filters: --limit / --entity-ids / --exclude-entity-ids


def test_restore_gates_limit_filter_is_documented_only_as_a_smoke_test_cap():
    """--limit is documented on the CLI itself as a smoke-test cap; assert
    the help text still says so (behavioural: --limit is applied to `replay`
    candidates via slicing, exercised directly below for restore-gates via
    --entity-ids/--exclude-entity-ids, which restore-gates actually filters
    on -- --limit is shared-flag plumbing common to all three subcommands
    but restore-gates' own candidate-selection code path uses entity-id
    filters, asserted behaviourally next)."""
    result = _run_cli(["restore-gates", "--help"])
    assert result.returncode == 0
    assert "smoke test" in result.stdout


def test_restore_gates_entity_ids_filter_restricts_to_named_candidates(
    tmp_path, monkeypatch, capsys
):
    import neotoma_local_fork_replay as _mod

    _fake_github_open(monkeypatch)
    store_calls = _fake_hosted_gate_restore_router(monkeypatch)

    db_path = _make_gate_candidate_db(
        tmp_path,
        "multi.db",
        {
            "ent_a": ("owner/repo", 1, {"pm": "signed_off"}),
            "ent_b": ("owner/repo", 2, {"pm": "signed_off"}),
            "ent_c": ("owner/repo", 3, {"pm": "signed_off"}),
        },
    )
    args = _gate_restore_args(
        tmp_path, [db_path], apply=True, **{"--entity-ids": "ent_a,ent_c"}
    )

    _mod.run_gate_restore(args, "https://hosted.example.invalid", "tok", apply_mode=True)

    entity_ids_written = {c["entities"][0]["target_id"] for c in store_calls}
    assert entity_ids_written == {"ent_a", "ent_c"}


def test_restore_gates_exclude_entity_ids_filter_removes_named_candidates(
    tmp_path, monkeypatch, capsys
):
    import neotoma_local_fork_replay as _mod

    _fake_github_open(monkeypatch)
    store_calls = _fake_hosted_gate_restore_router(monkeypatch)

    db_path = _make_gate_candidate_db(
        tmp_path,
        "multi_exclude.db",
        {
            "ent_a": ("owner/repo", 1, {"pm": "signed_off"}),
            "ent_b": ("owner/repo", 2, {"pm": "signed_off"}),
            "ent_c": ("owner/repo", 3, {"pm": "signed_off"}),
        },
    )
    args = _gate_restore_args(
        tmp_path, [db_path], apply=True, **{"--exclude-entity-ids": "ent_b"}
    )

    _mod.run_gate_restore(args, "https://hosted.example.invalid", "tok", apply_mode=True)

    entity_ids_written = {c["entities"][0]["target_id"] for c in store_calls}
    assert entity_ids_written == {"ent_a", "ent_c"}


def test_restore_gates_entity_ids_then_exclude_applied_in_documented_order(
    tmp_path, monkeypatch
):
    """--exclude-entity-ids is documented as 'applied after --entity-ids, if
    both are given' -- assert that ordering directly: an id present in
    both --entity-ids and --exclude-entity-ids ends up excluded (exclude
    wins), and an id in neither list never appears."""
    import neotoma_local_fork_replay as _mod

    _fake_github_open(monkeypatch)
    store_calls = _fake_hosted_gate_restore_router(monkeypatch)

    db_path = _make_gate_candidate_db(
        tmp_path,
        "order.db",
        {
            "ent_a": ("owner/repo", 1, {"pm": "signed_off"}),
            "ent_b": ("owner/repo", 2, {"pm": "signed_off"}),
            "ent_c": ("owner/repo", 3, {"pm": "signed_off"}),
        },
    )
    args = _gate_restore_args(
        tmp_path,
        [db_path],
        apply=True,
        **{"--entity-ids": "ent_a,ent_b", "--exclude-entity-ids": "ent_b"},
    )

    _mod.run_gate_restore(args, "https://hosted.example.invalid", "tok", apply_mode=True)

    entity_ids_written = {c["entities"][0]["target_id"] for c in store_calls}
    assert entity_ids_written == {"ent_a"}  # ent_c never selected, ent_b excluded


# --- JSONL action log: "never logs field contents" -------------------------


def test_action_log_never_logs_field_contents(tmp_path, monkeypatch):
    """The gate-restore action log entries carry only ids/paths/status
    (log_action's own fields kwarg is entity_id/entity_type/entity_class/
    repo/github_number/fields_written [names only]/action/idempotency_key/
    http_status) -- never the actual field VALUES written. Runs a mocked
    apply with a sentinel value planted in the gate_status payload and
    asserts that sentinel string appears nowhere in the JSONL log, even
    though it was sent in the /store call itself."""
    import neotoma_local_fork_replay as _mod

    sentinel = "SENTINEL_DO_NOT_LOG_ME_9f3c2a"
    _fake_github_open(monkeypatch)
    store_calls = _fake_hosted_gate_restore_router(monkeypatch)

    # gate_status values aren't free text, but owner_history notes are --
    # plant the sentinel there via a local DB row carrying it, so it flows
    # through plan_gate_restore_for_entity into the /store payload.
    import sqlite3 as _sqlite3

    db_path = tmp_path / "sentinel.db"
    conn = _sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE observations (id TEXT, entity_id TEXT, entity_type TEXT, fields TEXT, created_at TEXT)"
    )
    conn.execute(
        "INSERT INTO observations VALUES (?,?,?,?,?)",
        (
            "o1",
            "ent_a",
            "issue",
            json.dumps(
                {
                    LEGACY_GATE_STATUS_FIELD: {"pm": "signed_off"},
                    "repo": "owner/repo",
                    "github_number": 42,
                    "owner_history": [
                        {
                            "agent": "pavo",
                            "action": "signed_off",
                            "gate": "pm",
                            "at": "2026-09-23T10:00:00Z",
                            "note": sentinel,
                        }
                    ],
                }
            ),
            "2026-09-23T09:00:00Z",
        ),
    )
    conn.commit()
    conn.close()

    args = _gate_restore_args(tmp_path, [str(db_path)], apply=True)
    log_path = tmp_path / "action.jsonl"

    _mod.run_gate_restore(args, "https://hosted.example.invalid", "tok", apply_mode=True)

    # The sentinel DID get sent to hosted (proves the test actually
    # exercises a payload carrying it, so a pass here is not vacuous).
    assert any(
        sentinel in json.dumps(c) for c in store_calls
    ), "sentinel never reached the /store payload -- test setup is broken"

    # ...but it must never appear in the action log.
    log_text = log_path.read_text(encoding="utf-8")
    assert sentinel not in log_text
    # Sanity: the log is non-empty and JSON-parseable per line, so this
    # isn't vacuously passing on an empty/malformed log either.
    lines = [ln for ln in log_text.splitlines() if ln.strip()]
    assert lines
    for line in lines:
        json.loads(line)
