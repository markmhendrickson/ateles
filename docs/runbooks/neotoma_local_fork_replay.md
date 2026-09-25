# Runbook — Neotoma local-fork replay (`neotoma_local_fork_replay.py`)

## Purpose

A local stdio Neotoma MCP process can keep writing to a retired local SQLite
database after the operator migrates to a hosted Neotoma instance, if nothing
stops the old process from starting. Rows written to that local fork after
the cutover moment exist only there and need to be replayed forward into the
hosted instance — without duplicating data that already converged there
independently, and without silently dropping a write because the hosted API
returned something ambiguous.

`execution/scripts/neotoma_local_fork_replay.py` is that replay tool. It goes
through the public Neotoma HTTP API only (`POST /store`,
`POST /create_relationship`, schema GET/extend routes) — never raw SQL against
hosted, never an in-process storage function.

## Scope

Three explicit, mutually exclusive subcommands:

| Subcommand | What it does |
|---|---|
| `replay` | Class-a observation/relationship replay: writes rows the local fork has for entities/relationships that are missing (or, with `--no-only-missing`, unconditionally) on hosted. |
| `reconcile` | Class-b field-level reconcile: writes only the `LOCAL_ONLY`/`LOCAL_NEWER` fields named in a pre-computed `--reconcile-file` onto entities that already exist on hosted. |
| `restore-gates` | Restores `issue` entities' `gate_status`, `owner_history`, and `current_owner` from one or more local DBs onto hosted, using a last-write merge policy that never downgrades or shrinks hosted state. |

