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
(and, on top of that, NEOTOMA_REPLAY_CONFIRM_APPLY=yes must be set in the
environment, as a second deliberate guard against accidental invocation).
--apply and --dry-run are mutually exclusive; omitting both means dry-run.

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
This script exposes three explicit, mutually exclusive subcommands. Running
it with no subcommand prints usage and exits non-zero (code=E_ARGUMENT_CONFLICT).
See docs/runbooks/neotoma_local_fork_replay.md for the full indexed runbook
(every mode, canary application, verification, and failure recovery).

  replay        -- class-a observation/relationship replay (the original mode)
  reconcile     -- class-b field-level reconcile from a --reconcile-file
  restore-gates -- restore issue gate_status/owner_history/current_owner

Examples:
  python3 neotoma_local_fork_replay.py replay --db <path> --cutover <ts>
  python3 neotoma_local_fork_replay.py replay --db <path> --cutover <ts> \
      --apply --limit 20
  python3 neotoma_local_fork_replay.py reconcile --db <path> --cutover <ts> \
      --reconcile-file reconciliation.json
  python3 neotoma_local_fork_replay.py restore-gates \
      --db <path1>,<path2> --cutover <ts>

Environment
-----------
  Requires NEOTOMA_BEARER_TOKEN_PROD or NEOTOMA_BEARER_TOKEN to already be
  exported in the environment (this script does not source any dotenv
  itself). Requires NEOTOMA_BASE_URL to be exported (no hardcoded default,
  since this repo is public and hosted instance URLs are operator-specific
  config). Never prints, logs, or echoes the token or any field value.

  --apply requires NEOTOMA_REPLAY_CONFIRM_APPLY=yes set explicitly, checked
  BEFORE get_base_url()/get_token()/any HTTP call, as a second deliberate
  guard against accidental invocation.

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
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# lib/daemon_runtime/neotoma_signed.py is the prior art for per-agent AAuth
# signing (ateles#795 / PR #1181): it shells out to neotoma's proven
# cliSignedFetch so a write is attributed to a named agent sub instead of the
# shared bearer identity. Imported defensively -- a checkout that lacks
# lib/daemon_runtime (or node, or the neotoma-rc-src signer) must not crash
# this script at import time; --sign-as fails closed later, at the point a
# write actually needs a signature (see resolve_signing_identity).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "lib" / "daemon_runtime"))
try:
    import neotoma_signed as _neotoma_signed  # type: ignore
except Exception:  # pragma: no cover -- exercised only on a broken checkout
    _neotoma_signed = None

USER_AGENT = "ateles-migrate/1.0"

# Load rule (operator instruction, hosted Neotoma migration): the client
# timeout on every request against hosted must be at least 180s, and an
# in-flight request must never be abandoned (an abandoned request is what
# crashed hosted per neotoma#2483). 180s, not the urllib default (fixed
# above at a bare `timeout=30`, which was itself a violation of this rule
# until corrected here).
HTTP_CLIENT_TIMEOUT_SECONDS = 180

# Load rule: check /health before each batch of this many requests, and
# abort the whole run on a non-200 health response.
HEALTH_CHECK_BATCH_SIZE = 20


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


def get_schema_declared_fields(
    entity_type: str, base_url: str, token: str, cache: dict
):
    """Fetch and cache a hosted entity type's declared field names.

    GET /schemas/<entity_type> is a read -- never a write -- and is fetched
    at most once per entity_type per run (results cached in `cache`). A
    CONFIRMED 200 means the type has a hosted schema; `schema_definition.
    fields` gives the declared field names. A CONFIRMED 404 means hosted has
    no schema at all for this type (class-a data of that type can still be
    replayed -- there is just nothing to check field names against, so every
    field is "unknown" in the sense that nothing declares it, but that is
    expected and not an UNKNOWN_FIELD store_warning risk distinct from any
    other field). Raises HostedProbeAmbiguousError on a 5xx/401/403/timeout
    that survives retrying -- NEVER silently treated as "no schema"
    (neotoma#2483: that would corrupt every downstream decision this cache
    feeds -- field stripping, UNKNOWN_FIELD prediction, merge_array
    detection, schema auto-extension -- all keyed on has_schema/
    declared_fields/merge_array_fields being a confirmed answer, not a guess
    made because hosted was unreachable).
    """
    if entity_type in cache:
        return cache[entity_type]
    exists, _status, body = probe_status_tristate(
        "GET", base_url, f"/schemas/{entity_type}", token, max_attempts=3, retry_backoff_seconds=2.0
    )
    if exists and isinstance(body, dict):
        fields = body.get("schema_definition", {}).get("fields", {})
        declared = set(fields.keys()) if isinstance(fields, dict) else set()
        merge_policies = (
            body.get("reducer_config", {}).get("merge_policies", {})
            if isinstance(body.get("reducer_config"), dict)
            else {}
        )
        merge_array_fields = {
            name
            for name, policy in (merge_policies or {}).items()
            if isinstance(policy, dict) and policy.get("strategy") == "merge_array"
        }
        cache[entity_type] = {
            "has_schema": True,
            "declared_fields": declared,
            "merge_array_fields": merge_array_fields,
        }
    else:
        cache[entity_type] = {
            "has_schema": False,
            "declared_fields": set(),
            "merge_array_fields": set(),
        }
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


def strip_reserved_fields(
    entity_type: str, fields: dict, schema_info: dict
) -> tuple[dict, list[str]]:
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
            with urllib.request.urlopen(req, timeout=HTTP_CLIENT_TIMEOUT_SECONDS) as resp:
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


class HostedProbeAmbiguousError(RuntimeError):
    """Raised when a read-only existence/identity probe against hosted got a
    response that is neither a confirmed 200 (exists) nor a confirmed 404
    (does not exist) after retrying -- e.g. a 5xx, a timeout, or a 401/403.

    This is the fix for a real incident: hosted crash-looped (Fly exit 134,
    neotoma#2483) and returned 502s during a run; every probe in this script
    treated `status == 200` as the ONLY "exists" signal and anything else --
    including that 502 -- as "missing", which would have made a replay CREATE
    duplicate entities for issues that already existed on hosted and were
    merely unreachable at that moment. A probe must never silently classify
    "hosted is broken right now" as "this entity does not exist". Callers
    catch this and abort the whole run with a clear error rather than
    guessing either way.
    """


def probe_status_tristate(
    method: str,
    base_url: str,
    path: str,
    token: str,
    body=None,
    *,
    max_attempts: int = 3,
    retry_backoff_seconds: float = 2.0,
) -> tuple[bool, int, object]:
    """Tri-state existence/identity probe: (True, 200, resp_body) exists,
    (False, 404, resp_body) confirmed missing, or raises
    HostedProbeAmbiguousError after `max_attempts` attempts for every other
    outcome (5xx, 401, 403, or a connection-level failure/timeout that
    survived http_request's own connection-retry budget).

    A 5xx/401/403/timeout is retried here (in addition to http_request's own
    connection-level retry, which only covers DNS/timeout/connection-reset --
    never a real HTTP status code, since retrying a request the server
    already answered risks a duplicate side effect on a non-idempotent call).
    This function is used only for read-only probes in this script (GET
    existence checks and POST /retrieve_entity_by_identifier, itself a read),
    so that concern does not apply here. Only 200 and 404 are ever treated as
    confirmed outcomes; every other status, and every connection-level
    exception that exhausts retries, raises rather than returning a boolean --
    there is no silent "assume missing" path. Returning the response body on
    both confirmed outcomes (not just 200) lets callers avoid a second round
    trip to fetch what they just probed.
    """
    last_status = None
    last_body = None
    for attempt in range(1, max_attempts + 1):
        try:
            status, resp_body = http_request(
                method, base_url, path, token, body, retries=1, retry_backoff_seconds=retry_backoff_seconds
            )
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            last_status, last_body = None, repr(e)
        else:
            if status == 200:
                return True, status, resp_body
            if status == 404:
                return False, status, resp_body
            last_status, last_body = status, resp_body
        if attempt < max_attempts:
            print(
                f"  (ambiguous probe response on {method} {path}: "
                f"status={last_status} -- retry {attempt}/{max_attempts} in "
                f"{retry_backoff_seconds:.1f}s)",
                file=sys.stderr,
            )
            time.sleep(retry_backoff_seconds)
    raise HostedProbeAmbiguousError(
        f"{method} {path}: got status={last_status!r} body={last_body!r} on every "
        f"attempt (of {max_attempts}) -- never a confirmed 200 or 404. Aborting "
        "rather than treating this as 'entity missing' (neotoma#2483: hosted has "
        "crash-looped and returned 502s before; classifying that as 404 would "
        "create duplicate entities)."
    )


def entity_exists(entity_id: str, base_url: str, token: str) -> bool:
    """True if hosted confirms the entity exists (200), False if hosted
    confirms it does not (404). Raises HostedProbeAmbiguousError on any other
    outcome (5xx, 401/403, timeout) after retrying -- NEVER silently
    classifies an ambiguous/error response as "missing". Callers that need a
    softer failure mode should catch HostedProbeAmbiguousError explicitly;
    none in this script currently do, since an unresolvable probe should
    abort the run (see module-level usage in main()/run_gate_restore()).
    """
    exists, _status, _body = probe_status_tristate(
        "GET", base_url, f"/entities/{entity_id}", token, max_attempts=3, retry_backoff_seconds=2.0
    )
    return exists


# --- merge_array presence via OBSERVATIONS, not the snapshot (neotoma#2341) -
#
# The hosted SNAPSHOT never reflects a merge_array-reducer field's true state:
# store() writes to an array field (e.g. issue.owner_history) are accepted
# and increment observation_count, but the snapshot the entity GET returns
# keeps showing the field's PRE-write value indefinitely (confirmed live
# during the 2026-09-23 gate-restore: ~150 redundant observations sent across
# repeated runs, each computing "missing" against a snapshot that never
# converged). Every presence/diff decision for a merge_array field -- or, more
# conservatively, ANY array-valued field, since a field can use merge_array
# without this script's local schema_info cache having been asked about it
# yet -- must be made from hosted's OBSERVATIONS via POST /list_observations,
# never from GET /entities/<id>'s snapshot. This function is that read: the
# union, across every observation for entity_id, of whatever that field held
# on each one (an observation's `fields` may set the field to a whole array,
# per the write shape this script itself sends -- build_entity_record puts
# the full local array value under the field name -- so "union across
# observations" means "union of every array any single observation set for
# this field", not a per-element list, though in practice each write is
# itself already a full array).
LIST_OBSERVATIONS_PAGE_SIZE = 100


def get_hosted_field_observations(
    entity_id: str, field_name: str, base_url: str, token: str
) -> list:
    """Every value observed for `field_name` on `entity_id`, oldest call order
    not guaranteed -- returns a flat list of the raw per-observation values
    (whatever type each observation stored under this field key; for an
    array-valued field this is normally a list-of-lists, one list per
    observation that touched the field). Paginates POST /list_observations
    (limit/offset) until a short page ends it. Read-only.

    Raises HostedProbeAmbiguousError (via probe_status_tristate) if any page
    gets a 5xx/401/403/timeout that survives retrying -- this function used
    to return [] on ANY non-200 response, on the reasoning that "fail toward
    found nothing new" was the safe direction for a presence check. That
    reasoning was backwards and is the exact class of bug neotoma#2483
    describes: "found nothing new" here means "hosted's observations contain
    NONE of this field's local entries", which makes every local entry look
    missing and sends the WHOLE local array as "new" -- during a hosted
    outage (5xx) this would duplicate every already-present array entry
    rather than skip the write, precisely the bug this presence check exists
    to prevent. An ambiguous read must abort the run, not silently answer
    "empty".
    """
    values: list = []
    offset = 0
    while True:
        exists, _status, body = probe_status_tristate(
            "POST",
            base_url,
            "/list_observations",
            token,
            {
                "entity_id": entity_id,
                "limit": LIST_OBSERVATIONS_PAGE_SIZE,
                "offset": offset,
            },
            max_attempts=3,
            retry_backoff_seconds=2.0,
        )
        # /list_observations has no 404 case for a valid entity_id (an empty
        # result is a 200 with an empty `observations` list, not a 404) --
        # but probe_status_tristate's 404 branch is handled the same way as
        # "confirmed page ended" for uniformity, not treated as an error.
        if not isinstance(body, dict):
            break
        page = body.get("observations")
        if not isinstance(page, list) or not page:
            break
        for obs in page:
            if not isinstance(obs, dict):
                continue
            fields = obs.get("fields")
            if isinstance(fields, dict) and field_name in fields:
                values.append(fields[field_name])
        if len(page) < LIST_OBSERVATIONS_PAGE_SIZE:
            break
        offset += LIST_OBSERVATIONS_PAGE_SIZE
    return values


