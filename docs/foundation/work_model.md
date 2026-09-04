# Work model: how work is created, claimed, worked, and passed through workflows

**Kernel document:** read on every review (`conformance.md`). **Kind:** foundation; states the design and
never the state of a checkout. **Derived from:** synthesis `ent_b0ce322f768e4fc676b73139` (PR-01 to PR-03,
PR-05, C1, C2), prior art `ent_08460968e6f49dac21510f4a`, task `ent_da60df3beccb675ef8c8c0c5`, throughput
plan `ent_18b902cf72822373f9da8ced` decisions `pull_model_sequencing_build_the_claim_not_the_router`,
`non_github_execution_makes_pull_decisive`, `three_execution_mechanisms_not_one`, and PR #745 operator
review (2026-09-04). Supersedes `docs/archive/task_execution_loop.md`. What is built is `status.md`.

## Purpose

State how work is created, taken, worked, and returned: pull-only delivery; assignment as eligibility;
claim and lease as one primitive (lease as relationship); liveness derived at read time; no assignment
log; a task carries only status and edges; intake is every task's first passage; aggregation, split, and
nesting; artifacts as records a passage leaves, never its subject.

## Scope

The task path: a Neotoma `task` claimed by an agent. The other two execution mechanisms are named below;
steps and gates are `gates_and_workflows.md`; core workflows (including intake) are `workflows.md`
(authored companion, not inlined into review prompts); authority is `authority_model.md`; terms are
`vocabulary.md`. Walkthroughs: `scenarios.md`.

## The invariants

### Pull is the only delivery; assignment constrains eligibility

Work reaches an agent only by claim. No router chooses claimants; no principal delivers a task; a workflow
step is claimed by its owner the same way (`gates_and_workflows.md`). The actor that judges fit must be
the actor that acts and answers for the outcome. Subscriptions wake an agent; they never deliver work.

### Assignment restricts eligibility; it never creates a lease

`assigned_to` is eligibility: who may claim. It is not the holder. The assignee still pulls by claiming;
the lease holder is always the claimant. A task with an assignee who never claims is assigned-and-unclaimed,
not a stranded lease. Camunda's `setAssignee()` (installs a holder with no check) is the operation this
design does not have.

### The claim and the lease are one primitive

Two agents must not both take a task; a killed runner must not strand one. The claim is keyed on the task;
the holder is read back after the write: an agent holds only if the persisted lease names its runner id
(principle 2). Atomicity is proven by the implementation, never assumed of the store.

### The lease is a relationship, not a set of task fields

The lease is an edge (principal ↔ task) with `claimed_at` and `expires_at`; renewal moves `expires_at`.
The task carries no claim fields. Derived at read: `held` while `expires_at` is future; `lapsed` once
past without an explicit end; `returned` when the claimant ended it. Nothing transitions a lease to
`lapsed` — the clock does (principle 11).

### Liveness is derived from activity at read time, never declared

`active` = held lease plus activity (`agent_session` or observations) within the lease window. A stored
liveness flag fails when the process that would clear it died. `executing` / `running` as liveness
assertions are retired.

### No assignment log; history is the task's own observations

Every status change is an observation; every lease is an edge with timestamps. A parallel assignment or
claim-history entity would be a second source of truth (principle 9). The current lease answers what
history cannot: who holds this right now.

### The transition vocabulary

The task's vocabulary is `created` plus its status (`open` / `blocked` / terminal). The lease carries
`held` / `lapsed` / `returned`. `active` is a derived read, never a state. Definitions:
`vocabulary.md`.

### There is no task lifecycle; there are passages

A task carries status and edges only. Other state is a passage, lease, sign-off, or activity entity. A
task is never `routed`, `executing`, `verified`, or "in review"; the passage and its `FOLLOWS` chain say
which of those is true (`gates_and_workflows.md`). This is C1: push-model states were facts about a
passage, lease, or sign-off written onto the task.

### Intake is every task's first passage

Every task passes through intake before any other workflow (`workflows.md#intake`): `classify`, `link`,
`dedupe`, `prioritize`, `route` (closing sign-off names one successor, none, or operator-only). An
unrouted task is a task with no intake passage — no separate unrouted state. Children and split-outs open
their own intake; a fast path never skips intake.

### What a claim predicate treats as claimable

Claimable: not terminal, not `blocked`, no held lease. A lapsed lease never blocks a claim. Unassigned
tasks are an open pool; an assigned task is claimable only by its assignee. Live status distribution:
`status.md`.

### A lapsed lease is not reaped; repeated lapse escalates

No process returns a lapsed lease — it already does not count. The reaper is retired. The watchdog
observes lapses and escalates past a per-task cap (`failure_posture.md`); it never chooses a new claimant.

### At-least-once implies effect dedup

Lapse and re-claim is at-least-once. Every outbound effect is idempotent or deduplicated on its own key;
a re-claimed task is never replayed (`failure_posture.md`). The dedup key lives on the `action` entity
(`gates_and_workflows.md`).

### Operator-only tasks are claimed by the operator-facing agent

A task with `operator_only` actions is claimed by the `ateles` agent, which carries it to the operator and
holds the lease while the operator decides. The execution gate resolves `operator_only` to `NEVER`
(`gates_and_workflows.md`). The task path stays pull; only the action waits on a human.

### What passes through a workflow is a passage of tasks

A `passage` is one passage of tasks through a workflow's steps. Tasks attach by `ADDRESSED_BY`. Several
tasks may aggregate into one passage; split ends one task's edge and starts a new passage. Aggregation and
split are edges, never fields (principle 11).

### Artifacts are records a passage leaves, never its subject

An `artifact` is an external record (issue, PR, release, message) linked by edge. A step is signed off on
the passage's tasks; the PR is the record left behind. An action is the intended effect; the artifact is
what the effect leaves.

### A task is in at most one passage at a time

At most one `ADDRESSED_BY` to a non-terminal passage. Sequential passages are normal. Work needing two
workflows at once splits into child tasks.

### Parent and child tasks

Children `PART_OF` a parent (at most one parent). Parent completion is derived from children's terminals.
Children pass through workflows independently. No passage opens for a parent — it is an aggregate.

## The three execution mechanisms

(1) Task path above. (2) Dedicated daemons that self-trigger (e.g. mail poller) and never receive a task.
(3) The GitHub pipeline, which sequences steps for a passage and never writes task status — a step opening
is publication of claimable step work; the step owner claims with a lease (`gates_and_workflows.md`). A
roster role reachable by none of the three cannot receive work; the count is `status.md`.

## Contradictions this document settles

**C1.** Push state machine (`created, routed, executing, verified, done`) superseded by pull + passages;
that plan's criteria are a request to its maintainer. **C2.** "Tasks authorize every side effect" is true
of the task path only; a daemon's outbound effect is still an `action` through the same execution gate.
Open: unify daemon loop with task path or keep both under an explicit contract. **Reaper retired** —
lapse is read, not written; watchdog keeps only escalation (`failure_posture.md` rules 4–5).
