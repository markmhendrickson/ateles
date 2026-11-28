# Store disk watch

## Purpose and scope

`execution/scripts/store_disk_watch.py` makes one volume-usage poll, without
model calls. It warns at 70% used and raises critical at 85%. A changed level
produces an event; an unchanged level produces none. Falling below a threshold
records recovery/downgrade, and a later crossing can alert again. Each volume
keeps its last successful source-sample time across process restarts. An unknown
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
| `STORE_WATCH_METRICS_URL` | Organization's HTTPS Prometheus instant-query endpoint, ending in `/api/v1/query`; no URL credentials, query or fragment. |
| `STORE_WATCH_METRICS` | JSON object containing `used`, `size`, `app_label`, `volume_label`, `mount_label`. Both metrics must be byte gauges. No metric or label name is guessed in code. |
| `STORE_WATCH_CONFIG_IDS` | Nonempty JSON array of the selected existing `deployment_configuration` entity ids. Every selected record is read on every run. |
| `NEOTOMA_BASE_URL`, `NEOTOMA_BEARER_TOKEN` | Existing authorized production record endpoint and credential, able to read selected deployment records and create/read tasks and correct their notes. |

Each selected deployment record supplies its existing `fly_app` value and one
line in its existing `gotchas` string or string array. Preserve all other
gotchas. That line starts with `store-watch:` followed by a JSON array, with one
object per volume: the required string keys are `volume` (provider volume
identity) and `mount` (absolute mount path). This is an opt-in convention within
an existing field, not a schema registration or migration. Read it back after
editing it; undeclared fields are not an alternative. Duplicate targets,
missing/malformed mounts, failed record reads, empty selection and partial
selection fail the entire configuration. The job emits a critical configuration
event and retains the last known target list for the no-read alarm.

### Confirm the provider mapping before activation

Use the provider's metric/label discovery endpoints with the read-only token;
inspect actual series for **every** configured app, volume and mount. Confirm
which gauge reports used bytes and which reports capacity bytes, and whether
one or several instances export a volume. Supply those exact names in the one
mapping setting. A percent gauge is not accepted as a byte gauge by contract.
Cross-check one usage ratio against an independently obtained capacity reading.
Do not add aggregation to make multiple series appear to be one volume.

Each poll makes four instant queries per volume: used, capacity and
`timestamp()` of each gauge. Require exactly one series per query, matching all
three identifying labels, finite numbers, positive capacity, used between zero
and capacity, and source samples no older than five minutes (at most 30 seconds
in the future). HTTP errors, redirects, partial-result warnings, missing series,
ambiguous series and malformed answers are UNKNOWN. An instant query's fresh
evaluation timestamp alone does not establish a fresh source sample.

Fly documents read-only tokens and the `FlyV1` authorization scheme in its
[metrics API documentation](https://fly.io/docs/monitoring/metrics/).
Credential scope and the live metric/label mapping still need verification in
the intended organization. This implementation was tested only synthetically.

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
Task creation and delivery-note updates are read back. Only a literal successful
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
state. Preserve and repair the original files before trusting recovery. If
recording, transport and local persistence all fail, the JSON failure report is
the remaining evidence; no person delivery is claimed. A process that is never
invoked cannot self-alert: schedule liveness must be checked by the existing
host supervision, separately from this per-volume one-hour test.

Exit 0 means valid configuration, fresh healthy volumes, and no unrecorded or
undelivered events. Any warning, critical, UNKNOWN or pending effect exits 1.

## Verification

```bash
python3 -m pytest execution/scripts/test_store_disk_watch.py -q
ruff check execution/scripts/store_disk_watch.py execution/scripts/test_store_disk_watch.py
ruff format --check execution/scripts/store_disk_watch.py execution/scripts/test_store_disk_watch.py
bash scripts/lint.sh
```

The script test directory is already collected by the blocking
`ateles-tests.yml` workflow. This repository has no root Node package or
generated test catalog; no catalog regeneration applies to this Python test.