def array_entry_dedupe_key(entry) -> str:
    """Canonical dedupe key for one array-field entry: JSON-canonicalised
    (sorted keys, no incidental whitespace) full content, hashed. Two entries
    with the same content in a different key order or float/int spelling
    still collide correctly because json.dumps(sort_keys=True) is applied to
    the whole entry before hashing, not to a hand-picked subset of fields
    (contrast merge_owner_history's `_owner_history_entry_key`, which is
    deliberately narrower for the owner_history-specific gate-restore path).
    Every JSON-serializable entry hashes (dict, list, str, number, bool,
    None); a non-JSON-serializable entry falls back to its repr() so this
    function never raises.
    """
    try:
        canonical = json.dumps(entry, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        canonical = repr(entry)
    return hashlib.sha256(canonical.encode("utf-8", errors="replace")).hexdigest()


def hosted_array_field_present_keys(
    entity_id: str, field_name: str, base_url: str, token: str
) -> set:
    """Dedupe-key set of every entry hosted's OBSERVATIONS already hold for
    `field_name` on `entity_id` -- the union of every array any observation
    stored under this field, each element keyed by array_entry_dedupe_key.
    This is the presence source of truth for a merge_array (or any
    array-valued) field: never the snapshot (see module note above).
    """
    present: set = set()
    for value in get_hosted_field_observations(entity_id, field_name, base_url, token):
        if isinstance(value, list):
            for entry in value:
                present.add(array_entry_dedupe_key(entry))
        else:
            # A non-list value observed under a nominally array field (e.g. a
            # legacy write before the field was declared merge_array) -- key
            # it as a single entry rather than silently dropping it, so it
            # still counts toward "already present" and is never re-sent.
            present.add(array_entry_dedupe_key(value))
    return present


def plan_array_field_missing_entries(
    local_entries: list, hosted_present_keys: set
) -> list:
    """Given the LOCAL array's full entry list and the dedupe-key set already
    present on hosted (from hosted_array_field_present_keys), return only the
    entries genuinely missing -- deduped against each other too, so a locally
    duplicated entry is sent at most once. Order is preserved (first
    occurrence wins) so behaviour is deterministic across runs. An empty
    result means "send nothing" -- callers must not write the field at all
    in that case (an empty merge_array write is still a write, and per
    neotoma#2033 hosted creates a new observation even for a content-
    identical payload).
    """
    missing = []
    seen_this_call: set = set()
    for entry in local_entries or []:
        key = array_entry_dedupe_key(entry)
        if key in hosted_present_keys or key in seen_this_call:
            continue
        seen_this_call.add(key)
        missing.append(entry)
    return missing


def check_health(base_url: str, token: str) -> tuple[bool, int | None]:
    """GET /health -- returns (ok, status). ok is True iff status == 200 AND
    the body's own `ok` field (when present) is not False. Called before
    each batch of HEALTH_CHECK_BATCH_SIZE requests during a --gate-restore
    apply run (load rule); the caller aborts the whole run on ok=False
    rather than continuing to hammer a degraded or down hosted instance.
    """
    status, body = http_request("GET", base_url, "/health", token, retries=1, retry_backoff_seconds=2.0)
    if status != 200:
        return False, status
    if isinstance(body, dict) and body.get("ok") is False:
        return False, status
    return True, status


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
    excluded_schema_lag = [
        row for row in all_obs if is_schema_lag_background_rewrite(row[9])
    ]
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


def predict_unknown_fields(
    entity_type: str, fields: dict, schema_info: dict
) -> list[str]:
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


def plan_schema_extensions(
    entity_type: str, sample_values_by_field: dict, schema_info: dict
):
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
    declared = (
        schema_info.get("declared_fields", set())
        if schema_info.get("has_schema")
        else set()
    )
    return [
        {"field_name": name, "field_type": infer_field_type(value), "required": False}
        for name, value in sorted(sample_values_by_field.items())
        if name not in declared
    ]


def build_update_schema_incremental_payload(
    entity_type: str, fields_to_add: list[dict]
) -> dict:
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
    sign_as: str | None = None,
) -> tuple[bool, dict]:
    """Apply fields_to_add to hosted -- register_schema for a no-schema type,
    update_schema_incremental for an existing one. Returns (ok, response).

    A WRITE call: when `sign_as` is given (the --apply path), this is signed
    as that AAuth sub via signed_write rather than sent with the bearer
    token -- operator ruling 2026-09-24 (ateles#1223), never a silent
    fallback to the shared bearer identity for a write.

    Callers MUST re-fetch the schema and verify the new fields are present
    (see verify_schema_extension_applied) before relying on them for a
    /store call -- this function reports what hosted's response said, not
    what is actually active, since register/update responses have been
    seen to report success while a subsequent read lagged (documented CLAUDE.md
    verification-discipline rule: "a write that reports success has not
    necessarily happened").
    """
    if schema_info.get("has_schema"):
        path = "/update_schema_incremental"
        payload = build_update_schema_incremental_payload(entity_type, fields_to_add)
    else:
        path = "/register_schema"
        payload = build_register_schema_payload(entity_type, fields_to_add)
    if sign_as:
        status, resp = signed_write("POST", base_url, path, sign_as, payload)
    else:
        status, resp = http_request("POST", base_url, path, token, payload)
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
        raise ValueError(
            f"{path}: expected a JSON list of entity records, got {type(data).__name__}"
        )
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


def local_post_cutover_field_state(
    conn: sqlite3.Connection, entity_id: str, cutover_ts: str
) -> dict:
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


def build_reconcile_idempotency_key(
    cutover_date: str, entity_id: str, field_names: list[str]
) -> str:
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
        idem_key = build_reconcile_idempotency_key(
            cutover_date, entity_id, sorted(fields_to_write.keys())
        )
    return entity_id, entity_type, fields_to_write, drifted_fields, idem_key


def reduce_array_fields_to_missing_entries(
    entity_id: str,
    entity_type: str,
    fields_to_write: dict,
    schema_info: dict,
    base_url: str,
    token: str,
) -> dict:
    """Reduce every merge_array-reducer (or otherwise list-valued) field in
    `fields_to_write` down to only the entries hosted's OBSERVATIONS don't
    already have, per neotoma#2341 -- the snapshot never reflects a
    merge_array write, so presence must come from POST /list_observations
    (hosted_array_field_present_keys), never from a snapshot-based hash
    comparison. A field with nothing missing is DROPPED from the returned
    dict (never sent with an empty/unchanged value -- neotoma#2033 means
    even a content-identical write still creates a new observation). Runs
    for every field whose name is in schema_info['merge_array_fields'], and
    -- defensively, since a field can be array-valued without this script's
    schema cache having classified it yet -- for any field whose CURRENT
    fields_to_write value is itself a Python list, so an array field on a
    no-schema entity_type is still protected. Non-array fields pass through
    untouched, still subject to the caller's own hash-based drift check.
    """
    merge_array_fields = schema_info.get("merge_array_fields", set()) if schema_info else set()
    reduced = dict(fields_to_write)
    for name, value in list(fields_to_write.items()):
        if name not in merge_array_fields and not isinstance(value, list):
            continue
        if not isinstance(value, list):
            # Declared merge_array on hosted but this run's local value isn't
            # a list -- fail closed, drop rather than guess how to diff it.
            reduced.pop(name, None)
            continue
        hosted_present_keys = hosted_array_field_present_keys(
            entity_id, name, base_url, token
        )
        missing_entries = plan_array_field_missing_entries(value, hosted_present_keys)
        if not missing_entries:
            reduced.pop(name, None)
        else:
            reduced[name] = missing_entries
    return reduced


def recheck_hosted_drift(
    entity_id: str,
    field_names: list[str],
    expected_hashes: dict,
    base_url: str,
    token: str,
    array_field_names: set | None = None,
) -> list[str]:
    """Re-fetch hosted's CURRENT value for each field and compare hashes.

    Returns the subset of field_names whose hosted value's hash no longer
    matches `expected_hashes[name]` (the reconciliation file's
    hosted_value_hash) -- i.e. hosted changed since the reconciliation was
    computed. Those fields must be skipped and logged as drifted rather
    than overwritten, per the brief. Uses GET /entities/<id> (a read) --
    never a write -- and only the fields this entity's plan actually
    touches are compared.

    `array_field_names` (neotoma#2341) names the fields this call's
    `fields_to_write` were already reduced to "missing entries only" by
    reduce_array_fields_to_missing_entries, upstream of this hash check --
    for those, `field_names`' VALUE is no longer the full field content the
    reconciliation.json's hosted_value_hash was computed against (a full
    array vs. a missing-entries sublist would never hash-match even with
    zero actual drift), so this function skips the hash comparison for them
    entirely and trusts the observations-based presence check that already
    ran. Every other (non-array) field is still hash-checked exactly as
    before.
    """
    array_field_names = array_field_names or set()
    status, body = http_request(
        "GET",
        base_url,
        f"/entities/{entity_id}",
        token,
        retries=3,
        retry_backoff_seconds=2.0,
    )
    if status != 200 or not isinstance(body, dict):
        # Can't verify -- treat every field as drifted (fail closed: skip
        # rather than write over a state we could not just confirm).
        return [n for n in field_names if n not in array_field_names]
    hosted_fields = body.get("fields") or body.get("entity", {}).get("fields") or {}
    drifted = []
    for name in field_names:
        if name in array_field_names:
            continue
        current_hash = (
            value_hash(hosted_fields.get(name)) if name in hosted_fields else None
        )
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


def build_store_payload_for_reconcile(
    entity_id: str, entity_type: str, fields_to_write: dict, idem_key: str
) -> dict:
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


def run_reconciliation(
    args, conn, base_url, token, cutover_date, apply_mode, entity_ids
) -> None:
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

    print_run_banner(
        mode="reconcile",
        source_paths=[args.db, args.reconcile_file],
        cutover=args.cutover,
        base_url=base_url,
        filters={"limit": args.limit, "entity_ids": args.entity_ids},
        schema_extension_policy="n/a (reconcile does not extend schemas)",
        apply_mode=apply_mode,
        log_path=args.log,
    )
    print(f"Loaded reconciliation file: {args.reconcile_file}")
    print(f"  entities in file (after --limit/--entity-ids): {len(entities)}")
    print()

    if not entities:
        print("NO_CHANGES: reconciliation file has no entities matching the filters.")
        print(f"Action log: {args.log}")
        return

    planned = []
    entities_with_nothing_to_apply = 0
    total_fields_planned = 0
    total_fields_drifted_local = 0
    total_array_fields_already_present = 0
    reconcile_schema_cache: dict = {}
    for entity_record in entities:
        entity_id, entity_type, fields_to_write, drifted_fields, idem_key = (
            plan_class_b_reconciliation_for_entity(
                entity_record, conn, args.cutover, cutover_date
            )
        )
        total_fields_drifted_local += len(drifted_fields)
        if not fields_to_write:
            entities_with_nothing_to_apply += 1
            continue

        # merge_array presence check (neotoma#2341): reduce any array-valued
        # writable field down to only the entries hosted's OBSERVATIONS don't
        # already have, BEFORE this entity's fields_to_write/idem_key are
        # finalized -- a field entirely present on hosted is dropped here so
        # it never contributes to entities_with_nothing_to_apply miscounting
        # it as "planned" nor to a redundant idempotency_key/write below.
        schema_info = get_schema_declared_fields(
            entity_type, base_url, token, reconcile_schema_cache
        )
        before_field_names = set(fields_to_write.keys())
        fields_to_write = reduce_array_fields_to_missing_entries(
            entity_id, entity_type, fields_to_write, schema_info, base_url, token
        )
        total_array_fields_already_present += len(
            before_field_names - set(fields_to_write.keys())
        )
        if not fields_to_write:
            entities_with_nothing_to_apply += 1
            continue
        idem_key = build_reconcile_idempotency_key(
            cutover_date, entity_id, sorted(fields_to_write.keys())
        )
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
        array_field_names = {
            name
            for name in fields_to_write
            if name in schema_info.get("merge_array_fields", set())
            or isinstance(fields_to_write[name], list)
        }
        planned.append(
            (
                entity_id,
                entity_type,
                fields_to_write,
                expected_hashes,
                idem_key,
                array_field_names,
            )
        )

    log_path = args.log

    if apply_mode and planned:
        # Pre-flight (ateles#1223 follow-up): reconcile writes /store for
        # every distinct entity_type in the filtered plan.
        needed_ops = {("store_structured", entity_type) for (_id, entity_type, *_rest) in planned}
        preflight_check_grant_covers_plan(
            base_url, token, args.sign_as, needed_ops, log_path=log_path
        )

    applied_entities = 0
    applied_fields = 0
    apply_time_drift_fields = 0
    run_counts = {
        "planned": len(planned),
        "applied": 0,
        "skipped": entities_with_nothing_to_apply,
        "deferred": 0,
        "failed": 0,
        "unresolved": 0,
    }
    with open(log_path, "a", encoding="utf-8") as log_fh:
        for (
            entity_id,
            entity_type,
            fields_to_write,
            expected_hashes,
            idem_key,
            array_field_names,
        ) in planned:
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

            drift_now = recheck_hosted_drift(
                entity_id,
                field_names,
                expected_hashes,
                base_url,
                token,
                array_field_names=array_field_names,
            )
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

            payload = build_store_payload_for_reconcile(
                entity_id, entity_type, fields_to_write, idem_key
            )
            status, resp = signed_write("POST", base_url, "/store", args.sign_as, payload)
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
                run_counts["failed"] += 1
                emit_error(
                    CODE_WRITE_FAILED,
                    f"reconcile write failed for entity_id={entity_id} "
                    f"entity_type={entity_type} http_status={status}",
                    writes_occurred=(applied_entities > 0),
                    log_path=log_path,
                    next_action="inspect the action log entry for this entity_id and "
                    "re-run the same command -- idempotency keys make this safe",
                )
            applied_entities += 1
            applied_fields += len(fields_to_write)
            run_counts["applied"] += 1
            time.sleep(0.05)

    print_run_summary(run_counts)
    if apply_mode and run_counts["planned"] > 0 and run_counts["applied"] == 0 and run_counts["failed"] == 0:
        print("NO_CHANGES: every planned entity resolved to a skip (nothing left to apply).")
    print()
    print("=== Reconciliation summary ===")
    print(
        f"Entities with nothing to apply (SAME/HOSTED_NEWER only): {entities_with_nothing_to_apply}"
    )
    print(f"Entities planned to write:                               {len(planned)}")
    print(
        f"Fields planned to write:                                 {total_fields_planned}"
    )
    print(
        f"Fields skipped (local value drifted from reconciliation.json): {total_fields_drifted_local}"
    )
    print(
        f"Array fields already fully present on hosted (observations-based, neotoma#2341): {total_array_fields_already_present}"
    )
    if apply_mode:
        print(
            f"Entities applied:                                        {applied_entities}"
        )
        print(
            f"Fields applied:                                          {applied_fields}"
        )
        print(
            f"Fields skipped (hosted drifted since reconciliation was computed): {apply_time_drift_fields}"
        )
    print(f"Action log: {log_path}")
    if not apply_mode:
        print("This was a DRY RUN. No data was written to hosted Neotoma.")
        if run_counts["planned"] > 0:
            print(
                f"To apply: {NEOTOMA_REPLAY_CONFIRM_APPLY_VAR}=yes "
                f"{build_apply_hint(args.mode, sys.argv[1:])}"
            )
        else:
            print("NO_CHANGES: nothing planned -- no apply hint to print.")


