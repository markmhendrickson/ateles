# Scenarios (extended): human-reference walkthroughs

**Not on the review reading list.** Companion to [`scenarios.md`](scenarios.md). Walkthroughs (a)–(d)
stay in that file; these (e)–(j) are human reference so the reading block stays under budget.

## Purpose

Carry the walkthroughs that do not fit [`scenarios.md`](scenarios.md)'s reading-block budget, in the
same form: one situation, the invariants it exercises, and what the record shows afterwards. Like its
companion, it walks the design through concrete cases and never states the state of a checkout.

## Scope

Scenarios (e)–(j) only. The design these walk is stated in `work_model.md`,
`gates_and_workflows.md`, `failure_posture.md`, and `authority_model.md`; where a walkthrough and one
of those disagree, the owning document governs. Nothing here is normative on its own.

## (e) A task detached from a batch

Review finds that one of the three tasks does not belong in the change. The task is split out: its
`ADDRESSED_BY` edge to the batch is ended and it enters the workflow again as a new batch of one, from
the first step, with no sign-offs carried over. The original batch continues with the two tasks still
attached and loses no sign-off; its pull request stays attached to it as an artifact, and the new batch
will leave its own. Nothing on either task or either batch records the detachment as a field; the two
edges, one ended and one live, are the record.

```mermaid
flowchart TD
    subgraph before
        T1a[task 1] --> Ra[batch A]
        T2a[task 2] --> Ra
        T3a[task 3] --> Ra
        PRa[artifact: PR] -.-> Ra
    end
    before -->|review: task 3 does not belong| after
    subgraph after
        T1b[task 1] --> Rb[batch A continues]
        T2b[task 2] --> Rb
        PRb[artifact: PR] -.-> Rb
        T3b[task 3] -.->|edge ended| Rb
        T3b -->|new ADDRESSED_BY| Rc[batch B, from step 1]
    end
```