Out of scope for every mode: deleting anything from the local fork; cleaning
up pre-existing duplicate entries in array-typed fields (blocked on
[neotoma#2119](https://github.com/markmhendrickson/neotoma/issues/2119));
replaying file-backed source blobs (`sources` rows are logged as `DEFERRED`,
never silently dropped as if they succeeded).

## Prerequisites

- A **frozen, read-only** copy of the local SQLite fork. Never point this
  script at a live, writable database — every mode opens its `--db` path
  with SQLite's own read-only URI mode (`file:<path>?mode=ro`), but a
  database that is still being written by another process is not "frozen"
  in the sense this runbook means; copy it first.
- `NEOTOMA_BASE_URL` and `NEOTOMA_BEARER_TOKEN_PROD` (or
  `NEOTOMA_BEARER_TOKEN`) exported in the environment. No default base URL
  is hardcoded (this repo is public); the script refuses to start without
  both set.
- For `restore-gates`, `gh` authenticated with a token that can read the
  relevant repos (used for a read-only `gh api repos/<repo>/issues/<n>`
  identity check before any candidate becomes a write plan).

## Environment-variable setup (no real values shown)

```bash
export NEOTOMA_BASE_URL="https://<your-hosted-instance-host>"
export NEOTOMA_BEARER_TOKEN_PROD="<redacted>"
# Only for --apply, and only after reviewing a dry-run:
export NEOTOMA_REPLAY_CONFIRM_APPLY=yes
```

`NEOTOMA_REPLAY_CONFIRM_APPLY` is the confirmation variable. A deprecated
alias, `MIGRATE_CONFIRM_APPLY`, is honored only when the new variable is
absent, and using it prints a deprecation warning — prefer the new name.

## How to freeze and validate a read-only source database

```bash
# Copy, don't reference the live file directly.
cp ~/data/neotoma.db /tmp/neotoma-fork-frozen.db

# Sanity-check it opens read-only and has the expected tables.
sqlite3 "file:/tmp/neotoma-fork-frozen.db?mode=ro" \
  ".tables" # expect observations, relationship_observations, sources, ...
```

## Dry-run examples (every mode)

Dry-run is the default: omitting both `--apply` and `--dry-run` means
dry-run, and `--apply`+`--dry-run` together is refused before any network
request (`code=E_ARGUMENT_CONFLICT`).

```bash
# replay
python3 execution/scripts/neotoma_local_fork_replay.py replay \
  --db /tmp/neotoma-fork-frozen.db \
  --cutover 2026-08-04T00:00:00Z

# reconcile
python3 execution/scripts/neotoma_local_fork_replay.py reconcile \
  --db /tmp/neotoma-fork-frozen.db \
  --cutover 2026-08-04T00:00:00Z \
  --reconcile-file reconciliation.json

# restore-gates
python3 execution/scripts/neotoma_local_fork_replay.py restore-gates \
  --db /tmp/neotoma-fork-frozen.db,/tmp/neotoma-fork-frozen-2.db \
  --cutover 2026-08-04T00:00:00Z
```

Every dry-run prints a field-free configuration banner (mode, source
path(s), cutover, hosted **host only** — never the token, active filters,
schema-extension policy, dry-run/apply state, log path), then a plan, then
an end-of-run summary with counts for `planned`, `applied`, `skipped`,
`deferred`, `failed`, `unresolved`.

## Canary-apply examples

Apply only after reviewing the dry-run's plan and log, and only with
`--limit` on the first run against a batch of unfamiliar data:

```bash
NEOTOMA_REPLAY_CONFIRM_APPLY=yes python3 execution/scripts/neotoma_local_fork_replay.py replay \
  --db /tmp/neotoma-fork-frozen.db \
  --cutover 2026-08-04T00:00:00Z \
  --apply --limit 20

NEOTOMA_REPLAY_CONFIRM_APPLY=yes python3 execution/scripts/neotoma_local_fork_replay.py reconcile \
  --db /tmp/neotoma-fork-frozen.db \
  --cutover 2026-08-04T00:00:00Z \
  --reconcile-file reconciliation.json \
  --apply --limit 20

NEOTOMA_REPLAY_CONFIRM_APPLY=yes python3 execution/scripts/neotoma_local_fork_replay.py restore-gates \
  --db /tmp/neotoma-fork-frozen.db \
  --cutover 2026-08-04T00:00:00Z \
  --apply --limit 20
```

Every successful dry-run with `planned > 0` prints a **To apply:** line that
reproduces the exact resolved command for that run (mode, `--db`,
`--cutover`, and every other safety-relevant flag you passed) with only
`--apply` appended — copy that line rather than reconstructing the command
by hand.

## Using `--limit`, entity allowlists, exclusions, schema extension, and custom log paths

```bash
# Restrict to specific entities (smoke test).
... replay --db <path> --cutover <ts> --entity-ids ent_a,ent_b

# Exclude entities already applied in an earlier pilot run (restore-gates only).
... restore-gates --db <path> --cutover <ts> --exclude-entity-ids ent_c,ent_d

# Disable the pre-write existence probe for replay (not recommended for --apply).
... replay --db <path> --cutover <ts> --no-only-missing

# Disable schema auto-extension for replay (on by default under --apply).
... replay --db <path> --cutover <ts> --apply --no-extend-schemas

# Custom action-log path (default: ./neotoma_local_fork_replay.jsonl).
... replay --db <path> --cutover <ts> --log /tmp/my-run.jsonl
```

## `--on-extension-failure` and `--on-identity-conflict`: continuing past two known, narrow failure modes

Both default to `stop` (this script's existing fail-closed behavior) and are
`replay`-only. Reach for either only when the failure it names is the one
you are actually seeing — neither should be turned on pre-emptively "just in
case", since each widens what a run will silently pass over rather than
halt on.

- **`--on-extension-failure {stop,continue}`** — a schema-extension write
  (`POST /update_schema_incremental` or `/register_schema`, under
  `--extend-schemas`) can fail for a specific `entity_type` while every
  other type extends fine; hosted has been observed returning
  `DB_QUERY_FAILED` this way for a fixed set of types
  ([neotoma#2496](https://github.com/markmhendrickson/neotoma/issues/2496):
  `contract_review`, `email_message`, `generic`, `incident`,
  `legal_research`, `legal_review`, `repository`). Under `stop` (default)
  the whole run halts before any `/store` call for ANY type, including ones
  whose extension already succeeded. Under `continue`, that one type's
  extension is recorded as `{"applied": false, "error": ...}` and the run
  proceeds — its undeclared fields are never registered and stay on the raw
  observations (sent as-is, not stripped, not retried as declared). Use
  this when you have already confirmed the failure is neotoma#2496 (or the
  same shape: a per-type 5xx from the extension route) and want the rest of
  the run's types to proceed rather than blocking on a type whose fix is
  tracked upstream.
- **`--on-identity-conflict {stop,skip}`** — a class-a `/store` write that
  forces `target_id` onto a specific local `entity_id` can 400 with
  `ERR_STORE_RESOLUTION_FAILED` or `ERR_MERGE_REFUSED` and
  `details.reason == "identity_conflict"` — hosted already holds the SAME
  canonical identity (e.g. an issue's `repo` + `github_number`) under a
  DIFFERENT `entity_id`. This is the same class of conflict
  `canonical_identity_lookup` (ateles#998) exists to avoid earlier in the
  pipeline, reachable here when that earlier check didn't catch it. Writing
  onto the hosted entity by forcing `target_id` risks overwriting newer
  hosted state with older local-fork data. Under `stop` (default) the write
  failure halts the run like any other `E_WRITE_FAILED`. Under `skip`, the
  write is skipped (never retried forcing the local id, never merged) and
  logged as `action=skipped_identity_conflict` with the hosted
  `entity_id` it collided with, so the field-level reconcile pass
  (`reconcile` mode) can pick it up deliberately afterward.

Both are matched on the response's structured `error.code` /
`error.details.reason`, never a substring search of the response body, so
an unrelated 400 that happens to mention "conflict" in its message is still
treated as an ordinary write failure.

```bash
# Continue past a known per-type schema-extension failure (neotoma#2496)
# rather than blocking the whole run on it.
... replay --db <path> --cutover <ts> --apply --extend-schemas \
  --on-extension-failure continue

# Skip a class-a write that collides with an existing hosted identity under
# a different entity_id, instead of stopping the run.
... replay --db <path> --cutover <ts> --apply \
  --on-identity-conflict skip
```

Equivalent env vars (kept for the exact command line the 2026-09-25
migration run used; the CLI flag takes precedence when both are given):
`NEOTOMA_REPLAY_EXTENSION_FAILURES=continue` and
`NEOTOMA_REPLAY_IDENTITY_CONFLICTS=skip`. Prefer the flags in a new run —
the env vars exist for reproducing that specific run's command line, not as
the primary interface.

## Interpreting classifications, summaries, exit codes, and the JSONL log

- **`class=a_missing`** (replay) — the entity is confirmed absent on hosted
  (a 404 probe); this write is a genuine new insert.
- **`class=b_diverged_or_present`** (replay) — the entity already exists on
  hosted and is skipped under `--only-missing` (the default) to avoid
  overwriting independent hosted state.
- **`entity_class=class_b_reconcile`** (reconcile) — a field-level write for
  an entity known to exist on both sides.
- **`entity_class=drifted`** (reconcile) — a field was skipped because
  hosted's value changed since the reconciliation file was computed.
- **`action=skipped_unimplemented` / `entity_class=deferred_source_blob`** —
  a `sources` row; file-blob replay is not implemented and is always logged
  as deferred, never silently treated as applied.

Every planned/attempted action (dry-run or apply) is appended to the JSONL
log as one object per line with keys `entity_id`, `entity_type`,
`classification`, `action`, `http_status` (never field values). The
end-of-run summary's `planned`/`applied`/`skipped`/`deferred`/`failed`/
`unresolved` counts are the first place to look after any run; an empty plan
prints `NO_CHANGES` and exits 0 without printing an apply hint. Two further
counts, `schema_extension_failures` and `skipped_identity_conflicts`, always
print (0 unless their opt-in `continue`/`skip` policy above is in effect) —
a non-zero value there after a `continue`/`skip` run is the count to check
against what you expected before treating the run as fully converged.

## Recovery procedures, by error code

| Code | Meaning | Recovery |
|---|---|---|
| `E_ARGUMENT_CONFLICT` | Conflicting modes, no subcommand, or `--apply`+`--dry-run` together | Re-run with exactly one of `--apply`/`--dry-run` (or neither) and a valid subcommand. |
| `E_CONFIRMATION_REQUIRED` | `--apply` without `NEOTOMA_REPLAY_CONFIRM_APPLY=yes` | Set the env var exactly, then re-run the printed command. |
| `E_HOSTED_STATE_UNKNOWN` | A probe got a 5xx/401/403/timeout, never a confirmed 200 or 404 | Check hosted health/credentials, then re-run the identical command — idempotency keys make this safe. |
| `E_SCHEMA_NOT_VERIFIED` | An additive schema extension reported success but a re-fetch doesn't show the field as declared, or a field was predicted `UNKNOWN_FIELD` under `--unknown-fields=stop` | Inspect the hosted schema for the named entity_type; re-run with `--unknown-fields=warn` only after reviewing, or fix the schema first. |
| `E_HOSTED_UNHEALTHY` | `/health` failed before or during an apply run | Wait for hosted to recover; re-run the same command (already-applied writes are idempotent no-ops). |
| `E_WRITE_FAILED` | A write returned a non-2xx or ambiguous result | Inspect the log entry for that `entity_id`; re-run the same command. If it's a confirmed 400 `identity_conflict` (`error.details.reason`), consider `--on-identity-conflict skip` instead of re-running unchanged — a plain re-run will hit the same conflict every time. |
| `E_SANITY_THRESHOLD` | `restore-gates` proposes more than 150 issue changes in one dry run | Review the dry-run report; narrow with `--limit` or `--entity-ids`/`--exclude-entity-ids`. |
| `NO_CHANGES` | Nothing eligible to write, filters matched nothing, or hosted already converged | Nothing to do; this is a successful, no-op outcome (exit 0). |
| `DEFERRED` | A row can't be replayed in this mode (currently: source blobs) | Tracked separately, never counted as applied; handle in a follow-up pass. |

Every failure prints a stable `code=`, a field-free `cause=`, whether
`writes_occurred=`, the `log_path=`, and one `next_action=` — never a
response body or field/entity value.

## Final convergence check

After an apply run, repeat the **same command as dry-run** (drop `--apply`,
or add `--dry-run`). A converged migration reports `NO_CHANGES` — if it
still reports a nonzero `planned` count, something did not land; check the
JSONL log for the entity ids involved before re-applying.

```bash
python3 execution/scripts/neotoma_local_fork_replay.py replay \
  --db /tmp/neotoma-fork-frozen.db --cutover 2026-08-04T00:00:00Z
# Expect: NO_CHANGES
```

## Guarantees

- Local fork data is **never deleted** by this script, in any mode.
- Pre-existing duplicate entries in array-typed fields are **out of scope**
  for this script (tracked as
  [neotoma#2119](https://github.com/markmhendrickson/neotoma/issues/2119))
  — it never attempts to clean them up.
