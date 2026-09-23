#!/usr/bin/env python3
"""
Replay observations, relationship_observations, and sources written to a
local SQLite fork of Neotoma after a hosted-migration cutover, into the
canonical hosted Neotoma instance's public HTTP API.

Background: a local stdio MCP process kept writing to a retired local
database after the operator migrated to a hosted Neotoma instance, because
nothing stopped the old process from starting. Once the local writer was
killed, the rows written to the local DB after the cutover timestamp needed
to be replayed forward into hosted so they are not lost.

DRY-RUN BY DEFAULT. Nothing is written unless --apply is passed explicitly
(and, on top of that, MIGRATE_CONFIRM_APPLY=yes must be set in the
environment, as a second deliberate guard against accidental invocation).

Method
------
Goes through the public Neotoma HTTP API (POST /store, POST
/create_relationship), NOT any in-process storage function.

Request shapes below are taken directly from the neotoma repo source (read
at the time this script was hardened, commit a80340c66):
  - src/shared/action_schemas.ts:723-763 StoreRequestSchema -- entities is
    `z.array(z.record(z.unknown()))` (a flat, arbitrary-key record per
    entity); idempotency_key (action_schemas.ts:733), source_priority
    (:729), and observation_source (:730) are TOP-LEVEL request fields, not
    per-entity. There is no `observed_at` field anywhere on this schema.
  - src/actions.ts:7646-7708 (the /store entity-resolution loop) reads
    exactly three reserved keys off each entity record -- `entity_type`
    (required), `target_id` (optional: forces "extend this exact entity_id"
    / bypasses canonical-name derivation), and `intent` (optional:
    "create_new" forces strict mode) -- and treats every OTHER key on the
    record as entity field data (actions.ts:7690-7698). There is no
    `entity_id` hint field: passing one puts a literal "entity_id" key into
    the entity's fields and the server returns an UNKNOWN_FIELD warning.
  - Entity identity/id resolution (src/services/entity_resolution.ts:
    954-1038): the server resolves the entity id either from a schema's
    declared `canonical_name_fields` (deterministic hash) or, when the
    caller passes `target_id`, by extending that exact id
    (identityBasis: "target_id", entity_resolution.ts:1033-1038). This
    script always passes `target_id` = the LOCAL entity_id, so a replayed
    entity lands on the SAME id it had locally rather than depending on
    canonical-name derivation agreeing (it usually does, since ids are
    themselves canonical-name hashes, but target_id makes it explicit and
    authoritative).
  - observed_at is server-stamped at insert time
    (actions.ts:7158/8031 `observed_at: new Date().toISOString()`); the
    public /store schema has no client override for it. This script does
    NOT attempt to send it -- there is no schema slot for it, and flattening
    it onto the entity record would corrupt the entity's field data (this
    is exactly the bug an earlier version of this script had: passing
    observed_at as a "field" caused the server to store a literal
    "observed_at" key in entity fields, flagged as UNKNOWN_FIELD).
  - src/shared/action_schemas.ts:120-127 CreateRelationshipRequestSchema --
    relationship_type, source_entity_id, target_entity_id, source_id
    (optional; a source/provenance pointer, NOT an idempotency key),
    metadata, user_id. There is no idempotency_key field on this schema at
    all. Re-running create_relationship for the same (relationship_type,
    source_entity_id, target_entity_id) triple is idempotent by construction:
    src/actions.ts:9763-9803 keys the stored row on
    `relationship_key = f"{type}:{source}:{target}"`
    (src/actions.ts:5790-5791), so a duplicate call reuses the same row
    rather than creating a second edge.

Idempotency key scheme
-----------------------
  observations: "migrate-<cutover-date>-obs-<local_observation_id>"
                (one /store call per local observation row; the
                idempotency_key is the top-level request field)
  relationships: no idempotency_key -- idempotent via relationship_key
                 (relationship_type:source_entity_id:target_entity_id)
  sources:       "migrate-<cutover-date>-src-<local_source_id>" (reserved;
                 source blob replay is not implemented -- see below)

Re-running with --apply after a partial failure is safe: a duplicate
observation idempotency_key is a no-op / returns the existing row, and a
duplicate relationship triple resolves to the existing relationship_key.

--only-missing mode (default whenever --apply is used)
--------------------------------------------------------
Probes GET /entities/<id> immediately before writing each entity's
observation and skips the write unless the probe returns 404. This keeps
the replay from touching an entity that already exists on hosted and may
have diverged there independently (class-b entities per the migration
plan) -- only entities that are genuinely missing on hosted (class-a) get
written. Use --no-only-missing to disable the probe (not recommended for
--apply runs against a shared instance).

Usage
-----
  python3 neotoma_local_fork_replay.py --db <path-to-local-db> --dry-run
  python3 neotoma_local_fork_replay.py --db <path-to-local-db> --apply --limit 20
  python3 neotoma_local_fork_replay.py --db <path-to-local-db> --apply --limit 20 --entity-ids ent_a,ent_b

Environment
-----------
  Requires NEOTOMA_BEARER_TOKEN_PROD or NEOTOMA_BEARER_TOKEN to already be
  exported in the environment (this script does not source any dotenv
  itself). Requires NEOTOMA_BASE_URL to be exported (no hardcoded default,
  since this repo is public and hosted instance URLs are operator-specific
  config). Never prints, logs, or echoes the token or any field value.

JSONL action log
-----------------
  Every planned/attempted action (dry-run or apply) is appended to the file
  given by --log (default: neotoma_local_fork_replay.jsonl in the current
  directory) as one JSON object per line:
    {"ts", "kind", "local_id", "entity_id", "entity_type", "class",
     "action", "idempotency_key", "http_status"}
  Field CONTENTS (fields/metadata payloads) are never logged -- only ids,
  types, classification, action taken, and HTTP status.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

USER_AGENT = "ateles-migrate/1.0"


def get_base_url() -> str:
    base = os.environ.get("NEOTOMA_BASE_URL")
    if not base:
        print(
            "ERROR: NEOTOMA_BASE_URL must be set in env (no hardcoded default "
            "-- this repo is public).",
            file=sys.stderr,
        )
        sys.exit(1)
    return base.rstrip("/")


def get_token() -> str:
    tok = os.environ.get("NEOTOMA_BEARER_TOKEN_PROD") or os.environ.get(
        "NEOTOMA_BEARER_TOKEN"
    )
    if not tok:
        print(
            "ERROR: NEOTOMA_BEARER_TOKEN_PROD or NEOTOMA_BEARER_TOKEN must be set in env.",
            file=sys.stderr,
        )
        sys.exit(1)
    return tok


def get_schema_declared_fields(entity_type: str, base_url: str, token: str, cache: dict):
    """Fetch and cache a hosted entity type's declared field names.

    GET /schemas/<entity_type> is a read -- never a write -- and is fetched
    at most once per entity_type per run (results cached in `cache`). A 200
    means the type has a hosted schema; `schema_definition.fields` gives the
    declared field names. A 404 means hosted has no schema at all for this
    type (class-a data of that type can still be replayed -- there is just
    nothing to check field names against, so every field is "unknown" in
    the sense that nothing declares it, but that is expected and not an
    UNKNOWN_FIELD store_warning risk distinct from any other field).
    """
    if entity_type in cache:
        return cache[entity_type]
    status, body = http_request(
        "GET", base_url, f"/schemas/{entity_type}", token, retries=3, retry_backoff_seconds=2.0
    )
    if status == 200 and isinstance(body, dict):
        fields = body.get("schema_definition", {}).get("fields", {})
        declared = set(fields.keys()) if isinstance(fields, dict) else set()
        cache[entity_type] = {"has_schema": True, "declared_fields": declared}
    else:
        cache[entity_type] = {"has_schema": False, "declared_fields": set()}
    return cache[entity_type]


# Keys that are never real entity field data regardless of entity_type --
# they are either reserved by the /store request schema itself (and so
# belong at the top level of the request, never inside an entity record --
# see build_entity_record's docstring) or are local-fork bookkeeping that
# has no meaning on hosted at all. Stripped unconditionally.
ALWAYS_STRIP_FIELD_KEYS = {"_migration_run_id"}

# Keys that are reserved on MOST entity types (a stray collision with a
# /store request's reserved per-entity keys, or with a local-fork-only
# bookkeeping convention) but are documented as genuine declared fields on
# a small set of types. Whether to strip these is schema-driven, not
# hardcoded: a key is kept if-and-only-if the hosted schema for that
# specific entity_type declares it, so a schema change on hosted is picked
# up automatically rather than requiring this script to be edited.
CONDITIONALLY_RESERVED_FIELD_KEYS = {"entity_id", "idempotency_key", "canonical_name"}


def strip_reserved_fields(entity_type: str, fields: dict, schema_info: dict) -> tuple[dict, list[str]]:
    """Remove reserved/bogus keys from an entity's field payload.

    Returns (cleaned_fields, stripped_key_names). Two categories are
    stripped, both documented on the module and on the constants above:

    1. ALWAYS_STRIP_FIELD_KEYS -- `_migration_run_id` is local-fork
       migration-tracking metadata written by the background re-write
       process on the LOCAL fork; it was never a real field on any hosted
       schema and has no meaning there. Always dropped.
    2. CONDITIONALLY_RESERVED_FIELD_KEYS -- `entity_id`, `idempotency_key`,
       and `canonical_name` collide with reserved concepts elsewhere in the
       /store request or with entity-identity machinery, but a handful of
       entity types (agent_definition, workflow_definition,
       operator_profile, agent_strategy, per the migration brief) declare
       `entity_id` as a genuine field on their hosted schema. This function
       keeps a conditionally-reserved key exactly when the entity_type's
       hosted schema (fetched via get_schema_declared_fields, never
       hardcoded) declares it, and strips it otherwise. When the schema
       fetch found no hosted schema for the type at all (has_schema=False),
       these keys are stripped defensively -- with no schema to declare
       them, there is nothing to keep them for, and the collision risk
       (`entity_id` in particular is read by the /store entity-resolution
       loop as a NAME, not a hint, per actions.ts:7690-7698 in this
       module's docstring) outweighs preserving a field with no known home.
    """
    declared = schema_info.get("declared_fields", set())
    has_schema = schema_info.get("has_schema", False)
    cleaned = {}
    stripped = []
    for k, v in fields.items():
        if k in ALWAYS_STRIP_FIELD_KEYS:
            stripped.append(k)
            continue
        if k in CONDITIONALLY_RESERVED_FIELD_KEYS:
            if has_schema and k in declared:
                cleaned[k] = v
            else:
                stripped.append(k)
            continue
        cleaned[k] = v
    return cleaned, stripped


def is_schema_lag_background_rewrite(fields_json: str | None) -> bool:
    """True if an observation's fields carry a schema_lag_bg_* migration_run_id.

    These ~960 post-cutover observations are the LOCAL fork's own automatic
    background re-write process re-touching rows it already had -- not new
    operator or agent data that failed to reach hosted. Replaying them would
    re-inject stale local rewrites over whatever hosted independently holds
    for the same entities, which is exactly the class-b divergence this
    script's --only-missing probe exists to avoid. Excluded unconditionally,
    before class-a/class-b classification runs.
    """
    if not fields_json:
        return False
    try:
        fields = json.loads(fields_json)
    except (TypeError, ValueError):
        return False
    if not isinstance(fields, dict):
        return False
    mrid = fields.get("_migration_run_id")
    return isinstance(mrid, str) and mrid.startswith("schema_lag_bg_")


def http_request(
    method: str,
    base_url: str,
    path: str,
    token: str,
    body=None,
    retries: int = 0,
    retry_backoff_seconds: float = 1.0,
):
    """Issue one HTTP request, with optional retry for TRANSIENT failures only.

    `retries` is 0 by default (no retry) -- callers that want resilience
    against transient network hiccups (the GET-heavy existence-probe path in
    particular, which "pools the GETs politely" over potentially thousands
    of entities) opt in explicitly. Retries apply ONLY to a connection-level
    failure (timeout, connection reset, DNS hiccup) -- an HTTPError with a
    real status code (404, 500, ...) is returned immediately, never retried,
    since retrying a request the server already answered risks a duplicate
    side effect on a non-idempotent call. GETs are idempotent by definition
    so this is safe for the probe path; POST /store and POST
    /create_relationship are also idempotent by construction (idempotency_key
    / relationship_key, per the module docstring) but this script does not
    pass retries> 0 for those calls -- a write failure surfaces immediately
    and stops the run rather than being silently retried, matching the
    existing "stop on any write error" behavior.
    """
    url = f"{base_url}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    attempt = 0
    while True:
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("User-Agent", USER_AGENT)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as e:
            try:
                payload = json.loads(e.read().decode("utf-8"))
            except Exception:
                payload = {"error": str(e)}
            return e.code, payload
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            attempt += 1
            if attempt > retries:
                raise
            print(
                f"  (transient network error on {method} {path}: {e!r} -- "
                f"retry {attempt}/{retries} in {retry_backoff_seconds:.1f}s)",
                file=sys.stderr,
            )
            time.sleep(retry_backoff_seconds)


def entity_exists(entity_id: str, base_url: str, token: str) -> bool:
    status, _ = http_request(
        "GET", base_url, f"/entities/{entity_id}", token, retries=3, retry_backoff_seconds=2.0
    )
    return status == 200


def load_candidates(conn: sqlite3.Connection, cutover_ts: str, entity_ids=None):
    cur = conn.cursor()
    cur.execute(
        "SELECT id, entity_id, entity_type, schema_version, source_id, "
        "interpretation_id, observed_at, specificity_score, source_priority, "
        "fields, created_at, user_id, idempotency_key, observation_source "
        "FROM observations WHERE created_at > ? ORDER BY created_at",
        (cutover_ts,),
    )
    all_obs = cur.fetchall()
    excluded_schema_lag = [row for row in all_obs if is_schema_lag_background_rewrite(row[9])]
    obs = [row for row in all_obs if not is_schema_lag_background_rewrite(row[9])]
    if entity_ids:
        obs = [row for row in obs if row[1] in entity_ids]

    cur.execute(
        "SELECT id, relationship_key, relationship_type, source_entity_id, "
        "target_entity_id, source_id, interpretation_id, observed_at, "
        "specificity_score, source_priority, metadata, created_at, user_id "
        "FROM relationship_observations WHERE created_at > ? ORDER BY created_at",
        (cutover_ts,),
    )
    rels = cur.fetchall()
    if entity_ids:
        rels = [row for row in rels if row[3] in entity_ids and row[4] in entity_ids]

    cur.execute(
        "SELECT id, user_id, content_hash, mime_type, storage_url, file_size, "
        "original_filename, provenance, created_at, idempotency_key, "
        "source_type, storage_mode, reference_path "
        "FROM sources WHERE created_at > ? ORDER BY created_at",
        (cutover_ts,),
    )
    srcs = cur.fetchall()

    return obs, rels, srcs, excluded_schema_lag


def build_entity_record(entity_type: str, target_id: str, fields: dict) -> dict:
    """Build one element of a /store request's `entities` array.

    Per neotoma src/actions.ts:7646-7708, the server reads exactly three
    reserved keys off this record -- entity_type, target_id, intent -- and
    treats every other key as entity field data. `target_id` forces the
    write onto that exact hosted entity_id (entity_resolution.ts:998-1038,
    identityBasis "target_id") rather than depending on canonical-name
    derivation. No other metadata (entity_id, observed_at,
    observation_source, source_priority, idempotency_key) belongs on this
    record -- each of those lives at the top level of the /store request
    body instead (StoreRequestSchema, action_schemas.ts:723-763), and the
    schema has no per-entity slot for any of them. A key with no home in
    the schema is dropped rather than flattened onto the entity, since a
    flattened stray key becomes a real (wrong) field on the entity.
    """
    record = {"entity_type": entity_type, "target_id": target_id}
    record.update(fields)
    return record


def build_observation_payload(row, cutover_date: str, schema_info: dict | None = None):
    (
        local_id,
        entity_id,
        entity_type,
        schema_version,
        source_id,
        interpretation_id,
        observed_at,
        specificity_score,
        source_priority,
        fields_json,
        created_at,
        user_id,
        existing_idem_key,
        observation_source,
    ) = row
    fields = json.loads(fields_json) if fields_json else {}
    stripped_keys: list[str] = []
    if schema_info is not None:
        fields, stripped_keys = strip_reserved_fields(entity_type, fields, schema_info)
    idem_key = f"migrate-{cutover_date}-obs-{local_id}"
    # observed_at is intentionally NOT included anywhere in this payload:
    # /store has no client-supplied observed_at override (it is
    # server-stamped at insert, action_schemas.ts has no such field), and
    # earlier versions of this script corrupted entity field data by
    # flattening observed_at onto the entity record instead of dropping it.
    return (
        {
            "entities": [build_entity_record(entity_type, entity_id, fields)],
            "idempotency_key": idem_key,
            "observation_source": observation_source or "import",
            "source_priority": source_priority,
        },
        idem_key,
        stripped_keys,
    )


def predict_unknown_fields(entity_type: str, fields: dict, schema_info: dict) -> list[str]:
    """Pre-flight prediction of which of an entity's (already-stripped)
    field keys would come back as UNKNOWN_FIELD store_warnings.

    Runs against the same fetched hosted schema used by strip_reserved_fields
    -- one GET per entity_type, cached, never a write. When hosted has no
    schema at all for entity_type (has_schema=False), every field is
    "unknown" in the sense that nothing declares it; that case is reported
    separately (as a no-schema type) rather than folded into this list,
    since it is a different condition than "schema exists but omits this
    field".
    """
    if not schema_info.get("has_schema"):
        return []
    declared = schema_info.get("declared_fields", set())
    return sorted(k for k in fields if k not in declared)


# --- Schema auto-extension (operator ruling 2026-09-23) --------------------
#
# The operator has ruled that agents extend Neotoma schemas themselves when
# replayed data doesn't fit -- this script no longer stops or asks on an
# undeclared ADDITIVE field; it registers it. Two REST routes, read from the
# neotoma repo source at ~/repos/neotoma-wt-migrate-audit (fetched fresh for
# this change):
#
#   POST /update_schema_incremental -- src/actions.ts:11098-11241, request
#   shape UpdateSchemaIncrementalRequestSchema (src/shared/action_schemas.ts:
#   947-985). Extends an EXISTING schema. Body: {entity_type, fields_to_add:
#   [{field_name, field_type, required: false}], activate: true}. This
#   script only ever sends fields_to_add -- never fields_to_remove and never
#   canonical_name_fields, so it can only ADD optional fields, never remove,
#   rename, retype, or re-key an entity type's identity. field_type is one
#   of the 6 accepted by the schema: "string" | "number" | "date" |
#   "boolean" | "array" | "object" (action_schemas.ts:954).
#
#   POST /register_schema -- src/actions.ts:11394-11456+, request shape
#   RegisterSchemaRequestSchema (action_schemas.ts:987-996). Used ONLY for
#   the 3 entity types with no hosted schema at all (symptom_report,
#   github_issue_ref, github_comment_intent per the migration brief), since
#   update_schema_incremental requires an existing SchemaDefinition to
#   extend (actions.ts:11172, ERR_NO_SCHEMA_FOR_ENTITY_TYPE). Body:
#   {entity_type, schema_definition: {fields: {<name>: {type}},
#   identity_opt_out: "heuristic_canonical_name"}, reducer_config:
#   {merge_policies: {}}, activate: true}. identity_opt_out is set
#   explicitly (rather than omitting canonical_name_fields and letting the
#   server default it, actions.ts:11424-11430) so this script never
#   silently mints a canonical_name_fields rule for a type it knows nothing
#   about -- the operator ruling covers additive fields, not identity
#   rules, and canonical_name_fields is never touched by this script for
#   any type, new or existing.

FIELD_TYPE_CHOICES = {"string", "number", "date", "boolean", "array", "object"}


def infer_field_type(value) -> str:
    """Infer a Neotoma schema field_type from a Python JSON value.

    Falls back to "string" for anything not obviously one of the 6 accepted
    field_type values (action_schemas.ts:954) -- None/null, and any type
    this function doesn't recognize -- since "string" is the least
    constraining declared type and the goal here is registering a field
    that unblocks a write, not modeling its full type precisely. A
    dedicated corrector can retype AFTER a field is un-registered and
    proves out its actual on-the-wire shape; retyping is explicitly never
    something this script does to an ALREADY-declared field.
    """
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, str):
        return "string"
    return "string"


def plan_schema_extensions(entity_type: str, sample_values_by_field: dict, schema_info: dict):
    """Compute the fields_to_add plan for one entity_type from observed values.

    `sample_values_by_field` maps field_name -> a representative value seen
    on a replayed observation of this type (used only for infer_field_type;
    never the full set of values, and never logged). Returns a list of
    {field_name, field_type, required: False} dicts, sorted by field_name for
    deterministic output -- one entry per field this run's class-a data uses
    that the CURRENTLY fetched hosted schema does not declare. `required` is
    always False: an auto-extension must never turn into a constraint that
    could reject an unrelated future write that happens to omit the field.
    """
    declared = schema_info.get("declared_fields", set()) if schema_info.get("has_schema") else set()
    return [
        {"field_name": name, "field_type": infer_field_type(value), "required": False}
        for name, value in sorted(sample_values_by_field.items())
        if name not in declared
    ]


def build_update_schema_incremental_payload(entity_type: str, fields_to_add: list[dict]) -> dict:
    """Build a POST /update_schema_incremental body that can ONLY add fields.

    Deliberately omits fields_to_remove and canonical_name_fields -- both
    accepted by UpdateSchemaIncrementalRequestSchema (action_schemas.ts:
    947-985) but never sent by this script, so a call built by this
    function cannot remove, rename, retype an existing field, or re-key an
    entity type's identity, no matter what caller constructs the
    fields_to_add list.
    """
    return {
        "entity_type": entity_type,
        "fields_to_add": fields_to_add,
        "activate": True,
    }


def build_register_schema_payload(entity_type: str, fields_to_add: list[dict]) -> dict:
    """Build a POST /register_schema body for a type with NO hosted schema.

    Used only when get_schema_declared_fields reports has_schema=False.
    identity_opt_out is set explicitly (action_schemas.ts:987-996 /
    actions.ts:11424-11430) rather than leaving canonical_name_fields
    undeclared -- this script never infers or asserts an identity rule for
    a type it is seeing for the first time; that is a design decision for
    the operator or a follow-up schema review, not something a replay
    script should guess at from one run's sample of values.
    """
    fields = {f["field_name"]: {"type": f["field_type"]} for f in fields_to_add}
    return {
        "entity_type": entity_type,
        "schema_definition": {
            "fields": fields,
            "identity_opt_out": "heuristic_canonical_name",
        },
        "reducer_config": {"merge_policies": {}},
        "activate": True,
    }


def extend_hosted_schema(
    entity_type: str,
    fields_to_add: list[dict],
    schema_info: dict,
    base_url: str,
    token: str,
) -> tuple[bool, dict]:
    """Apply fields_to_add to hosted -- register_schema for a no-schema type,
    update_schema_incremental for an existing one. Returns (ok, response).

    Callers MUST re-fetch the schema and verify the new fields are present
    (see verify_schema_extension_applied) before relying on them for a
    /store call -- this function reports what hosted's response said, not
    what is actually active, since register/update responses have been
    seen to report success while a subsequent read lagged (documented CLAUDE.md
    verification-discipline rule: "a write that reports success has not
    necessarily happened").
    """
    if schema_info.get("has_schema"):
        payload = build_update_schema_incremental_payload(entity_type, fields_to_add)
        status, resp = http_request("POST", base_url, "/update_schema_incremental", token, payload)
    else:
        payload = build_register_schema_payload(entity_type, fields_to_add)
        status, resp = http_request("POST", base_url, "/register_schema", token, payload)
    ok = status in (200, 201) and isinstance(resp, dict) and not resp.get("error")
    return ok, resp


def verify_schema_extension_applied(
    entity_type: str, expected_field_names: list[str], base_url: str, token: str
) -> tuple[bool, list[str]]:
    """Re-fetch the schema and assert every expected field is now declared.

    Returns (all_present, missing_field_names). Called with an EMPTY cache
    dict so it always re-fetches rather than reading a pre-extension cached
    result -- the whole point is confirming what changed.
    """
    fresh_cache: dict = {}
    info = get_schema_declared_fields(entity_type, base_url, token, fresh_cache)
    declared = info.get("declared_fields", set())
    missing = [f for f in expected_field_names if f not in declared]
    return (len(missing) == 0), missing


# --- Class-b field reconciliation (--reconcile-file) ------------------------
#
# Separate from the class-a replay above: this mode writes SPECIFIC FIELDS on
# entities that already exist on hosted (class-b), using a pre-computed
# reconciliation.json produced by a prior audit pass (per entity:
# {id, entity_type, fields: [{name, classification, recommendation,
# local_value_hash, local_value_len, hosted_value_hash, hosted_value_len}]}).
# Only LOCAL_ONLY and LOCAL_NEWER fields are ever written; SAME and
# HOSTED_NEWER are always skipped. The reconciliation JSON holds only
# HASHES of values (never the values themselves, so it stays diffable/small
# and PII-light) -- the actual value to write is read fresh from the local
# fork's latest post-cutover observation for that (entity_id, field_name) at
# apply time, matching the fold-forward convention already established by
# the audit's own 03_reconcile.py (scratchpad/reconcile/03_reconcile.py:
# local_post_cutover_state -- last observation wins per field, schema_lag_bg_*
# rows excluded, leading-underscore keys excluded).


def load_reconciliation_file(path: str) -> list[dict]:
    """Load and lightly validate the reconciliation.json list-of-entities file."""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError(f"{path}: expected a JSON list of entity records, got {type(data).__name__}")
    return data


WRITABLE_CLASSIFICATIONS = {"LOCAL_ONLY", "LOCAL_NEWER"}


def filter_writable_fields(entity_record: dict) -> list[dict]:
    """Return only the field entries this reconciliation run should write.

    A field is writable exactly when its classification is LOCAL_ONLY or
    LOCAL_NEWER (WRITABLE_CLASSIFICATIONS) -- SAME needs no write, and
    HOSTED_NEWER means hosted's value is the one to keep, so writing local
    over it would be the data-loss direction this whole reconciliation pass
    exists to avoid. Any other/unrecognized classification string is also
    excluded (fail closed on an unrecognized value rather than writing it).
    """
    fields = entity_record.get("fields") or []
    return [f for f in fields if f.get("classification") in WRITABLE_CLASSIFICATIONS]


def local_post_cutover_field_state(conn: sqlite3.Connection, entity_id: str, cutover_ts: str) -> dict:
    """field_name -> latest post-cutover local value for one entity.

    Mirrors scratchpad/reconcile/03_reconcile.py:local_post_cutover_state:
    folds every post-cutover, non-schema_lag_bg_*, non-underscore-prefixed
    field across that entity's observations in created_at order, so the
    last write per field wins -- the same "latest local value" the
    reconciliation.json's own audit pass used when computing
    local_value_hash, so a value read here should hash-match what's in the
    file (checked by the caller before writing; see reconcile_class_b_entity).
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT fields, created_at FROM observations WHERE entity_id = ? AND created_at > ? "
        "ORDER BY created_at ASC",
        (entity_id, cutover_ts),
    )
    state: dict[str, object] = {}
    for fields_json, _created_at in cur.fetchall():
        if is_schema_lag_background_rewrite(fields_json):
            continue
        if not fields_json:
            continue
        try:
            fields = json.loads(fields_json)
        except (TypeError, ValueError):
            continue
        if not isinstance(fields, dict):
            continue
        for k, v in fields.items():
            if k.startswith("_"):
                continue
            state[k] = v
    return state


def value_hash(value) -> str:
    """EXACT hash scheme as scratchpad/reconcile/03_reconcile.py's val_hash_len,
    reproduced field-for-field so a value read here hashes identically to how
    the reconciliation.json's own local_value_hash/hosted_value_hash were
    computed: None -> the literal string "null"; a str -> hashed AS-IS, not
    JSON-quoted; anything else -> json.dumps(sort_keys=True). Using
    json.dumps for strings too (an earlier version of this function did)
    hashes a DIFFERENT byte string than the audit did for every string
    field, which silently marked every string-valued field as "drifted"
    -- used ONLY to compare against a reconciliation record's hash for
    drift detection, never to identify or log the value itself.
    """
    if value is None:
        s = "null"
    elif isinstance(value, str):
        s = value
    else:
        s = json.dumps(value, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(s.encode("utf-8", errors="replace")).hexdigest()[:12]


def build_reconcile_idempotency_key(cutover_date: str, entity_id: str, field_names: list[str]) -> str:
    """Deterministic idempotency_key for one entity's reconciliation store.

    migrate-<cutover-date>-recon-<entity_id>-<hash of the field set>. The
    hash covers the SORTED field-name set (not values) so the SAME set of
    fields for the SAME entity always produces the SAME key -- re-running
    the reconciliation after a partial failure re-uses the same key for an
    unchanged field set (idempotent no-op on hosted) rather than minting
    a fresh row, while a DIFFERENT field set (e.g. a field skipped for
    drift last time now succeeds) gets its own key, since it is a
    different write.
    """
    field_set_repr = ",".join(sorted(field_names))
    field_set_hash = hashlib.sha256(field_set_repr.encode("utf-8")).hexdigest()[:12]
    return f"migrate-{cutover_date}-recon-{entity_id}-{field_set_hash}"


def plan_class_b_reconciliation_for_entity(
    entity_record: dict, conn: sqlite3.Connection, cutover_ts: str, cutover_date: str
):
    """Compute one entity's reconciliation write plan (no I/O to hosted).

    Returns (entity_id, entity_type, fields_to_write: {name: value},
    drifted_fields: [name], idem_key) where fields_to_write already excludes
    any field whose LOCAL value's hash doesn't match the reconciliation
    file's own local_value_hash (a stale reconciliation.json entry -- the
    local fork is frozen/read-only so this should not happen, but is
    checked rather than assumed) and callers separately re-check
    hosted_value_hash against a fresh hosted read at apply time (see
    reconcile_class_b_entity) for the drift case the brief specifies --
    hosted changing SINCE the reconciliation was computed.
    """
    entity_id = entity_record["id"]
    entity_type = entity_record.get("entity_type", "unknown")
    writable = filter_writable_fields(entity_record)
    if not writable:
        return entity_id, entity_type, {}, [], None

    local_state = local_post_cutover_field_state(conn, entity_id, cutover_ts)
    fields_to_write: dict = {}
    drifted_fields: list[str] = []
    for f in writable:
        name = f["name"]
        if name not in local_state:
            # Reconciliation file says LOCAL_ONLY/LOCAL_NEWER but this
            # entity's fold-forward state (excluding schema_lag_bg_* and
            # underscore keys, matching how the audit itself computed
            # local_value_hash) has nothing for this field now -- skip
            # rather than write a value we cannot re-derive locally.
            drifted_fields.append(name)
            continue
        local_val = local_state[name]
        if value_hash(local_val) != f.get("local_value_hash"):
            # The local value this run reads no longer matches what the
            # reconciliation audit hashed -- report as drifted rather than
            # writing a value that disagrees with the plan it was approved
            # against.
            drifted_fields.append(name)
            continue
        fields_to_write[name] = local_val

    idem_key = None
    if fields_to_write:
        idem_key = build_reconcile_idempotency_key(cutover_date, entity_id, sorted(fields_to_write.keys()))
    return entity_id, entity_type, fields_to_write, drifted_fields, idem_key


def recheck_hosted_drift(
    entity_id: str, field_names: list[str], expected_hashes: dict, base_url: str, token: str
) -> list[str]:
    """Re-fetch hosted's CURRENT value for each field and compare hashes.

    Returns the subset of field_names whose hosted value's hash no longer
    matches `expected_hashes[name]` (the reconciliation file's
    hosted_value_hash) -- i.e. hosted changed since the reconciliation was
    computed. Those fields must be skipped and logged as drifted rather
    than overwritten, per the brief. Uses GET /entities/<id> (a read) --
    never a write -- and only the fields this entity's plan actually
    touches are compared.
    """
    status, body = http_request("GET", base_url, f"/entities/{entity_id}", token, retries=3, retry_backoff_seconds=2.0)
    if status != 200 or not isinstance(body, dict):
        # Can't verify -- treat every field as drifted (fail closed: skip
        # rather than write over a state we could not just confirm).
        return list(field_names)
    hosted_fields = body.get("fields") or body.get("entity", {}).get("fields") or {}
    drifted = []
    for name in field_names:
        current_hash = value_hash(hosted_fields.get(name)) if name in hosted_fields else None
        if current_hash != expected_hashes.get(name):
            drifted.append(name)
    return drifted


def build_relationship_payload(row, cutover_date: str):
    (
        local_id,
        rel_key,
        rel_type,
        source_entity_id,
        target_entity_id,
        source_id,
        interpretation_id,
        observed_at,
        specificity_score,
        source_priority,
        metadata_json,
        created_at,
        user_id,
    ) = row
    metadata = json.loads(metadata_json) if metadata_json else {}
    # CreateRelationshipRequestSchema (action_schemas.ts:120-127) has no
    # idempotency_key field. Re-running create_relationship for the same
    # (relationship_type, source_entity_id, target_entity_id) triple is
    # idempotent by construction: the stored row is keyed on
    # relationship_key = f"{type}:{source}:{target}" (actions.ts:5790-5791),
    # so a duplicate call resolves to the existing row rather than creating
    # a second edge. observed_at has no home on this schema either and is
    # dropped -- relationships get a server-stamped created_at, a known,
    # accepted provenance gap (see module docstring).
    idem_key = (
        f"migrate-{cutover_date}-rel-{local_id}"  # used only for our own action log
    )
    return {
        "relationship_type": rel_type,
        "source_entity_id": source_entity_id,
        "target_entity_id": target_entity_id,
        "metadata": metadata,
    }, idem_key


def log_action(log_fh, **fields) -> None:
    fields["ts"] = datetime.now(timezone.utc).isoformat()
    log_fh.write(json.dumps(fields, sort_keys=True) + "\n")
    log_fh.flush()


def build_store_payload_for_reconcile(entity_id: str, entity_type: str, fields_to_write: dict, idem_key: str) -> dict:
    """Build one /store request body for a class-b reconciliation write.

    Same shape discipline as build_observation_payload: target_id forces
    the write onto the exact existing hosted entity_id (never derives a
    canonical name), idempotency_key is a top-level request field, and no
    other reserved/stray keys are sent. observation_source is "import" to
    match the rest of this script's replayed writes.
    """
    return {
        "entities": [build_entity_record(entity_type, entity_id, fields_to_write)],
        "idempotency_key": idem_key,
        "observation_source": "import",
    }


def run_reconciliation(args, conn, base_url, token, cutover_date, apply_mode, entity_ids) -> None:
    """--reconcile-file mode: write class-b LOCAL_ONLY/LOCAL_NEWER fields to hosted.

    Entirely separate from the class-a observation/relationship replay --
    this never touches `observations`/`relationship_observations` rows
    directly; it reads the pre-computed reconciliation.json plan, re-derives
    each writable field's CURRENT local value, and (in --apply) writes one
    /store call per entity carrying only the fields whose local value still
    matches the plan AND whose hosted value has not drifted since.
    """
    entities = load_reconciliation_file(args.reconcile_file)
    if entity_ids:
        entities = [e for e in entities if e.get("id") in entity_ids]
    if args.limit:
        entities = entities[: args.limit]

    print(f"Loaded reconciliation file: {args.reconcile_file}")
    print(f"  entities in file (after --limit/--entity-ids): {len(entities)}")
    print(
        f"  mode: {'APPLY (writing to hosted)' if apply_mode else 'DRY-RUN (no writes)'}"
    )
    print()

    planned = []
    entities_with_nothing_to_apply = 0
    total_fields_planned = 0
    total_fields_drifted_local = 0
    for entity_record in entities:
        entity_id, entity_type, fields_to_write, drifted_fields, idem_key = (
            plan_class_b_reconciliation_for_entity(entity_record, conn, args.cutover, cutover_date)
        )
        total_fields_drifted_local += len(drifted_fields)
        if not fields_to_write:
            entities_with_nothing_to_apply += 1
            continue
        total_fields_planned += len(fields_to_write)
        # hosted_value_hash per writable field, for the apply-time drift
        # re-check (recheck_hosted_drift) -- built from the SAME writable
        # field list plan_class_b_reconciliation_for_entity used, so it
        # only ever covers fields already selected to write.
        expected_hashes = {
            f["name"]: f.get("hosted_value_hash")
            for f in filter_writable_fields(entity_record)
            if f["name"] in fields_to_write
        }
        planned.append((entity_id, entity_type, fields_to_write, expected_hashes, idem_key))

    log_path = args.log
    applied_entities = 0
    applied_fields = 0
    apply_time_drift_fields = 0
    with open(log_path, "a", encoding="utf-8") as log_fh:
        for entity_id, entity_type, fields_to_write, expected_hashes, idem_key in planned:
            field_names = sorted(fields_to_write.keys())
            if not apply_mode:
                print(
                    f"DRY-RUN would reconcile: entity={entity_id} type={entity_type} "
                    f"fields={field_names} idem={idem_key}"
                )
                log_action(
                    log_fh,
                    kind="reconcile",
                    local_id=entity_id,
                    entity_id=entity_id,
                    entity_type=entity_type,
                    entity_class="class_b_reconcile",
                    field_count=len(field_names),
                    action="dry_run",
                    idempotency_key=idem_key,
                    http_status=None,
                )
                continue

            drift_now = recheck_hosted_drift(entity_id, field_names, expected_hashes, base_url, token)
            if drift_now:
                apply_time_drift_fields += len(drift_now)
                for name in drift_now:
                    fields_to_write.pop(name, None)
                log_action(
                    log_fh,
                    kind="reconcile",
                    local_id=entity_id,
                    entity_id=entity_id,
                    entity_type=entity_type,
                    entity_class="drifted",
                    field_count=len(drift_now),
                    action="skipped_drift",
                    idempotency_key=idem_key,
                    http_status=None,
                )
            if not fields_to_write:
                continue

            payload = build_store_payload_for_reconcile(entity_id, entity_type, fields_to_write, idem_key)
            status, resp = http_request("POST", base_url, "/store", token, payload)
            ok = status in (200, 201)
            print(
                f"{'APPLIED' if ok else 'ERROR'}: entity={entity_id} type={entity_type} "
                f"fields={sorted(fields_to_write.keys())} status={status}"
            )
            log_action(
                log_fh,
                kind="reconcile",
                local_id=entity_id,
                entity_id=entity_id,
                entity_type=entity_type,
                entity_class="class_b_reconcile",
                field_count=len(fields_to_write),
                action="applied" if ok else "apply_failed",
                idempotency_key=idem_key,
                http_status=status,
            )
            if not ok:
                print(
                    "  Stopping: an error occurred mid-run. Not attempting cleanup.",
                    file=sys.stderr,
                )
                sys.exit(1)
            applied_entities += 1
            applied_fields += len(fields_to_write)
            time.sleep(0.05)

    print()
    print("=== Reconciliation summary ===")
    print(f"Entities with nothing to apply (SAME/HOSTED_NEWER only): {entities_with_nothing_to_apply}")
    print(f"Entities planned to write:                               {len(planned)}")
    print(f"Fields planned to write:                                 {total_fields_planned}")
    print(f"Fields skipped (local value drifted from reconciliation.json): {total_fields_drifted_local}")
    if apply_mode:
        print(f"Entities applied:                                        {applied_entities}")
        print(f"Fields applied:                                          {applied_fields}")
        print(f"Fields skipped (hosted drifted since reconciliation was computed): {apply_time_drift_fields}")
    print(f"Action log: {log_path}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--apply",
        action="store_true",
        help="Actually write to hosted. Default is dry-run.",
    )
    ap.add_argument(
        "--dry-run", action="store_true", help="Explicit dry-run (default behavior)."
    )
    ap.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap total number of entities processed (smoke test).",
    )
    ap.add_argument(
        "--db",
        required=True,
        help="Path to the LOCAL SQLite copy to replay from. Must be a read-only, "
        "frozen fork; never a live writable database.",
    )
    ap.add_argument(
        "--cutover",
        required=True,
        help="ISO-8601 cutover timestamp. Only rows with created_at > this value are considered.",
    )
    ap.add_argument(
        "--only-missing",
        dest="only_missing",
        action="store_true",
        default=True,
        help="Probe GET /entities/<id> before writing and skip unless 404 (default: on).",
    )
    ap.add_argument(
        "--no-only-missing",
        dest="only_missing",
        action="store_false",
        help="Disable the pre-write existence probe (not recommended for --apply).",
    )
    ap.add_argument(
        "--entity-ids",
        default=None,
        help="Comma-separated allowlist of entity ids to restrict the replay to (smoke tests).",
    )
    ap.add_argument(
        "--log",
        default="neotoma_local_fork_replay.jsonl",
        help="Path to append the JSONL action log to (default: ./neotoma_local_fork_replay.jsonl).",
    )
    ap.add_argument(
        "--unknown-fields",
        dest="unknown_fields_policy",
        choices=("stop", "warn"),
        default="stop",
        help=(
            "Policy when a write's response carries an UNKNOWN_FIELD "
            "store_warning (or, in --dry-run, when pre-flight prediction "
            "against the fetched hosted schema finds a field the schema "
            "does not declare): 'stop' (default) halts the run immediately, "
            "matching the existing behavior this script has always had. "
            "'warn' logs the warning and continues to the next planned "
            "action instead of exiting -- use only once the schema_additions "
            "proposal this script can generate has been reviewed and the "
            "operator has decided which undeclared fields are acceptable "
            "to keep sending. Ignored when --extend-schemas is set (the "
            "default for --apply): an undeclared additive field is then "
            "registered on hosted rather than stopped or warned on."
        ),
    )
    ap.add_argument(
        "--extend-schemas",
        dest="extend_schemas",
        action="store_true",
        default=None,
        help=(
            "Auto-extend hosted schemas with additive optional fields "
            "(operator ruling 2026-09-23): for each entity_type in this "
            "run's class-a data, compute the undeclared fields it uses "
            "(predict_unknown_fields), register them on hosted via "
            "POST /update_schema_incremental (existing schema) or "
            "POST /register_schema (the 3 no-schema types), infer field "
            "type from the observed value falling back to string, and "
            "re-fetch + verify before any dependent /store call. NEVER "
            "removes, renames, or retypes an existing field, and NEVER "
            "touches canonical_name_fields. Defaults to ON for --apply "
            "(pass --no-extend-schemas to opt out); OFF for a plain "
            "--dry-run unless passed explicitly, in which case the planned "
            "extensions are printed and NOT sent."
        ),
    )
    ap.add_argument(
        "--no-extend-schemas",
        dest="extend_schemas",
        action="store_false",
        help="Disable schema auto-extension even under --apply.",
    )
    ap.add_argument(
        "--reconcile-file",
        dest="reconcile_file",
        default=None,
        help=(
            "Path to a reconciliation.json (list of {id, entity_type, "
            "fields: [{name, classification, ...}]}) describing class-b "
            "field-level differences between the local fork and hosted. "
            "When set, this run performs ONLY the reconciliation pass "
            "(writes LOCAL_ONLY/LOCAL_NEWER fields to hosted, one /store "
            "per entity with target_id + a deterministic idempotency_key) "
            "instead of the class-a observation/relationship replay above. "
            "SAME and HOSTED_NEWER fields are always skipped. Re-verifies "
            "each writable field's hosted_value_hash immediately before "
            "writing and skips (logging entity_class=drifted) any field "
            "whose hosted value changed since the reconciliation file was "
            "computed."
        ),
    )
    args = ap.parse_args()

    if args.extend_schemas is None:
        args.extend_schemas = bool(args.apply)

    apply_mode = args.apply  # dry-run is the default; --apply is the only way to write
    if apply_mode:
        confirm = os.environ.get("MIGRATE_CONFIRM_APPLY")
        if confirm != "yes":
            print(
                "Refusing to run --apply without MIGRATE_CONFIRM_APPLY=yes set explicitly "
                "as an extra guard against accidental invocation.",
                file=sys.stderr,
            )
            sys.exit(1)

    base_url = get_base_url()
    token = get_token()
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)

    entity_ids = None
    if args.entity_ids:
        entity_ids = {e.strip() for e in args.entity_ids.split(",") if e.strip()}

    cutover_date = args.cutover.split("T")[0].replace("-", "")

    if args.reconcile_file:
        run_reconciliation(
            args, conn, base_url, token, cutover_date, apply_mode, entity_ids
        )
        return

    obs, rels, srcs, excluded_schema_lag = load_candidates(
        conn, args.cutover, entity_ids=entity_ids
    )
    if args.limit:
        obs, rels, srcs = obs[: args.limit], rels[: args.limit], srcs[: args.limit]

    print(f"Loaded from {args.db}:")
    print(f"  observations candidates (class a+b):   {len(obs)}")
    print(
        f"  excluded schema_lag_bg_* rewrites:     {len(excluded_schema_lag)}"
    )
    print(f"  relationship_observations candidates:  {len(rels)}")
    print(f"  sources candidates:                    {len(srcs)}")
    print(
        f"  mode: {'APPLY (writing to hosted)' if apply_mode else 'DRY-RUN (no writes)'}"
    )
    print(f"  only-missing probe: {'on' if args.only_missing else 'off'}")
    print(f"  unknown-fields policy: {args.unknown_fields_policy}")
    print()

    plan = []
    entity_cache: dict[str, bool] = {}
    schema_cache: dict[str, dict] = {}

    # Fetch each observation's entity_type's hosted schema ONCE up front
    # (GET /schemas/<type>, cached in schema_cache) -- used for both
    # field-stripping (strip_reserved_fields) and pre-flight UNKNOWN_FIELD
    # prediction (predict_unknown_fields) below, so a run never issues more
    # than one schema GET per distinct entity_type regardless of how many
    # observations of that type it processes.
    distinct_obs_types = sorted({row[2] for row in obs})
    for et in distinct_obs_types:
        get_schema_declared_fields(et, base_url, token, schema_cache)

    # --extend-schemas (operator ruling 2026-09-23): compute, per entity_type,
    # every undeclared field this run's class-a data actually uses, and
    # register them as additive optional fields BEFORE building any /store
    # payload -- so predict_unknown_fields below reflects the schema as it
    # will be at write time, not as it was before extension. Runs against
    # the CURRENT record's raw field data (schema-stripped, matching what
    # build_observation_payload will send), never a hardcoded field list.
    schema_extension_plan: dict[str, list[dict]] = {}
    schema_extension_results: dict[str, dict] = {}
    if args.extend_schemas:
        undeclared_samples: dict[str, dict] = {}
        for row in obs:
            entity_type = row[2]
            fields_json = row[9]
            schema_info = schema_cache.get(entity_type, {"has_schema": False, "declared_fields": set()})
            fields = json.loads(fields_json) if fields_json else {}
            stripped_fields, _stripped = strip_reserved_fields(entity_type, fields, schema_info)
            for name, value in stripped_fields.items():
                declared = schema_info.get("declared_fields", set()) if schema_info.get("has_schema") else set()
                if name not in declared:
                    undeclared_samples.setdefault(entity_type, {}).setdefault(name, value)

        for entity_type, samples in sorted(undeclared_samples.items()):
            schema_info = schema_cache.get(entity_type, {"has_schema": False, "declared_fields": set()})
            fields_to_add = plan_schema_extensions(entity_type, samples, schema_info)
            if not fields_to_add:
                continue
            schema_extension_plan[entity_type] = fields_to_add
            if not apply_mode:
                print(
                    f"PLANNED SCHEMA EXTENSION (not sent, dry-run): {entity_type} += "
                    f"{[f['field_name'] + ':' + f['field_type'] for f in fields_to_add]}"
                )
                continue
            ok, resp = extend_hosted_schema(entity_type, fields_to_add, schema_info, base_url, token)
            if not ok:
                print(
                    f"  ERROR extending schema for {entity_type}: "
                    f"{resp.get('error') if isinstance(resp, dict) else resp}",
                    file=sys.stderr,
                )
                sys.exit(1)
            expected_names = [f["field_name"] for f in fields_to_add]
            verified, missing = verify_schema_extension_applied(entity_type, expected_names, base_url, token)
            if not verified:
                print(
                    f"  ERROR: schema extension for {entity_type} reported success but "
                    f"re-fetch does not show fields {missing} as declared. Stopping before "
                    "any dependent /store call -- a write that reports success has not "
                    "necessarily happened.",
                    file=sys.stderr,
                )
                sys.exit(1)
            # Refresh schema_cache with the now-current declared_fields so the
            # observation-building loop below stops predicting these fields
            # as unknown and stops stripping them as conditionally-reserved.
            fresh_info = get_schema_declared_fields(entity_type, base_url, token, {})
            schema_cache[entity_type] = fresh_info
            schema_extension_results[entity_type] = {"applied": True, "fields": expected_names}
            print(f"EXTENDED AND VERIFIED: {entity_type} += {expected_names}")

    stats = {
        "entities_touched": set(),
        "observations_by_class": {},
        "relationships_among_replayed": 0,
        "relationships_to_hosted_existing": 0,
        "relationships_deferred": 0,
        "predicted_unknown_field_warnings": {},  # entity_type -> {field: count}
        "no_schema_type_counts": {},
    }

    for row in obs:
        local_id, entity_id, entity_type = row[0], row[1], row[2]
        schema_info = schema_cache.get(entity_type, {"has_schema": False, "declared_fields": set()})
        payload, idem_key, stripped_keys = build_observation_payload(
            row, cutover_date, schema_info=schema_info
        )
        (entity_record,) = payload["entities"]
        entity_fields = {
            k: v for k, v in entity_record.items() if k not in ("entity_type", "target_id")
        }
        if not schema_info.get("has_schema"):
            stats["no_schema_type_counts"][entity_type] = (
                stats["no_schema_type_counts"].get(entity_type, 0) + 1
            )
        predicted_unknown = predict_unknown_fields(entity_type, entity_fields, schema_info)
        if predicted_unknown:
            bucket = stats["predicted_unknown_field_warnings"].setdefault(entity_type, {})
            for k in predicted_unknown:
                bucket[k] = bucket.get(k, 0) + 1
        stats["entities_touched"].add(entity_id)
        plan.append(
            (
                "observation",
                local_id,
                entity_id,
                entity_type,
                idem_key,
                payload,
                stripped_keys,
                predicted_unknown,
            )
        )

    replayed_entity_ids = stats["entities_touched"]
    for row in rels:
        local_id = row[0]
        source_entity_id, target_entity_id = row[3], row[4]
        payload, idem_key = build_relationship_payload(row, cutover_date)
        source_replayed = source_entity_id in replayed_entity_ids
        target_replayed = target_entity_id in replayed_entity_ids
        if source_replayed and target_replayed:
            rel_class = "among_replayed"
            stats["relationships_among_replayed"] += 1
        elif source_replayed or target_replayed:
            rel_class = "to_hosted_existing"
            stats["relationships_to_hosted_existing"] += 1
        else:
            # Neither endpoint is a class-a entity this run is replaying --
            # nothing to check without probing hosted for both ids, which
            # only happens under --only-missing for observations today.
            # Reported separately rather than guessed at.
            rel_class = "deferred"
            stats["relationships_deferred"] += 1
        plan.append(
            (
                "relationship",
                local_id,
                f"{source_entity_id}->{target_entity_id}",
                "relationship_observation",
                idem_key,
                payload,
                [],
                [],
            )
        )
        _ = rel_class  # recorded in stats above; not threaded into the log tuple

    for row in srcs:
        local_id = row[0]
        idem_key = f"migrate-{cutover_date}-src-{local_id}"
        plan.append(
            (
                "source",
                local_id,
                row[6] or "(no filename)",
                "source",
                idem_key,
                {"content_hash": row[2]},
                [],
                [],
            )
        )

    log_path = args.log
    with open(log_path, "a", encoding="utf-8") as log_fh:
        for (
            kind,
            local_id,
            target_desc,
            entity_type,
            idem_key,
            payload,
            stripped_keys,
            predicted_unknown,
        ) in plan:
            entity_class = "n/a"
            if kind == "observation":
                if args.only_missing:
                    if target_desc not in entity_cache:
                        entity_cache[target_desc] = entity_exists(
                            target_desc, base_url, token
                        )
                        time.sleep(0.05)  # polite pacing between probe GETs
                    exists_on_hosted = entity_cache[target_desc]
                    entity_class = (
                        "b_diverged_or_present" if exists_on_hosted else "a_missing"
                    )
                else:
                    entity_class = "unknown_probe_disabled"

            prefix = (
                f"[{kind:12s}] local_id={local_id[:12]}... target={target_desc} "
                f"idem={idem_key} class={entity_class}"
            )
            if stripped_keys:
                prefix += f" stripped={stripped_keys}"

            # Pre-flight UNKNOWN_FIELD prediction applies in BOTH dry-run and
            # apply mode -- it is computed from the fetched hosted schema,
            # not from a live write's response, so it is available before
            # any write happens. --unknown-fields=stop halts here (before
            # ever making the write) exactly as it would after a live
            # UNKNOWN_FIELD store_warning; --unknown-fields=warn logs and
            # proceeds either way.
            if kind == "observation" and predicted_unknown:
                print(f"  PREDICTED UNKNOWN_FIELD (pre-flight): {predicted_unknown}")
                if args.unknown_fields_policy == "stop":
                    log_action(
                        log_fh,
                        kind=kind,
                        local_id=local_id,
                        entity_id=target_desc,
                        entity_type=entity_type,
                        entity_class=entity_class,
                        stripped_keys=stripped_keys,
                        predicted_unknown_fields=predicted_unknown,
                        action="stopped_predicted_unknown_field",
                        idempotency_key=idem_key,
                        http_status=None,
                    )
                    print(
                        "  Stopping: --unknown-fields=stop (default) and a field "
                        "the hosted schema does not declare was predicted for this "
                        "write. Re-run with --unknown-fields=warn to proceed past "
                        "predicted (not yet confirmed) UNKNOWN_FIELD cases, or "
                        "register the field on hosted first.",
                        file=sys.stderr,
                    )
                    sys.exit(1)

            if not apply_mode:
                print(f"DRY-RUN would write: {prefix}")
                log_action(
                    log_fh,
                    kind=kind,
                    local_id=local_id,
                    entity_id=target_desc,
                    entity_type=entity_type,
                    entity_class=entity_class,
                    stripped_keys=stripped_keys,
                    predicted_unknown_fields=predicted_unknown,
                    action="dry_run",
                    idempotency_key=idem_key,
                    http_status=None,
                )
                continue

            if (
                kind == "observation"
                and args.only_missing
                and entity_class != "a_missing"
            ):
                print(f"SKIP (not missing on hosted): {prefix}")
                log_action(
                    log_fh,
                    kind=kind,
                    local_id=local_id,
                    entity_id=target_desc,
                    entity_type=entity_type,
                    entity_class=entity_class,
                    stripped_keys=stripped_keys,
                    action="skipped_not_missing",
                    idempotency_key=idem_key,
                    http_status=None,
                )
                continue

            print(f"APPLYING: {prefix}")
            if kind == "observation":
                status, resp = http_request("POST", base_url, "/store", token, payload)
            elif kind == "relationship":
                status, resp = http_request(
                    "POST", base_url, "/create_relationship", token, payload
                )
            elif kind == "source":
                print(
                    "  SKIP: source blob replay not implemented in this script -- "
                    "sources carry file content (storage_url/reference_path) that "
                    "needs its own re-upload path; flagged for manual/second-pass handling."
                )
                log_action(
                    log_fh,
                    kind=kind,
                    local_id=local_id,
                    entity_id=target_desc,
                    entity_type=entity_type,
                    entity_class="deferred_source_blob",
                    action="skipped_unimplemented",
                    idempotency_key=idem_key,
                    http_status=None,
                )
                continue
            else:
                continue

            ok = status in (200, 201)
            unknown_field_warnings = []
            if ok and isinstance(resp, dict):
                for w in resp.get("store_warnings") or []:
                    if isinstance(w, dict) and w.get("code") == "UNKNOWN_FIELD":
                        unknown_field_warnings.append(w.get("entity_id") or target_desc)
            if not ok:
                print(
                    f"  ERROR status={status} resp_keys={sorted(resp.keys()) if isinstance(resp, dict) else type(resp).__name__}"
                )
            elif unknown_field_warnings:
                print(
                    f"  OK status={status} but UNKNOWN_FIELD store_warnings present -- STOPPING"
                )
            else:
                print(f"  OK status={status}")
            log_action(
                log_fh,
                kind=kind,
                local_id=local_id,
                entity_id=target_desc,
                entity_type=entity_type,
                entity_class=entity_class,
                unknown_field_warning_count=len(unknown_field_warnings),
                action="applied" if ok else "apply_failed",
                idempotency_key=idem_key,
                http_status=status,
            )
            if not ok:
                print(
                    "  Stopping: an error occurred mid-run. Not attempting cleanup.",
                    file=sys.stderr,
                )
                sys.exit(1)
            if unknown_field_warnings and args.unknown_fields_policy == "stop":
                print(
                    "  Stopping: UNKNOWN_FIELD store_warnings on a write means a field "
                    "the payload sent has no home on the schema. Not attempting cleanup. "
                    "Re-run with --unknown-fields=warn to continue past this once the "
                    "warning has been reviewed.",
                    file=sys.stderr,
                )
                sys.exit(1)
            elif unknown_field_warnings:
                print(
                    "  Continuing past UNKNOWN_FIELD store_warnings (--unknown-fields=warn)."
                )
            time.sleep(0.05)  # gentle rate limiting

    print()
    print("=== Summary ===")
    print(f"Entities touched (distinct entity_id across observations): {len(stats['entities_touched'])}")
    print(f"Observations planned:                                      {len(obs)}")
    print(f"Observations excluded (schema_lag_bg_* rewrites):          {len(excluded_schema_lag)}")
    print(f"Relationships among replayed entities (both endpoints):    {stats['relationships_among_replayed']}")
    print(f"Relationships to a hosted-existing entity (one endpoint):  {stats['relationships_to_hosted_existing']}")
    print(f"Relationships deferred (neither endpoint replayed here):   {stats['relationships_deferred']}")
    print(f"Sources deferred (blob replay unimplemented):              {len(srcs)}")
    if stats["no_schema_type_counts"]:
        print("Entity types with no hosted schema (class-a obs count):")
        for et, n in sorted(stats["no_schema_type_counts"].items(), key=lambda x: -x[1]):
            print(f"  {et}: {n}")
    if stats["predicted_unknown_field_warnings"]:
        print("Predicted UNKNOWN_FIELD store_warnings (pre-flight, by type):")
        for et, fields in sorted(stats["predicted_unknown_field_warnings"].items()):
            field_summary = ", ".join(f"{k}x{v}" for k, v in sorted(fields.items(), key=lambda x: -x[1]))
            print(f"  {et}: {field_summary}")
    else:
        print("Predicted UNKNOWN_FIELD store_warnings (pre-flight): none")
    if schema_extension_plan:
        label = "Applied schema extensions" if apply_mode else "Planned schema extensions (dry-run, not sent)"
        print(f"{label} (by type):")
        for et, fields_to_add in sorted(schema_extension_plan.items()):
            field_summary = ", ".join(f"{f['field_name']}:{f['field_type']}" for f in fields_to_add)
            print(f"  {et}: {field_summary}")
    else:
        print("Schema extensions: none planned" if args.extend_schemas else "Schema extensions: --extend-schemas not set")
    print()
    print(f"Total planned operations: {len(plan)}")
    print(f"Action log: {log_path}")
    if not apply_mode:
        print("This was a DRY RUN. No data was written to hosted Neotoma.")
        print(
            "To apply: MIGRATE_CONFIRM_APPLY=yes python3 neotoma_local_fork_replay.py "
            "--db <path> --cutover <ts> --apply --limit <n>"
        )


if __name__ == "__main__":
    main()
