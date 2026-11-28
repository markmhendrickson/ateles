# Store disk watch

## Purpose and scope

`execution/scripts/store_disk_watch.py` makes one filesystem-usage poll, without
model calls. It warns at 70% used and raises critical at 85%. A changed level
produces an event; an unchanged level produces none. Falling below a threshold
records recovery/downgrade, and a later crossing can alert again. Each app/mount
target keeps its last successful source-sample time across process restarts. An unknown
read never clears a disk condition. After one hour without a successful read,
the job raises a separate critical event, once until reads recover.

This does not read application logs, scheduler output, web errors or application
health checks. It never resizes a volume or deploys anything.

## Runtime configuration

Use the existing swarm Fly setup, with Python 3.13+ and this repository's locked
runtime dependencies. Arrange an invocation every five minutes through its
existing scheduling facility **after activation is approved**. This change
creates no schedule, machine, app, grant, credential or deployment. Run from the
repository root:

```bash
python3 execution/scripts/store_disk_watch.py --state /data/store-watch/state.json
```

The state path is illustrative and must be a private directory on persistent
storage, separate from the volumes being monitored. Keep these existing notifier
files on that same persistent storage using `ATELES_DEDUPE_JOURNAL_PATH`,
`ATELES_DIGEST_QUEUE_PATH` and `ATELES_NOTIFY_SINK_DIR`. Do not use temporary
container storage: losing state resets the no-read clock and threshold memory.
Only one scheduled owner may run against the same state; the shared notifier's
fail-closed file lock excludes overlapping polls.

Required runtime settings:

| Setting | Required value |
| --- | --- |
| `STORE_WATCH_FLY_METRICS_TOKEN` | Operator-created, organization-scoped, read-only metrics credential, including its `FlyV1 ` prefix. No fallback to an ambient Fly token. |
| `STORE_WATCH_METRICS_URL` | `https://api.fly.io/prometheus/<org-slug>/api/v1/query`; no URL credentials, query or fragment. |
| `STORE_WATCH_CONFIG_IDS` | Nonempty JSON array of the selected existing `deployment_configuration` entity ids. Every selected record is read on every run. |
| `NEOTOMA_BASE_URL`, `NEOTOMA_BEARER_TOKEN` | Existing authorized production record endpoint and credential, able to read selected deployment records and create/read tasks and correct their notes. |

Each selected deployment record supplies its existing `fly_app` value and one
line in its existing `gotchas` string or string array. Preserve all other
gotchas. That line starts with `store-watch:` followed by a JSON array, with one
string per mount: for example `store-watch:["/data"]`. Use the exact observed
`mount` label, without normalization; there is no volume identity setting.
This is an opt-in convention within an existing field, not a schema registration
or migration. Read it back after editing it; undeclared fields are not an alternative. Duplicate targets,
missing/malformed mounts, failed record reads, empty selection and partial
selection fail the entire configuration. The job emits a critical configuration
event and retains the last known target list for the no-read alarm.

### Confirm real series before activation

