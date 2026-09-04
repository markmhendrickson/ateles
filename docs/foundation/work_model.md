# Work model: how work is created, claimed, executed, and goes through workflows

**Kernel document:** read on every review (`conformance.md`). **Kind:** foundation; states the design and
never the state of a checkout. **Derived from:** synthesis `ent_b0ce322f768e4fc676b73139` (PR-01 to PR-03,
PR-05, C1, C2), prior art `ent_08460968e6f49dac21510f4a`, task `ent_da60df3beccb675ef8c8c0c5`, throughput
plan `ent_18b902cf72822373f9da8ced` decisions `pull_model_sequencing_build_the_claim_not_the_router`,
`non_github_execution_makes_pull_decisive`, `three_execution_mechanisms_not_one`, and PR #745 operator
review (2026-09-04). Supersedes `docs/archive/task_execution_loop.md`. What is built is `status.md`; how
each concept is recorded is `data_model.md`.

## Purpose

State how work is created, taken, executed, and returned: pull-only delivery; assignment as eligibility;
claim and lease as one primitive (lease as relationship); liveness derived at read time; no assignment
log; a task carries only status and edges; intake is every task's first workflow; tasks go through
workflows in batches, are attached to and detached from them, and nest under parents; artifacts are
records a batch leaves, never its subject.

## Scope

The task path: a task claimed and executed by an agent. The other two execution mechanisms are named
below; steps and gates are `gates_and_workflows.md`; core workflows (including intake) are `workflows.md`
(authored companion, not inlined into review prompts); authority is `authority_model.md`; terms are
`vocabulary.md`; the record is `data_model.md`. Walkthroughs: `scenarios.md`.

## The invariants

### Pull is the only delivery; assignment constrains eligibility

Work reaches an agent only by claim. No router chooses claimants; no principal delivers a task; a workflow
step is claimed by its step owner the same way (`gates_and_workflows.md`). Reason: the actor that judges fit
must be the actor that acts and answers for the outcome. A router's inference sits in an actor that
neither acts nor answers for a misroute, so a wrong guess reaches an executor with nobody accountable for
the choice. A claim is a 1:1 judgment, "is this mine", bounded by the claimant's own `agent_definition`,
which no central table encodes; routing is a 1:N choice with fallthrough, and fallthrough is where an
unknown principal is quietly resolved to somebody. A claim cannot fall through: the predicate reads
`assigned_to` directly, and an `assigned_to` naming a principal nobody can run raises a checkpoint
(reason `unspawnable_assignee`) instead of being resolved to someone else. Non-code agents are the first
consumers: their work has no file paths or closure keywords for a keyword matcher, and a claim bounded by
their own definition needs none. Subscriptions wake an agent; they never deliver work.

### Assignment restricts eligibility; it never creates a lease

`assigned_to` is eligibility: who may claim. It is not the holder. The principal the assignment names
still pulls by claiming; the lease holder is always the claimant. A task whose named principal never claims
is assigned-and-unclaimed, a fact about that principal, not a lease left hanging. Camunda's
`setAssignee()` (installs a holder with no check) is the operation this design does not have.

### The claim and the lease are one primitive

Two agents must not both take a task; a killed runner must not leave one held forever. The claim is keyed
on the task; the holder is read back after the write: an agent holds only if the persisted lease names
its runner id (principle 2). Atomicity is proven by the implementation, never assumed of the store: a
last-writer-wins snapshot and a name-collision policy that de-duplicates into one row do not raise.

### The lease is a relationship, not a set of task fields

The lease is an edge (principal ↔ task) with `claimed_at` and `expires_at`; renewal moves `expires_at`.
The task carries no lease fields. Derived at read: `held` while `expires_at` is future; `lapsed` once
past without an explicit end; `returned` when the claimant ended it. Nothing transitions a lease to
`lapsed` — the clock does (principle 11).

### Liveness is derived from activity at read time, never declared

`active` = held lease plus activity (`agent_session` or observations) within the lease window. A stored
liveness flag fails when the process that would clear it died. The two liveness assertions of the archived
loop document are retired: `executing` / `running` are not states.

### No assignment log; history is the task's own observations

Every status change is an observation; every lease is an edge with timestamps. A parallel assignment or
claim-history entity would be a second source of truth (principle 9). The current lease answers what
history cannot: who holds this right now. `agent_session` carries the identity half observations lack
(host, checkout, branch, head), not a history of runners.

### The transition vocabulary

The task's vocabulary is `created` plus its status (`open` / `blocked` / terminal). The lease carries
`held` / `lapsed` / `returned`. `active` is a derived read, never a state. Each word names one thing: a
task whose claimant died is a task with a lapsed lease, and a task its named principal has not taken is
assigned-and-unclaimed. Definitions: `vocabulary.md`.

### There is no task lifecycle; there are batches

A task carries status and edges only. Other state is a batch, lease, sign-off, or activity entity. A
task is never routed, executing, verified, or in review as a status; the batch it is in and that batch's
`FOLLOWS` chain say which of those is true (`gates_and_workflows.md`). This is C1: the states of the
archived loop document were facts about a batch, a lease, or a sign-off written onto the task, where a
process then had to keep them true (principle 11).

### Intake is every task's first workflow

