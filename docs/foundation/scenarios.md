# Scenarios: the work model and the gate model, walked through

**Keyed document:** read when the claim, lease, watchdog, workflow-resolver, or gating paths change, or
when `docs/foundation/` changes (`conformance.md`). **Kind:** foundation; walks the design through
concrete passages so the invariants can be read in motion, and never states the state of a checkout.
**Derived from:** `work_model.md`, `gates_and_workflows.md`, `failure_posture.md`, `authority_model.md`,
and PR #745 operator review (2026-09-04), which asked for each scenario below. Structure follows Neotoma's
`docs/subsystems/` flow documents: one paragraph, one diagram, the invariants exercised.

## Purpose

Show each invariant doing work. A reviewer who cannot say which scenario a change alters has not found the
change's design basis; a scenario that no invariant explains is a gap in the foundation, to be filed
against it.

## Scope

Nine walkthroughs: the plain task life, a lapse and its escalation, assignment, aggregation of tasks into
one passage, a split, a parent with children, an operator-only task, an action discovered mid-workflow at
each blast tier, and a halt. Names of agents are placeholders for roles; no scenario names a checkout, a count,
or a date.

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
an `ADDRESSED_BY` edge to the passage. The passage moves through the workflow's steps: each step opens,
its step owner claims it (a lease on the step), and closes it with a `sign-off`; `step_status` on each
task projects the same state for the hot path. The pull request that carries the change is an `artifact`
attached to the passage by edge; no step is taken on it. When every required step is signed off, the
merge is an `action` that the steward evaluates at the execution gate; on permit it executes, and the
merged PR is the record it leaves. A second passage, for the release workflow, then carries the same tasks
sequentially, with the release as its artifact. The subject of every step was the tasks.

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
    S5 --> M{all required steps signed off?}
    M -->|yes| ACT[action: merge]
    ACT --> G{execution gate}
    G -->|permit| X[merge executes]
    X --> R2[passage: release workflow]
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
[`#two-policies-workflow-policy-and-execution-policy`](gates_and_workflows.md#two-policies-workflow-policy-and-execution-policy).

## (e) A task split out of a passage

Review finds that one of the three tasks does not belong in the change. Its `ADDRESSED_BY` edge to the
passage is ended and a new passage is started for it, from the first step, with no sign-offs carried
over. The original passage continues with the two tasks still attached and loses no sign-off; its pull
request stays attached to it as an artifact, and the new passage will leave its own. Nothing on either
task or either passage records the split as a field; the two edges, one ended and one live, are the
record.

```mermaid
flowchart TD
    subgraph before
        T1a[task 1] --> Ra[passage A]
        T2a[task 2] --> Ra
        T3a[task 3] --> Ra
        PRa[artifact: PR] -.-> Ra
    end
    before -->|review: task 3 does not belong| after
    subgraph after
        T1b[task 1] --> Rb[passage A continues]
        T2b[task 2] --> Rb
        PRb[artifact: PR] -.-> Rb
        T3b[task 3] -.->|edge ended| Rb
        T3b -->|new ADDRESSED_BY| Rc[passage B, from step 1]
    end
```

**Invariants:** [`work_model.md#what-passes-through-a-workflow-is-a-passage-of-tasks`](work_model.md#what-passes-through-a-workflow-is-a-passage-of-tasks),
[`#artifacts-are-records-a-passage-leaves-never-its-subject`](work_model.md#artifacts-are-records-a-passage-leaves-never-its-subject),
[`#a-task-is-in-at-most-one-passage-at-a-time`](work_model.md#a-task-is-in-at-most-one-passage-at-a-time);
`principles.md` invariant 11.

## (f) A parent task with children in independent passages

A parent task is created as the aggregate of a piece of work; three child tasks each carry a `PART_OF`
edge to it. Each child is claimed, worked, and carried through its own passage on its own schedule. The
parent is never claimed and never enters a passage. When a reader asks whether the parent is complete, the
answer is derived from the children's terminal states at that moment and is stored nowhere.

```mermaid
flowchart TD
    P[parent task: never claimed, never in a passage]
    C1[child 1] -->|PART_OF| P
    C2[child 2] -->|PART_OF| P
    C3[child 3] -->|PART_OF| P
    C1 -->|ADDRESSED_BY| R1[passage 1]
    C2 -->|ADDRESSED_BY| R2[passage 2]
    C3 -->|ADDRESSED_BY| R3[passage 3]
    R1 --> D{all children terminal?}
    R2 --> D
    R3 --> D
    D -->|derived at read| PC[parent reads complete]
```

**Invariants:** [`work_model.md#parent-and-child-tasks`](work_model.md#parent-and-child-tasks);
`principles.md` invariant 11.

## (g) An operator-only task, claimed by the operator-facing agent

A task is created with `operator_only` among its declared action classes. It is claimable, by the
operator-facing agent, which claims it and holds the lease. The one action the task needs is created and
evaluated at the execution gate, which resolves `operator_only` to `NEVER` ahead of any policy and writes
a `checkpoint_brief` awaiting the operator. The agent carries the brief to the operator through the
configured channel and renews its lease while waiting. The operator resolves the brief; the agent records
the outcome on the task, completes it, and returns the lease. Nothing executed without the operator, and
the task path stayed pull.

```mermaid
sequenceDiagram
    participant N as record
    participant A as operator-facing agent
    participant G as execution gate
    participant O as operator
    A->>N: claim task (operator_only declared); read back
    A->>N: create action (class operator_only)
    A->>G: evaluate action
    G->>N: write checkpoint_brief (NEVER; awaits operator)
    A->>O: carry the brief
    loop while awaiting
        A->>N: renew lease
    end
    O->>N: resolve brief (terminal state, resolver recorded)
    A->>N: record outcome; status completed; read back
    A->>N: lease returned
```

**Invariants:** [`work_model.md#operator-only-tasks-are-claimed-by-the-operator-facing-agent`](work_model.md#operator-only-tasks-are-claimed-by-the-operator-facing-agent),
[`gates_and_workflows.md#confidence-and-three-blast-tiers`](gates_and_workflows.md#confidence-and-three-blast-tiers),
[`#the-approval-object`](gates_and_workflows.md#the-approval-object),
[`authority_model.md#approval`](authority_model.md#approval); `principles.md` invariant 5.

## (h) An action discovered mid-workflow, at NEVER, HIGH, and LOW

A task declared `docs` at creation. While working it, the agent finds the change also needs an outreach
mail. That effect becomes an `action` the moment it is known, with class `send_external_comms`; the
declaration at creation is not amended, because it was a declaration of expectation, not a bound. The
principal executing the action evaluates the gate with the action's class, its confidence, the
`execution_policy`, and the class's recurrences. At `NEVER` the brief is written and nothing else is
consulted. At `HIGH` the brief is written unless a recurring series has graduated the class. At `LOW` the
action executes at or above the confidence threshold, or once the series has graduated, and checkpoints
otherwise. A class in neither set logs the value and resolves to `NEVER`.

```mermaid
flowchart TD
    W[working a task declared docs] --> D[effect discovered: outreach mail]
    D --> A[create action: class send_external_comms, PRODUCES from task]
    A --> G{execution gate: class under execution_policy}
    G -->|operator_only, or class unclassified| N[NEVER: checkpoint_brief; nothing else consulted]
    G -->|HIGH| H{recurring series graduated?}
    H -->|no| HB[checkpoint_brief]
    H -->|yes| HX[execute]
    G -->|LOW| L{confidence at threshold, or series graduated?}
    L -->|yes| LX[execute]
    L -->|no| LB[checkpoint_brief]
```

**Invariants:** [`gates_and_workflows.md#actions-are-entities-only-actions-execute`](gates_and_workflows.md#actions-are-entities-only-actions-execute),
[`#confidence-and-three-blast-tiers`](gates_and_workflows.md#confidence-and-three-blast-tiers),
[`#the-execution-gate-is-pr-independent`](gates_and_workflows.md#the-execution-gate-is-pr-independent),
[`#non-code-deliverables-pass-through-the-same-gate`](gates_and_workflows.md#non-code-deliverables-pass-through-the-same-gate);
`principles.md` invariant 5.

## (i) Neotoma unreachable, halt

A runner about to claim a task performs the reachability probe, a real read of what the work will read.
The read fails. The runner claims nothing, evaluates no gate, and announces the halt on the off-Neotoma
path, aggregated per window. A second runner already mid-task attempts its sign-off write, which fails; it
leaves the task in its prior state, writes its diagnostic capture to local disk, and stops. Its lease
lapses on its own. When the probe succeeds again, the halt is announced as lifted, the task is claimable,
and a re-claim finds the effects it already executed by their dedup keys. Any grant or gate read during
the outage returned `unknown`, which every enforcement point treated as deny.

```mermaid
flowchart TD
    R1[runner 1: about to claim] --> P{reachability probe: real read}
    P -->|fails| H[halt: no claim, no gate decision]
    H --> AN[announce halt off-Neotoma, per window]
    R2[runner 2: mid-task] --> W{sign-off write}
    W -->|fails| L[leave prior state; capture to local disk; stop]
    L --> LP[lease lapses on its own]
    P -->|succeeds later| U[announce halt lifted]
    LP --> C[task claimable again]
    U --> C
    C --> RC[re-claim; dedup keys skip executed effects]
    subgraph during the outage
        Q[any grant or gate read] --> UN[unknown]
        UN --> DN[enforcement point: deny]
    end
```

**Invariants:** [`failure_posture.md#the-decision`](failure_posture.md#the-decision),
[`#the-rules`](failure_posture.md#the-rules) (1 to 7),
[`work_model.md#a-lapsed-lease-is-not-reaped-repeated-lapse-escalates`](work_model.md#a-lapsed-lease-is-not-reaped-repeated-lapse-escalates),
[`authority_model.md#the-tuple`](authority_model.md#the-tuple) (`Indeterminate` is deny); `principles.md`
invariants 2 and 7.

## What the scenarios do not show

None of them shows a router choosing a claimant, work reaching an agent by any path but its own claim, a
process returning a lapsed lease, a pull request or an issue as the subject of a step, a per-step status
row, a parent task being claimed, a task being "executed", a stored liveness flag, or a gate consulted on
anything but an `action`. Each absence is an invariant; a change that needs one of
these to appear is a change to the foundation, made through a PR that says so (`conformance.md`).
