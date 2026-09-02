# Connectors — external system state in Neotoma

## The defect this closes

Facts that live only in a live CLI query are facts the swarm cannot act on.
Every item below cost real time on 2026-08-31, and every one was discoverable
only by a human thinking to run a command:

| What was wrong | How long it hid | How it surfaced |
|---|---|---|
| Production served an application version ~5 minor releases behind what was published | ~1 month | Incidentally, 8h into an unrelated investigation |
| A committed config file declares less CPU/RAM than the running machine — deploying it would silently shrink production | unknown | A drift script written that day, which has nowhere to display its output |
| Daemons run from a checkout that lags `main`, so merged fixes never reach them | 3 recurrences | `checkout_drift.py` logs it; nobody reads the log |
| The instance returned `200` on `/health` all day while unable to serve | 1 day | A `/ready` probe added that day |
| `sync_issues` has no daemon and no scheduled caller — written, never invoked | since it was written | All 54 open PRs correctly show "not in Neotoma" |

The last row is this codebase's signature failure, and it has a lineage: the
worker pool built 2026-07-29 and never selected; `agent_auto_invocation.py`,
fully tested and wired into zero lines of config; the digest queue with zero
non-test callers. **A connector that ships without a live trigger joins that
list**, which is why the trigger — not the collector — is the acceptance
criterion for stage 1.

## The one idea

Fly and GitHub are two instances of a single pattern, and the pattern is worth
building once:

> An external system is the source of truth. Neotoma holds a **timestamped
> observation** of it, which can go stale.

That framing generalizes — the Theodore project has wanted connectors and none
were built, and it is a third implementation of the same contract rather than a
new thing. If Theodore cannot be implemented against this contract without
rework, the abstraction is two pipelines sharing a name and should be rejected.

## Why staleness is the whole design

Deployment state is **not** like PRs and issues, where Neotoma is canonical and
GitHub is overlaid. Here the external API is the truth, and a Neotoma record is
a cache with no invalidation.

A stale record claiming the current version is `0.22.1` while the machine
actually serves `0.17.0` is **worse than no record at all**. It is the identical
failure to a health check returning `200` while nothing works — which is the
defect this entire effort exists to prevent. Building the fix in the shape of
the bug would be a poor trade.

Three rules follow, and they are non-negotiable:

1. **Every observation carries `observed_at`.** No exceptions. A value without a
   timestamp is unusable, because nothing downstream can judge it.
2. **Consumers render age, never bare values.** "0.17.0, seen 4m ago" is a fact.
   A bare "0.17.0" is an assertion the data cannot support.
3. **A stale observation is visibly stale, never silently wrong.** Past the
   threshold the UI shows the age and the staleness, and a watchdog declines to
   alarm on it — an alarm computed from an unknown present is noise.

### The threshold, and why this number

Staleness is **per-connector**, declared by the connector itself, because the
right number is a property of how fast the source changes and how often we poll:

```
stale_after = max(3 × poll_interval, 15 minutes)
```

- **3 × poll interval** — one missed run is routine (a laptop sleeps, a fetch
  times out). Three consecutive misses is a broken connector. Alarming on one
  miss trains operators to ignore the alarm, which is how the checkout-drift
  log became unread.
- **15-minute floor** — stops a fast-polling connector from declaring itself
  stale during a brief network blip.

For the Fly connector at a 15-minute poll: `stale_after = 45 minutes`.

Three states, deliberately not two:

| State | Meaning | Consumer behavior |
|---|---|---|
| `fresh` | `age <= stale_after` | Use the value; alarms may fire |
| `stale` | `age > stale_after` | Show value **and** age, marked stale; **alarms suppressed** |
| `unknown` | never observed, or the connector has never succeeded | Show "never observed"; never infer |

`unknown` is separate from `stale` for the same reason `checkout_drift` treats a
failed fetch as `unknown` rather than drift: *we could not tell* and *we can
tell, and it is bad* are different facts, and collapsing them produces either
false alarms or ignored ones.

## Polling and webhooks

### What the two sources actually support (checked, not assumed)

