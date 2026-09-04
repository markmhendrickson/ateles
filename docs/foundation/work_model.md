# Work model: how work moves through the swarm

**Kernel document:** read on every review (`conformance.md`). **Kind:** foundation; states the design and
never the state of a checkout. **Derived from:** synthesis `ent_b0ce322f768e4fc676b73139` (PR-01 to PR-03,
PR-05, C1, C2), prior art `ent_08460968e6f49dac21510f4a` (Track 1), task `ent_da60df3beccb675ef8c8c0c5`
(description and result), throughput plan `ent_18b902cf72822373f9da8ced` decisions
`pull_model_sequencing_build_the_claim_not_the_router`, `non_github_execution_makes_pull_decisive`,
`three_execution_mechanisms_not_one`, and PR #745 operator review (2026-09-04). Supersedes
`docs/archive/task_execution_loop.md`, which described the push model. What is built is `status.md`.

## Purpose

State how a unit of work is created, taken, worked, and returned: pull over push with assignment as the
only push; the claim and the lease as one primitive, the lease a relationship rather than task fields;
liveness derived from activity at read time; no assignment log; the transition vocabulary; how tasks
aggregate into workflow runs, split out of them, and nest under parents.

## Scope

The task path: a Neotoma `task` claimed and worked by an agent. The two other execution mechanisms are
named below; the step and gate model is `gates_and_workflows.md`; who may act on what is
`authority_model.md`; terms are `vocabulary.md`.

## The invariants

### Pull over push; assignment is the only push

Agents claim work from the queue. No central router chooses owners on the common path; the one push is
assignment, a principal restricting a task's eligibility to one named principal, used where the named
principal is the point: a step handed to its declared step owner, work a principal wants a particular
agent to take. Reason: the actor that judges fit should be the actor that acts and answers for the
outcome. A router's inference sits in an actor that neither acts nor answers for a misroute, so a wrong
guess reaches an executor with nobody accountable for the choice. A claim is a 1:1 judgment, "is this
mine", bounded by the claimant's own `agent_definition`, which no central table encodes; routing is a 1:N
choice with fallthrough, and fallthrough is where an unknown assignee is quietly resolved to somebody. A
claim also cannot fall through: the predicate reads `assigned_to` directly, and an `assigned_to` naming a
principal nobody can run blocks visibly. Non-code agents are the first consumers: their work has no file
paths or closure keywords for a keyword matcher, and a claim bounded by their own definition needs none.

### Assignment restricts eligibility; it never creates a lease

`assigned_to` is eligibility: who may claim. It is not the holder. An assignment leaves the task
unclaimed until the assignee claims it, so no principal ever "has claimed" a task it has not acted on; the
lease holder is always the claimant, and the claimant is read from the lease. Camunda's `setAssignee()`,
which installs a holder with no check, is the operation this design does not have. A task with an
`assigned_to` that names a principal who never claims is visible as assigned-and-unclaimed, which is a
fact about the assignee, not a stranded lease.

### The claim and the lease are one primitive

Two agents reading one queue must not both take a task; a killed runner must not strand one. Both need an
atomic claim that expires, requiring no cooperation from a dying process. Atomicity is proven by the
implementation, never assumed of the store: the snapshot is last-writer-wins, and a name-collision policy
that de-duplicates into one row does not raise. So the claim is keyed on the task, claimants collide on one
key, and the holder is read back after the write: an agent holds the task only if the persisted lease
names its own runner id (principle 2).

### The lease is a relationship, not a set of task fields

The lease is an edge between a principal and a task, carrying `claimed_at` and `expires_at`; renewal
moves `expires_at` and is the heartbeat. The task carries no claim fields. Its states are derived when the
edge is read: `held` while `expires_at` is in the future, `lapsed` once it has passed without the claimant
ending the lease, `returned` when the claimant ended it explicitly on completion or failure. Nothing
transitions a lease to `lapsed`; the clock does, and a reader sees it. This is principle 11 applied to the
one field that would otherwise need a watchdog to stay true.

### Liveness is derived from activity at read time, never declared

`active` is computed: a held lease and activity entities, an `agent_session` or observations, related to
the task within the lease window. A stored flag asserting liveness fails exactly when it matters, because
the process that would clear it is the one that died. No status value asserts liveness; `executing` and
`running` as liveness assertions are retired. A dashboard derives live-versus-quiet from the lease and the
activity, not from a status field.

### No assignment log; history is the task's own observations

Neotoma is append-only: every status change is an observation with timestamp and provenance, and every
lease is an edge with its own timestamps, so when a task became claimable, who claimed it, and when it
went quiet are answerable from the task and its edges. A parallel assignment or claim-history entity would
be a second source of truth (principle 9). Leases are 1:N with tasks over time; the current lease answers
the one question history cannot: is a principal holding this right now. `agent_session` carries the
identity half observations lack (host, checkout, branch, head), not a run history.

### The transition vocabulary

The task's own vocabulary is `created` plus its status. `created`: the task exists in the record;
publication is creation, and there is no separate "published" state. The lease carries `held`, `lapsed`,
and `returned`, above. `active` is a derived read, never a state. Each word names one thing, so a task
whose claimant died is a task with a lapsed lease, not a task "stuck in executing", and a task an
assignee has not taken is assigned-and-unclaimed, not "dispatched". Definitions and forbidden synonyms:
`vocabulary.md`.

### What a claim predicate treats as claimable

