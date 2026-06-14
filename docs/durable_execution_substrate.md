# Neotoma as a durable-execution substrate

Whether Ateles (and Neotoma integrators generally) should adopt Temporal — or instead extend Neotoma so durable execution is a native capability that obviates Temporal long-term.

## Purpose

Record the architectural position behind the standing "evaluate Temporal at Phase 3" line in `docs/phases.md` and `docs/architecture.md`. This note argues that the higher-leverage path is to absorb a small set of execution primitives into Neotoma rather than bolt on a second orchestration engine, and specifies the design hedges that keep that bet reversible if scale ever demands a dedicated engine.

## Scope

Covers (a) why Neotoma is already most of a durable-execution engine, (b) the four primitives that would close the gap, (c) the scale/isolation scenarios where the native bet fails, and (d) the cheap design-time hedges that turn "we bet wrong" into a per-workflow-class backend swap instead of a rewrite. Does not specify schemas in full — those land as `register_schema` work when the primitives are built.

## Position

**Do not adopt Temporal as a second source of truth. Extend Neotoma so durable execution is native, behind a swappable execution-backend port, with declarative workflow definitions.**

Neotoma is already ~70% of a durable-execution engine: a durable, versioned, idempotent event log with pub/sub (SSE) and a relationship graph. The expensive part of a system like Temporal — crash-safe durable storage plus reliable event delivery — is already owned. Adopting Temporal instead introduces a second source of truth with an opaque, replay-based execution model that fights Neotoma's thesis that every state transition is a legible, inspectable entity.

## What exists today

| Capability | Where |
|---|---|
| Durable, versioned event log | Neotoma entity corrections (timestamped, provenance) |
| Event delivery | SSE subscriptions; `lib/daemon_runtime/sse_client.py` (exponential reconnect, base 2s / max 60s) |
| Idempotency keys | `idempotency_key` on store/correct calls |
| Human-in-the-loop gates | `checkpoint_brief` + operator approval (Telegram) |
| Per-plan autonomy policy | `execution_policy` + `lib/daemon_runtime/gating.py` |
| Phase/gate progress record | `participation_record` (`execution/daemons/anthus/participation.py`) |

What is missing: a first-class resumable run, durable timers, a formal effect ledger, and retry policy as data. Scheduling today is host-bound to launchd (`StartCalendarInterval` in `.plist` units) — there is no Neotoma-native durable timer.

## The four primitives to absorb

| Primitive | Replaces (from Temporal) | Status |
|---|---|---|
| **`run` / `workflow_execution` entity** — the durable, resumable unit (definition_ref, current phase, status, cursor, attempt counts, lease/owner) | Workflow execution + history | Half-built; generalize `participation_record` |
| **Durable timers** — a Neotoma `scheduled_wake` (next_wake_at) reliably re-delivered via SSE when due, swept server-side | Durable `sleep` / timers | **Missing — keystone.** Today launchd, host-bound, macOS-only |
| **Step/effect ledger** — `(run_id, step_name)` records so re-execution after a crash dedups side effects | At-least-once activities + idempotency | Informal; formalize existing keys into a queryable ledger |
| **Retry policy as data** — backoff/timeout/max-attempts on `execution_policy`, honored by a runtime executor | Retry policies | Partial; extend schema + add executor loop |

Signals are already solved — SSE events and `checkpoint_brief` status flips *are* signals; they need correlation by `run_id`. The "engine" is a small reducer in `daemon_runtime`: *load run → next step from definition → check gate → execute / schedule-timer / await-signal / await-checkpoint → record step → persist new state → repeat.* Anthus's orchestrator becomes one consumer of it rather than a bespoke implementation.

**Durable timers are the keystone.** They are the one thing currently host-bound to launchd, and the single primitive an integrator on arbitrary infra will reach for Temporal/Inngest *precisely and only* to get. A Neotoma-native `scheduled_wake` turns a dependency you'd otherwise tell integrators to add into a built-in capability.

## When the bet is wrong

The bet is "Neotoma-as-execution-engine." It fails along scale/isolation axes — each a case of asking the entity store to do a scheduler/queue's job. None are *correctness* failures; the native model stays correct at any scale. What crosses over is operational cost.

| Axis | Native model is fine when… | It breaks when… |
|---|---|---|
| **Throughput / step rate** | dozens–low-thousands of in-flight runs, minute-resolution timers | thousands+ concurrent runs; sub-second timers; high step-rate workflows → timer sweep becomes a hot loop, correction writes contend, SSE fanout saturates |
| **Hot partition** | signals/runs spread across many entities | many signals into one run, or many runs mutating shared entities → optimistic-concurrency retry storms, lease thrashing |
| **Heavy compute activities** | activities are short calls (today: fire-and-forget `claude --print` subprocesses) | activities are long / CPU-GPU-bound and need worker pools, heartbeats, queue-depth backpressure — an entity store does not *schedule compute* |
| **Multi-tenancy** | single operator | integrator serves many end-customers needing isolation, per-tenant quotas, fairness, noisy-neighbor protection |
| **Visibility at scale** | "list stuck runs" is a `retrieve_entities` query | tens of thousands of runs needing dashboards, bulk-terminate, replay tooling |