| Source | Webhooks? | Evidence |
|---|---|---|
| **GitHub** | **Yes, and already in use** | Live hooks on both repos (`gh api .../hooks`): active, six event types — `check_suite`, `issues`, `issue_comment`, `pull_request`, `pull_request_review`, `push` — delivering to an Apis gateway. Recent deliveries all `OK`. `execution/daemons/apis/swarm_dispatch.py` is the existing consumer. |
| **Fly** | **No — not for ordinary app owners** | Machine-event webhooks exist only in the [Extensions API](https://fly.io/docs/reference/extensions_api/), for registered Extension *providers* through Fly's provider onboarding path. General deploy/machine webhooks have been requested repeatedly on the community forum and remain unshipped; the standing community advice is "API polling and direct Prometheus integration are the two threads I'd pull on". |

So the answer is **asymmetric**, which settles the design cleanly: Fly is poll-only because nothing else is on offer, and GitHub already has a live push path that a connector should align with rather than replace.

### Are webhooks preferable? Not uniformly — they trade away the one property this design guarantees

Webhooks win on **latency** (seconds, not up to a poll interval) and **cost** (no ~96 daily calls to learn nothing changed).

They lose on **falsifiability**:

> A webhook tells you what **changed**. A poll tells you what **is**.

If a delivery is dropped — endpoint down, subscription expired, secret rotated — nothing ever tells you. The record diverges permanently while every timestamp still looks fresh, **because nothing was expected and so nothing aged**.

That is not hypothetical. It is the SSE failure exactly, and the code makes the mechanism plain:

- the subscription ID came from an env var that was absent for **88 days**;
- `sse_client.py` logged a `WARNING`, set `_running = False`, and **returned** — a clean exit indistinguishable from a healthy idle stream;
- **67,450 silent skips**, no task event after 2026-06-04;
- the stream *does* receive server heartbeats — and **discards them** (`yield None`), so nothing records that events are still arriving and nothing can notice when they stop;
- the streaming client runs with `timeout=None`, so a permanently silent stream blocks forever rather than aging.

The transport carried the liveness signal the whole time. The application threw it away.

Polling has the inverse properties: wasteful and slow, and **it cannot silently diverge** — the observation either arrives or it ages into staleness.

### The shape: both, with different jobs

**Push for latency, poll for liveness.** The verify poll can then be *infrequent* — hourly rather than every fifteen minutes — because its job is catching drift, not being current. That collapses most of the cost objection to polling, which makes the hybrid cheaper than it first appears.

Concretely, in the contract:

- `poll_interval_seconds` is **required on every connector, push-fed included**, and means **"how often we verify"**, not "how often we fetch". A small change in meaning, a large change in what the field guarantees.
- `stale_after = max(3 × verify_interval, 15 min)` keys off that verify cadence — the liveness guarantee — while push supplies freshness in between.
- `last_push_at` is **recorded but never used to compute freshness**. If a delivery reset the staleness clock, a source that went quiet would be indistinguishable from a healthy one — the SSE failure again. It exists so the push path is *observable*: "verified, events also arriving" and "verified, nothing pushed in a week" are different situations.
- There is **no `push` ingestion mode**, only `poll` and `hybrid`. A push-only connector cannot notice its own silence, and a mode nobody can use safely is a mode worth not offering.

### Where this lands per stage

- **Fly (stage 2): poll-only**, because Fly offers nothing else.
- **GitHub (stage 5): `hybrid` is the target** — reconciling with the existing Apis webhook consumer rather than standing up a second one — but stage 5 is held regardless, and **poll-only is the correct first implementation**. The verify path is what makes push safe to add; building it in the other order reproduces the failure.

There is already precedent for the backstop pattern: `execution/scripts/triage_backfill_sweep.py` exists precisely because triage fires on an `issue.opened` webhook "and nowhere else — one-shot, no sweep, no retry", leaving 176 issues silently untriaged. That sweep is a poll backstop for a webhook, written after the fact. This contract makes it the default rather than the remedy.

## The connector contract

A connector is a small object that knows how to observe one external system.
Everything else — scheduling, status recording, staleness, the UI — is shared.

```python
@dataclass(frozen=True)
class ConnectorResult:
    """One connector run's outcome. Never raised — always returned."""
    ok: bool
    records_written: int = 0
    error: str = ""              # one line, no secrets, no tokens
    detail: dict | None = None   # small, renderable summary


class Connector(Protocol):
    name: str                    # "fly", "github", "theodore"
    poll_interval_seconds: int   # declares its own cadence

    @property
    def stale_after_seconds(self) -> int:
        return max(3 * self.poll_interval_seconds, 900)

    def observe(self) -> ConnectorResult:
        """Read the external system and write observations to Neotoma.

        MUST NOT raise. MUST be idempotent — see below.
        """
```

`observe()` never raising is load-bearing: the runner drives every connector in
one loop, and one source's outage must not stop the others.

### Idempotency is a design property, not a hope

The previous GitHub sync produced **520+ duplicate issues and 35 orphaned
entities**. Root cause: `ops.correct()` passed `{corrections: <map>}` where the
server expects `{entity_id, entity_type, field, value, idempotency_key}`. Zod
rejected it *silently*, the code read the non-error as success, and it
re-corrected in a loop. Its push leg was disabled and never re-enabled.

Three rules, each aimed at one link in that chain:

1. **Every write carries a deterministic `idempotency_key`** derived from stable
   identity, never from a clock or a counter:
   `connector-{name}-{external_id}-{content_hash}`. A re-run of an unchanged
   record is then a no-op at the server rather than a duplicate.
2. **A write is verified by read-back, never by a success code.** A `body` field
   on a `task` was accepted with `success: true` and **silently dropped** on this
   instance today. `success: true` means "the request parsed", not "the data
   persisted". This is also the exact failure mode above.
3. **`correct()` for existing fields; `store()` only for new entities.** The
   correct payload shape is `{entity_id, entity_type, field, value,
   idempotency_key}` — as used in `lib/daemon_runtime/gating.py`, which is the
   reference implementation. Writing a `last_write` field with `store()`
   clobbers concurrent updates (neotoma#2033).

**A bounded write budget per run** backs this up. If a connector tries to write
more than `ATELES_CONNECTOR_MAX_WRITES` (default 200) in one run it aborts and
reports the overrun instead of continuing. The runaway wrote 520+ records; a
budget would have stopped it at 200 with a loud error. Cheap insurance against
the failure that already happened once.

## The idempotency trap: never put your own clock in the payload

Neotoma's idempotency check hashes the **full entity payload** server-side, not
just the key. A wall-clock field *inside* the entity therefore changes the
content on every run while the key stays stable — and the store is then
rejected with `ERR_IDEMPOTENCY_MISMATCH` **permanently**.

This already happened. `sync_issues_from_github.ts` carries a long comment
about it: a wall-clock `last_synced_at` in the issue payload froze every
already-synced issue, and the only escape was a one-time `SYNC_KEY_MIGRATION`
token (`"m2"`) that abandons the poisoned rows wholesale, because they cannot
be overwritten. Their fix was to derive provenance from the source's own
`updated_at` rather than `Date.now()`.

`observed_at` is exactly that shape of field, so this design would have walked
into the same wall. `observation_payload()` strips our own clock
(`observed_at`, `observed_by`, `connector_name`) before hashing, so an
unchanged source is byte-stable across runs and the intended no-op holds, while
a genuine change still produces a new key.

**The rule: key on what the source says, never on when we happened to look.**

## Relationship to the existing status shapes

Neotoma already has three near-identical records of "is this connection
working", and this design deliberately does not become a fourth unrelated one:

| Entity | Success clock | Failure counter | Splits attempt from success? |
|---|---|---|---|
| `subscription` | `last_delivered_at` | `consecutive_failures` + `max_failures` | No |
| `peer_config` | `last_sync_at` | `consecutive_failures` | No |
| `credential_health` (spec only, unbuilt) | `last_verified_at` | — (`status`) | **Yes** (`last_checked_at`) |
| `connector_status` (this design) | `last_success_at` | `consecutive_failures` | **Yes** (`last_attempt_at`) |

Only the unbuilt `credential_health` spec and this design separate *attempted*
from *succeeded*. `subscription` and `peer_config` record success only, so a
peer failing every minute has a stale `last_sync_at` and no way to distinguish
"not attempted" from "attempted and failed" except by reading the failure
counter as a proxy. That is the blind spot the split exists to close.

Per-record freshness rides **`observed_at`**, which is already the universal
observation timestamp in Neotoma (243 uses) rather than a new sibling field.

Two consolidation opportunities follow, neither taken here:

- **`credential_health` is unbuilt and is a connector**, not a fourth entity
  type — a periodic probe recording status and freshness is exactly this
  contract.
- **The SSE subscription is itself an unmonitored connector.** A connector
  reading `subscription.active` + `last_delivered_at` would have caught the
  88-day silence on day one, because a subscription that is inactive or has not
  delivered is precisely a stale observation. Nothing polls the transport
  today; every existing backstop sits one level down, at the *effect*
  (stranded tasks, untriaged issues) rather than at the *cause*.

## The backstop pattern already has a reference implementation

`execution/daemons/apis/task_reconciler.py` is a level-triggered sweep under an
edge-triggered SSE path, written because of the 88-day failure. Its
double-dispatch argument is the part to reuse when stage 5 adds a push path:

1. **Status filter** — the push path writes `ROUTED` before any gate, so a task
   it picks up leaves the sweep's eligible set immediately.
2. **Grace window** — a minimum age longer than a dispatch takes, so the live
   path always wins the race and the sweep only sees what push demonstrably
   missed.
3. **In-process claim ledger** — because the status write is fail-open, a lost
   write must not let layers 1–2 re-select.

Also worth copying: bounded work per pass, a closed-vocabulary skip reason so
skips are countable rather than invisible, and the boot-time disclosure rule —
*a reconciler that is off must say so, or its absence looks identical to one
that ran and found nothing*, which is the exact ambiguity that hid the dead
subscription for 88 days.

## Status model — what the app reads

One `connector_status` entity per connector, corrected in place each run:

| Field | Why |
|---|---|
| `connector_name` | identity |
| `last_attempt_at` | when it last ran at all |
| `last_success_at` | when it last **worked** |
| `status` | `ok` / `failing` / `never_run` |
| `last_error` | one line, no secrets |
| `records_written` | last successful run's count |
| `poll_interval_seconds`, `stale_after_seconds` | lets any consumer compute staleness without hardcoding |

**`last_attempt_at` and `last_success_at` must be distinct fields.** A connector
attempting and failing every minute is indistinguishable from a healthy one if
only attempts are recorded — and that is exactly the class of silent failure
this whole effort exists to end.

## Observation model — history, not just current state

The Fly release history is the single most informative artifact from the
investigation, and current-state-only cannot express what it shows:

```
v16      3h49m ago      deployment-01M1EBTEB…   ← distinct image
v15      Aug 27 14:46   deployment-01M11TXSC…   ← distinct image
v14      Aug 9  08:39   deployment-01KZJTRBK…
v11/v10  Aug 6          deployment-01KZBG32X…   ← same image, two releases
v3–v8    Aug 2–3        deployment-01KZ1ZMHA…   ← one image, six releases
```

Only the history reveals that v15 and v16 each built a **fresh image** while the
reported application version did not move. That contradiction is the crux of the
open question — is production genuinely a month old, or merely mislabelled? — and
a single mutable "current deployment" row cannot state it.

So: **one immutable `deployment_observation` entity per release**, never
overwritten. Fields: `version`, `image_ref`, `deployed_at`, `status`,
`triggered_by`, `observed_at`, plus the connector that saw it.

Config-only redeploys sharing an image are **normal** here, so "a release exists"
carries little information about what code is running. The history must make
image reuse visible — the UI groups consecutive releases sharing an `image_ref`
— rather than implying each release shipped something.

## Not committed to a public repo

Both `ateles` and `neotoma` are public. A review on ateles PR #655 caught a Fly
app name as an env-var default on 2026-08-31; that is a standing constraint, not
a one-off.

No app names, hostnames, machine IDs, or domains in code or config. Instance
identity resolves at runtime from env or from a `deployment_configuration`
entity. Connectors that find no binding **skip and report**, never guess.

Observation records store an opaque `instance_ref` that resolves through
Neotoma, not a literal hostname.

## Staging

Deliberately staged. A deployments view that renders stale data as current would
be worse than the status quo, so each stage lands complete or not at all.

| Stage | Contents | Acceptance |
|---|---|---|
| **1. Contract + status + trigger** | `Connector` protocol, staleness, `connector_status`, the runner, **and its launchd trigger** | A scheduled thing runs and writes real status, verified by read-back |
| **2. Fly connector** | releases, machine config, health/ready, config drift, checkout freshness | Real observations in Neotoma; history shows image reuse |
| **3. App view** | `/api/connectors`, `#/connectors`, deployment history | Reads **Neotoma**; renders age; stale is visibly stale |
| **4. Alarming** | version-behind, config-would-shrink, connector-failing | Alarms fire from durable state, suppressed when stale |
| **5. GitHub connector** | issues + PRs **with their parent edges**, replacing client-side polling and superseding `sync_issues` | **Held** until the Neotoma performance fix lands |

### GitHub sync is folded into this contract — design of record

Operator decision (2026-09-02): *"For the GitHub to Neotoma sync question, it
seems like it is best to fold it into that connector work."*

So there is **no separate GitHub→Neotoma sync mechanism**. `sync_issues` and any
successor are stage 5 of this contract, not a parallel path.

The reason is the divergence problem, and it is not theoretical here: a config
audit found four live instances of the copied-mechanism shape in this codebase,
including a gate set duplicated four times with two copies already wrong — one
of them a **re-introduction of a bug that had been fixed in the dispatcher and
never fixed in the label path**. A second sync beside this one would be the
fifth instance, and the connector contract exists precisely so that staleness,
idempotency, budgets, and status are defined once.

#### The sync must carry the RELATIONSHIP, not just the row

A `pull_request` entity with no edge to the work that motivated it reproduces
today's blindness *inside* Neotoma rather than fixing it. The evidence:

- Lanius blocked PR #687 with exactly this: *"PR body scanned for `Closes #N` /
  `Fixes #N` — none found. Thematic Neotoma search — no linked parent issue."*
  Confirmed firsthand — that PR's body genuinely carries no closing keyword.
- Its task entity (`ent_f6b3103a0042eeeb95f7b606`) exists and is current, but
  nothing connects it to the PR, so the gate cannot find it.
- **24 of 56 open PRs had no detectable parent**, and every agent-opened PR that
  day carried a `pipeline-bypass-notice` for this reason.

The writer side is already filed as `ent_eb763ee6d04ec1c0e4a5dbb9` — *"Store the
`pull_request PART_OF issue` edge at PR-open instead of re-deriving it by
regex"*. **Coordinate with it rather than solving this twice.** The division:

| | Owner |
|---|---|
| **Writing** the edge at PR-open, from dispatch context | `ent_eb763ee6d04ec1c0e4a5dbb9` |
| **Reading/preserving** it during sync, and not inventing a second scheme | stage 5 (this contract) |

That task's resolution order is the one to honor: **(a) the dispatch context
that opened the PR, (b) the body closing-keyword, (c) the branch name** — with
regex as fallback only for human-authored PRs. Its central observation is worth
restating, because it is what makes the fix cheap: *the swarm already knows the
parent when it opens a PR, discards that knowledge, then re-infers it from
prose.*

So the sync's job is to **preserve an edge that exists**, and to fall back to
the widened regex only when one does not. It must not invent a third derivation
scheme — that would be the divergence problem again, one layer down.

#### Every upstream timestamp is an idempotency hazard

A synced `pull_request` carries GitHub's `updated_at`, which is **exactly the
shape of field that poisoned `sync_issues_from_github.ts`** — see the trap
section above. The rule generalizes beyond our own `observed_at`:

> Any timestamp that changes without the record's substance changing must be
> stripped before hashing, or kept out of the payload entirely.

`observation_payload()` strips our clock. **Stage 5 must extend
`_VOLATILE_FIELDS`** to cover the upstream timestamps it carries, or key on the
source's `updated_at` the way the existing sync now does — deliberately, and
with a test proving an unchanged upstream record re-syncs as a byte-stable
no-op. Getting this wrong does not fail loudly; it freezes the records
permanently.

#### Sequencing is unchanged

Fly first, still. Fly is 16 releases; GitHub is 661 issues plus 56 PRs against a
datastore that was unreachable as recently as yesterday. The contract gets
proven against trivial volume before it is pointed at the large, slow source —
which is the ordering the previous runaway did not have.

### Alarming needs no new plumbing

Anthus already subscribes over SSE to `daemon_report` and surfaces `error` /
`critical` to the operator. A connector that emits a `daemon_report` at
`severity: "error"` is therefore paged for free, and stage 4 is mostly deciding
*what deserves an alarm* rather than building a delivery path.

The staleness rule binds hardest here: **an alarm computed from a stale
observation must not fire.** "Production is 5 releases behind" derived from a
day-old reading is exactly the false-authority failure this design exists to
prevent. Suppress, and alarm on the connector's own failure instead — a
connector that stopped working is the more actionable fact.

**Fly is first** because its volume is trivially small (16 releases) versus
GitHub's 661 issues, and because GitHub's sync would run against a currently
502-ing instance — which is how the last runaway happened. Building the general
contract against the small source first is what makes the large one safe.

Stage 1's acceptance criterion is the trigger, not the code. That is the
difference between this and `sync_issues`.

## Install and verify

Install the launchd resident agent from the repo root:

```bash
./execution/daemons/connectors/install.sh
```

Stage 2a registers `FlyConnector` by default in
`execution/daemons/connectors/connectors_daemon.py` (`build_connectors()`).
Unbound hosts still start cleanly: Fly observes as a non-alerting **skip** until
binding env is set. To leave Fly off the list entirely, set
`ATELES_CONNECTORS` to a comma-separated allow-list that omits `fly`.

### Binding and auth (env names only)

| Variable | Role |
|---|---|
| `FLY_APP` **or** `DEPLOYMENT_CONFIGURATION_ID` | Binding — app from env, or a `deployment_configuration` entity that carries `fly_app` |
| `FLY_CONFIG_PATH` | Optional; default `fly.toml` |
| `FLY_API_TOKEN` | Fly API auth (with `flyctl` on `PATH`) |
| `NEOTOMA_BEARER_TOKEN` | Neotoma writes (base URL via `NEOTOMA_BASE_URL` when not default) |
| `ATELES_CONNECTORS` | Optional allow-list; omit `fly` to disable Fly |

Before durable Fly writes succeed, connector schemas must verify:

```bash
python3 execution/daemons/connectors/register_schemas.py --json
```

### One-pass verify (bound vs unbound)

```bash
# Unbound (no FLY_APP / DEPLOYMENT_CONFIGURATION_ID):
python3 execution/daemons/connectors/connectors_daemon.py --once
# expect: fly skip/empty + remediation string mentioning FLY_APP /
#         DEPLOYMENT_CONFIGURATION_ID; 0 deployment_observation writes;
#         NO alert path

# Bound:
FLY_APP=<your-app> FLY_API_TOKEN=… NEOTOMA_BEARER_TOKEN=… \
  python3 execution/daemons/connectors/connectors_daemon.py --once
# expect: connector_status for fly; deployment_observation rows verified by
#         read-back when releases exist (after schemas are registered)
```

Verify the scheduled trigger, not just the manual command:

```bash
launchctl list | grep com.ateles.connectors
tail -f ~/Library/Logs/ateles/connectors.log
```

The expected first log story is `connector daemon starting — poll every 900s`,
then a pass with **Fly registered** (either `skipped — …` when unbound, or a
real observe when bound) — not Stage 1's empty "no connectors registered".

Installer failures should be actionable:

- Missing `com.ateles.connectors.plist`: exits `1` before `launchctl bootstrap` and
  prints the exact missing path plus the manual `--once` fallback.
- `launchctl bootstrap` failure: inspect `~/Library/Logs/ateles/connectors.log` and
  `launchctl print gui/$UID/com.ateles.connectors`.
- Neotoma unavailable: the resident loop logs the failure and continues; a
  manual `--once` returns non-zero when registered connectors hard-fail or when
  Stage 2a status/observation read-back cannot be verified.

## Schema registration — register, then verify by read-back

Do **not** treat schemas as done from a write success code or from #671 being
CLOSED. Live prod historically held a legacy chat/provider `connector_status`
v1.0 (no `connector_name` / machine / drift fields) and **no**
`deployment_observation` type. Stage 2a registers:

- **`connector_status` v2.0** — supersedes the legacy active shape for daemon
  use. Identity: `canonical_name_fields: ["connector_name"]`. Declares Stage 1
  status fields plus Fly Stage 2a machine/drift fields. Reducers: `last_write`
  + `observed_at` tie-breaker on every mutable non-identity field.
- **`deployment_observation` v1.0** — append-only. Identity: composite
  `instance_ref + release_id` (not `version` — same version label can cover
  distinct images). Empty `merge_policies`. Deploy actor is stored as an
  **opaque** `triggered_by` hash, never a raw email.

```bash
python3 execution/daemons/connectors/register_schemas.py --json
# exit 0 only when both schemas verify by GET /schemas/{type} read-back
```

Fly durable writes are gated on that contract matching. Until read-back passes,
bound observes fail loud with a remediation pointing at `register_schemas.py`
rather than writing undeclared fields into `raw_fragments`.

Two registration notes for whoever adds the next type:

- The REST route is **`POST /register_schema`**, not `/schemas/register` — the
  latter 404s. This is the same MCP-tool-name-is-not-a-route trap that
  `scripts/linters/check_neotoma_rest_paths.py` exists to catch.
- The server's pluralization lint may warn on `connector_status` (suggesting
  `connector_statu`). "Status" is not a plural; `register_schemas.py` passes
  `force: true` for that type.

### Observation model note (privacy)

Release history records who triggered a deploy as an opaque `triggered_by`
ref (hash of the Fly user email/name). No raw `@` addresses are stored in
`deployment_observation` rows.
