# Work model: how work moves through the swarm

**Vision phase:** P1 (governed execution for one principal). **Kind:** consolidation, not design.
**Derived from:** synthesis `ent_b0ce322f768e4fc676b73139` (PR-01 to PR-03, PR-05, C1, C2), prior art
`ent_08460968e6f49dac21510f4a` (Track 1), task `ent_da60df3beccb675ef8c8c0c5` (description and result),
throughput plan `ent_18b902cf72822373f9da8ced` decisions `pull_model_sequencing_build_the_claim_not_the_router`,
`non_github_execution_makes_pull_decisive`, `three_execution_mechanisms_not_one`. Code read on `origin/main`
at `496bab3`, 2026-09-02. Supersedes `task_execution_loop.md` (archived), which described the push model.

## Purpose

State how a unit of work is created, taken, run, and returned: pull over push, the claim and the lease as
one primitive, liveness derived at read time, no dispatch log, and the transition vocabulary.

## Scope

The task path: a Neotoma `task` claimed and run by an agent. The two other execution mechanisms are named
below; their gate model is `gates_and_workflows.md`. The claim primitive is designed (task
`ent_da60df3beccb675ef8c8c0c5`) and open as ateles#733; nothing on main implements it.

## The invariants

### Pull over push; push only for named ownership

Agents claim work from the queue. A central router assigning owners is retired for the common path; push is
reserved for work where the owner is the point (an operator-only task, a gate assigned to a named reviewer).
Measured reason: `routing.py` resolves an unknown assignee by keyword fallthrough, so a task assigned to a
name with no route silently runs under a different agent (ateles#702, open; the mechanism is intact at
`496bab3`). Pull cannot misroute because nothing routes: the claim predicate reads `assigned_to` directly,
and the agent judging "is this mine" holds its own `agent_definition`, which no central table encodes.
Non-code agents are the first consumers: their work has no file paths or closure keywords for a keyword
matcher, and `assigned_to == me` needs none.

### The claim and the lease are one primitive

Two agents reading one queue must not both take a task; a killed runner must not strand one. Both need an
atomic claim that expires, requiring no cooperation from a dying process. Correction from the
implementation (task result, 2026-09-02): `agent_session`'s `name_collision_policy: reject` de-duplicates
into one row and does not raise, so atomicity is not a store property. The claim is keyed on the task
(`native_session_id = "task:<entity_id>"`) so claimants collide on one row, and the holder is read back
after the write: an agent holds the task only if the persisted holder is its own runner id. Any
implementation must prove this, since the snapshot is last-writer-wins.

### Liveness is derived at read time, never declared

`running` is computed: claim held and `last_activity_at` within the lease window. A stored flag asserting
liveness fails exactly when it matters, because the process that would clear it is the one that died.
`EXECUTING` is retired as a liveness assertion. On main `task_lifecycle.py` still defines `executing` and
`apis.py` writes it before the spawn with no `finally`; the dashboard derives live-versus-stranded from the
lease, not from that field.

### No dispatch log; history is the task's own observations

Neotoma is append-only: every status change is an observation with timestamp and provenance, so when a
task became claimable, who claimed it, and when it went quiet are answerable from the task. A parallel
dispatch entity would be a second source of truth (principle 9). Claims are 1:N with tasks; the lease
answers the one question history cannot: is a process holding this right now. `agent_session` is reused for
the identity half observations lack (host, checkout, branch, head), not as a run-history table.

### Vocabulary: `created / claimed / running / released`

`created`: the task exists and is claimable (publication is creation). `claimed`: one agent holds the lease.
`running`: derived, above. `released`: the lease returned, by completion, failure, or expiry. `dispatch` is
reserved for the push exception; it formerly did three jobs (publication, claim, execution), and the
conflation was visible in the day's bugs: a field-less task was invisible rather than "undispatched"
(ateles#698), and a killed runner pinned a task at `EXECUTING` because claim and execution were fused.
Definitions and forbidden synonyms: `vocabulary.md`.

### What a claim predicate treats as claimable

The prod status vocabulary is not `TaskStatus`. A 500-row sample, 2026-09-02: `completed` 329, `pending`
87, `open` 31, unset 19, `done` 7, `in_progress` 6, `canceled` 6, `todo` 5, `queued` 1, `blocked` 4.
`normalize()` does not map `completed` onto `done`, so an enum-based predicate would treat 329 finished tasks
as claimable. Claimable is: not terminal (`completed`, `done`, `canceled`), not `blocked`, no live lease.
`assigned_to` is unset on 492 of 500 rows, so unassigned tasks are an open pool; an assigned task is
claimable only by its assignee.

### The reaper releases; it does not re-route

`task_watchdog.py` already sweeps stalled tasks with backoff, `MAX_ATTEMPTS`, and escalate-on-exhaustion.
Its weakness is the signal: `_age_seconds` infers liveness from `updated_at`, so any unrelated write resets
the clock. Under the lease it takes the claim's expiry, and its job is to release an expired claim. Repeated
expiry on one task escalates rather than retrying forever (`failure_posture.md`).

### At-least-once implies effect dedup

A lease that expires and a task that is re-claimed is at-least-once delivery. Every outbound effect is
idempotent or deduplicated on its own key from the first implementation, the position
`durable_execution_substrate.md` records.

## The three execution mechanisms

The invariants above describe the task path. Two others exist (`three_execution_mechanisms_not_one`):
dedicated daemons that self-trigger on their own loop (Turdus polls mail and produces tasks; it never
receives one), and the GitHub pipeline (`swarm_dispatch.py`, which spawns Cicada, Vanellus, Lanius, and the
review panel and never writes a task status). Of 37 roster roles, 12 have a daemon; 25 are reachable by none
of the three automatically. Turdus shows a non-code agent can self-trigger, not that it can receive work.

## Contradictions this document touches

**C1, push state machine versus pull vocabulary.** The task-spine plan `ent_aff87747b49e338790568af6`
(`task_lifecycle_state_machine`) and `task_execution_loop.md` define `created, routed, executing, verified,
done` with Apis routing. Resolved for pull: `executing` asserts what it cannot back, and routing is the
misroute. That plan is read-only here; retiring its state-machine criteria is a request to its owner.

**C2, "tasks authorize every side effect" versus three mechanisms.** `subscriptions_detect_tasks_authorize`
is true of the task path and false of the 18 daemons, whose side effects never pass through a task. Stated,
not resolved: whether to unify the daemon loop with the task path or keep both under an explicit contract is
open (digest `ent_e04244959daf92416597ce28`). What is settled is that a daemon's outbound action passes
through the same execution gate whether or not a task carried it there.

## Prior art

The claim-with-lease is the SQS visibility timeout almost exactly: a received message is invisible while
held and visible again on expiry with no cooperation from the consumer, renewal as the heartbeat,
at-least-once. Not shared: the 12-hour cap (tasks run for days) and deletion on success (observations are
the history). Temporal confirms the no-probe posture: its server does not detect a crashed worker; liveness
is timeout expiry, and worker pull is its model verbatim. Camunda supplies the vocabulary #702 lacks:
`claim()` throws when already claimed, `setAssignee()` overrides with no check. Postgres `SKIP LOCKED` is the
property a claim needs; the substrate has to prove it. Sources: `ent_08460968e6f49dac21510f4a`.

## Status on main

No claim, lease, or queue primitive exists under `lib/` or `execution/daemons/` at `496bab3`;
`TaskReconciler.claim()` is an in-process set. ateles#733 (the primitive) and #702 (the fallthrough) are
open. The mechanism lands when they merge and the deployed checkout moves.