# --- Gate restore (--gate-restore, operator-approved 2026-09-23) -----------
#
# Separate from both the class-a replay and the --reconcile-file pass above:
# restores `issue` entities' gate fields (gate_status, owner_history,
# current_owner) that Cursor-dispatched swarm agents wrote to the LOCAL
# fork(s) by mistake (an MCP config pointing at a local dev build instead of
# hosted) back onto the hosted instance. Operator approved this specific
# write on 2026-09-23: issue entities' gate fields only -- no other field is
# ever read or written by this mode.
#
# Method: scan both local DBs (read-only) for issue observations with
# created_at > cutover that touch any of the three gate fields, fold them
# forward per entity_id (last observation wins per field, across BOTH dbs
# combined and sorted by created_at) to get each issue's latest local state,
# then for each issue GET hosted by entity id (falling back to a
# best-effort canonical repo+number match only if that 404s), and MERGE:
#   - gate_status: per-gate, keep local only if hosted's gate is absent or
#     strictly less advanced by GATE_STATUS_RANK; a status not on the rank
#     table is treated conservatively (never used to justify an overwrite
#     unless hosted's own value for that gate is literally absent).
#   - owner_history: union entries deduped on (agent, timestamp-or-note),
#     sorted by time.
#   - current_owner: local wins only if local's latest write of that field
#     is newer than hosted's latest write of it (compared via each side's
#     own observation/provenance history).
# gate_status/current_owner use `strategy: last_write` on the hosted schema
# (fetched from GET /schemas/issue, not assumed), so the full merged map
# must always be sent -- a store call replaces the field, it does not patch
# individual keys within it.

LEGACY_GATE_STATUS_FIELD = (
    "gate_status"  # vocab-ok: compatibility read/write for the one-off migration
)

GATE_STATUS_RANK = {
    "pending": 0,
    "in_review": 1,
    "legacy-uninitialized": 1,
    "changes_requested": 2,
    "blocked": 2,
    "spec_signed": 2,
    "skipped": 3,
    "waived": 3,
    "approved_awaiting_merge": 3,
    "signed_off": 4,
    "not_required": 4,
}


def gate_status_rank(value) -> int | None:
    """Return GATE_STATUS_RANK[value], or None for an unranked/unknown status.

    None is a deliberate "I don't know how advanced this is" signal, not a
    rank of 0 -- treating an unrecognized status as rank 0 would let it be
    silently overwritten by anything, and treating it as infinitely advanced
    would let it silently block every local value. Callers that see None
    fall back to the "hosted absent" test only (see merge_gate_status).
    """
    if not isinstance(value, str):
        return None
    return GATE_STATUS_RANK.get(value)


def merge_gate_status(
    hosted_gate_status: dict, local_gate_status: dict
) -> tuple[dict, list[tuple[str, object, object]]]:
    """Merge one issue's gate_status maps. Returns (merged, changes).

    changes is a list of (gate_name, hosted_value, new_value) for every gate
    whose value actually changes -- used for the dry-run report and the
    sanity gate. NEVER downgrades a hosted gate (a local rank <= hosted rank
    is skipped) and NEVER touches a gate absent from local_gate_status at
    all (hosted's untouched gates are carried through unchanged). A gate
    with an unranked local status is taken only when hosted has no value at
    all for that gate; an unranked hosted status is left alone regardless
    of what local says (fail closed: don't overwrite a status we can't
    rank the advancement of).
    """
    merged = dict(hosted_gate_status or {})
    changes: list[tuple[str, object, object]] = []
    for gate, local_val in (local_gate_status or {}).items():
        hosted_val = merged.get(gate)
        if gate not in merged:
            merged[gate] = local_val
            changes.append((gate, None, local_val))
            continue
        if hosted_val == local_val:
            continue
        hosted_rank = gate_status_rank(hosted_val)
        local_rank = gate_status_rank(local_val)
        if hosted_rank is None:
            # Hosted holds a status this table doesn't recognize -- fail
            # closed, never overwrite it from local.
            continue
        if local_rank is None:
            # Local holds an unranked status but hosted has a ranked one --
            # never overwrite a known-ranked hosted value with an unranked
            # local one.
            continue
        if local_rank > hosted_rank:
            merged[gate] = local_val
            changes.append((gate, hosted_val, local_val))
        # local_rank <= hosted_rank: never downgrade, skip.
    return merged, changes


def _owner_history_entry_key(entry) -> tuple:
    """Dedup key for one owner_history entry: (agent, action, gate, timestamp-or-note).

    Real data (confirmed against local-fork rows, e.g. entity
    ent_f89b4fe5636ac3275d629c3e) shows the SAME agent can log MULTIPLE
    distinct entries at the exact same `at` timestamp -- a `signed_off` and
    a `handed_off` row for the same gate handoff both stamped at the moment
    the transition happened. A key of (agent, at) alone collapses these
    into one, discarding a real, distinct entry -- confirmed against real
    data: one entity's 355-entry local owner_history deduped down to 7
    under (agent, at) alone, which is data loss, not deduplication. Adding
    `action` and `gate` (both present on every real entry inspected)
    distinguishes exactly this case while still deduping TRUE duplicates
    (the same action+gate+agent+at appearing in both local DBs from the
    same underlying write). Falls back to the full entry's JSON
    representation when agent/action/gate/at/note are all absent, which
    keeps the key always hashable and always distinguishing for any
    still-unanticipated entry shape.
    """
    if not isinstance(entry, dict):
        return (json.dumps(entry, sort_keys=True),)
    agent = entry.get("agent")
    action = entry.get("action")
    gate = entry.get("gate")
    ts = entry.get("at") or entry.get("note")
    if agent is None and action is None and gate is None and ts is None:
        return (json.dumps(entry, sort_keys=True),)
    return (agent, action, gate, ts)


def merge_owner_history(hosted_history, local_history) -> list:
    """Union owner_history entries, dedup ONLY within local, then append onto
    hosted's history verbatim. Sorted by time within each side, hosted first.

    Deliberately does NOT dedup across hosted's own existing entries: real
    hosted data (entity ent_fec57fb48b3485bff6a24412) already carries an
    internal near-duplicate pair (the same agent/action/gate/at, one with an
    extra `note` field) -- a pre-existing hosted data quality issue, not
    something this replay caused or should silently collapse. This
    function's job is restoring local entries hosted is MISSING, never
    editing hosted's own history downward -- so the invariant
    len(merged) >= max(len(local), len(hosted)) always holds: every hosted
    entry survives untouched, and only genuinely-new local entries (deduped
    against each other AND against hosted, so a local entry hosted already
    has verbatim is not appended a second time) are added on top.
    """
    hosted_history = hosted_history if isinstance(hosted_history, list) else []
    local_history = local_history if isinstance(local_history, list) else []

    hosted_keys = {_owner_history_entry_key(e) for e in hosted_history}
    seen_local_keys: set = set()
    new_from_local = []
    for entry in local_history:
        key = _owner_history_entry_key(entry)
        if key in hosted_keys or key in seen_local_keys:
            continue
        seen_local_keys.add(key)
        new_from_local.append(entry)

    new_from_local.sort(
        key=lambda e: (e.get("at") or "") if isinstance(e, dict) else ""
    )
    return list(hosted_history) + new_from_local


def latest_field_write_ts(
    conn: sqlite3.Connection, entity_id: str, field_name: str, cutover_ts: str
) -> str | None:
    """Latest created_at (post-cutover) among this entity's LOCAL observations
    that set `field_name` -- used to compare against hosted's own latest
    write of current_owner (see merge_current_owner) to decide which side's
    value is actually newer, rather than assuming local always wins.
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT fields, created_at FROM observations WHERE entity_id = ? AND created_at > ? "
        "ORDER BY created_at DESC",
        (entity_id, cutover_ts),
    )
    for fields_json, created_at in cur.fetchall():
        if is_schema_lag_background_rewrite(fields_json):
            continue
        try:
            fields = json.loads(fields_json) if fields_json else {}
        except (TypeError, ValueError):
            continue
        if isinstance(fields, dict) and field_name in fields:
            return created_at
    return None


def hosted_latest_field_write_ts(
    entity_id: str, field_name: str, base_url: str, token: str
) -> str | None:
    """Latest observed_at for `field_name` on hosted, via GET /entities/<id>/history
    if available, falling back to the entity's own last_observation_at when a
    per-field history endpoint isn't available. Returns None on any failure
    (fail closed -- an unknown hosted timestamp means we cannot prove local
    is newer, so merge_current_owner will not overwrite it).
    """
    status, body = http_request(
        "GET",
        base_url,
        f"/entities/{entity_id}/field_history?field={field_name}",
        token,
        retries=1,
        retry_backoff_seconds=1.0,
    )
    if status == 200 and isinstance(body, dict):
        history = body.get("history") or body.get("observations") or []
        if isinstance(history, list) and history:
            timestamps = [
                h.get("observed_at") or h.get("created_at")
                for h in history
                if isinstance(h, dict)
            ]
            timestamps = [t for t in timestamps if t]
            if timestamps:
                return max(timestamps)
    # No per-field history route (or it errored/404s) -- fall back to the
    # entity snapshot's own last_observation_at as a conservative proxy for
    # "when was this entity, including this field, last written on hosted".
    status2, body2 = http_request(
        "GET",
        base_url,
        f"/entities/{entity_id}",
        token,
        retries=1,
        retry_backoff_seconds=1.0,
    )
    if status2 == 200 and isinstance(body2, dict):
        return body2.get("last_observation_at")
    return None


def merge_current_owner(
    hosted_current_owner,
    local_current_owner,
    local_ts: str | None,
    hosted_ts: str | None,
) -> tuple[object, bool]:
    """Decide current_owner. Returns (value_to_use, changed).

    Local wins ONLY when local_current_owner is present AND local_ts is
    present AND (hosted_ts is absent OR local_ts > hosted_ts) -- i.e. we can
    positively show local's write is newer. Any missing timestamp fails
    closed to keeping hosted's value, since we cannot prove local is newer
    without it.
    """
    if local_current_owner is None:
        return hosted_current_owner, False
    if local_ts is None:
        return hosted_current_owner, False
    if hosted_ts is not None and local_ts <= hosted_ts:
        return hosted_current_owner, False
    if hosted_current_owner == local_current_owner:
        return hosted_current_owner, False
    return local_current_owner, True


def local_gate_state_for_entity(
    conn: sqlite3.Connection, entity_id: str, cutover_ts: str
) -> dict:
    """Fold-forward this entity's LOCAL post-cutover observations for exactly
    the three gate fields plus its identifying fields (repo, github_number,
    local_issue_id, title) -- last write wins per field, schema_lag_bg_*
    rewrites excluded, same convention as local_post_cutover_field_state.
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT fields, created_at FROM observations WHERE entity_id = ? AND entity_type = 'issue' "
        "AND created_at > ? ORDER BY created_at ASC",
        (entity_id, cutover_ts),
    )
    state: dict = {}
    tracked = {
        LEGACY_GATE_STATUS_FIELD,
        "owner_history",
        "current_owner",
        "repo",
        "github_number",
        "local_issue_id",
        "title",
    }
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
            if k in tracked:
                state[k] = v
    return state


def scan_local_gate_candidates(db_paths: list[str], cutover_ts: str) -> dict:
    """Scan BOTH local DBs for issue entities with post-cutover writes to any
    gate field, and fold each entity's state forward ACROSS BOTH DBs
    combined (not per-db) so an entity touched in both gets one merged
    local view, ordered by created_at across the union.

    Returns entity_id -> {gate_status, owner_history, current_owner, repo,
    github_number, local_issue_id, title} (only keys actually seen).
    """
    combined_rows: list[
        tuple[str, str, str]
    ] = []  # (entity_id, fields_json, created_at)
    for db_path in db_paths:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        cur = conn.cursor()
        cur.execute(
            "SELECT entity_id, fields, created_at FROM observations "
            "WHERE entity_type = 'issue' AND created_at > ? "
            "AND (fields LIKE ? OR fields LIKE '%owner_history%' OR fields LIKE '%current_owner%') "
            "ORDER BY created_at ASC",
            (cutover_ts, f"%{LEGACY_GATE_STATUS_FIELD}%"),
        )
        combined_rows.extend(cur.fetchall())
        conn.close()
    combined_rows.sort(key=lambda r: r[2])

    tracked = {
        LEGACY_GATE_STATUS_FIELD,
        "owner_history",
        "current_owner",
        "repo",
        "github_number",
        "local_issue_id",
        "title",
    }
    states: dict[str, dict] = {}
    for entity_id, fields_json, _created_at in combined_rows:
        if is_schema_lag_background_rewrite(fields_json):
            continue
        try:
            fields = json.loads(fields_json) if fields_json else {}
        except (TypeError, ValueError):
            continue
        if not isinstance(fields, dict):
            continue
        st = states.setdefault(entity_id, {})
        for k, v in fields.items():
            if k in tracked:
                st[k] = v
    # Only keep entities that actually set at least one of the 3 gate fields
    # (an entity whose only tracked-field write was e.g. repo/title without
    # ever touching gate_status/owner_history/current_owner has nothing to
    # restore).
    gate_field_names = {LEGACY_GATE_STATUS_FIELD, "owner_history", "current_owner"}
    return {eid: st for eid, st in states.items() if gate_field_names & set(st.keys())}


