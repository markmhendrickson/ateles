# Neotoma degradation watchdog (Nyctea)

Detecting the state the swarm had no name for: **up, answering health checks,
and unable to serve a single read.**

## The incident (2026-09-01)

The operator noticed the UI was slow and asked why. Measured against the hosted
instance, three consecutive times:

```
GET  /health                        -> 200 in 0.89s
POST /entities/query {"limit": 1}   -> timeout at 45s
```

Nothing in the swarm noticed. Every liveness check was green, because `/health`
returns `{"ok":true,"version":"0.22.1"}` without touching the database
(ateles#577). The swarm's entire durable memory was unreadable and the only
detector was a human looking at a slow page.

Earlier in the same session the same query measured 16s, then 90s, then 100s,
then timed out. The degradation was **progressive**, and it tracked concurrent
agent load — roughly 13 agents querying at once, plus a dev server and the UI.

## Which instance is actually in play

This matters, because the first pass of the investigation looked at the wrong app.

| | |
|---|---|
| Canonical host | `neotoma.markmhendrickson.com` |
| DNS | → `neotoma-markmhendrickson.fly.dev` (66.241.124.140) |
| Fly app | **`neotoma-markmhendrickson`** (org `neotoma`) |
| Machine | `d896099a33e228`, ams, **performance-2x, 2 vCPU, 4GB** |

The app named `neotoma` (org `personal`, machines `0805621c302d78` and
`2873566c535238`) is **a different, retired app**, last deployed 2026-05-12. Its
config does have `auto_stop_machines: true` / `min_machines_running: 0`, but it
does not serve the swarm and its autostop explains nothing about this incident.

### Why the machine stopped — and why autostop is not the answer

The production app has `auto_stop_machines: **false**` and
`min_machines_running: **1**`. There is no autostop, so cold start does not
explain the latency.

The machine's own event log gives the real reason:

```
stopped  exit  flyd  2026-08-31T22:49:22  exit_code=134,oom_killed=false,requested_stop=false
started  start flyd  2026-08-31T22:49:24
starting restart flyd 2026-08-31T17:52:13
```

Two self-restarts on 2026-08-31. **Exit 134 is SIGABRT**, which for Node is the
V8 fatal-error path — characteristically a JS heap OOM aborting the process.
`oom_killed=false` only means the *kernel* OOM-killer did not fire; V8 aborted
itself first, before the cgroup limit was hit. `requested_stop=false` rules out
a deliberate stop or a deploy.

So: the process is crashing under memory pressure and Fly's `restart: always`
policy brings it back. That is consistent with a slow read path holding large
result sets in memory, and with the observed pattern where load, not time,
predicts the latency.

The stopped machine in the *other* app was normal idle autostop and is unrelated.

### The second stopped machine

`2873566c535238` (app `neotoma`, lhr, stopped since 2026-05-12) belongs to the
retired app. It should **stay stopped**; it serves no traffic and starting it
would run a four-month-old image against the wrong dataset. Destroying it is
reasonable cleanup but is an operator decision and not urgent — a stopped
machine costs nothing. Neither machine in that app should be started.

## What the watchdog probes

`POST /entities/query {"limit": 1}`, authenticated, with a bounded timeout. The
cheapest request that still traverses auth, HTTP, the query planner, and the
database. `limit:1` returns a single row, so a slow result is a slow *system*,
never a large payload.

`/health` is called **only** to disambiguate a failure — never to produce a
healthy verdict.

### Verdicts and thresholds

| Verdict | Condition | Remedy |
|---|---|---|
| `HEALTHY` | read < 2s | none |
| `DEGRADED` | read succeeds, ≥ 2s | shed load; **do not restart** |
| `SATURATED` | read times out (20s), `/health` still 200 | shed load, consider scaling; **do not restart** |
| `WEDGED` | read times out **and** `/health` fails | restart defensible |
| `UNREACHABLE` | nothing answers | network / Fly |

All thresholds are env-overridable (`NYCTEA_DEGRADED_SECONDS`,
`NYCTEA_READ_TIMEOUT_SECONDS`).

The 20s read timeout is chosen because past that point every real caller has
already given up — waiting 90s measures something no agent would ever wait for.

**`SATURATED` vs `WEDGED` is the load-bearing distinction.** They look almost
identical and their remedies are opposite. A restart against a saturated
instance drops in-flight writes to fix a queue that refills in seconds. During
this incident a restart was very nearly taken against what was actually
saturation.

Latency is recorded on every probe, not just success/failure, because a boolean
probe turns a 16s → 90s → timeout ramp into a cliff that arrives without warning.

## Escalation: why it bypasses quiet hours

Saturated/wedged/unreachable escalate at `Priority.CRITICAL`, which
`lib/notify` delivers immediately even inside the silence window.

This is deliberate, against a known failure: 34 escalations were queued into a
quiet-hours digest overnight and **zero** digest sends fired, so the operator
was never told about 12 stalled PRs (ateles#626/#627). A watchdog that queued
into that same channel would reproduce the exact failure it exists to prevent.

The justification is specific, not "this alert feels important": while Neotoma
is unreadable, **every other alerting path is also degraded**, because daemons
resolve config, rubric, and escalation targets from Neotoma. A Neotoma outage is
the one condition under which silence cannot be read as "nothing is wrong" — it
is indistinguishable from "everything is wrong and nothing can say so".

`DEGRADED` escalates at `BLOCKER` (sent now, no silence bypass) — a slow read is
worth knowing about, not worth waking someone.

To stop the bypass becoming noise, Nyctea escalates only on sustained conditions
(2 consecutive cycles) and on transitions, and re-pages at most every 30
minutes. Recovery is announced exactly once, so an operator paged at 03:00 knows
it cleared without going to look.

## Recovery: automated vs deliberately manual

**Automated: nothing that mutates infrastructure.** This is a position, not an
omission.

- **Restart — not automated.** Under saturation the instance is alive and
  working; restarting loses in-flight writes for no benefit. `WEDGED` and
  `SATURATED` are separable only by a liveness signal that is itself flapping
  under load, which is exactly when an automated restarter misfires. Fly already
  restarts the machine on crash (`restart: always`, and it has done so twice);
  a second, dumber restarter races the working one.
- **Scaling — not automated.** It costs money, so it is presented with a number,
  never taken.

**Automated: load shedding**, because the load is the swarm's own doing.
`lib/neotoma_concurrency.py` caps concurrent readers (default 4). It is
reversible, costs nothing, and cannot lose data.

Honest limitation: that semaphore is **process-local**. It bounds one daemon's
fan-out, not the ~13 independent agent processes that caused this. A true
system-wide cap belongs in Neotoma itself.

## Would the open Neotoma PRs have prevented this?

- **neotoma#2217 (2-worker reader pool, no statement timeout)** — this is the
  more important one, and it is a cause rather than a mitigation. Two workers
  and *no statement timeout* means a couple of slow queries block every
  subsequent read indefinitely, which is precisely the observed behaviour: reads
  that never return while the process happily answers `/health`. Adding a
  statement timeout would convert an indefinite hang into a fast error — it
  would not make the instance fast, but it would make it *fail honestly*, which
  is what the swarm needs to react.
- **neotoma#2267 (query fix, MERGEABLE)** — reduces per-query cost. That delays
  the onset under load; it does not change the failure mode. With no statement
  timeout and a 2-worker pool, enough concurrency still wedges the pool.

Neither would have *prevented* this. #2217 would have made it visible; #2267
would have made it slower to arrive.

## Self-check

The defining defect class here is components that die quietly — Anthus dead 2
months, Apis deaf 88 days, the tailer dying in a pause. A watchdog that dies
silently is worse than none, because its silence reads as health.

Nyctea writes a heartbeat on **every** cycle, including healthy ones where
nothing is sent:

```
python3 execution/daemons/nyctea/nyctea.py --self-check   # exit 0 iff alive
```

The exit-code contract lets another daemon, a cron line, or the operator answer
"is the watchdog alive?" without trusting Nyctea's own reporting.

## Operator decisions outstanding

1. **Scale the instance.** Currently performance-2x / 2 vCPU / 4GB, crashing
   with SIGABRT under load. Doubling memory addresses the crash signature
   directly; the cost is small relative to a swarm-wide memory outage. Needs the
   operator's approval because it costs money.
2. **Land neotoma#2217 with a statement timeout.** The highest-value fix: it
   converts indefinite hangs into errors the swarm can see and react to.
3. **Cap swarm-wide concurrency.** 13 concurrent readers against 2 vCPU is too
   many. The per-process semaphore here is a partial measure.
4. **Retired `neotoma` app.** Leave both machines stopped; destroy at leisure.
