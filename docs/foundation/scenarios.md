# Scenarios: the work model and the gate model, walked through

**Authored companion (not on the review reading list):** explanatory walkthroughs of the work and gate
models. Runtime claim/lifecycle/gating paths load the kernel instead (`conformance.md`). Further
scenarios (e)–(j) live in [`scenarios_extended.md`](scenarios_extended.md).

**Kind:** foundation; walks the design through concrete passages so the invariants can be read in motion,
and never states the state of a checkout. **Derived from:** `work_model.md`, `gates_and_workflows.md`,
`failure_posture.md`, `authority_model.md`, and PR #745 operator review (2026-09-04). Structure follows
Neotoma's `docs/subsystems/` flow documents: one paragraph, one diagram, the invariants exercised.

## Purpose

Show each invariant doing work. A reviewer who cannot say which scenario a change alters has not found the
change's design basis; a scenario that no invariant explains is a gap in the foundation, to be filed
against it.

## Scope

Four bindable walkthroughs (a)–(d): the plain task life, a lapse and its escalation, assignment, and
aggregation of tasks into one passage. Further walkthroughs (e)–(j) are human reference in
`scenarios_extended.md`. Names of agents are placeholders for roles; no scenario names a checkout, a
count, or a date.

## (a) Create, claim, work, return, complete

A task is created; publication is creation. An agent whose definition matches the task reads it as
claimable, writes a lease edge keyed on the task, and reads the edge back: it holds the task only if the
persisted lease names its own runner id. It works the task, renewing `expires_at` as its heartbeat, and
its `agent_session` and observations make the lease read as `active`. On completion it writes the task's
terminal status, reads that back, and returns the lease. The task carries no claim field at any point.

```mermaid
sequenceDiagram
    participant P as principal (creator)
    participant N as record
    participant A as agent runner
    P->>N: store task (status open, action_type declared)
    A->>N: read task; claimable?
    A->>N: write lease edge (agent → task, claimed_at, expires_at)
    A->>N: read lease back
    N-->>A: lease holder == my runner id
    loop while working
        A->>N: observations, agent_session (task reads active)
        A->>N: renew expires_at (heartbeat)
    end
    A->>N: status = completed
    A->>N: read status back
    A->>N: lease returned
```

