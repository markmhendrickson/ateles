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
            "to keep sending."
        ),
    )
    args = ap.parse_args()

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