The status vocabulary in the record is what tasks actually carry, not what an enum in code declares; a
predicate is written against the record (principle 3), and a normalizer that fails to map a live value onto
a terminal one makes finished work claimable. Claimable: not terminal (`completed`, `done`, `canceled`),
not `blocked`, and no held lease. A lapsed lease is not a held lease, so it never blocks a claim and never
needs clearing. Unassigned tasks are an open pool; an assigned task is claimable only by its assignee. The
measured distribution of status values is `status.md`.

### A lapsed lease is not reaped; repeated lapse escalates

No process returns a lapsed lease, because a lapsed lease already does not count. The reaper is retired.
What remains is a rule about repetition: the watchdog observes lapses per task, and when one task lapses
past the cap it escalates rather than being re-claimed forever, with bounded backoff between attempts
(`failure_posture.md`). The watchdog holds no authority over leases and never chooses a new claimant.

### At-least-once implies effect dedup

A lease that lapses and a task that is re-claimed is at-least-once delivery. Every outbound effect is
idempotent or deduplicated on its own key from the first implementation, the position
`durable_execution_substrate.md` records, and a re-claimed task is never replayed (`failure_posture.md`).
Each such effect is an `action` entity (`gates_and_workflows.md`), which is where the dedup key lives.

### Operator-only tasks are claimed by the operator-facing agent

A task whose declared action classes include `operator_only` is not pushed to the operator; it is claimed
by the operator-facing agent (the `ateles` `agent_definition`), which carries the task to the operator,
holds the lease while the operator decides, and records the outcome. The `NEVER` tier still governs the
action: nothing executes without the operator, because the execution gate resolves `operator_only` to
`NEVER` ahead of any policy (`gates_and_workflows.md`). The task path stays pull; only the action waits on
a human.

### The unit that enters a workflow is the run, not the task

A task does not pass through a workflow; a `workflow_run` does, carrying a work item (an issue, a pull
request, a release) through the workflow's steps. Tasks attach to a run by an `ADDRESSED_BY` edge from
each task to the run, so several tasks may be addressed by one pull request that is then reviewed and
carried to release as one passage. Splitting is the reverse: detach one task's edge from the run and start a new run
for it; the original run continues with the tasks still attached. Aggregation and split are recorded as
edges, never as a field on the task or on the run (principle 11).

### A task is in at most one workflow run at a time

A task has at most one `ADDRESSED_BY` edge to a run that is not terminal. Sequential runs are normal: a
task addressed by a pull-request run and then by a release run has two edges, one to a finished run and
one to a live one. Work that needs two workflows at once is split into child tasks, one per run.

### Parent and child tasks

A parent aggregates children through `PART_OF` edges from each child to the parent; a task has at most one
parent. A parent's completion is derived from its children's terminal states, never stored. Children enter
workflow runs independently of one another. A parent never enters a workflow run itself; it is an
aggregate, and a run needs a work item, which a parent is not.

## The three execution mechanisms

The invariants above describe the task path. Two others exist (`three_execution_mechanisms_not_one`):
dedicated daemons that self-trigger on their own loop (a mail poller produces tasks and never receives one),
and the GitHub pipeline, which spawns a runner for the implementer, the steward, a step-inheritance
checker, and each lens of the review panel by declared step owner and never writes a task status. The
pipeline is assignment at scale: every spawn is to a step's declared owner, and the spawned runner still
claims the step it works. A roster role reachable by none of the three cannot receive work; the count is
`status.md`. A daemon showing that a non-code agent can self-trigger does not show it can receive work.

## Contradictions this document settles

**C1, push state machine versus pull vocabulary.** The task-spine plan `ent_aff87747b49e338790568af6`
(`task_lifecycle_state_machine`) and the archived loop document define `created, routed, executing,
verified, done` with a router assigning owners. Resolved for pull: `executing` asserts what it cannot back,
and routing places the judgment of fit in an actor that neither acts nor answers for it. That plan's
state-machine criteria are superseded by this document; the correction to the plan is a request to that
plan's maintainer.

**C2, "tasks authorize every side effect" versus three mechanisms.** `subscriptions_detect_tasks_authorize`
is true of the task path and false of the self-triggering daemons, whose effects never pass through a
task. Settled: a daemon's outbound effect is an `action` and passes through the same execution gate
whether or not a task carried it there (`gates_and_workflows.md`). Open: whether to unify the daemon loop
with the task path or keep both under an explicit contract (digest `ent_e04244959daf92416597ce28`).

**The reaper, retired.** Earlier drafts of this document and the archived loop document gave the watchdog
the job of releasing an expired claim. With the lease as a relationship whose lapse is read rather than
written, there is nothing to release; the watchdog keeps only its escalation rule. `failure_posture.md`
rules 4 and 5 are stated against this model.

## Prior art

The claim-with-lease is the SQS visibility timeout almost exactly: a received message is invisible while
held and visible again on expiry with no cooperation from the consumer, renewal as the heartbeat,
at-least-once. Not shared: the 12-hour cap (tasks run for days) and deletion on success (observations are
the history). Temporal confirms the no-probe posture: its server does not detect a crashed worker; liveness
is timeout expiry, and worker pull is its model verbatim. Camunda supplies the vocabulary by contrast:
`claim()` throws when already claimed, which is the claim here; `setAssignee()` installs a holder with no
check, which is what assignment here deliberately does not do. Postgres `SKIP LOCKED` is the property a
claim needs; the substrate has to prove it. Sources: `ent_08460968e6f49dac21510f4a`.