Every task enters intake before any other workflow (`workflows.md#intake`): `classify`, `link`,
`dedupe`, `prioritize`, `route` (closing sign-off names one successor, none, or operator-only). An
unrouted task is a task with no intake batch — no separate unrouted state. Tasks a batch creates
(children, detached tasks, tasks extracted from a meeting) enter intake themselves; a child may take
intake's declared fast path and never skips intake.

### What a claim predicate treats as claimable

Claimable: not terminal, not `blocked`, no held lease. A lapsed lease never blocks a claim. Unassigned
tasks are a shared pool; an assigned task is claimable only by the principal it names. The status
vocabulary in the record is what tasks actually carry, not what an enum in code declares: a predicate is
written against the record (principle 3), because a normalizer that fails to map a live value onto a
terminal one makes finished work claimable. Live status distribution: `status.md`.

### A lapsed lease is not reaped; repeated lapse raises a checkpoint

No process returns a lapsed lease — it already does not count. The reaper is retired. The watchdog
observes lapses and, past a per-task cap, escalates: it raises one checkpoint on the task with reason
`repeated_lapse` (`failure_posture.md`); it never chooses a new claimant.

### At-least-once implies effect dedup

Lapse and re-claim is at-least-once. Every outbound effect is idempotent or deduplicated on its own key;
a re-claimed task is never replayed (`failure_posture.md`). The dedup key lives on the `action` entity
(`gates_and_workflows.md`).

### Operator-only tasks are claimed by the operator-facing agent

A task with `operator_only` actions is an ordinary task claimed by the `ateles` agent, which carries it to
the operator and holds the lease while the operator decides. It is not itself a checkpoint: it raises one
only when an action inside it reaches the action gate, which resolves `operator_only` to `NEVER`
(`gates_and_workflows.md`). The task path stays pull; only the action waits on a human.

### What goes through a workflow is a batch of tasks

Tasks go through a workflow in a batch: one or more tasks together, and the record of that. A single task
is a batch of one; there is no separate single-task path. When tasks enter a workflow a batch record is
opened if none exists, and each task is attached by an `ADDRESSED_BY` edge. Detaching a task ends its
edge; to split a task is to detach it and open a new batch for it, from the first step, while the original
batch continues with the tasks still attached. Attach and detach are edges, never fields (principle 11).

### Artifacts are records a batch leaves, never its subject

An `artifact` is an external record (issue, PR, release, message) linked by edge. A step is signed off on
the batch's tasks; the PR is the record left behind. An action is the intended effect; the artifact is
what the effect leaves. Which system holds the artifact, and what that system calls it, is outside the
design; how that system's events and operations map onto the record is `adapters.md`.

### A task is in at most one batch at a time

At most one `ADDRESSED_BY` edge to a non-terminal batch. Sequential batches are normal: a task that went
through a pull-request workflow and then a release workflow has two edges, one to a closed batch and one to
a live one. Work needing two workflows at once is split into child tasks, one per batch.

### Parent and child tasks

Children `PART_OF` a parent (at most one parent). Parent completion is derived from children's terminal
states. Children go through workflows independently. A parent never enters a workflow — it is a
grouping, and a batch carries tasks that are executed, which a parent never is.

## The three execution mechanisms

(1) Task path above. (2) Dedicated daemons that self-trigger (a mail poller produces tasks and never
receives one). (3) The GitHub pipeline, which sequences steps for a batch and never writes task status — a
step opening is publication of claimable step work; the step owner claims it with a lease, the same
primitive as on a task (`gates_and_workflows.md`). It is the same pull, over steps. A roster role reachable
by none of the three cannot receive work; the count is `status.md`. A daemon showing that a non-code agent
can self-trigger does not show it can receive work.

## Contradictions this document settles

**C1.** The task-spine plan `ent_aff87747b49e338790568af6` (`task_lifecycle_state_machine`) and the archived
loop document define `created, routed, executing, verified, done` with a router assigning owners. Resolved
for pull: the third of those states asserts what it cannot back, and routing places the judgment of fit in an actor that
neither acts nor answers for it. That plan's state-machine criteria are superseded; the correction is a
request to its maintainer. **C2.** `subscriptions_detect_tasks_authorize` is true of the task path and
false of the self-triggering daemons, whose effects never pass through a task; a daemon's outbound effect
is still an `action` through the same action gate (`gates_and_workflows.md`). Open: unify the daemon loop
with the task path or keep both under an explicit contract (digest `ent_e04244959daf92416597ce28`).
**The reaper, retired** — lapse is read, not written; the watchdog keeps only its escalation rule
(`failure_posture.md` rules 4–5).

## Prior art

The claim-with-lease is the SQS visibility timeout almost exactly: a received message is invisible while
held and visible again on expiry with no cooperation from the consumer, renewal as the heartbeat,
at-least-once. Not shared: the 12-hour cap (tasks run for days) and deletion on success (observations are
the history). Temporal confirms the no-probe posture: its server does not detect a crashed process;
liveness is timeout expiry, and pull by the process is its model verbatim. Camunda supplies the vocabulary
by contrast: `claim()` throws when already claimed, which is the claim here; `setAssignee()` installs a
holder with no check, which is what assignment here deliberately does not do. Postgres `SKIP LOCKED` is
the property a claim needs; the substrate has to prove it. Sources: `ent_08460968e6f49dac21510f4a`.
