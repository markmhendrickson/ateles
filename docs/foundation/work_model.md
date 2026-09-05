# Work model: how work is created, claimed, executed, and goes through workflows

**Kernel document:** read on every review (`conformance.md`). **Kind:** foundation; states the design and
never the state of a checkout. **Derived from:** synthesis `ent_b0ce322f768e4fc676b73139` (PR-01 to PR-03,
PR-05, C1, C2), prior art `ent_08460968e6f49dac21510f4a`, task `ent_da60df3beccb675ef8c8c0c5`, throughput
plan `ent_18b902cf72822373f9da8ced` decisions `pull_model_sequencing_build_the_claim_not_the_router`,
`non_github_execution_makes_pull_decisive`, `three_execution_mechanisms_not_one`, PR #745 operator
review (2026-09-04), and the operator's 2026-09-05 review (revision 18: how a batch is formed and what
chooses its workflow; revision 20: the batch-formation diagram, on the operator's request for visuals
during review). Supersedes `docs/archive/task_execution_loop.md`. What is built is `status.md`; how
each concept is recorded is `data_model.md`.

## Purpose

State how work is created, taken, executed, and returned: pull-only delivery; assignment as eligibility;
claim and lease as one primitive (lease as relationship); liveness derived at read time; no assignment
log; a task carries only status and edges; intake is every task's first workflow; tasks go through
workflows in batches, are attached to and detached from them, and nest under parents; a batch is opened
by a closing sign-off naming a successor and goes through exactly one workflow; artifacts are
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
the choice. A claim is a 1:1 judgment, "is this mine", bounded by the claimant's own `agent`,
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

### A task is executed only through a workflow

There is no path by which a task is executed outside a workflow. Every task enters intake, and intake's
closing sign off names the successor workflow it goes to, or none, or operator-only; whatever it does
after that, it does inside a batch going through a declared workflow, with that workflow's steps, step
owners, and sign offs. The design offers no side door: no status a principal sets that means "done
without a workflow", no direct-execution mode for small work, no class of task exempt because it is
urgent or trivial. Work small enough that most steps are unnecessary takes a declared fast path
(`gates_and_workflows.md`), which is a workflow saying which steps it skips — a decision recorded in the
declaration, judged once, and visible to every reader — rather than a task escaping the model.

**What this means for the self-triggering daemons.** A daemon produces tasks and takes actions, and it
receives no task itself (the four execution mechanisms, below); none of that is an exception to this
rule. Any task a daemon produces enters intake exactly like a task from any other source and is executed
through a workflow from there — the daemon that created it holds no privilege over it and does not
execute it outside the model. The daemon's own outbound effects are not task execution at all: they are
actions, and each passes the action gate on its own (`gates_and_workflows.md`, C2). So the daemon loop
sits beside the task path rather than around it, and the two meet where a daemon's output becomes a task
in intake.

### What goes through a workflow is a batch of tasks

Tasks go through a workflow in a batch: one or more tasks together, and the record of that. A single task
is a batch of one; there is no separate single-task path. When tasks enter a workflow a batch record is
opened if none exists, and each task is attached by an `ADDRESSED_BY` edge. Detaching a task ends its
edge; to split a task is to detach it and open a new batch for it, from the first step, while the original
batch continues with the tasks still attached. Attach and detach are edges, never fields (principle 11).

### How a batch is formed, and what chooses its workflow

The rule above says tasks go through a workflow in a batch and that a batch record is opened if none
exists. That leaves three questions a reader has to answer before they can build anything: what causes a
batch to come into existence, which tasks are in it, and which workflow it goes through. Each is answered
by a mechanism the model already has, and stating them together is what stops the answer being re-derived
differently at each call site.

**A batch comes into existence when a closing sign-off names a successor, and at no other moment.** There
is one cause, not several. Intake's `route` step closes on a sign-off naming one successor workflow, none,
or operator-only; every later batch closes the same way (`gates_and_workflows.md#sequencing-is-data-successors-and-the-chain`).
Where a successor is named, the batch for it opens and carries a `FOLLOWS` edge back to the batch that
named it. Where none is named, the task's chain ends. Nothing else opens a batch: no daemon opens one
because it noticed eligible tasks, no adapter opens one on an inbound event, and no scheduler sweeps for
work to group. The one batch with no predecessor is a task's intake batch, opened on the task's creation,
which is the universal entry (`#intake-is-every-tasks-first-workflow`, above) and the reason every chain
has a first link.