Provider metric and label facts are defined once in `store_disk_watch.py`, from
[Fly's metrics documentation](https://docs.fly.io/monitoring/metrics). No mapping
setting or custom exporter is required. The job selects `app` and `mount` only
and reads these gauges for every matching instance:

- `fly_instance_filesystem_blocks`
- `fly_instance_filesystem_blocks_free`
- `fly_instance_filesystem_blocks_avail`

Application-visible percent used is `100 * (blocks - blocks_free) / (blocks -
blocks_free + blocks_avail)`, the `df` figure. Zero available blocks means 100%.
Block size cancels out, so `fly_instance_filesystem_block_size` is not queried.
Volume thin-pool usage does not establish writable filesystem space.

Each target needs six instant queries: three gauges and
`timestamp(<gauge>{app="...",mount="..."})` for each. `timestamp()` takes the
plain instant-vector selector, not a range or an aggregated value; see
[Prometheus timestamp](https://prometheus.io/docs/prometheus/latest/querying/functions/#timestamp).
The source timestamp is returned as the value, distinct from query evaluation
time. Every gauge must have a finite value, a source timestamp no older than
five minutes (at most 30 seconds in the future), and consistent capacity:
`blocks > 0`, `0 <= avail <= free <= blocks`, and `used + avail > 0`.

Join series by all labels except the metric name; `instance` is required.
Evaluate every matching series without aggregation. A valid warning/critical
instance alerts even if a sibling is stale, invalid, duplicated or missing a
gauge. The reported level/percent is the worst valid instance and its notice
identifies the instance. Only a complete, valid, fresh set advances the target's
last-success clock or clears a prior condition. Partial healthy data remains
UNKNOWN; partial data can escalate but cannot downgrade an existing alert.
HTTP errors, redirects, warnings, partial-response flags and malformed envelopes
are UNKNOWN. An empty vector is not a healthy target.

The operator must create the read-only credential (this change never does):

```bash
fly tokens create readonly --org "$STORE_WATCH_ORG_SLUG"
```

Set `STORE_WATCH_ORG_SLUG` to the intended organization for that command; the
job itself only needs the three watch settings in the table. Store the entire
printed `FlyV1 ...` credential privately as `STORE_WATCH_FLY_METRICS_TOKEN`.
The token is organization-scoped; do not substitute a deploy or ambient token.
See [the token command](https://docs.fly.io/flyctl/cmd/fly_tokens_create_readonly).

Before activation, inspect actual filesystem series for **every** configured
app/mount/instance using that endpoint and token. Confirm what the `mount` label
actually contains: Fly describes `/` or the destination name in `fly.toml`;
do not assume that it is always an absolute path or strip a leading slash.
Cross-check at least one calculated ratio against an independent `df` reading
inside the same instance and mounted filesystem. Confirm token scope and all
three source timestamps. Documentation and synthetic payloads establish the
model only: the real series, mount spelling, credential scope and independent
ratio remain **unverified** until this check is completed.

## Notification and task contract

The job reuses `lib.notify.Notifier.send` with persistent, retryable receipts,
stable event keys and `email_eligible=False`. The existing Apprise transport
uses the host's existing `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`; no new
recipient or credential is created here. Warnings bypass silence because this
job's only notices require operational assessment. `ATELES_NOTIFY_NEOTOMA_SINK=on`
also records through the existing Neotoma escalation sink for the coordinator
session to consume. A stored escalation is not proof a person saw a notice.

Each event also becomes a `task` with `status=blocked`, `assigned_to=operator`,
and a human-assessment reason. The SSE router recognizes this explicit human
assignee and spawns no agent; the reconciler skips non-pending tasks and the
watchdog does not retry blocked tasks. Tests exercise those existing decisions.
Task titles name the condition plus app/mount/instance (or the watch failure),
with a stable event suffix for unique reconciliation. Task creation and
delivery-note updates are read back. Notes contain one machine-owned line,
`store-watch-delivery: {"delivery": "...", "event": "..."}`. Each retry reads the
current notes, replaces only that event's line and preserves all other text and
human-owned status. Correction keys include the merged notes hash so a retry
preserves notes added since an earlier update. The API has no compare-and-swap
here: a simultaneous human edit between the read and correction can still race;
readback confirms the merged value, not atomicity. Only a literal successful
send or a valid `delivered` receipt confirms transport acceptance. Held, absent,
failed and uncertain sends remain `undelivered` in the task notes and local
event. This confirms transport acceptance, not that a person read it.

Before trusting activation, send one authorized synthetic notice through the
intended host, read its receipt and task back, and prove the linked escalation
surfaces in the operator's actual coordinator harness. Until that succeeds,
human delivery is **unverified**. Apprise was unavailable on the build host;
no real notification or production write was attempted during testing.

## Failure and recovery behavior

The job reuses the notifier's private atomic-file writer and file locks. It
checkpoints new events before any task write or send. Crashing before that
checkpoint causes no external effect; crashing after it replays the same event
id and task idempotency key. The existing notifier receipt handles interrupted
or ambiguous sends without blind redelivery. A task write/readback failure
leaves the event unrecorded for retry and does not prevent the person alert.
A transport failure leaves that same task and event undelivered for retry.
The last 100 completed events are retained locally; pending events are retained
until both recording and delivery succeed. Neotoma tasks retain the event record.

Unreadable/malformed state and lock/write failures return nonzero and attempt
an existing-route critical notice and blocked task, without replacing damaged
state. State-failure episode identity and pending effects live as optional
`watch_event` metadata on the existing notification receipt, under the existing
journal/state locks; there is no additional store or lock mechanism. A UUID
identifies each episode's task and send receipt. Repeated polls/restarts reuse
that event, including after a lost task response or interrupted send. A poll
that can save watch state drains the episode's remaining effects and then
removes the episode metadata only once both recording and delivery are confirmed.
Pending episode effects also contribute to the next poll's unrecorded/undelivered counts and nonzero exit.
The next state failure gets a new UUID/task even if a person closed the old task.
Trade-off: failures separated by no successful state checkpoint, or recovery
whose task/delivery is still pending, remain one episode. Losing the journal
also loses deduplication history. Unreadable/malformed episode metadata or a
journal lock/read/write failure prevents external effects; JSON reports unknown
delivery instead. An overlapping poll unable to acquire the state lock reports
locally without competing for the active episode. Preserve and repair the
original files before trusting recovery. If recording, transport and local persistence all fail, the JSON failure report is
the remaining evidence; no person delivery is claimed. A process that is never
invoked cannot self-alert: schedule liveness must be checked by the existing
host supervision, separately from this per-target one-hour test.

Exit 0 means valid configuration, fresh healthy filesystems, and no unrecorded or
undelivered events. Any warning, critical, UNKNOWN or pending effect exits 1.

## Verification

```bash
python3.13 -m pytest execution/scripts/test_store_disk_watch.py lib/notify/ -q
ruff check execution/scripts/store_disk_watch.py execution/scripts/test_store_disk_watch.py
ruff format --check execution/scripts/store_disk_watch.py execution/scripts/test_store_disk_watch.py
python3 scripts/linters/check_hardcoded_config.py execution/scripts/store_disk_watch.py execution/scripts/test_store_disk_watch.py
python3 scripts/linters/check_neotoma_rest_paths.py
python3 scripts/linters/check_agent_roster.py
python3 scripts/linters/check_documentation.py docs/store_disk_watch.md
```

The script test directory is already collected by the blocking
`ateles-tests.yml` workflow. This repository has no root Node package or
generated test catalog; no catalog regeneration applies to this Python test.