**Invariants:** [`work_model.md#the-claim-and-the-lease-are-one-primitive`](work_model.md#the-claim-and-the-lease-are-one-primitive),
[`#the-lease-is-a-relationship-not-a-set-of-task-fields`](work_model.md#the-lease-is-a-relationship-not-a-set-of-task-fields),
[`#liveness-is-derived-from-activity-at-read-time-never-declared`](work_model.md#liveness-is-derived-from-activity-at-read-time-never-declared),
[`#the-transition-vocabulary`](work_model.md#the-transition-vocabulary); `principles.md` invariants 2 and 11.

## (b) Lapse, re-claim, repeated lapse, escalation

The runner dies mid-task. Nothing clears anything: `expires_at` passes and the lease reads as `lapsed`, so
the task is claimable again. A second runner claims it; effect dedup means any action that already
executed is not repeated. If the task keeps lapsing, the watchdog, which only counts, sees the count reach
the cap and raises one `escalation` with the task, the count, and the last claimants. No process ever
returned a lease, and the watchdog never chose a claimant.

```mermaid
flowchart TD
    A[lease held by runner 1] -->|runner 1 dies| B[expires_at passes]
    B --> C{read lease}
    C -->|lapsed| D[task claimable again]
    D --> E[runner 2 claims; new lease edge]
    E --> F{actions already executed?}
    F -->|yes, dedup key matches| G[skip effect; continue task]
    F -->|no| H[execute through the gate]
    G --> I{lapses again?}
    H --> I
    I -->|count below cap| J[backoff; claimable again]
    J --> E
    I -->|count at cap| K[watchdog raises one escalation]
```

**Invariants:** [`work_model.md#a-lapsed-lease-is-not-reaped-repeated-lapse-escalates`](work_model.md#a-lapsed-lease-is-not-reaped-repeated-lapse-escalates),
[`#at-least-once-implies-effect-dedup`](work_model.md#at-least-once-implies-effect-dedup),
[`failure_posture.md#repeated-lapse-escalates`](failure_posture.md#repeated-lapse-escalates),
[`#refuse-resume-by-replay-where-actions-are-consent-gated`](failure_posture.md#refuse-resume-by-replay-where-actions-are-consent-gated).

## (c) Assignment, then the assignee claims

A principal writes `assigned_to` naming one agent, a field write like any other. Nothing was delivered:
the task is now eligible for that agent alone; it is not claimed, and no lease exists. Other agents read it
as not claimable for them. The assignee, on its own loop, reads it as claimable, claims it, and from that
point the scenario is (a). If the assignee never
claims, the task is visible as assigned-and-unclaimed, a fact about the assignee rather than a stranded
lease. If `assigned_to` names a principal nobody can run, the task blocks visibly and never falls through
to inference.

```mermaid
sequenceDiagram
    participant P as principal
    participant N as record
    participant X as other agent
    participant Y as assignee
    P->>N: assigned_to = Y (no lease written)
    X->>N: read task; claimable for X?
    N-->>X: no (assigned to Y)
    Y->>N: read task; claimable for Y?
    N-->>Y: yes (assigned to Y, no held lease)
    Y->>N: write lease edge; read back
    Note over Y,N: from here, scenario (a)
```

**Invariants:** [`work_model.md#pull-is-the-only-delivery-assignment-constrains-eligibility`](work_model.md#pull-is-the-only-delivery-assignment-constrains-eligibility),
[`#assignment-restricts-eligibility-it-never-creates-a-lease`](work_model.md#assignment-restricts-eligibility-it-never-creates-a-lease),
[`#what-a-claim-predicate-treats-as-claimable`](work_model.md#what-a-claim-predicate-treats-as-claimable).

## (d) Several tasks aggregated into one passage, review through release

Three tasks belong in one change. A `passage` is created against the project's `workflow`; each task gets
an `ADDRESSED_BY` edge to the passage. The passage advances from step to step: each step opens,
its step owner claims it (a lease on the step), and closes it with a `sign-off`; `step_status` on each
task projects the same state for the hot path. The pull request that carries the change is an `artifact`
attached to the passage by edge; no step is taken on it. When every required review step is signed off,
the `merge` step opens and the steward claims it; the merge is an `action` the steward evaluates at the
execution gate; on permit it executes, the merged PR is the record it leaves, and the steward's sign-off
closes the passage naming `release` as its successor. A second passage, for the release workflow, then
opens for the same tasks with a `FOLLOWS` edge to the closed one, and the release is its artifact. The
subject of every step was the tasks.

```mermaid
flowchart LR
    T1[task 1] -->|ADDRESSED_BY| R[passage: PR workflow]
    T2[task 2] -->|ADDRESSED_BY| R
    T3[task 3] -->|ADDRESSED_BY| R
    PR[artifact: pull request] -.->|attached by edge| R
    R --> S1[step pm: claimed, signed off]
    S1 --> S2[step arch: claimed, signed off]
    S2 --> S3[step impl: claimed, signed off]
    S3 --> S4[step pr_review: claimed, signed off]
    S4 --> S5[step qa: claimed, signed off]
    S5 --> M{all required review steps signed off?}
    M -->|yes| S6[step merge: steward claims]
    S6 --> ACT[action: merge]
    ACT --> G{execution gate}
    G -->|permit| X[merge executes; sign-off names release]
    X --> R2[passage: release workflow]
    R2 -.->|FOLLOWS| R
    REL[artifact: release] -.-> R2
    T1 -.->|ADDRESSED_BY, later| R2
    T2 -.-> R2
    T3 -.-> R2
```

**Invariants:** [`work_model.md#what-passes-through-a-workflow-is-a-passage-of-tasks`](work_model.md#what-passes-through-a-workflow-is-a-passage-of-tasks),
[`#artifacts-are-records-a-passage-leaves-never-its-subject`](work_model.md#artifacts-are-records-a-passage-leaves-never-its-subject),
[`#a-task-is-in-at-most-one-passage-at-a-time`](work_model.md#a-task-is-in-at-most-one-passage-at-a-time),
[`gates_and_workflows.md#declaration-passage-projection`](gates_and_workflows.md#declaration-passage-projection),
[`#actions-are-entities-only-actions-execute`](gates_and_workflows.md#actions-are-entities-only-actions-execute),
[`#two-policies-workflow-policy-and-execution-policy`](gates_and_workflows.md#two-policies-workflow-policy-and-execution-policy),
[`#sequencing-is-data-successors-and-the-chain`](gates_and_workflows.md#sequencing-is-data-successors-and-the-chain);
[`workflows.md#feature`](workflows.md#feature).

## Further scenarios (human reference)

Scenarios (e)–(j) — split-out, parent/child, operator-only claim, mid-workflow action at NEVER/HIGH/LOW,
halt on unreachable Neotoma, and intake→successor routing — live in
[`scenarios_extended.md`](scenarios_extended.md). Neither this file nor that companion is on the review
reading list; runtime paths load the kernel (and gates) instead (`conformance.md`).

## What the scenarios do not show

None of them shows a router choosing a claimant, work reaching an agent by any path but its own claim, a
process returning a lapsed lease, a pull request or an issue as the subject of a step, a per-step status
row, a parent task being claimed, a task being "executed", a stored liveness flag, a gate consulted on
anything but an `action`, a task in any passage but intake with no intake passage before it, a passage
naming two successors, or a program entity holding a sequence of workflows. Each absence is an invariant; a change that needs one of
these to appear is a change to the foundation, made through a PR that says so (`conformance.md`).