The consequence worth naming: a batch is always opened **by a principal's recorded verdict**, never by a
process acting on its own reading of the record. The sign-off names the successor, so the decision has an
author, a timestamp, and a reason, and a reader asking why these tasks are in this workflow is answered by
a verdict rather than by inferring what some sweeper's predicate must have matched.

**A batch's tasks are the tasks the closing sign-off carried, and grouping beyond that is a step's
judgement, recorded as one.** The default is the simple one: the tasks attached to the closing batch move
together into the successor, and a batch of one stays a batch of one. Two operations change a task set,
both already defined and both edges (principle 11): **detach**, which ends a task's `ADDRESSED_BY` edge and
opens a new batch for it from the first step of its workflow, and **attach**, which writes that edge. What
this section adds is who may do them and on what basis. Attaching a task to a batch that is already open,
part-way through its steps, is a step owner's judgement written into that step's sign-off, never an
adapter's guess and never a matcher's inference — the adapter rule already forbids the first
(`adapters.md#what-the-adapter-does-with-every-event`), and the second is the routing fallthrough the pull
rule forbids. A task attached part-way through enters at the batch's current step and inherits the
sign-offs already written on it, which is exactly why the judgement is a recorded one: those sign-offs were made
against a task set that did not include it, and a step owner who attaches is asserting that they still
hold. Where that assertion is not safe, the task is its own batch.

**The workflow is chosen once, by the sign-off that names the successor, from the declared list.** The
choice is not open-ended: `workflow.successors` names the workflows a closing batch's tasks may enter, and
the closing sign-off selects exactly one from that list or none. So the workflow for a batch is fixed
before the batch opens, by a named principal, bounded by a declaration that was reviewed when it was
written. There is no run-time selection inside the batch, no re-selection, and no workflow chosen by
matching a property of the tasks after the fact.

**A batch goes through exactly one workflow, for its whole life.** The workflow is a field of the batch
record (`data_model.md#concepts`), fixed at open. A batch that needed a different workflow does not switch:
it closes, and its closing sign-off names the one the tasks go to, which opens a new batch. This is what
makes the chain readable — each link is one workflow, entered by one verdict — and a batch that changed
workflow mid-flight would leave its earlier sign-offs pinned to steps that no longer exist in its
declaration.

**And a task is in one batch at a time but many over its life, which is the distinction to hold.** The
one-at-a-time rule is about simultaneity (above); the chain is the sequence. A task that went through
intake, then feature, then release has three batches, two closed and one live, and asking "which workflow
is this task in" is answered by its live batch alone.

The three questions and their one answer each, with the paths the rules above exclude drawn beside them:

```mermaid
flowchart TD
    CR["task created"] --> IB["its intake batch opens: the one batch with no predecessor"]
    IB --> CS["closing sign-off of a batch"]
    CS --> SEL{"does it name a successor?"}
    SEL -->|"none"| END["the task's chain ends"]
    SEL -->|"one, selected from workflow.successors"| OPEN["a batch opens, FOLLOWS the batch that named it"]
    OPEN --> W["its workflow is fixed at open, and never switched"]
    OPEN --> TASKS["its tasks are the tasks that sign-off carried"]
    TASKS --> MID{"a task attached part-way through?"}
    MID -->|"a step owner's judgement, written into that step's sign-off"| INH["it enters at the current step and inherits the sign-offs already written"]
    MID -->|"the assertion is not safe"| OWN["it is its own batch, from the first step"]
    W --> LIFE["one workflow for the batch's whole life; a batch needing another closes and names it"]
    X1["a daemon noticing eligible tasks"] -.->|"opens no batch"| OPEN
    X2["an adapter, on an inbound event"] -.->|"opens no batch"| OPEN
    X3["a sweeper's predicate over the record"] -.->|"opens no batch"| OPEN
    X4["a label an external system carries"] -.->|"chooses no workflow"| W
```

Every arrow into a batch is a principal's recorded verdict; every dotted one is a path the rules above
close.