def canonical_identity_lookup(
    entity_type: str, repo, github_number, base_url: str, token: str
) -> str | None:
    """Best-effort lookup of a hosted entity id by (repo, github_number) when
    a direct GET by local entity_id 404s. Tries POST /retrieve_entity_by_identifier
    with the schema's declared composite identifier shape; returns None on a
    CONFIRMED 200-with-no-match (an actual "no canonical entity exists"
    answer from hosted) -- callers treat that as "no canonical match found"
    and fall through to class-a create. Raises HostedProbeAmbiguousError
    (never returns None) on a 5xx/401/403/timeout that survives retrying:
    an ambiguous response here must not be read as "no match", since that
    silently sends this run down the class-a CREATE path for an issue that
    may already exist on hosted under this canonical identity (neotoma#2483 --
    the same class of bug entity_exists/get_hosted_entity fixed).

    identifier format is `<entity_type>:<github_number>|<repo>`, matching the
    `issue` schema's declared canonical_name_fields composite (repo -> the
    hosted /schemas/issue response documents `canonical_name_fields:
    [local_issue_id, {composite: [github_number, repo]}, title]`, and a real
    hosted entity's canonical_name is literally "issue:998|markmhendrickson/
    ateles" -- confirmed against a live 400 ERR_MERGE_REFUSED response
    during the 2026-09-23 gate-restore apply, which reported the SAME issue
    already existing on hosted under a DIFFERENT entity_id than this
    script's local fork held, with exactly that canonical_name). An earlier
    version of this function omitted the `<entity_type>:` prefix
    (`f"{github_number}|{repo}"`), which silently never matched --
    /retrieve_entity_by_identifier returned `{"entities": [], "total": 0,
    "match_mode": "none"}` for every call, so every canonical-identity
    fallback silently failed and fell through to attempting a create against
    an id hosted already used for a different entity under identity
    conflict. This bug caused a live ERR_MERGE_REFUSED (ateles#998) that
    halted a gate-restore apply run mid-batch.
    """
    if not repo or github_number is None:
        return None
    payload = {
        "entity_type": entity_type,
        "identifier": f"{entity_type}:{github_number}|{repo}",
    }
    # /retrieve_entity_by_identifier answers "no match" with a 200 carrying
    # an empty `entities` list (per the docstring's confirmed live example),
    # NOT a 404 -- so this probe's "confirmed" outcome for a genuine miss is
    # the 200 branch, and probe_status_tristate's 404 branch is not expected
    # to fire for this route in practice but is still handled the same way
    # (body inspected, no match -> None) for uniformity/safety. A 5xx/401/403
    # /timeout still raises via HostedProbeAmbiguousError rather than
    # silently falling through to "no canonical match" -- see docstring.
    _exists, _status, body = probe_status_tristate(
        "POST", base_url, "/retrieve_entity_by_identifier", token, payload,
        max_attempts=3, retry_backoff_seconds=2.0,
    )
    if isinstance(body, dict):
        entities = body.get("entities")
        if isinstance(entities, list) and entities:
            eid = entities[0].get("id") or entities[0].get("entity_id")
            if isinstance(eid, str) and eid and eid != "PLACEHOLDER":
                return eid
    return None


def get_hosted_entity(entity_id: str, base_url: str, token: str) -> dict | None:
    """Returns the entity dict on a confirmed 200, or None on a CONFIRMED 404
    (entity genuinely does not exist). Raises HostedProbeAmbiguousError on
    any other outcome (5xx, 401/403, timeout) after retrying -- callers must
    not treat that the same as a 404-confirmed absence (neotoma#2483: a 502
    during a hosted crash-loop is not evidence the entity is missing).
    """
    exists, _status, body = probe_status_tristate(
        "GET", base_url, f"/entities/{entity_id}", token, max_attempts=3, retry_backoff_seconds=2.0
    )
    return body if exists and isinstance(body, dict) else None


def build_gate_restore_idempotency_key(entity_id: str, merged_payload: dict) -> str:
    """migrate-gates-<entity_id>-<sha of the merged payload> -- deterministic
    so re-running after a partial failure (or a verification pass) sends the
    SAME key for the SAME merged result and resolves to a no-op on hosted,
    while a genuinely different merged result (e.g. a gate that was
    downgraded then re-upgraded) gets its own key.
    """
    payload_repr = json.dumps(merged_payload, sort_keys=True, ensure_ascii=False)
    payload_hash = hashlib.sha256(payload_repr.encode("utf-8")).hexdigest()[:16]
    return f"migrate-gates-{entity_id}-{payload_hash}"


def recheck_fields_against_current_hosted(
    entity_id: str, action: str, fields: dict, local_state: dict, base_url: str, token: str
) -> dict:
    """Re-fetch hosted's CURRENT state immediately before a write and drop
    any field from `fields` that is a no-op against that fresh read.

    Regression this exists to close (found live, 2026-09-23, during an
    idempotency re-run against ~130 already-applied issues): hosted's
    /store creates a new observation on EVERY call regardless of whether
    the field VALUE actually changes -- confirmed by a direct probe
    (neotoma#2033: observation_count went 112 -> 113 from a write whose
    payload was byte-identical to what was already stored, using a fresh
    idempotency_key). Since this script's idempotency_key is a hash of the
    merged payload, and other agents write to these same issues
    concurrently (this migration ran alongside live swarm activity), a
    plan computed against a SNAPSHOT of hosted taken minutes earlier can
    legitimately differ from hosted's state at write time in ways that are
    themselves already-applied by an earlier pass of this same script --
    re-running the merge against a FRESH hosted read is the only way to
    tell "genuinely new local data" from "the diff computed against a
    stale snapshot". This function is the fix: never trust `fields` as
    computed at plan time for a MERGE action -- re-derive it here against
    a live GET, and only send what that live comparison still calls a
    change.

    Does nothing for a CREATE action (there's no existing hosted state to
    re-diff against) or for `current_owner` (already re-decided fresh by
    its own dedicated lazy check in the apply loop; left untouched here).

    `owner_history` is a merge_array-reducer field (neotoma#2341): its
    presence is decided from hosted's OBSERVATIONS via
    hosted_array_field_present_keys, never from the snapshot the entity GET
    returns, because that snapshot never reflects a merge_array write. Only
    the entries genuinely missing from hosted's observations are kept; if
    none are missing, the field is dropped from the write entirely (an empty
    merge_array write is still a write -- neotoma#2033 -- so "nothing new"
    must mean "field absent from the payload", not "field present with an
    empty/unchanged list"). `gate_status` uses last_write, not merge_array,
    so it is unaffected by this bug and keeps reading the snapshot.
    """
    if action != "merge" or not fields:
        return fields
    if LEGACY_GATE_STATUS_FIELD not in fields and "owner_history" not in fields:
        return fields

    hosted_now = get_hosted_entity(entity_id, base_url, token)
    if hosted_now is None:
        # Entity vanished between planning and apply (shouldn't happen for
        # a merge target, but fail closed: send nothing rather than guess).
        return {
            k: v
            for k, v in fields.items()
            if k not in (LEGACY_GATE_STATUS_FIELD, "owner_history")
        }

    hosted_snapshot_now = hosted_now.get("snapshot") or {}
    rechecked = dict(fields)

    if LEGACY_GATE_STATUS_FIELD in fields:
        hosted_gate_status_now = (
            hosted_snapshot_now.get(LEGACY_GATE_STATUS_FIELD)
            if isinstance(hosted_snapshot_now.get(LEGACY_GATE_STATUS_FIELD), dict)
            else {}
        )
        local_gate_status = (
            local_state.get(LEGACY_GATE_STATUS_FIELD)
            if isinstance(local_state.get(LEGACY_GATE_STATUS_FIELD), dict)
            else {}
        )
        merged_now, changes_now = merge_gate_status(hosted_gate_status_now, local_gate_status)
        if not changes_now:
            rechecked.pop(LEGACY_GATE_STATUS_FIELD, None)
        else:
            rechecked[LEGACY_GATE_STATUS_FIELD] = merged_now

    if "owner_history" in fields:
        local_owner_history = local_state.get("owner_history") if isinstance(local_state.get("owner_history"), list) else []
        hosted_present_keys = hosted_array_field_present_keys(
            entity_id, "owner_history", base_url, token
        )
        missing_entries = plan_array_field_missing_entries(
            local_owner_history, hosted_present_keys
        )
        if not missing_entries:
            rechecked.pop("owner_history", None)
        else:
            # Send hosted's CURRENT full history (from the snapshot -- this
            # is still the correct base to append onto; only PRESENCE is
            # decided from observations) plus only the genuinely-missing
            # local entries, matching merge_owner_history's "hosted first,
            # new entries appended" shape.
            hosted_owner_history_now = (
                hosted_snapshot_now.get("owner_history")
                if isinstance(hosted_snapshot_now.get("owner_history"), list)
                else []
            )
            rechecked["owner_history"] = list(hosted_owner_history_now) + missing_entries

    return rechecked


def plan_gate_restore_for_entity(
    entity_id: str,
    local_state: dict,
    hosted_entity: dict | None,
    base_url: str | None = None,
    token: str | None = None,
) -> dict:
    """Compute one issue's gate-restore plan. Returns a dict describing the
    action (create/merge/noop) and, for merge/create, the full field set to
    send plus a human-readable list of per-gate changes.

    `owner_history` presence is decided from hosted's OBSERVATIONS (never the
    snapshot -- neotoma#2341, see hosted_array_field_present_keys), which
    means this function makes ONE extra read (POST /list_observations,
    paginated) per entity that has a hosted_entity and local owner_history
    entries to check. `base_url`/`token` are optional so existing pure-unit
    tests that only cover gate_status (last_write, unaffected by this bug)
    can keep calling this without a live/mocked HTTP layer; when omitted,
    owner_history falls back to the snapshot-only comparison (the pre-fix
    behavior) -- every real caller in this module passes them.
    """
    local_gate_status = (
        local_state.get(LEGACY_GATE_STATUS_FIELD)
        if isinstance(local_state.get(LEGACY_GATE_STATUS_FIELD), dict)
        else {}
    )
    local_owner_history = (
        local_state.get("owner_history")
        if isinstance(local_state.get("owner_history"), list)
        else []
    )
    local_current_owner = local_state.get("current_owner")

    if hosted_entity is None:
        fields = {}
        if local_gate_status:
            fields[LEGACY_GATE_STATUS_FIELD] = local_gate_status
        if local_owner_history:
            fields["owner_history"] = local_owner_history
        if local_current_owner is not None:
            fields["current_owner"] = local_current_owner
        for idfield in ("repo", "github_number", "local_issue_id", "title"):
            if local_state.get(idfield) is not None:
                fields[idfield] = local_state[idfield]
        return {
            "action": "create",
            "entity_id": entity_id,
            "fields": fields,
            "gate_changes": [
                (g, None, v) for g, v in sorted(local_gate_status.items())
            ],
            "owner_history_changed": bool(local_owner_history),
            "current_owner_change": (None, local_current_owner)
            if local_current_owner is not None
            else None,
        }

    hosted_snapshot = hosted_entity.get("snapshot") or {}
    hosted_gate_status = (
        hosted_snapshot.get(LEGACY_GATE_STATUS_FIELD)
        if isinstance(hosted_snapshot.get(LEGACY_GATE_STATUS_FIELD), dict)
        else {}
    )
    hosted_owner_history = (
        hosted_snapshot.get("owner_history")
        if isinstance(hosted_snapshot.get("owner_history"), list)
        else []
    )
    hosted_current_owner = hosted_snapshot.get("current_owner")

    merged_gate_status, gate_changes = merge_gate_status(
        hosted_gate_status, local_gate_status
    )

    if base_url is not None and token is not None:
        hosted_present_keys = hosted_array_field_present_keys(
            entity_id, "owner_history", base_url, token
        )
        missing_entries = plan_array_field_missing_entries(
            local_owner_history, hosted_present_keys
        )
        owner_history_changed = bool(missing_entries)
        merged_owner_history = list(hosted_owner_history) + missing_entries
    else:
        # No hosted/token supplied (pure-unit-test call site) -- fall back
        # to the snapshot-only comparison; every real caller passes both.
        merged_owner_history = merge_owner_history(
            hosted_owner_history, local_owner_history
        )
        owner_history_changed = merged_owner_history != hosted_owner_history

    fields = {}
    if gate_changes:
        fields[LEGACY_GATE_STATUS_FIELD] = merged_gate_status
    if owner_history_changed:
        fields["owner_history"] = merged_owner_history

    return {
        "action": "merge" if fields else "noop_pending_owner",
        "entity_id": entity_id,
        "fields": fields,
        "gate_changes": gate_changes,
        "owner_history_changed": owner_history_changed,
        "hosted_gate_status": hosted_gate_status,
        "hosted_owner_history": hosted_owner_history,
        "hosted_current_owner": hosted_current_owner,
        "local_current_owner": local_current_owner,
        "local_gate_status": local_gate_status,
        "local_owner_history": local_owner_history,
    }


