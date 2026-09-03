# Work model: how work moves through the swarm

**Kernel document:** read on every review (`conformance.md`). **Kind:** foundation; states the design and
never the state of a checkout. **Derived from:** synthesis `ent_b0ce322f768e4fc676b73139` (PR-01 to PR-03,
PR-05, C1, C2), prior art `ent_08460968e6f49dac21510f4a` (Track 1), task `ent_da60df3beccb675ef8c8c0c5`
(description and result), throughput plan `ent_18b902cf72822373f9da8ced` decisions
`pull_model_sequencing_build_the_claim_not_the_router`, `non_github_execution_makes_pull_decisive`,
`three_execution_mechanisms_not_one`. Supersedes `docs/archive/task_execution_loop.md`, which described the
push model. What is built is `status.md`.

## Purpose

State how a unit of work is created, taken, run, and returned: pull over push, the claim and the lease as
one primitive, liveness derived at read time, no dispatch log, and the transition vocabulary.

## Scope

The task path: a Neotoma `task` claimed and run by an agent. The two other execution mechanisms are named
below; their gate model is `gates_and_workflows.md`; who may act on what is `authority_model.md`.

## The invariants

### Pull over push; push only for named ownership

Agents claim work from the queue. No central router assigns owners on the common path; push is reserved for
work where the named principal is the point: an operator-only task, a gate handed to its gate owner. Reason: a
router that resolves an unknown assignee by inference misroutes silently, and a misroute on the task path
reaches an executor that acts. A claim cannot misroute because nothing routes: the claim predicate reads
`assigned_to` directly, and the agent judging "is this mine" holds its own `agent_definition`, which no
central table encodes. An `assigned_to` naming an assignee nobody can spawn blocks visibly; it never falls
through to inference. Non-code agents are the first consumers: their work has no file paths or closure
keywords for a keyword matcher, and `assigned_to == me` needs none.

### The claim and the lease are one primitive

Two agents reading one queue must not both take a task; a killed runner must not strand one. Both need an
atomic claim that expires, requiring no cooperation from a dying process. Atomicity is proven by the
implementation, never assumed of the store: the snapshot is last-writer-wins, and a name-collision policy
that de-duplicates into one row does not raise. So the claim is keyed on the task, claimants collide on one
row, and the holder is read back after the write: an agent holds the task only if the persisted holder is
its own runner id (principle 2).

### Liveness is derived at read time, never declared

`running` is computed: claim held and `last_activity_at` within the lease window. A stored flag asserting
liveness fails exactly when it matters, because the process that would clear it is the one that died. No
status value asserts liveness; `executing` as a liveness assertion is retired. A dashboard derives
live-versus-stranded from the lease, not from a status field.

### No dispatch log; history is the task's own observations

Neotoma is append-only: every status change is an observation with timestamp and provenance, so when a
task became claimable, who claimed it, and when it went quiet are answerable from the task. A parallel
dispatch entity would be a second source of truth (principle 9). Claims are 1:N with tasks; the lease
answers the one question history cannot: is a process holding this right now. `agent_session` carries the
identity half observations lack (host, checkout, branch, head), not a run history.

### The transition vocabulary: `created / claimed / running / released`

`created`: the task exists in the record; publication is creation, and there is no separate "published"
state. `claimed`: one agent holds the lease. `running`: derived, above. `released`: the lease is returned,
by completion, failure, or expiry; a released task is claimable again unless its status is terminal. Each
word names one event, so a stranded task is a `claimed` task whose lease has lapsed, not a task "stuck in
executing". `dispatch` names only the push exception. Definitions and forbidden synonyms: `vocabulary.md`.

### What a claim predicate treats as claimable

The status vocabulary in the record is what tasks actually carry, not what an enum in code declares; a
predicate is written against the record (principle 3), and a normalizer that fails to map a live value onto
a terminal one makes finished work claimable. Claimable: not terminal (`completed`, `done`, `canceled`), not
`blocked`, and no live lease. Unassigned tasks are an open pool; an assigned task is claimable only by its
assignee. The measured distribution of status values is `status.md`.

### The reaper releases; it does not re-route

The watchdog's signal is the lease's expiry, not a generic last-updated timestamp, since any unrelated write
resets the latter. Its job is to release an expired claim. Repeated expiry on one task escalates rather than
retrying forever, with bounded backoff (`failure_posture.md`). It never chooses a new assignee.

### At-least-once implies effect dedup

A lease that expires and a task that is re-claimed is at-least-once delivery. Every outbound effect is
idempotent or deduplicated on its own key from the first implementation, the position
`durable_execution_substrate.md` records, and a re-claimed task is never replayed (`failure_posture.md`).

## The three execution mechanisms

The invariants above describe the task path. Two others exist (`three_execution_mechanisms_not_one`):
dedicated daemons that self-trigger on their own loop (a mail poller produces tasks and never receives one),
and the GitHub pipeline, which spawns an implementer, a steward, a gate-inheritance checker, and the review
panel by named role and never writes a task status. The pipeline is the push exception at scale: every
spawn is to a declared gate owner. A roster role reachable by none of the three cannot receive work; the count is
`status.md`. A daemon showing that a non-code agent can self-trigger does not show it can receive work.

## Contradictions this document settles

**C1, push state machine versus pull vocabulary.** The task-spine plan `ent_aff87747b49e338790568af6`
(`task_lifecycle_state_machine`) and the archived loop document define `created, routed, executing,
verified, done` with a router assigning owners. Resolved for pull: `executing` asserts what it cannot back,
and routing is the misroute. That plan's state-machine criteria are superseded by this document; the
correction to the plan is a request to that plan's maintainer.

**C2, "tasks authorize every side effect" versus three mechanisms.** `subscriptions_detect_tasks_authorize`
is true of the task path and false of the self-triggering daemons, whose side effects never pass through a
task. Settled: a daemon's outbound action passes through the same execution gate whether or not a task
carried it there (`gates_and_workflows.md`). Open: whether to unify the daemon loop with the task path or
keep both under an explicit contract (digest `ent_e04244959daf92416597ce28`).

## Prior art

The claim-with-lease is the SQS visibility timeout almost exactly: a received message is invisible while
held and visible again on expiry with no cooperation from the consumer, renewal as the heartbeat,
at-least-once. Not shared: the 12-hour cap (tasks run for days) and deletion on success (observations are
the history). Temporal confirms the no-probe posture: its server does not detect a crashed worker; liveness
is timeout expiry, and worker pull is its model verbatim. Camunda supplies the vocabulary: `claim()` throws
when already claimed, `setAssignee()` overrides with no check. Postgres `SKIP LOCKED` is the property a
claim needs; the substrate has to prove it. Sources: `ent_08460968e6f49dac21510f4a`.