**What this deliberately does not do is let batch formation key on anything discovered later.** A
declaration's conditional may turn only on a property of the task set at intake, never on a label an
external system carries (`workflows.md`), and the rules above are the same constraint stated for
formation: the successor is named by a verdict at a close, from a list fixed in the declaration, on tasks
whose properties intake established. A formation rule that grouped tasks by a label an adapter wrote, or
that chose a workflow from an artifact's state, would put the choice back into an external system's hands
through the side door the boundary rules close.

Two questions about a batch's **lifetime** remain open and are not settled by any of the above: whether a
batch may hold on a condition discovered mid-flight, and whether a batch may depend on a task it created
(`gates_and_workflows.md#a-finding-is-one-off-or-standing-and-a-standing-one-obliges-a-change-to-what-produced-it`).
Both are downstream of this section rather than blocked by it — formation and workflow choice are settled
whichever way they go, because both concern what a batch may wait on once it is already open.

### Artifacts are records a batch leaves, never its subject

An `artifact` is an external record (issue, PR, release, message) linked by edge. A step is signed off on
the batch's tasks; the PR is the record left behind. An action is the intended effect; the artifact is
what the effect leaves. Which system holds the artifact, and what that system calls it, is outside the
design; how that system's events and operations map onto the record is `adapters.md`.

**The word is bound, and it is not a catch-all for outputs.** An artifact is a record living in an
external system, reachable only through that system's adapter, and always identified by the pair `system`
and `external_id` — a thing the swarm can point at but does not hold. Anything the swarm produces that
lives in the record is an **entity**, not an artifact: a sign off, an analysis, a draft, a checkpoint, a
plan, a page the swarm rendered into the record. The test is where the thing lives and how it is reached,
never how output-shaped it feels: if reading it means asking an external system through an adapter, it is
an artifact; if reading it is a retrieval from the record, it is an entity. Keeping the word this narrow
is what lets every rule about artifacts hold at once — that they are found by `system` and `external_id`,
that only an adapter touches them, that they are never the subject of a step, and that what happens to
them reaches a step only through a principal who reads and signs. A word that also covered the swarm's
own outputs would break all four.

### A task is in at most one batch at a time

At most one `ADDRESSED_BY` edge to a non-terminal batch. Sequential batches are normal: a task that went
through a pull-request workflow and then a release workflow has two edges, one to a closed batch and one to
a live one. Work needing two workflows at once is split into child tasks, one per batch.

### Parent and child tasks

Children `PART_OF` a parent (at most one parent). Parent completion is derived from children's terminal
states. Children go through workflows independently. A parent never enters a workflow — it is a
grouping, and a batch carries tasks that are executed, which a parent never is.

## The four execution mechanisms

(1) Task path above. (2) Dedicated daemons that self-trigger (a mail poller produces tasks and never
receives one). (3) The GitHub pipeline, which sequences steps for a batch and never writes task status — a
step opening is publication of claimable step work; the step owner claims it with a lease, the same
primitive as on a task (`gates_and_workflows.md`). It is the same pull, over steps. A roster role reachable
by none of these cannot receive work; the count is `status.md`. A daemon showing that a non-code agent
can self-trigger does not show it can receive work.

(4) **The interactive session, a work source that holds no lease.** An operator working directly with an
agent produces work, takes actions, and receives none of the swarm's work: it claims nothing, so it holds
no lease. Naming it a mechanism is what makes its consequence statable rather than merely true. Every
recovery guarantee in this document runs through the lease — a lapsed lease makes a task claimable again
with no process acting on it, and repeated lapse raises a checkpoint — and none of them reaches a session.
A session that dies mid-request leaves no lease to lapse and no task to re-claim.

So the session's output becomes **tasks**, and the sequence that turns it into them is a declared workflow
like any other (`workflows.md`), with an owning role, rather than an emergent practice. Work an
interrupted session left unfinished is recovered by **digestion** — reading the session back and filing
what it left — and that is stated as the design, not as a habit: the recovery is a workflow someone owns,
and its absence would be visible. Requiring a session to claim a lease like the other three would close the
gap in theory and be bypassed in practice, and a rule that is bypassed is not a control (principle 1);
naming the mechanism honestly and designing its recovery is what the model can actually hold.

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