def format_issue_label(repo, github_number, entity_id: str) -> str:
    if repo and github_number is not None:
        return f"{repo}#{github_number}"
    return entity_id


def run_gate_restore(args, base_url: str, token: str, apply_mode: bool) -> None:
    db_paths = [p.strip() for p in args.gate_restore_db.split(",") if p.strip()]
    cutover_ts = args.cutover
    run_ts = datetime.now(timezone.utc).isoformat()
    print_run_banner(
        mode="restore-gates",
        source_paths=db_paths,
        cutover=cutover_ts,
        base_url=base_url,
        filters={
            "limit": args.limit,
            "entity_ids": args.entity_ids,
            "exclude_entity_ids": getattr(args, "gate_restore_exclude_entity_ids", None),
        },
        schema_extension_policy="n/a (restore-gates does not extend schemas)",
        apply_mode=apply_mode,
        log_path=args.log,
    )
    print(
        f"Gate-restore run started at: {run_ts} (writes from agent runs after this instant are NOT covered)"
    )
    print()

    candidates = scan_local_gate_candidates(db_paths, cutover_ts)
    print(f"Issues with post-cutover local gate-field writes: {len(candidates)}")

    if args.entity_ids:
        allowlist = {e.strip() for e in args.entity_ids.split(",") if e.strip()}
        candidates = {eid: st for eid, st in candidates.items() if eid in allowlist}
        print(f"--entity-ids restricts this run to: {sorted(allowlist)}")
        print(f"Candidates after --entity-ids restriction: {len(candidates)}")

    if getattr(args, "gate_restore_exclude_entity_ids", None):
        excludelist = {e.strip() for e in args.gate_restore_exclude_entity_ids.split(",") if e.strip()}
        before = len(candidates)
        candidates = {eid: st for eid, st in candidates.items() if eid not in excludelist}
        print(f"--exclude-entity-ids excludes: {sorted(excludelist)}")
        print(f"Candidates after exclusion: {len(candidates)} (was {before})")

    if not candidates:
        print()
        print("NO_CHANGES: no issues have post-cutover local gate-field writes matching the filters.")
        print(f"Action log: {args.log}")
        return

    schema_info_cache: dict = {}
    get_schema_declared_fields("issue", base_url, token, schema_info_cache)

    # --- Identity filter (operator instruction 2026-09-23) ----------------
    # Only restore issues that have a resolvable (repo, github_number) AND
    # that identity actually exists on GitHub. This is checked BEFORE any
    # hosted GET or plan is built -- it is a precondition on the candidate
    # itself, not on what hosted happens to hold. `gh api
    # repos/<repo>/issues/<n>` confirms existence AND gives state (open/
    # closed) in the SAME call, so this replaces the separate closed-check
    # that used to run after the sanity gate: an issue that is CLOSED never
    # even becomes a plan, and is logged as a skip alongside every other
    # identity failure. One call per candidate, strictly sequential (load
    # rule: concurrency 1), so this is the dominant cost of a dry run --
    # accepted, since correctness here is the point of this filter.
    skips_no_identity = []
    skips_not_found_on_github = []
    skips_closed = []
    filtered_candidates: dict[str, dict] = {}
    for entity_id, local_state in sorted(candidates.items()):
        repo = local_state.get("repo")
        github_number = local_state.get("github_number")

        if (not repo or github_number is None) and entity_id != "test":
            # This entity's gate-field-bearing observations didn't happen to
            # also carry repo/github_number in the SAME observation (they're
            # separate fields on the same entity, written at different
            # times) -- but the entity may still have a resolvable identity
            # on hosted (its snapshot folds in fields from ALL of its
            # observations, not just the gate-bearing ones this scan
            # matched). One extra read-only GET, only for entities missing
            # identity locally, recovers it rather than skipping a real
            # issue for a scan artifact. `entity_id == "test"` is excluded
            # from this fallback outright -- it is test/junk data, not a
            # real issue, and a GET for it would be wasted.
            hosted_probe = get_hosted_entity(entity_id, base_url, token)
            if hosted_probe:
                probe_snapshot = hosted_probe.get("snapshot") or {}
                repo = (
                    repo
                    or probe_snapshot.get("repo")
                    or probe_snapshot.get("repository")
                )
                github_number = (
                    github_number
                    if github_number is not None
                    else (
                        probe_snapshot.get("github_number")
                        or probe_snapshot.get("issue_number")
                    )
                )

        label = format_issue_label(repo, github_number, entity_id)

        if (
            not repo
            or github_number is None
            or not isinstance(repo, str)
            or "/" not in repo
        ):
            skips_no_identity.append(
                (entity_id, label, "no resolvable repo+github_number")
            )
            continue

        gh_info = github_issue_lookup(repo, int(github_number))
        if gh_info is None:
            skips_not_found_on_github.append(
                (entity_id, label, "gh api lookup failed or issue not found")
            )
            continue
        if gh_info.get("state") == "closed":
            skips_closed.append((entity_id, label, "issue is CLOSED on GitHub"))
            continue

        # Write the (possibly hosted-recovered) identity back onto
        # local_state so the plan-building loop below -- which re-reads
        # local_state.get("repo")/("github_number") independently -- sees
        # the same resolved identity this filter just validated, rather
        # than re-deriving from local_state's original (possibly empty)
        # values.
        local_state = dict(local_state)
        local_state["repo"] = repo
        local_state["github_number"] = github_number
        filtered_candidates[entity_id] = local_state

    print(f"Skipped (no resolvable repo+github_number): {len(skips_no_identity)}")
    print(
        f"Skipped (not found on GitHub):               {len(skips_not_found_on_github)}"
    )
    print(f"Skipped (CLOSED on GitHub):                  {len(skips_closed)}")
    for _eid, label, reason in (
        skips_no_identity + skips_not_found_on_github + skips_closed
    ):
        print(f"  SKIP {label}: {reason}")
    print(f"Candidates passing identity filter: {len(filtered_candidates)}")
    print()

    plans = []
    creations = 0
    merges = 0
    noops = 0

    for entity_id, local_state in sorted(filtered_candidates.items()):
        hosted_entity = get_hosted_entity(entity_id, base_url, token)
        repo = local_state.get("repo")
        github_number = local_state.get("github_number")

        if hosted_entity is None:
            canon_id = canonical_identity_lookup(
                "issue", repo, github_number, base_url, token
            )
            if canon_id and canon_id != entity_id:
                hosted_entity = get_hosted_entity(canon_id, base_url, token)
                if hosted_entity is not None:
                    entity_id = canon_id  # write onto hosted's own id, not local's

        plan = plan_gate_restore_for_entity(
            entity_id, local_state, hosted_entity, base_url=base_url, token=token
        )
        plan["repo"] = repo
        plan["github_number"] = github_number
        plan["label"] = format_issue_label(repo, github_number, entity_id)

        if plan["action"] == "create":
            creations += 1
        elif plan["action"] == "merge":
            merges += 1
        else:
            noops += 1
        plans.append(plan)

    changed_plans = [p for p in plans if p["action"] in ("create", "merge")]

    # --- Print dry-run report --------------------------------------------
    dry_lines = []
    for p in changed_plans:
        label = p["label"]
        if p["action"] == "create":
            dry_lines.append(
                f"{label}: CREATE issue entity {p['entity_id']} with gate fields {sorted(p['fields'].keys())}"
            )
            for gate, old, new in p["gate_changes"]:
                dry_lines.append(f"  {label}: gate {gate}: (none) -> {new}")
        else:
            for gate, old, new in p["gate_changes"]:
                dry_lines.append(f"{label}: gate {gate}: {old!r} -> {new!r}")
            if p["owner_history_changed"]:
                before_n = len(p.get("hosted_owner_history") or [])
                after_n = len(p["fields"].get("owner_history") or [])
                dry_lines.append(
                    f"{label}: owner_history: {before_n} entries -> {after_n} entries (union)"
                )

    report_text = "\n".join(dry_lines) if dry_lines else "(no changes)"
    print()
    print("=== Dry-run report ===")
    print(report_text)

    dry_run_log_path = args.gate_restore_dry_log
    if dry_run_log_path:
        import os as _os

        _os.makedirs(_os.path.dirname(dry_run_log_path), exist_ok=True)
        with open(dry_run_log_path, "w", encoding="utf-8") as f:
            f.write(report_text + "\n")
        print(f"Dry-run report saved to: {dry_run_log_path}")

    # --- Sanity gate --------------------------------------------------
    # Note: the CLOSED-on-GitHub check happens earlier, in the identity
    # filter above -- an issue confirmed CLOSED there never becomes a plan,
    # so changed_plans here can never contain one. That filter also refuses
    # anything without a resolvable, GitHub-confirmed (repo, github_number)
    # identity, so there is nothing further to check here beyond the count.
    print()
    print("=== Sanity gate ===")
    print(
        f"Issues scanned (post-cutover, gate-field-bearing):        {len(candidates)}"
    )
    print(
        f"Issues passing identity filter (open, on GitHub):         {len(filtered_candidates)}"
    )
    print(f"Issues to CREATE on hosted (class-a, no hosted issue):    {creations}")
    print(f"Issues to MERGE on hosted (class-b, gate field upgrade):  {merges}")
    print(f"Issues with nothing to change:                            {noops}")

    total_changes = creations + merges
    if total_changes > 150:
        emit_error(
            CODE_SANITY_THRESHOLD,
            f"dry run proposes {total_changes} issue changes, over the "
            "150-issue sanity threshold",
            writes_occurred=False,
            log_path=dry_run_log_path,
            next_action=f"review the dry-run report at {dry_run_log_path or args.log} "
            "before re-running, and consider --limit or --entity-ids to narrow the run",
        )

    if apply_mode and total_changes > 0:
        # Pre-flight (ateles#1223 follow-up): restore-gates only ever writes
        # `issue` entities via /store -- a single-pair check, but run through
        # the shared helper so every mode fails closed the same way.
        preflight_check_grant_covers_plan(
            base_url, token, args.sign_as, {("store_structured", "issue")}, log_path=args.log
        )

    if not apply_mode:
        print()
        run_counts = {
            "planned": total_changes,
            "applied": 0,
            "skipped": noops,
            "deferred": 0,
            "failed": 0,
            "unresolved": 0,
        }
        print_run_summary(run_counts)
        if total_changes == 0:
            print("NO_CHANGES: no gate-restore writes are needed.")
        print("This was a DRY RUN. No data was written to hosted Neotoma.")
        if total_changes > 0:
            print(
                f"To apply: {NEOTOMA_REPLAY_CONFIRM_APPLY_VAR}=yes "
                f"{build_apply_hint(args.mode, sys.argv[1:])}"
            )
        return

    print()
    print("Sanity gate passed. Proceeding to apply.")
    print()

    log_path = args.log
    applied = 0
    failed = 0
    skipped_closed = 0
    request_count = 0
    run_counts = {
        "planned": total_changes,
        "applied": 0,
        "skipped": noops,
        "deferred": 0,
        "failed": 0,
        "unresolved": 0,
    }

    ok, hstatus = check_health(base_url, token)
    print(f"Health check (pre-apply): {'OK' if ok else 'FAILED'} (status={hstatus})")
    if not ok:
        emit_error(
            CODE_HOSTED_UNHEALTHY,
            "/health did not return a healthy 200 before apply began",
            writes_occurred=False,
            log_path=log_path,
            next_action="wait for hosted to recover, then re-run the same command "
            "-- idempotency keys make this safe",
        )

    with open(log_path, "a", encoding="utf-8") as log_fh:
        for p in changed_plans:
            entity_id = p["entity_id"]
            label = p["label"]
            fields = dict(p["fields"])

            # Load rule: check /health before each batch of
            # HEALTH_CHECK_BATCH_SIZE requests, abort the whole run on a
            # non-200/unhealthy response rather than continuing to write
            # against a degraded hosted instance. Counted against the
            # REQUEST count (GETs + the store POST), not just applies, so a
            # run heavy on current_owner freshness lookups (each an extra
            # GET) still checks health often enough.
            if request_count > 0 and request_count % HEALTH_CHECK_BATCH_SIZE == 0:
                ok, hstatus = check_health(base_url, token)
                print(f"Health check (after {request_count} requests): {'OK' if ok else 'FAILED'} (status={hstatus})")
                if not ok:
                    run_counts["applied"] = applied
                    run_counts["failed"] = failed
                    emit_error(
                        CODE_HOSTED_UNHEALTHY,
                        f"/health returned an unhealthy response after {request_count} requests",
                        writes_occurred=(applied > 0),
                        log_path=log_path,
                        next_action=f"wait for hosted to recover, then re-run the same "
                        f"command with --exclude-entity-ids for the {applied} issue(s) "
                        "already applied, or rely on idempotency and re-run unchanged",
                    )

            # current_owner is decided lazily here (needs a per-field
            # timestamp comparison, which is an extra hosted round-trip we
            # only pay for entities that are actually about to be written).
            if p["action"] == "merge" and local_state_current_owner_present(p):
                local_ts = None
                for conn_path in db_paths:
                    conn = sqlite3.connect(f"file:{conn_path}?mode=ro", uri=True)
                    ts = latest_field_write_ts(
                        conn, entity_id, "current_owner", cutover_ts
                    )
                    conn.close()
                    if ts and (local_ts is None or ts > local_ts):
                        local_ts = ts
                hosted_ts = hosted_latest_field_write_ts(
                    entity_id, "current_owner", base_url, token
                )
                request_count += 1  # hosted_latest_field_write_ts issues 1-2 GETs; counted conservatively as 1
                new_owner, changed = merge_current_owner(
                    p.get("hosted_current_owner"),
                    p.get("local_current_owner"),
                    local_ts,
                    hosted_ts,
                )
                if changed:
                    fields["current_owner"] = new_owner
                    print(
                        f"{label}: current_owner: {p.get('hosted_current_owner')!r} -> {new_owner!r} (local write newer)"
                    )

            if not fields:
                continue

            # Re-derive gate_status/owner_history against a FRESH hosted
            # read taken right now, not the snapshot the plan was computed
            # from -- see recheck_fields_against_current_hosted docstring.
            # This is what makes re-running the apply against an
            # already-applied batch converge to zero writes instead of
            # creating a fresh (duplicate-content) observation every time.
            local_state_for_recheck = candidates.get(entity_id) or filtered_candidates.get(entity_id) or {}
            issues_recheck_request = p["action"] == "merge" and (
                LEGACY_GATE_STATUS_FIELD in fields or "owner_history" in fields
            )
            fields = recheck_fields_against_current_hosted(
                entity_id, p["action"], fields, local_state_for_recheck, base_url, token
            )
            if issues_recheck_request:
                request_count += 1  # the recheck GET inside recheck_fields_against_current_hosted

            if not fields:
                print(f"SKIP (no-op against current hosted state): {label} entity={entity_id}")
                log_action(
                    log_fh,
                    kind="gate_restore",
                    local_id=entity_id,
                    entity_id=entity_id,
                    entity_type="issue",
                    entity_class=p["action"],
                    repo=p.get("repo"),
                    github_number=p.get("github_number"),
                    fields_written=[],
                    action="skipped_noop_against_current_hosted",
                    idempotency_key=None,
                    http_status=None,
                )
                continue

            merged_payload_for_key = fields
            idem_key = build_gate_restore_idempotency_key(
                entity_id, merged_payload_for_key
            )
            store_payload = {
                "entities": [build_entity_record("issue", entity_id, fields)],
                "idempotency_key": idem_key,
                "observation_source": "import",
            }
            status, resp = signed_write(
                "POST", base_url, "/store", args.sign_as, store_payload
            )
            request_count += 1
            ok = status in (200, 201)
            print(
                f"{'APPLIED' if ok else 'ERROR'}: {label} entity={entity_id} status={status}"
            )
            log_action(
                log_fh,
                kind="gate_restore",
                local_id=entity_id,
                entity_id=entity_id,
                entity_type="issue",
                entity_class=p["action"],
                repo=p.get("repo"),
                github_number=p.get("github_number"),
                fields_written=sorted(fields.keys()),
                action="applied" if ok else "apply_failed",
                idempotency_key=idem_key,
                http_status=status,
            )
            if not ok:
                failed += 1
                run_counts["applied"] = applied
                run_counts["failed"] = failed
                emit_error(
                    CODE_WRITE_FAILED,
                    f"gate-restore write failed for entity_id={entity_id} "
                    f"http_status={status}",
                    writes_occurred=(applied > 0),
                    log_path=log_path,
                    next_action="inspect the action log entry for this entity_id and "
                    "re-run the same command -- idempotency keys make this safe",
                )
            applied += 1
            time.sleep(0.1)

    run_counts["applied"] = applied
    run_counts["failed"] = failed
    print_run_summary(run_counts)
    print()
    print("=== Gate-restore apply summary ===")
    print(f"Applied: {applied}")
    print(f"Failed:  {failed}")
    print(f"Skipped (CLOSED on GitHub, gate change refused): {skipped_closed}")
    print(f"Action log: {log_path}")