**Invariants:** [`work_model.md#what-goes-through-a-workflow-is-a-batch-of-tasks`](work_model.md#what-goes-through-a-workflow-is-a-batch-of-tasks),
[`#artifacts-are-records-a-batch-leaves-never-its-subject`](work_model.md#artifacts-are-records-a-batch-leaves-never-its-subject),
[`#a-task-is-in-at-most-one-batch-at-a-time`](work_model.md#a-task-is-in-at-most-one-batch-at-a-time);
`principles.md` invariant 11.

## (f) A parent task with children in independent batches

A parent task is created as the grouping of a piece of work; three child tasks each carry a `PART_OF`
edge to it. Each child is claimed, executed, and goes through its own batch on its own schedule. The
parent is never claimed and never enters a workflow. When a reader asks whether the parent is complete,
the answer is derived from the children's terminal states at that moment and is stored nowhere.

```mermaid
flowchart TD
    P[parent task: never claimed, never in a batch]
    C1[child 1] -->|PART_OF| P
    C2[child 2] -->|PART_OF| P
    C3[child 3] -->|PART_OF| P
    C1 -->|ADDRESSED_BY| R1[batch 1]
    C2 -->|ADDRESSED_BY| R2[batch 2]
    C3 -->|ADDRESSED_BY| R3[batch 3]
    R1 --> D{all children terminal?}
    R2 --> D
    R3 --> D
    D -->|derived at read| PC[parent reads complete]
```

**Invariants:** [`work_model.md#parent-and-child-tasks`](work_model.md#parent-and-child-tasks);
`principles.md` invariant 11.

## (g) An operator-only task, claimed by the operator-facing agent

A task is created with `operator_only` among its declared action classes. It is an ordinary task,
claimable by the operator-facing agent, which claims it and holds the lease; being operator-only raises
no checkpoint by itself. The one action the task needs is created and evaluated at the action gate,
which resolves `operator_only` to `NEVER` ahead of any policy and writes a checkpoint: subject the
action, reason `gate_hold`, awaiting the operator. The agent carries the checkpoint to the operator
through the configured channel and renews its lease while waiting. The operator resolves the checkpoint;
the agent records the outcome on the task, completes it, and returns the lease. No action was taken
without the operator, and the task path stayed pull.

```mermaid
sequenceDiagram
    participant N as record
    participant A as operator-facing agent
    participant G as action gate
    participant O as operator
    A->>N: claim task (operator_only declared), read back
    A->>N: create action (class operator_only)
    A->>G: evaluate action
    G->>N: write checkpoint (subject action, reason gate_hold, NEVER, awaits operator)
    A->>O: carry the checkpoint
    loop while awaiting
        A->>N: renew lease
    end
    O->>N: resolve checkpoint (terminal state, resolver recorded)
    A->>N: record outcome, status completed, read back
    A->>N: lease returned
```

**Invariants:** [`work_model.md#operator-only-tasks-are-claimed-by-the-operator-facing-agent`](work_model.md#operator-only-tasks-are-claimed-by-the-operator-facing-agent),
[`gates_and_workflows.md#confidence-and-three-blast-tiers`](gates_and_workflows.md#confidence-and-three-blast-tiers),
[`#the-checkpoint`](gates_and_workflows.md#the-checkpoint),
[`failure_posture.md#what-a-checkpoint-does-not-absorb`](failure_posture.md#what-a-checkpoint-does-not-absorb),
[`authority_model.md#approval`](authority_model.md#approval); `principles.md` invariant 5.

## (h) An action discovered mid-workflow, at NEVER, HIGH, and LOW

A task declared `docs` at creation. While executing it, the agent finds the change also needs an outreach
mail. That effect becomes an `action` the moment it is known, with class `send_external_comms`; the
declaration at creation is not amended, because it was a declaration of expectation, not a bound. The
principal about to take the action evaluates the gate with the action's class, its confidence, the
`action_policy`, and the class's recurrences. At `NEVER` the checkpoint is written (reason `gate_hold`)
and nothing else is consulted. At `HIGH` the checkpoint is written unless an action series has
graduated the class. At `LOW` the action is taken at or above the confidence threshold, or once the
series has graduated, and is checkpointed otherwise. A class in neither set logs the value and resolves
to `NEVER`.

```mermaid
flowchart TD
    W[executing a task declared docs] --> D[effect discovered: outreach mail]
    D --> A[create action: class send_external_comms, PRODUCES from task]
    A --> G{action gate: class under action_policy}
    G -->|operator_only, or class unclassified| N[NEVER: checkpoint, reason gate_hold; nothing else consulted]
    G -->|HIGH| H{action series graduated?}
    H -->|no| HB[checkpoint]
    H -->|yes| HX[take the action]
    G -->|LOW| L{confidence at threshold, or series graduated?}
    L -->|yes| LX[take the action]
    L -->|no| LB[checkpoint]
```

**Invariants:** [`gates_and_workflows.md#actions-are-entities-only-actions-are-taken`](gates_and_workflows.md#actions-are-entities-only-actions-are-taken),
[`#confidence-and-three-blast-tiers`](gates_and_workflows.md#confidence-and-three-blast-tiers),
[`#the-action-gate-is-pr-independent`](gates_and_workflows.md#the-action-gate-is-pr-independent),
[`#non-code-deliverables-go-through-the-same-gate`](gates_and_workflows.md#non-code-deliverables-go-through-the-same-gate);
`principles.md` invariant 5.

## (i) Neotoma unreachable, halt

A runner about to claim a task performs the reachability probe, a real read of what the work will read.
The read fails. The runner claims nothing, evaluates no gate, writes no checkpoint (there is nothing to
write to), and announces the halt on the off-Neotoma path, aggregated per window. A second runner already
mid-task attempts its sign-off write, which fails; it leaves the task in its prior state, writes its
diagnostic capture to local disk, and stops. Its lease lapses on its own. When the probe succeeds again,
the halt is announced as lifted, the task is claimable, and a re-claim finds the effects already taken by
their dedup keys; a lapse count that reached its cap during the outage becomes a checkpoint now. Any grant
or gate read during the outage returned `unknown`, which every enforcement point treated as deny.

```mermaid
flowchart TD
    R1[runner 1: about to claim] --> P{reachability probe: real read}
    P -->|fails| H[halt: no claim, no gate decision, no checkpoint]
    H --> AN[announce halt off-Neotoma, per window]
    R2[runner 2: mid-task] --> W{sign-off write}
    W -->|fails| L[leave prior state; capture to local disk; stop]
    L --> LP[lease lapses on its own]
    P -->|succeeds later| U[announce halt lifted]
    LP --> C[task claimable again]
    U --> C
    U --> CK[lapse cap reached during the outage: checkpoint written now]
    C --> RC[re-claim; dedup keys skip effects already taken]
    subgraph during the outage
        Q[any grant or gate read] --> UN[unknown]
        UN --> DN[enforcement point: deny]
    end
```

**Invariants:** [`failure_posture.md#the-decision`](failure_posture.md#the-decision),
[`#the-rules`](failure_posture.md#the-rules) (1 to 7),
[`#what-a-checkpoint-does-not-absorb`](failure_posture.md#what-a-checkpoint-does-not-absorb),
[`work_model.md#a-lapsed-lease-is-not-reaped-repeated-lapse-raises-a-checkpoint`](work_model.md#a-lapsed-lease-is-not-reaped-repeated-lapse-raises-a-checkpoint),
[`authority_model.md#the-tuple`](authority_model.md#the-tuple) (`Indeterminate` is deny); `principles.md`
invariants 2 and 7.

## (j) A task created, routed by intake, and entering its successor

A task is created; that is its publication. It has no intake batch, so it is unrouted by that fact, and
nothing else records it as such. The task enters intake: a batch record opens for it, and the product
its step owner claims each step in turn, a lease on the step and a sign-off to close it: `classify` writes the
task's `action_type` and, where a named principal is the point, `assigned_to`; `link` attaches the issue
the task already concerns as an artifact; `dedupe` finds no open duplicate; `prioritize` sets the
priority from the `priority_rubric` entity; `route`'s sign-off, the closing sign-off of the batch, names
`feature` as the successor. The task leaves intake and enters the feature workflow: a new batch record
opens with a `FOLLOWS` edge to the intake batch, and from there the scenario is (d). At any moment the
task's chain is read along `FOLLOWS` from its live batch back to intake; nothing on the task records
which workflows it has gone through, and no router chose the successor: a step owner signed it.

```mermaid
flowchart TD
    C[task created: publication] --> U{intake batch exists?}
    U -->|no: unrouted by that fact| I[task enters intake; batch record opens]
    I --> S1[classify: action_type, assigned_to, parent or children]
    S1 --> S2[link: existing issue attached as artifact]
    S2 --> S3[dedupe: no open duplicate]
    S3 --> S4[prioritize: from the priority_rubric entity]
    S4 --> S5[route: closing sign-off names feature]
    S5 --> F[task enters feature; batch record opens]
    F -.->|FOLLOWS| I
    F --> D[from here, scenario d]
    D --> R[batch: release workflow]
    R -.->|FOLLOWS| F
    R --> CH[chain, read along FOLLOWS: intake → feature → release; stored nowhere]
```

**Invariants:** [`work_model.md#intake-is-every-tasks-first-workflow`](work_model.md#intake-is-every-tasks-first-workflow),
[`#there-is-no-task-lifecycle-there-are-batches`](work_model.md#there-is-no-task-lifecycle-there-are-batches),
[`#pull-is-the-only-delivery-assignment-constrains-eligibility`](work_model.md#pull-is-the-only-delivery-assignment-constrains-eligibility),
[`gates_and_workflows.md#sequencing-is-data-successors-and-the-chain`](gates_and_workflows.md#sequencing-is-data-successors-and-the-chain),
[`workflows.md#intake`](workflows.md#intake); `principles.md` invariant 11.
