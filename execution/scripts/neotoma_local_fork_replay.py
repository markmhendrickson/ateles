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
/create_relationship), NOT any in-process storage function, because:
  - Entity ids and observation ids on hosted are deterministic hashes
    derived from (entity_type, canonical_name) and (source_id,
    interpretation_id, entity_id, fields, idempotency_key) respectively.
    Submitting the same content through /store with a deterministic
    idempotency_key derived from the LOCAL observation's own id resolves to
    the same ids and is safe to re-run.
  - The public /store schema does not accept a client-supplied observed_at
    override or raw id override -- only idempotency_key, source_priority,
    observation_source. This script still submits observed_at as a
    field-level value inside the request body where the schema allows it,
    and logs the server's own response so a caller can check afterward
    whether the value was honored or the server stamped "now" instead.
  - create_relationship has no timestamp override at all -- replayed
    relationships get a new created_at on hosted. This is a known, accepted
    provenance gap.

Idempotency key scheme
-----------------------
  observations:              "migrate-<cutover-date>-obs-<local_observation_id>"
  relationship_observations: "migrate-<cutover-date>-rel-<local_relationship_observation_id>"
  sources:                   "migrate-<cutover-date>-src-<local_source_id>"

Re-running with --apply after a partial failure is safe: the server treats
a duplicate idempotency_key as a no-op / returns the existing row.

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
    return (
        {
            "entities": [
                {
                    "entity_type": entity_type,
                    "entity_id": entity_id,  # hint; server resolves canonically
                    "fields": fields,
                    "observed_at": observed_at,  # best-effort; public schema may not honor this
                    "idempotency_key": idem_key,
                    "observation_source": observation_source or "import",
                    "source_priority": source_priority,
                }
            ]
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
    idem_key = f"migrate-{cutover_date}-rel-{local_id}"
    return {
        "relationship_type": rel_type,
        "source_entity_id": source_entity_id,
        "target_entity_id": target_entity_id,
        "metadata": metadata,
        "observed_at": observed_at,  # no server-side timestamp override; logged as gap
        "idempotency_key": idem_key,
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
            if not ok:
                print(
                    f"  ERROR status={status} resp_keys={sorted(resp.keys()) if isinstance(resp, dict) else type(resp).__name__}"
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