def local_state_current_owner_present(plan: dict) -> bool:
    return plan.get("local_current_owner") is not None


def github_issue_lookup(repo: str, number: int) -> dict | None:
    """Read-only `gh api repos/<repo>/issues/<number>` lookup -- ONE call per
    issue, run strictly sequentially by the caller (never in parallel; load
    rule: concurrency 1 applies to hosted Neotoma, and this script treats
    GitHub the same way out of caution). Returns {"state": "open"|"closed",
    ...} on success, or None if the issue does not exist, the lookup fails
    (network, `gh` not authenticated, malformed repo/number), or the
    response is unparseable.

    None is a distinct outcome from state=="closed": a candidate whose
    GitHub existence cannot be confirmed is skipped for "not found on
    GitHub" (identity filter, operator instruction 2026-09-23), never
    silently treated as open. This is the SAME identity check used to
    filter out the ~106 unlabelled/test-junk entities the first dry run
    surfaced (no resolvable repo+github_number, or a repo+github_number
    that doesn't resolve to a real GitHub issue) -- `gh api` 404s on a
    nonexistent issue/repo, which this function reports as None just like
    any other lookup failure.
    """
    if not repo or number is None:
        return None
    try:
        result = subprocess.run(
            ["gh", "api", f"repos/{repo}/issues/{number}"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout)
        if not isinstance(data, dict) or "state" not in data:
            return None
        return data
    except Exception:
        return None


NEOTOMA_REPLAY_CONFIRM_APPLY_VAR = "NEOTOMA_REPLAY_CONFIRM_APPLY"

# Deprecated alias, per the Accipiter ux spec's engineering silence on
# whether to keep it: kept as a deprecated fallback (with a warning) rather
# than removed outright, so a caller mid-migration to the new name is not
# silently locked out. The new NEOTOMA_REPLAY_CONFIRM_APPLY_VAR is always
# checked first and is the only one referenced by help text/docs.
MIGRATE_CONFIRM_APPLY_DEPRECATED_VAR = "MIGRATE_CONFIRM_APPLY"

# Stable error/result codes (Accipiter ux spec, Error and empty states table).
CODE_ARGUMENT_CONFLICT = "E_ARGUMENT_CONFLICT"
CODE_CONFIRMATION_REQUIRED = "E_CONFIRMATION_REQUIRED"
CODE_HOSTED_STATE_UNKNOWN = "E_HOSTED_STATE_UNKNOWN"
CODE_SCHEMA_NOT_VERIFIED = "E_SCHEMA_NOT_VERIFIED"
CODE_HOSTED_UNHEALTHY = "E_HOSTED_UNHEALTHY"
CODE_WRITE_FAILED = "E_WRITE_FAILED"
CODE_NO_CHANGES = "NO_CHANGES"
CODE_DEFERRED = "DEFERRED"
# Not in the Accipiter ux spec's table (which enumerates the class-a/class-b
# replay error states) -- restore-gates' own pre-existing 150-issue sanity
# threshold, kept as a distinct code so it is never confused with a hosted
# health failure.
CODE_SANITY_THRESHOLD = "E_SANITY_THRESHOLD"

# Operator ruling 2026-09-24 (ateles#1223): every hosted WRITE this script
# performs in --apply mode must be signed as this agent sub, never the shared
# NEOTOMA_BEARER_TOKEN identity -- see resolve_signing_identity below and
# lib/daemon_runtime/neotoma_signed.py (prior art: PR #1181 / ateles#795).
DEFAULT_SIGN_AS_SUB = "ateles@ateles-swarm"

CODE_SIGNING_UNAVAILABLE = "E_SIGNING_UNAVAILABLE"


def resolve_signing_identity(sign_as: str) -> dict:
    """Resolve the AAuth signing identity for `sign_as` (an agent name, i.e.
    the part of the sub before '@', e.g. 'ateles' for 'ateles@ateles-swarm').

    Returns {"agent_name": ..., "sub": ..., "kid": ...} on success. Raises
    RuntimeError with a clear, actionable message on any failure -- missing
    lib/daemon_runtime import, no JWK key on disk, or via_cli disabled -- so
    callers can fail closed (E_SIGNING_UNAVAILABLE) rather than silently
    falling back to the bearer token for a write. This function NEVER reads,
    prints, or logs key material -- it only checks that a key file exists
    and asks neotoma_signed to resolve the identity dict it already exposes.
    """
    if _neotoma_signed is None:
        raise RuntimeError(
            "lib/daemon_runtime/neotoma_signed.py could not be imported -- "
            "signing is unavailable in this checkout"
        )
    agent_name = sign_as.split("@", 1)[0]
    ident = _neotoma_signed.agent_identity(agent_name)
    if ident is None:
        raise RuntimeError(
            f"no AAuth JWK key found for agent {agent_name!r} under "
            f"{_neotoma_signed.AAUTH_KEYS_DIR} (expected {agent_name}.jwk.json) -- "
            "signing is unavailable for this identity"
        )
    return {"agent_name": agent_name, "sub": ident["sub"], "kid": ident["kid"]}


def fetch_agent_grant_capabilities(
    base_url: str, token: str, sub: str
) -> list[dict] | None:
    """Read the hosted agent_grant for `sub` via the bearer token (a READ --
    checking the grant does not need to be signed) and return its
    capabilities list, or None if no active grant matches this sub.

    Uses GET /entities?entity_type=agent_grant, the same list route the
    prerequisite check used interactively, then filters client-side on
    match_sub -- there is no server-side filter-by-snapshot-field on this
    public route. Fails closed by raising on any non-2xx/malformed response
    rather than returning an empty grant that would read as "covers
    nothing" (which is the RIGHT failure mode for the pre-flight check
    below, but should be visibly a fetch failure, not silently "0 grants").
    """
    status, body = http_request(
        "GET", base_url, "/entities?entity_type=agent_grant&limit=200", token, retries=2,
        retry_backoff_seconds=2.0,
    )
    if status != 200 or not isinstance(body, dict):
        raise RuntimeError(
            f"could not fetch agent_grant list from hosted to run the pre-flight "
            f"capability check (status={status})"
        )
    for entity in body.get("entities", []):
        snap = entity.get("snapshot") or {}
        if snap.get("match_sub") == sub and snap.get("status") == "active":
            caps = snap.get("capabilities")
            return caps if isinstance(caps, list) else []
    return None


# Mirrors neotoma's server-side grantOpMatchesRequested (agent_capabilities.ts):
# a grant entry for "store" also covers a requested "store_structured" and
# vice versa -- the two names are the same family on hosted's admission path.
_STORE_OP_FAMILY = {"store", "store_structured"}


def _grant_op_matches(grant_op: str, requested_op: str) -> bool:
    if grant_op == requested_op:
        return True
    return grant_op in _STORE_OP_FAMILY and requested_op in _STORE_OP_FAMILY


def find_grant_gaps(
    capabilities: list[dict] | None, needed: set[tuple[str, str]]
) -> list[tuple[str, str]]:
    """Return the sorted list of (op, entity_type) pairs in `needed` that
    `capabilities` does NOT cover, mirroring hosted's entryCovers logic
    (op-family match, "*" entity_types wildcard). `capabilities=None` means
    no active grant was found for the sub at all -- every pair is a gap.
    """
    if capabilities is None:
        return sorted(needed)
    gaps = []
    for op, entity_type in needed:
        covered = False
        for cap in capabilities:
            if not _grant_op_matches(cap.get("op", ""), op):
                continue
            types = cap.get("entity_types") or []
            if "*" in types or entity_type in types:
                covered = True
                break
        if not covered:
            gaps.append((op, entity_type))
    return sorted(gaps)


def preflight_check_grant_covers_plan(
    base_url: str,
    token: str,
    sign_as: str,
    needed: set[tuple[str, str]],
    *,
    log_path: str | None,
) -> None:
    """Abort the run (emit_error, exit 1) before any write if the signing
    identity's hosted agent_grant does not cover every (op, entity_type)
    this run's plan needs. Operator ruling 2026-09-24 (ateles#1223 follow-up):
    an --apply run must fail closed on a grant gap BEFORE any write, rather
    than getting partway through a batch and refusing mid-run on whichever
    entity_type happens to come first.
    """
    if not needed:
        return
    try:
        capabilities = fetch_agent_grant_capabilities(base_url, token, sign_as)
    except RuntimeError as exc:
        emit_error(
            CODE_SIGNING_UNAVAILABLE,
            f"pre-flight grant-coverage check could not run: {exc}",
            writes_occurred=False,
            log_path=log_path,
            next_action="confirm hosted is reachable and the bearer token is "
            "valid, then re-run",
        )
        return
    gaps = find_grant_gaps(capabilities, needed)
    if not gaps:
        return
    gap_summary = ", ".join(f"{op}:{et}" for op, et in gaps[:20])
    if len(gaps) > 20:
        gap_summary += f", ... ({len(gaps) - 20} more)"
    emit_error(
        CODE_SIGNING_UNAVAILABLE,
        f"agent_grant for --sign-as={sign_as} does not cover {len(gaps)} "
        f"(op, entity_type) pair(s) this run's plan needs: {gap_summary}",
        writes_occurred=False,
        log_path=log_path,
        next_action=(
            f"extend the agent_grant for {sign_as} (match_sub) with the "
            "missing capabilities -- re-read the grant, MERGE (never "
            "overwrite) the full capabilities array, write it back, and "
            "read it back to confirm -- then re-run with --apply. This "
            "script refuses to start a write batch it cannot finish."
        ),
    )


def signed_write(
    method: str,
    base_url: str,
    path: str,
    sign_as: str,
    body: dict | None = None,
    timeout: int = HTTP_CLIENT_TIMEOUT_SECONDS,
) -> tuple[int, dict]:
    """Perform a hosted WRITE signed as `sign_as` (never the shared bearer).

    Thin wrapper around neotoma_signed.signed_request that enables the
    NEOTOMA_AAUTH_VIA_CLI feature flag for the duration of the call (the
    underlying module defaults it off) and normalizes failures into the same
    RuntimeError contract as resolve_signing_identity, so main() can catch
    one exception type and fail the run with E_SIGNING_UNAVAILABLE / stop on
    any write error, matching this script's existing "stop on any write
    error" behavior for the bearer path.
    """
    if _neotoma_signed is None:
        raise RuntimeError("neotoma_signed is unavailable -- cannot sign this write")
    agent_name = sign_as.split("@", 1)[0]
    prior = os.environ.get("NEOTOMA_AAUTH_VIA_CLI")
    os.environ["NEOTOMA_AAUTH_VIA_CLI"] = "1"
    try:
        return _neotoma_signed.signed_request(
            method, f"{base_url}{path}", body=body, agent_name=agent_name, timeout=timeout
        )
    finally:
        if prior is None:
            os.environ.pop("NEOTOMA_AAUTH_VIA_CLI", None)
        else:
            os.environ["NEOTOMA_AAUTH_VIA_CLI"] = prior


def emit_error(
    code: str,
    cause: str,
    *,
    writes_occurred: bool,
    log_path: str | None,
    next_action: str,
) -> None:
    """Print a stable, field-free error envelope to stderr and exit 1.

    Matches the Accipiter ux spec's required shape exactly: a stable code, a
    field-free cause summary, whether any writes occurred, the audit-log
    path, and one concrete next action. Never includes a response body or
    field/entity value -- only ids, paths, and this function's own fixed
    vocabulary are ever passed as `cause`/`next_action` by callers.
    """
    print(f"code={code}", file=sys.stderr)
    print(f"cause={cause}", file=sys.stderr)
    print(f"writes_occurred={'yes' if writes_occurred else 'no'}", file=sys.stderr)
    print(f"log_path={log_path or '(none)'}", file=sys.stderr)
    print(f"next_action={next_action}", file=sys.stderr)
    sys.exit(1)


def print_run_banner(
    *,
    mode: str,
    source_paths: list[str],
    cutover: str | None,
    base_url: str,
    filters: dict,
    schema_extension_policy: str,
    apply_mode: bool,
    log_path: str,
) -> None:
    """Field-free, pre-work banner (Accipiter ux spec item 8): mode, source
    path(s), cutover, hosted host only (never the token), filters, schema
    policy, dry-run/apply state, log path.
    """
    from urllib.parse import urlparse

    host = urlparse(base_url).netloc or base_url
    print("=== Run configuration ===")
    print(f"  mode: {mode}")
    print(f"  source path(s): {source_paths}")
    print(f"  cutover: {cutover}")
    print(f"  hosted host: {host}")
    filt_desc = ", ".join(f"{k}={v}" for k, v in filters.items() if v not in (None, "")) or "(none)"
    print(f"  filters: {filt_desc}")
    print(f"  schema-extension policy: {schema_extension_policy}")
    print(f"  apply/dry-run: {'APPLY (writing to hosted)' if apply_mode else 'DRY-RUN (no writes)'}")
    print(f"  log path: {log_path}")
    print()


def print_run_summary(counts: dict) -> None:
    """End-of-run summary counts (Accipiter ux spec item 9): planned,
    applied, skipped, deferred, failed, unresolved.
    """
    print()
    print("=== Run summary ===")
    for key in ("planned", "applied", "skipped", "deferred", "failed", "unresolved"):
        print(f"  {key}: {counts.get(key, 0)}")


def build_apply_hint(mode: str, argv: list[str]) -> str:
    """Reproduce the exact resolved argv for this run, preserving mode and
    every safety-relevant flag, then append only --apply (Accipiter ux spec
    item 10 / Eng CLI contract item 10). `argv` is the actual argv this
    process was invoked with (sys.argv[1:]), with any --dry-run token
    stripped (mutually exclusive with --apply) and --apply added if absent.
    """
    cleaned = [a for a in argv if a != "--dry-run"]
    if "--apply" not in cleaned:
        cleaned = cleaned + ["--apply"]
    return " ".join([sys.argv[0], *cleaned])


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__)
    subparsers = ap.add_subparsers(dest="mode")

    def add_shared_flags(sp: argparse.ArgumentParser) -> None:
        sp.add_argument(
            "--apply",
            action="store_true",
            help="Actually write to hosted. Default is dry-run.",
        )
        sp.add_argument(
            "--dry-run",
            action="store_true",
            help="Explicit dry-run (default behavior). Mutually exclusive with --apply.",
        )
        sp.add_argument(
            "--cutover",
            required=True,
            default=None,
            help="ISO-8601 cutover timestamp. Only rows with created_at > this value "
            "are considered. Required by every mode.",
        )
        sp.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Cap total number of entities processed (smoke test).",
        )
        sp.add_argument(
            "--entity-ids",
            default=None,
            help="Comma-separated allowlist of entity ids to restrict this run to (smoke tests).",
        )
        sp.add_argument(
            "--log",
            default="neotoma_local_fork_replay.jsonl",
            help="Path to append the JSONL action log to (default: ./neotoma_local_fork_replay.jsonl).",
        )
        sp.add_argument(
            "--sign-as",
            dest="sign_as",
            default=DEFAULT_SIGN_AS_SUB,
            help=(
                "AAuth sub every hosted WRITE this run performs is signed as "
                f"(default: {DEFAULT_SIGN_AS_SUB}, per the operator ruling "
                "2026-09-24 that this migration's writes are attributed to "
                "the ateles swarm identity, never the shared bearer token). "
                "In --apply mode, if this identity cannot be resolved to a "
                "local AAuth JWK key, the run refuses before any write "
                "(E_SIGNING_UNAVAILABLE) rather than falling back to the "
                "bearer token. Dry-run reads (existence probes, schema "
                "fetches) still use the bearer token regardless of this "
                "flag -- only writes are signed."
            ),
        )

    replay_sp = subparsers.add_parser(
        "replay",
        help="Class-a observation/relationship replay: writes rows written to the "
        "local fork after --cutover into hosted.",
    )
    add_shared_flags(replay_sp)
    replay_sp.add_argument(
        "--db",
        required=True,
        help="Path to the LOCAL SQLite copy to replay from. Must be a read-only, "
        "frozen fork; never a live writable database. No default -- every run "
        "must name its source explicitly.",
    )
    replay_sp.add_argument(
        "--only-missing",
        dest="only_missing",
        action="store_true",
        default=True,
        help="Probe GET /entities/<id> before writing and skip unless 404 (default: on).",
    )
    replay_sp.add_argument(
        "--no-only-missing",
        dest="only_missing",
        action="store_false",
        help="Disable the pre-write existence probe (not recommended for --apply).",
    )
    replay_sp.add_argument(
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
    replay_sp.add_argument(
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
    replay_sp.add_argument(
        "--no-extend-schemas",
        dest="extend_schemas",
        action="store_false",
        help="Disable schema auto-extension even under --apply.",
    )

    reconcile_sp = subparsers.add_parser(
        "reconcile",
        help="Class-b field-level reconcile: writes LOCAL_ONLY/LOCAL_NEWER fields "
        "from a --reconcile-file onto entities that already exist on hosted.",
    )
    add_shared_flags(reconcile_sp)
    reconcile_sp.add_argument(
        "--db",
        required=True,
        help="Path to the LOCAL SQLite copy to read current field values from. "
        "Must be a read-only, frozen fork.",
    )
    reconcile_sp.add_argument(
        "--reconcile-file",
        dest="reconcile_file",
        required=True,
        default=None,
        help=(
            "Path to a reconciliation.json (list of {id, entity_type, "
            "fields: [{name, classification, ...}]}) describing class-b "
            "field-level differences between the local fork and hosted. "
            "Writes LOCAL_ONLY/LOCAL_NEWER fields to hosted, one /store "
            "per entity with target_id + a deterministic idempotency_key. "
            "SAME and HOSTED_NEWER fields are always skipped. Re-verifies "
            "each writable field's hosted_value_hash immediately before "
            "writing and skips (logging entity_class=drifted) any field "
            "whose hosted value changed since the reconciliation file was "
            "computed."
        ),
    )

    gate_sp = subparsers.add_parser(
        "restore-gates",
        help="Restore issue entities' gate_status/owner_history/current_owner "
        "from local DB(s) onto hosted.",
    )
    add_shared_flags(gate_sp)
    gate_sp.add_argument(
        "--db",
        dest="gate_restore_db",
        required=True,
        default=None,
        help="Comma-separated local DB paths to scan. No default: every run "
        "must name its source explicitly (a prior revision silently defaulted "
        "to a fixed operator path; that default is removed).",
    )
    gate_sp.add_argument(
        "--exclude-entity-ids",
        dest="gate_restore_exclude_entity_ids",
        default=None,
        help=(
            "Comma-separated entity ids to EXCLUDE from this run "
            "(e.g. issues already applied in an earlier pilot run). Applied "
            "after --entity-ids, if both are given."
        ),
    )
    gate_sp.add_argument(
        "--dry-run-report",
        dest="gate_restore_dry_log",
        default=None,
        help="Path to save the dry-run report to (in addition to printing it). "
        "Was previously --gate-restore-dry-log.",
    )

    return ap


def resolve_confirm_apply_env() -> tuple[bool, bool]:
    """Check the apply double-guard. Returns (confirmed, used_deprecated_alias).

    NEOTOMA_REPLAY_CONFIRM_APPLY is checked first and is authoritative. The
    deprecated MIGRATE_CONFIRM_APPLY alias is honored ONLY when the new
    variable is absent, and a deprecation warning is printed whenever it is
    the one that supplied the confirmation -- the spec's engineering section
    was silent on whether to keep this alias, so it is kept (never silently
    dropping an in-flight operator invocation mid-migration) but clearly
    marked as deprecated.
    """
    new_val = os.environ.get(NEOTOMA_REPLAY_CONFIRM_APPLY_VAR)
    if new_val is not None:
        return new_val == "yes", False
    old_val = os.environ.get(MIGRATE_CONFIRM_APPLY_DEPRECATED_VAR)
    if old_val is not None:
        print(
            f"WARNING: {MIGRATE_CONFIRM_APPLY_DEPRECATED_VAR} is deprecated -- "
            f"use {NEOTOMA_REPLAY_CONFIRM_APPLY_VAR}=yes instead. The old name "
            "will stop being honored in a future revision.",
            file=sys.stderr,
        )
        return old_val == "yes", True
    return False, False


def main() -> None:
    ap = build_arg_parser()
    argv = sys.argv[1:]
    args = ap.parse_args(argv)

    if args.mode is None:
        emit_error(
            CODE_ARGUMENT_CONFLICT,
            "no subcommand given -- choose one of: replay, reconcile, restore-gates",
            writes_occurred=False,
            log_path=None,
            next_action="re-run with one of: replay | reconcile | restore-gates "
            "(see --help for each mode's flags)",
        )

    if args.apply and args.dry_run:
        emit_error(
            CODE_ARGUMENT_CONFLICT,
            "--apply and --dry-run are mutually exclusive",
            writes_occurred=False,
            log_path=getattr(args, "log", None),
            next_action="pass exactly one of --apply or --dry-run (or neither, "
            "which defaults to dry-run)",
        )

    apply_mode = bool(args.apply)  # dry-run is the default; --apply is the only way to write

    if apply_mode:
        confirmed, _used_deprecated = resolve_confirm_apply_env()
        if not confirmed:
            emit_error(
                CODE_CONFIRMATION_REQUIRED,
                f"--apply requires {NEOTOMA_REPLAY_CONFIRM_APPLY_VAR}=yes set "
                "explicitly in the environment",
                writes_occurred=False,
                log_path=getattr(args, "log", None),
                next_action=f"re-run with {NEOTOMA_REPLAY_CONFIRM_APPLY_VAR}=yes "
                f"{build_apply_hint(args.mode, argv)}",
            )

        # Operator ruling 2026-09-24 (ateles#1223): every hosted write this
        # migration performs must be signed as args.sign_as, never the shared
        # bearer token. Checked BEFORE any HTTP call (matching the
        # confirm-apply double-guard's own placement above) -- an --apply run
        # with no usable signing identity refuses outright rather than
        # silently writing under the bearer's shared attribution.
        try:
            resolve_signing_identity(args.sign_as)
        except RuntimeError as exc:
            emit_error(
                CODE_SIGNING_UNAVAILABLE,
                f"--apply requires a usable AAuth signing identity for "
                f"--sign-as={args.sign_as}, but none is available: {exc}",
                writes_occurred=False,
                log_path=getattr(args, "log", None),
                next_action=(
                    "mint/verify the AAuth JWK key for this agent sub "
                    "(see docs/aauth.md) and confirm an active agent_grant "
                    "exists for it on hosted, then re-run with --apply. "
                    "This script never falls back to NEOTOMA_BEARER_TOKEN "
                    "for a write."
                ),
            )

    if getattr(args, "mode", None) == "replay" and args.extend_schemas is None:
        args.extend_schemas = bool(args.apply)

    base_url = get_base_url()
    token = get_token()

    if args.mode == "restore-gates":
        run_gate_restore(args, base_url, token, apply_mode)
        return

    if args.mode == "reconcile":
        conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
        entity_ids = None
        if args.entity_ids:
            entity_ids = {e.strip() for e in args.entity_ids.split(",") if e.strip()}
        cutover_date = args.cutover.split("T")[0].replace("-", "")
        run_reconciliation(
            args, conn, base_url, token, cutover_date, apply_mode, entity_ids
        )
        return

    # args.mode == "replay"
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

    if apply_mode:
        # Pre-flight (ateles#1223 follow-up): compute every (op, entity_type)
        # this run's ACTUAL filtered plan needs -- store_structured for each
        # observation's entity_type, create_relationship for each
        # relationship's endpoint entity_types (resolved from this same
        # local fork's observations table, entity_id -> entity_type) -- and
        # abort before any write if the signing identity's grant does not
        # cover all of them. Computed from the filtered obs/rels (after
        # --limit/--entity-ids), matching what this run will actually try
        # to write, not the whole local DB.
        needed_ops: set[tuple[str, str]] = {("store_structured", row[2]) for row in obs}
        endpoint_type_by_id = {row[1]: row[2] for row in obs}
        cur_for_rel_types = conn.cursor()
        rel_entity_ids = {eid for row in rels for eid in (row[3], row[4])}
        unresolved_rel_ids = rel_entity_ids - set(endpoint_type_by_id)
        if unresolved_rel_ids:
            cur_for_rel_types.execute(
                "SELECT DISTINCT entity_id, entity_type FROM observations "
                "WHERE entity_id IN ({})".format(
                    ",".join("?" for _ in unresolved_rel_ids)
                ),
                tuple(unresolved_rel_ids),
            )
            endpoint_type_by_id.update(dict(cur_for_rel_types.fetchall()))
        for row in rels:
            for eid in (row[3], row[4]):
                et = endpoint_type_by_id.get(eid)
                if et:
                    needed_ops.add(("create_relationship", et))
        preflight_check_grant_covers_plan(
            base_url, token, args.sign_as, needed_ops, log_path=args.log
        )

    print_run_banner(
        mode="replay",
        source_paths=[args.db],
        cutover=args.cutover,
        base_url=base_url,
        filters={"limit": args.limit, "entity_ids": args.entity_ids},
        schema_extension_policy=(
            "on" if args.extend_schemas else "off"
        ),
        apply_mode=apply_mode,
        log_path=args.log,
    )
    print(f"Loaded from {args.db}:")
    print(f"  observations candidates (class a+b):   {len(obs)}")
    print(f"  excluded schema_lag_bg_* rewrites:     {len(excluded_schema_lag)}")
    print(f"  relationship_observations candidates:  {len(rels)}")
    print(f"  sources candidates:                    {len(srcs)}")
    print(f"  only-missing probe: {'on' if args.only_missing else 'off'}")
    print(f"  unknown-fields policy: {args.unknown_fields_policy}")
    print()

    if not obs and not rels and not srcs:
        print("NO_CHANGES: nothing to replay (no post-cutover rows matched the filters).")
        print(f"Action log: {args.log}")
        return

    plan = []
    entity_cache: dict[str, bool] = {}
    schema_cache: dict[str, dict] = {}
    # merge_array presence tracking for class-a (neotoma#2341): when a
    # newly-missing entity has MULTIPLE post-cutover local observations that
    # each touch the same array field (a real shape in this data -- one
    # /store call per local observation row), the first write's entries must
    # not be re-sent by a later observation for the same (entity_id, field)
    # in the SAME run. Seeded from hosted's observations at first use (in
    # case a prior partial run already created the entity and wrote some
    # entries) and then updated locally as this run's own writes land, so it
    # never needs a fresh hosted read for every observation of the same
    # entity/field.
    array_field_sent_keys: dict[tuple[str, str], set] = {}

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
            schema_info = schema_cache.get(
                entity_type, {"has_schema": False, "declared_fields": set()}
            )
            fields = json.loads(fields_json) if fields_json else {}
            stripped_fields, _stripped = strip_reserved_fields(
                entity_type, fields, schema_info
            )
            for name, value in stripped_fields.items():
                declared = (
                    schema_info.get("declared_fields", set())
                    if schema_info.get("has_schema")
                    else set()
                )
                if name not in declared:
                    undeclared_samples.setdefault(entity_type, {}).setdefault(
                        name, value
                    )

        for entity_type, samples in sorted(undeclared_samples.items()):
            schema_info = schema_cache.get(
                entity_type, {"has_schema": False, "declared_fields": set()}
            )
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
            ok, resp = extend_hosted_schema(
                entity_type, fields_to_add, schema_info, base_url, token,
                sign_as=args.sign_as,
            )
            if not ok:
                print(
                    f"  ERROR extending schema for {entity_type}: "
                    f"{resp.get('error') if isinstance(resp, dict) else resp}",
                    file=sys.stderr,
                )
                sys.exit(1)
            expected_names = [f["field_name"] for f in fields_to_add]
            verified, missing = verify_schema_extension_applied(
                entity_type, expected_names, base_url, token
            )
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
            schema_extension_results[entity_type] = {
                "applied": True,
                "fields": expected_names,
            }
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
        schema_info = schema_cache.get(
            entity_type, {"has_schema": False, "declared_fields": set()}
        )
        payload, idem_key, stripped_keys = build_observation_payload(
            row, cutover_date, schema_info=schema_info
        )
        (entity_record,) = payload["entities"]
        entity_fields = {
            k: v
            for k, v in entity_record.items()
            if k not in ("entity_type", "target_id")
        }
        if not schema_info.get("has_schema"):
            stats["no_schema_type_counts"][entity_type] = (
                stats["no_schema_type_counts"].get(entity_type, 0) + 1
            )
        predicted_unknown = predict_unknown_fields(
            entity_type, entity_fields, schema_info
        )
        if predicted_unknown:
            bucket = stats["predicted_unknown_field_warnings"].setdefault(
                entity_type, {}
            )
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
    run_counts = {
        "planned": len(plan),
        "applied": 0,
        "skipped": 0,
        "deferred": 0,
        "failed": 0,
        "unresolved": 0,
    }
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
                    run_counts["unresolved"] += 1
                    emit_error(
                        CODE_SCHEMA_NOT_VERIFIED,
                        f"predicted UNKNOWN_FIELD field(s) {predicted_unknown} for "
                        f"entity_type={entity_type} under --unknown-fields=stop (default)",
                        writes_occurred=(run_counts["applied"] > 0),
                        log_path=log_path,
                        next_action="re-run with --unknown-fields=warn once reviewed, "
                        "or register the field on hosted first",
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
                run_counts["skipped"] += 1
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

            # merge_array presence check (neotoma#2341), class-a path: even
            # though this observation's entity is "a_missing" (probed 404
            # before ANY write this run), a SECOND post-cutover local
            # observation for the SAME newly-created entity can carry the
            # same array-field entries as the first one this run already
            # sent -- the probe only ran once, before either write, so it
            # cannot see that. Reduce any schema-declared merge_array (or
            # locally list-typed) field in this payload down to entries not
            # already sent this run / present on hosted; drop the field
            # entirely (never send an empty array write -- neotoma#2033) if
            # nothing is left, and skip this whole observation's write only
            # if that leaves it with no entity fields and no other kind of
            # change to make.
            if kind == "observation":
                (entity_record_for_reduction,) = payload["entities"]
                schema_info_for_reduction = schema_cache.get(
                    entity_type, {"has_schema": False, "declared_fields": set(), "merge_array_fields": set()}
                )
                merge_array_field_names = schema_info_for_reduction.get("merge_array_fields", set())
                array_keys_in_payload = [
                    k
                    for k, v in entity_record_for_reduction.items()
                    if k not in ("entity_type", "target_id")
                    and (k in merge_array_field_names or isinstance(v, list))
                ]
                for field_name in array_keys_in_payload:
                    cache_key = (target_desc, field_name)
                    if cache_key not in array_field_sent_keys:
                        array_field_sent_keys[cache_key] = hosted_array_field_present_keys(
                            target_desc, field_name, base_url, token
                        )
                    already_present = array_field_sent_keys[cache_key]
                    local_value = entity_record_for_reduction[field_name]
                    if not isinstance(local_value, list):
                        # Declared/observed as array elsewhere but this
                        # observation's value isn't a list -- fail closed,
                        # drop rather than guess.
                        entity_record_for_reduction.pop(field_name, None)
                        continue
                    missing_entries = plan_array_field_missing_entries(
                        local_value, already_present
                    )
                    if not missing_entries:
                        entity_record_for_reduction.pop(field_name, None)
                    else:
                        entity_record_for_reduction[field_name] = missing_entries
                        already_present.update(
                            array_entry_dedupe_key(e) for e in missing_entries
                        )
                remaining_entity_keys = {
                    k
                    for k in entity_record_for_reduction
                    if k not in ("entity_type", "target_id")
                }
                if array_keys_in_payload and not remaining_entity_keys:
                    print(f"SKIP (array fields already fully present on hosted): {prefix}")
                    run_counts["skipped"] += 1
                    log_action(
                        log_fh,
                        kind=kind,
                        local_id=local_id,
                        entity_id=target_desc,
                        entity_type=entity_type,
                        entity_class=entity_class,
                        stripped_keys=stripped_keys,
                        action="skipped_array_fields_present",
                        idempotency_key=idem_key,
                        http_status=None,
                    )
                    continue

            print(f"APPLYING: {prefix}")
            if kind == "observation":
                status, resp = signed_write("POST", base_url, "/store", args.sign_as, payload)
            elif kind == "relationship":
                status, resp = signed_write(
                    "POST", base_url, "/create_relationship", args.sign_as, payload
                )
            elif kind == "source":
                print(
                    "  SKIP: source blob replay not implemented in this script -- "
                    "sources carry file content (storage_url/reference_path) that "
                    "needs its own re-upload path; flagged for manual/second-pass handling."
                )
                run_counts["deferred"] += 1
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
                run_counts["failed"] += 1
                emit_error(
                    CODE_WRITE_FAILED,
                    f"write failed for entity_id={target_desc} entity_type={entity_type} "
                    f"kind={kind} http_status={status}",
                    writes_occurred=(run_counts["applied"] > 0),
                    log_path=log_path,
                    next_action="inspect the action log entry for this entity_id and "
                    "re-run the same command -- idempotency keys make this safe",
                )
            if unknown_field_warnings and args.unknown_fields_policy == "stop":
                run_counts["unresolved"] += 1
                emit_error(
                    CODE_SCHEMA_NOT_VERIFIED,
                    f"UNKNOWN_FIELD store_warnings on a write for entity_id={target_desc} "
                    f"entity_type={entity_type}",
                    writes_occurred=True,
                    log_path=log_path,
                    next_action="re-run with --unknown-fields=warn once reviewed, "
                    "or register the field on hosted first",
                )
            elif unknown_field_warnings:
                print(
                    "  Continuing past UNKNOWN_FIELD store_warnings (--unknown-fields=warn)."
                )
            run_counts["applied"] += 1
            time.sleep(0.05)  # gentle rate limiting

    print_run_summary(run_counts)
    if apply_mode and run_counts["planned"] > 0 and run_counts["applied"] == 0 and run_counts["failed"] == 0:
        print("NO_CHANGES: every planned action resolved to a skip (nothing left to apply).")
    print()
    print("=== Detail ===")
    print(
        f"Entities touched (distinct entity_id across observations): {len(stats['entities_touched'])}"
    )
    print(f"Observations planned:                                      {len(obs)}")
    print(
        f"Observations excluded (schema_lag_bg_* rewrites):          {len(excluded_schema_lag)}"
    )
    print(
        f"Relationships among replayed entities (both endpoints):    {stats['relationships_among_replayed']}"
    )
    print(
        f"Relationships to a hosted-existing entity (one endpoint):  {stats['relationships_to_hosted_existing']}"
    )
    print(
        f"Relationships deferred (neither endpoint replayed here):   {stats['relationships_deferred']}"
    )
    print(f"Sources deferred (blob replay unimplemented):              {len(srcs)}")
    if stats["no_schema_type_counts"]:
        print("Entity types with no hosted schema (class-a obs count):")
        for et, n in sorted(
            stats["no_schema_type_counts"].items(), key=lambda x: -x[1]
        ):
            print(f"  {et}: {n}")
    if stats["predicted_unknown_field_warnings"]:
        print("Predicted UNKNOWN_FIELD store_warnings (pre-flight, by type):")
        for et, fields in sorted(stats["predicted_unknown_field_warnings"].items()):
            field_summary = ", ".join(
                f"{k}x{v}" for k, v in sorted(fields.items(), key=lambda x: -x[1])
            )
            print(f"  {et}: {field_summary}")
    else:
        print("Predicted UNKNOWN_FIELD store_warnings (pre-flight): none")
    if schema_extension_plan:
        label = (
            "Applied schema extensions"
            if apply_mode
            else "Planned schema extensions (dry-run, not sent)"
        )
        print(f"{label} (by type):")
        for et, fields_to_add in sorted(schema_extension_plan.items()):
            field_summary = ", ".join(
                f"{f['field_name']}:{f['field_type']}" for f in fields_to_add
            )
            print(f"  {et}: {field_summary}")
    else:
        print(
            "Schema extensions: none planned"
            if args.extend_schemas
            else "Schema extensions: --extend-schemas not set"
        )
    print()
    print(f"Total planned operations: {len(plan)}")
    print(f"Action log: {log_path}")
    if not apply_mode:
        print("This was a DRY RUN. No data was written to hosted Neotoma.")
        if run_counts["planned"] > 0:
            print(
                f"To apply: {NEOTOMA_REPLAY_CONFIRM_APPLY_VAR}=yes "
                f"{build_apply_hint(args.mode, argv)}"
            )
        else:
            print("NO_CHANGES: nothing planned -- no apply hint to print.")


if __name__ == "__main__":
    main()
