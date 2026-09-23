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


def http_request(method: str, base_url: str, path: str, token: str, body=None):
    url = f"{base_url}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
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


def entity_exists(entity_id: str, base_url: str, token: str) -> bool:
    status, _ = http_request("GET", base_url, f"/entities/{entity_id}", token)
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
    obs = cur.fetchall()
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

    return obs, rels, srcs


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


def build_observation_payload(row, cutover_date: str):
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
    )


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

    obs, rels, srcs = load_candidates(conn, args.cutover, entity_ids=entity_ids)
    if args.limit:
        obs, rels, srcs = obs[: args.limit], rels[: args.limit], srcs[: args.limit]

    print(f"Loaded from {args.db}:")
    print(f"  observations candidates:               {len(obs)}")
    print(f"  relationship_observations candidates:  {len(rels)}")
    print(f"  sources candidates:                    {len(srcs)}")
    print(
        f"  mode: {'APPLY (writing to hosted)' if apply_mode else 'DRY-RUN (no writes)'}"
    )
    print(f"  only-missing probe: {'on' if args.only_missing else 'off'}")
    print()

    plan = []
    entity_cache: dict[str, bool] = {}

    for row in obs:
        local_id, entity_id, entity_type = row[0], row[1], row[2]
        payload, idem_key = build_observation_payload(row, cutover_date)
        plan.append(
            ("observation", local_id, entity_id, entity_type, idem_key, payload)
        )

    for row in rels:
        local_id = row[0]
        payload, idem_key = build_relationship_payload(row, cutover_date)
        plan.append(
            (
                "relationship",
                local_id,
                f"{row[3]}->{row[4]}",
                "relationship_observation",
                idem_key,
                payload,
            )
        )

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
            )
        )

    log_path = args.log
    with open(log_path, "a", encoding="utf-8") as log_fh:
        for kind, local_id, target_desc, entity_type, idem_key, payload in plan:
            entity_class = "n/a"
            if kind == "observation":
                if args.only_missing:
                    if target_desc not in entity_cache:
                        entity_cache[target_desc] = entity_exists(
                            target_desc, base_url, token
                        )
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

            if not apply_mode:
                print(f"DRY-RUN would write: {prefix}")
                log_action(
                    log_fh,
                    kind=kind,
                    local_id=local_id,
                    entity_id=target_desc,
                    entity_type=entity_type,
                    entity_class=entity_class,
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
            if unknown_field_warnings:
                print(
                    "  Stopping: UNKNOWN_FIELD store_warnings on a write means a field "
                    "the payload sent has no home on the schema. Not attempting cleanup.",
                    file=sys.stderr,
                )
                sys.exit(1)
            time.sleep(0.05)  # gentle rate limiting

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
