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
prints `NO_CHANGES` and exits 0 without printing an apply hint.

## Recovery procedures, by error code

| Code | Meaning | Recovery |
|---|---|---|
| `E_ARGUMENT_CONFLICT` | Conflicting modes, no subcommand, or `--apply`+`--dry-run` together | Re-run with exactly one of `--apply`/`--dry-run` (or neither) and a valid subcommand. |
| `E_CONFIRMATION_REQUIRED` | `--apply` without `NEOTOMA_REPLAY_CONFIRM_APPLY=yes` | Set the env var exactly, then re-run the printed command. |
| `E_HOSTED_STATE_UNKNOWN` | A probe got a 5xx/401/403/timeout, never a confirmed 200 or 404 | Check hosted health/credentials, then re-run the identical command — idempotency keys make this safe. |
| `E_SCHEMA_NOT_VERIFIED` | An additive schema extension reported success but a re-fetch doesn't show the field as declared, or a field was predicted `UNKNOWN_FIELD` under `--unknown-fields=stop` | Inspect the hosted schema for the named entity_type; re-run with `--unknown-fields=warn` only after reviewing, or fix the schema first. |
| `E_HOSTED_UNHEALTHY` | `/health` failed before or during an apply run | Wait for hosted to recover; re-run the same command (already-applied writes are idempotent no-ops). |
| `E_WRITE_FAILED` | A write returned a non-2xx or ambiguous result | Inspect the log entry for that `entity_id`; re-run the same command. |
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