## The hedge: one principle, not premature infrastructure

**Keep Neotoma as system-of-record always; make the execution backend a swappable port; keep workflow definitions declarative data.**

If those three hold, "the bet was wrong for workflow-class X" becomes *swap the backend for X to Temporal/Inngest* — not a rewrite. Even in that scale-out world Neotoma stays canonical: Temporal becomes an ephemeral *coordinator* for hot workflow classes that writes its results back through the same step/effect ledger. Truth never leaves Neotoma; only the coordination of execution moves. That is the difference between "we adopted Temporal" (two truths) and "we slid a faster engine under one workflow class" (one truth, swappable executor).

The integrator pitch this enables is stronger and more honest than "you will never need Temporal": **durable execution natively up to substantial scale, with a pre-specified graduation path that does not touch your definitions or your system-of-record.** Proactive integrators value *no rewrite cliff* over *never needing the other thing*.

## Proactive steps — split by cost

Discipline: pay the cheap design-time hedges now; defer every expensive operational one behind a documented trigger. Premature sharding is as much a failure as a missing seam.

### Do now (cheap — schema/specification decisions, ~no infra)

1. **Declarative definitions, thin reducer.** Workflow logic lives in `workflow_definition` data + a small `daemon_runtime` reducer — never buried in imperative daemon code. This is what makes definitions portable to another engine at all. Biggest single hedge.
2. **A real `ExecutionBackend` port** (schedule_timer, claim_run, persist_step, record_effect, deliver_signal). Build the Neotoma impl **and a trivial in-memory/test impl** — an abstraction with one implementation is fiction. The second backend keeps the seam honest and proves the Temporal swap is viable later.
3. **Mandatory idempotency / effect-dedup from day one.** Every external effect goes through the `(run_id, step)` ledger with a key, even at trivial scale. At-least-once + retries is only safe if effects dedup; retrofitting under load is where systems corrupt money/state. Impossible to backfill cleanly.
4. **Correlation + run-scoping on every event/signal** (`run_id`, `sequence`). Makes "all runs in state X" a free query and makes signal routing work unchanged at scale.
5. **Tenant/partition field on run/step/wake entities now**, even if always `"operator"`. Per-tenant isolation/quotas later become a feature flag, not a schema migration.
6. **One scheduling substrate, separated from logic.** Make the Neotoma `scheduled_wake` + a single sweeper the only timer source; business daemons react to wakes and own no cron. Scaling the scheduler (shard it, or hand it to a queue) then touches zero business logic. Directly retires the launchd coupling.
7. **A documented graduation runbook** with concrete thresholds: concurrent in-flight runs, p99 timer-sweep lag, correction write-contention rate, activity-duration distribution, tenant count. Breach → the pre-decided action is "swap backend for the hot class." A calm, pre-committed escape hatch is what a proactive integrator is asking for.

### Defer behind the trigger (expensive — do not build speculatively)

- The actual Temporal/Inngest backend implementation.
- Sharded sweepers, multi-region, worker pools / compute scheduling.
- Per-tenant fairness scheduling, quota enforcement, dashboards / replay tooling.

## What to explicitly not build (even in the native engine)

Temporal's deep complexity lives in things to deliberately refuse:

- **Deterministic code replay / sandboxed workflows.** Use explicit persisted state machines instead — more verbose, far more debuggable, aligned with "every state transition is an inspectable entity." This is where reinventing-Temporal-badly happens.
- **Sharded task queues / scale to millions of workflows.** Single-operator scale needs correctness and durability, not throughput.
- **History compaction / continue-as-new.** Not needed at this scale.

## Concrete first step

Generalize `participation_record` → a proper `run` entity, and add the `scheduled_wake` timer primitive in Neotoma. Those two close the actual Anthus in-memory-gate-state gap *and* lay the foundation that makes Temporal unnecessary long-term — a generalization of work already on the Phase 6 roadmap, not a greenfield detour.

## Relationship to the standing decision

This note is the recorded alternative to "evaluate Temporal at Phase 3 when 5+ daemons active and in-flight state loss has occurred; Inngest as fallback" (`docs/phases.md`, `docs/architecture.md`). It does not overturn that line — it reframes the evaluation: the trigger for *adopting an external engine* is recast as the graduation threshold in step 7, and the default investment shifts to native primitives behind a swappable port.
