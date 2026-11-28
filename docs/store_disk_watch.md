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

## Activation checklist

- Pre-provision the private persistent state directory and a dedicated notifier
  journal directory; set `ATELES_DEDUPE_JOURNAL_PATH` explicitly. This job must
  be the journal's only user. Missing directories or an unset/blank journal
  setting fail closed, without creating directories or using a temporary fallback.
- Configure and read back every selected deployment record and exact mount
  label. Supply the existing read-only metrics credential and documented endpoint.
- Run the job end to end against the real endpoint before enabling its schedule.
  Require `configuration=ok`, `complete=true` for every target, an `instance` id
  in each target's `instances`, and a percentage matching `df` on that same
  instance/mount. The synthetic tests do not satisfy this check.
- Confirm that `timestamp()` returns each gauge's source-sample time rather than
  query evaluation time. Exercise one authorized `--retire-instance` command and
  read its inventory change, warning task and delivery back before activation.
- Watched apps' machines must not auto-stop. Each stop removes metrics and
  raises the one-hour no-read alarm until the instance returns or is explicitly
  retired; a stopped machine is not automatically retired.
- Check host time synchronization. A source/evaluation timestamp more than
  30 seconds ahead of the host is `metrics_clock_skew`; a host clock more than
  30 seconds ahead can make source samples stale. Resolve clock skew before
  trusting the result; the maximum source age remains five minutes.
- Prove one authorized notice and its task surface in the actual coordinator
  harness. The task is the record a person acts on; transport acceptance alone
  does not establish that the person saw it. Then enable the existing schedule.

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
The journal path must be absolute and its parent directory must already exist.
It is dedicated to this job, including state-failure receipts; other jobs must
use a different journal. The state parent must also already exist.
Only one scheduled owner may run against the same state; the shared notifier's
fail-closed file lock excludes overlapping polls.

Required runtime settings:

| Setting | Required value |
| --- | --- |
| `STORE_WATCH_FLY_METRICS_TOKEN` | Operator-created, organization-scoped, read-only metrics credential, including its `FlyV1 ` prefix. No fallback to an ambient Fly token. |
| `STORE_WATCH_METRICS_URL` | `https://api.fly.io/prometheus/<org-slug>/api/v1/query`; the exact documented host, no explicit port, URL credentials, query or fragment. The metrics client ignores proxy and certificate environment settings (`trust_env=False`). |
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
On a first run with invalid configuration there are no known targets to poll:
the configuration event/task is raised immediately, the report is UNKNOWN and
the exit is nonzero. No per-target one-hour clock exists until a target has
been loaded successfully. Configuration recovery reports configuration only;
it cannot establish filesystem recovery.

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
The persisted instance inventory includes ids observed even in incomplete
gauge sets. A previously seen instance missing entirely from a later poll makes
that poll partial, across process restarts and past provider lookback expiry.
No instance ages out automatically. A newly discovered instance joins the set.
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
The initial create readback verifies `status`, `assigned_to` and `priority`
alongside the task text. Later changes to those human-owned fields are tolerated
only after the event has a verified task id. A lost create response with no
identified task cannot establish that a mismatched field was a person's edit;
the event stays unrecorded for assessment rather than trusting it.

Before trusting activation, send one authorized synthetic notice through the
intended host, read its receipt and task back, and prove the linked escalation
surfaces in the operator's actual coordinator harness. Until that succeeds,
human delivery is **unverified**. The tests use synthetic transports and records;
no real notification or production write was attempted during testing.

## Failure and recovery behavior

Target results carry a fixed `failure` category (or `null` on complete success),
and the one-hour no-read notice/task carries that category. Provider response
text and exceptions are never copied. `metrics_auth_failure` means a 401/403:
check the selected credential and scope without printing the token.
`metrics_token_missing_or_invalid` means the dedicated token is absent/malformed.
`metrics_labels_mismatch` means returned labels do not match the app/mount;
`metrics_empty_or_invalid` can also mean a wrong mount matched no series: confirm
the exact observed label. `metrics_clock_skew` means a timestamp is over
30 seconds in the future; check host time synchronization. `metrics_stale`
means a sample exceeded the age bound. `metrics_instance_missing` requires
restoring that instance's series or explicit retirement below. Other fixed
categories distinguish invalid capacity, duplicate series, missing gauges,
partial envelopes and request failures. Failure notices and their task text
point to this section for the next step.

### Instance retirement and legacy state

After a person confirms a machine was deliberately retired/replaced, run this
explicit action with the app, literal mount label and exact persisted instance
id of the old machine, not its replacement (synthetic example):

```bash
python3 execution/scripts/store_disk_watch.py --state /data/store-watch/state.json --retire-instance synthetic /data synthetic-a
```

This removes only that instance from the expected set and checkpoints a warning
event/task before sending. It does not poll metrics, clear the stored level or
stale alarm, or advance `last_success`. Its JSON status remains UNKNOWN even
when the action succeeds with exit 0; only the next complete read can report
recovery. The replacement joins the inventory when its series is observed.
If the retired instance reappears, it is observed again. A failed checkpoint
creates no retirement effects; a crash after the checkpoint replays the same
pending event on the next poll. Assess the task before retrying an action that may already have
committed; re-retiring an absent id is refused.

State version 1 lacked an inventory. Migration to version 2 preserves all
levels, timestamps and pending events, and sets existing inventories unknown.
Reads may escalate but cannot clear or refresh them. After independently
confirming the full current instance set for a legacy target, record its initial
baseline (one or more ids after the app and mount):

```bash
python3 execution/scripts/store_disk_watch.py --state /data/store-watch/state.json --confirm-instances synthetic /data synthetic-a synthetic-b
```

This action is accepted only for an unknown legacy inventory; it cannot replace
an already known set or silently retire machines. Like retirement, it records
an event without declaring recovery. A first-ever version 2 target seeds its
inventory from observed series; the activation check must confirm all intended
instances, since no metrics-only poll can discover a machine never observed.
Target removal is separate: an explicit edit of the selected configuration
removes an app/mount and records a warning task, rather than a healthy event.
Do not remove a target to hide an unreadable configuration or vanished machine.

Both manual commands exit 2 for bad input or a busy state lock and print the
fixed cause: `inventory_target_unknown`, `inventory_instance_unknown`,
`inventory_already_known`, `inventory_action_invalid`, or
`state_lock_unavailable`. These errors change no watch state and create no
failure episode, task or notice. Check the app/mount and persisted instance ids,
use confirmation only for an unknown legacy inventory, or retry after the
running poll releases the lock. An action checkpointed locally but still
unrecorded or undelivered exits 1; its pending event is retried by the next poll.
Unreadable state or a write failure still takes the critical failure path below.

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

For a poll, exit 0 means valid configuration, fresh healthy filesystems, and no
unrecorded or undelivered events. Any warning, critical, UNKNOWN or pending
effect exits 1. For either manual inventory command, exit 0 means its action
is recorded and delivered, while usage remains UNKNOWN; exit 1 means a pending
action effect or real job/state failure; exit 2 means bad input
or a busy lock (including argparse usage errors).

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
