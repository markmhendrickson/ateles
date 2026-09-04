# Vocabulary: canonical terms

**Keyed document:** read when a skill, an agent document, or the agent-doc renderer changes
(`conformance.md`). **Kind:** foundation; defines terms by what they are in the design, never by what a
checkout implements. **Derived from:** synthesis `ent_b0ce322f768e4fc676b73139` (PR-03, PR-08, C10), prior
art `ent_08460968e6f49dac21510f4a` (A2A `TaskState`, RFC 8693, Camunda), task
`ent_da60df3beccb675ef8c8c0c5`, the ateles#378 glossary (operator section, and the ux-signed swarm section
cited as proposal), `docs/multi_tenant.md` section 5, and PR #745 operator review (2026-09-04). Format
follows Neotoma's `docs/vocabulary/canonical_terms.md`.

## Purpose

One list of the terms the swarm's documents, schemas, prompts, and error messages use, each with a
definition, the terms it depends on, and the synonyms it forbids, grouped by the document that owns it.

## Scope

A term that names an entity type is written as the entity type (`checkpoint_brief`). Every definition is
one sentence; elaboration follows it or lives in the owning document. Every entry links the terms it
depends on and the owning section. Terms carry no phase marker: the roadmap is `status.md`, and a
definition does not change when its implementation lands.

## Work model (`work_model.md`)

### task
**Definition:** the atomic unit of accountable work, recorded as a Neotoma `task` entity.
**Related:** [claim](#claim), [action](#action), [artifact](#artifact), [parent task](#parent-task),
[passage](#passage), [intake](#intake), [chain](#chain);
[`work_model.md#the-transition-vocabulary`](work_model.md#the-transition-vocabulary),
[`work_model.md#there-is-no-task-lifecycle-there-are-passages`](work_model.md#there-is-no-task-lifecycle-there-are-passages).
**Forbidden:** "chip", "ticket" (a GitHub issue is an `issue`, an artifact; a task may refer to one), "work
item", "work entity" (retired; the thing that passes through a workflow is a task), "lifecycle" for the
task (its only states are its status and its edges; there is no task state machine, C1).

### work (a task)
**Definition:** to claim, progress, and complete a task.
Tasks are worked; only actions are executed.
**Related:** [task](#task), [claim](#claim), [execute (an action)](#execute-an-action);
[`gates_and_workflows.md#actions-are-entities-only-actions-execute`](gates_and_workflows.md#actions-are-entities-only-actions-execute).
**Forbidden:** "execute" for a task, "run" for a task.

### artifact
**Definition:** a record in an external system that a passage produces or references, such as a GitHub
issue, a pull request, a release, a published page, or a sent message, linked to the passage and its tasks
by edge and never the subject of a step.
An action is the intended effect; the artifact is the record the effect leaves.
**Related:** [passage](#passage), [task](#task), [action](#action), [issue](#issue), [sign-off](#sign-off);
[`work_model.md#artifacts-are-records-a-passage-leaves-never-its-subject`](work_model.md#artifacts-are-records-a-passage-leaves-never-its-subject).
**Forbidden:** "work item", "work entity" (retired), "deliverable" for the record, "task" for the artifact.

### claim
**Definition:** the act by which an agent takes the lease on a task, or on a step of a passage, itself,
atomic among concurrent claimants and keyed on the task or the step.
**Use:** "Corvus claims a task that is eligible for it: `assigned_to` is unset or names Corvus, and no
lease is held. The claim, not the assignment, makes Corvus the claimant."
**Related:** [lease](#lease), [claimant](#claimant), [claimable](#claimable), [assign](#assign);
[`work_model.md#the-claim-and-the-lease-are-one-primitive`](work_model.md#the-claim-and-the-lease-are-one-primitive).
**Forbidden:** "assign" (an eligibility constraint, which creates no lease), "pick up", "dispatch",
"push", "hand off", "spawn" for delivery (a runner is started; work is claimed).

### claimant
**Definition:** the principal that holds the lease on a task, read back from the persisted lease and never
from a task field.
**Related:** [claim](#claim), [lease](#lease), [runner](#runner), [assign](#assign);
[`work_model.md#the-lease-is-a-relationship-not-a-set-of-task-fields`](work_model.md#the-lease-is-a-relationship-not-a-set-of-task-fields).
**Forbidden:** "assignee" (the principal an assignment names, who has not necessarily claimed), "owner",
"holder" without the lease.

### assign
**Definition:** the act by which a principal restricts a task's eligibility to one named principal by
writing `assigned_to`, a field write like any other, creating no lease.
An assignment is the resulting state; it is not delivery, and the assignee still claims. Pull is the only
delivery.
**Related:** [claim](#claim), [claimant](#claimant), [claimable](#claimable), [step owner](#step-owner);
[`work_model.md#assignment-restricts-eligibility-it-never-creates-a-lease`](work_model.md#assignment-restricts-eligibility-it-never-creates-a-lease).
**Forbidden:** "dispatch" (retired; it once named publication, claim, assignment, and execution), "route",
"push" (there is no push path), "hand off", "spawn" for delivery, Camunda's `setAssignee` (which installs a
holder without a claim).

### lease
**Definition:** a relationship between a principal and a task, or between a step owner and a step on a
passage, carrying `claimed_at` and `expires_at`, that lapses without cooperation from its holder.
The claim and the lease are one primitive; renewal is the heartbeat; the task carries no lease fields.
**Related:** [claim](#claim), [claimant](#claimant), [held](#held), [lapsed](#lapsed), [returned](#returned),
[active](#active), [step state](#step-state);
[`work_model.md#the-lease-is-a-relationship-not-a-set-of-task-fields`](work_model.md#the-lease-is-a-relationship-not-a-set-of-task-fields).
**Forbidden:** "lock" (a lock outlives its holder), "heartbeat" alone (the heartbeat renews the lease; it
is not the lease), "claim fields".

### held
**Definition:** the lease state, derived at read time, in which `expires_at` is in the future.
**Related:** [lease](#lease), [lapsed](#lapsed), [returned](#returned), [claimable](#claimable);
[`work_model.md#the-transition-vocabulary`](work_model.md#the-transition-vocabulary).
**Forbidden:** "claimed" as a stored task status, "locked".

### lapsed
**Definition:** the lease state, derived at read time, in which `expires_at` has passed and the claimant
has not returned the lease.
A lapsed lease does not count for claimability, so the task is claimable again without any process acting
on the lease.
**Related:** [lease](#lease), [held](#held), [returned](#returned), [watchdog](#watchdog);
[`work_model.md#a-lapsed-lease-is-not-reaped-repeated-lapse-escalates`](work_model.md#a-lapsed-lease-is-not-reaped-repeated-lapse-escalates).
**Forbidden:** "stuck", "stranded" as a task status, "expired and released" (nothing releases it).

### returned
**Definition:** the lease state in which the claimant ended the lease explicitly, on completion or on
failure.
**Related:** [lease](#lease), [held](#held), [lapsed](#lapsed), [claimant](#claimant);
[`work_model.md#the-transition-vocabulary`](work_model.md#the-transition-vocabulary).
**Forbidden:** "released" (collides with the `release` step and the software release), "surrendered"
(expiry is not volitional and gets no such word).

### active
**Definition:** the derived read that a held lease has activity entities, such as an `agent_session` or
observations, related to the task within the lease window.
Never stored; a dashboard derives live-versus-quiet from it.
**Related:** [lease](#lease), [held](#held), [agent_session](#agent_session), [observation](#observation);
[`work_model.md#liveness-is-derived-from-activity-at-read-time-never-declared`](work_model.md#liveness-is-derived-from-activity-at-read-time-never-declared).
**Forbidden:** `running`, `executing` as liveness assertions, "in flight", `active` as a status value.

### created
**Definition:** the task transition in which the task comes to exist in the record, publication being
creation.
The task's own transition vocabulary is `created` plus its status; lease transitions belong to the lease.
**Related:** [task](#task), [held](#held), [lapsed](#lapsed), [returned](#returned), [active](#active);
[`work_model.md#the-transition-vocabulary`](work_model.md#the-transition-vocabulary).
**Forbidden:** "published" as a separate state, `routed`, `claimed` or `released` as task transitions.

### claimable
**Definition:** the derived property of a task whose status is neither terminal nor `blocked`, whose
`assigned_to` is unset or names the would-be claimant, and on which no lease is held.
**Related:** [claim](#claim), [held](#held), [lapsed](#lapsed), [assign](#assign), [terminal](#terminal);
[`work_model.md#what-a-claim-predicate-treats-as-claimable`](work_model.md#what-a-claim-predicate-treats-as-claimable).
**Forbidden:** "available", "open" as a synonym (a status value).

### terminal
**Definition:** a status value after which a task, a passage, or a checkpoint_brief changes no further.
**Related:** [claimable](#claimable), [parent task](#parent-task), [approval](#approval);
[`work_model.md#what-a-claim-predicate-treats-as-claimable`](work_model.md#what-a-claim-predicate-treats-as-claimable).
**Forbidden:** "closed" unqualified, "final".

### runner
**Definition:** the process that runs an agent and holds a lease on the agent's behalf, identified by a
runner id the persisted lease names.
**Related:** [agent](#agent), [claimant](#claimant), [lease](#lease), [agent_session](#agent_session);
[`work_model.md#the-claim-and-the-lease-are-one-primitive`](work_model.md#the-claim-and-the-lease-are-one-primitive).
**Forbidden:** "worker", "bot", "agent" when the process is meant.

### agent_session
**Definition:** the entity carrying the identity half of a runner's work that observations lack, such as host,
checkout, branch, and head, related to the task it works.
**Related:** [runner](#runner), [active](#active), [observation](#observation);
[`work_model.md#no-assignment-log-history-is-the-tasks-own-observations`](work_model.md#no-assignment-log-history-is-the-tasks-own-observations).
**Forbidden:** "run history", "dispatch record".

### observation
**Definition:** one append-only, timestamped, provenance-bearing write to an entity in the record, from
which the entity's history is read.
**Related:** [task](#task), [active](#active), [agent_session](#agent_session), [read-back](#read-back);
[`work_model.md#no-assignment-log-history-is-the-tasks-own-observations`](work_model.md#no-assignment-log-history-is-the-tasks-own-observations).
**Forbidden:** "event" for a stored change, "log line".

### hot path
**Definition:** a read path on which a decision must be taken from one entity read, for which a projection
such as `step_status` exists.
**Related:** [step_status](#step_status), [sign-off](#sign-off);
[`gates_and_workflows.md#declaration-passage-projection`](gates_and_workflows.md#declaration-passage-projection).
**Forbidden:** "fast path" (a declared skip of steps), "cache".

### watchdog
**Definition:** the observer that counts lapses on a task and escalates when the count reaches its cap,
holding no authority over any lease.
**Related:** [lapsed](#lapsed), [escalation](#escalation), [reaper](#reaper);
[`failure_posture.md#repeated-lapse-escalates`](failure_posture.md#repeated-lapse-escalates).
**Forbidden:** "reaper", "router", "retry loop".

### reaper
**Definition:** retired; the watchdog role that returned an expired lease, made unnecessary by a lease
whose lapse already stops it counting for claimability.
**Related:** [lapsed](#lapsed), [watchdog](#watchdog);
[`work_model.md#a-lapsed-lease-is-not-reaped-repeated-lapse-escalates`](work_model.md#a-lapsed-lease-is-not-reaped-repeated-lapse-escalates).
**Forbidden:** "reaper", "release an expired claim", "re-route".

### aggregation
**Definition:** the attachment of several tasks to one passage by an `ADDRESSED_BY` edge from each task to
the passage.
**Related:** [passage](#passage), [task](#task), [split](#split), [artifact](#artifact);
[`work_model.md#what-passes-through-a-workflow-is-a-passage-of-tasks`](work_model.md#what-passes-through-a-workflow-is-a-passage-of-tasks).
**Forbidden:** "batch", "bundle", a task field listing passages.

### split
**Definition:** ending a task's edge to a passage and starting a new passage for it, while the original
passage continues with the remaining tasks.
**Related:** [aggregation](#aggregation), [passage](#passage), [child task](#child-task);
[`work_model.md#what-passes-through-a-workflow-is-a-passage-of-tasks`](work_model.md#what-passes-through-a-workflow-is-a-passage-of-tasks).
**Forbidden:** "fork", "re-run".

### parent task
**Definition:** a task that aggregates child tasks through `PART_OF` edges from each child, whose
completion is derived from its children's terminal states and for which no passage ever opens.
**Related:** [child task](#child-task), [task](#task), [terminal](#terminal), [passage](#passage);
[`work_model.md#parent-and-child-tasks`](work_model.md#parent-and-child-tasks).
**Forbidden:** "epic", "umbrella", a stored parent status.

### child task
**Definition:** a task with one `PART_OF` edge to a parent task, which passes through workflows independently
of its siblings.
**Allowed:** "subtask" in prose.
**Related:** [parent task](#parent-task), [task](#task), [passage](#passage), [split](#split);
[`work_model.md#parent-and-child-tasks`](work_model.md#parent-and-child-tasks).
**Forbidden:** "story", a child with two parents.

### operator-facing agent
**Definition:** the agent, defined by the `ateles` `agent_definition`, that claims operator-only tasks,
carries them to the operator, and records the outcome.
**Related:** [operator_only](#operator_only), [claim](#claim), [checkpoint_brief](#checkpoint_brief);
[`work_model.md#operator-only-tasks-are-claimed-by-the-operator-facing-agent`](work_model.md#operator-only-tasks-are-claimed-by-the-operator-facing-agent).
**Forbidden:** "the operator" for the agent, "concierge" unqualified.

### daemon
**Definition:** a long-running process that self-triggers on its own loop, producing tasks or actions
without receiving a task.
**Related:** [runner](#runner), [action](#action), [execution gate](#execution-gate);
[`work_model.md#the-three-execution-mechanisms`](work_model.md#the-three-execution-mechanisms).
**Forbidden:** "service" unqualified, "bot".

### pipeline
**Definition:** the GitHub-hosted execution mechanism that opens each step of a workflow for a passage as
claimable step work, which the step owner claims, and never writes a task status.
It delivers nothing; it is the same pull, over steps.
**Related:** [step owner](#step-owner), [claim](#claim), [step state](#step-state), [steward](#steward),
[review panel](#review-panel);
[`work_model.md#the-three-execution-mechanisms`](work_model.md#the-three-execution-mechanisms).
**Forbidden:** "workflow" (the declaration), "CI" (one of its checks), "spawns a role" (a runner is
started; the step is claimed).

## Gate model (`gates_and_workflows.md`)

### workflow
**Definition:** the entity declaring, per (project, workflow type), an ordered list of steps, the fast
paths a passage may take, and the successors a closing sign-off may name.
**Related:** [step](#step), [stage](#stage), [passage](#passage), [step owner](#step-owner),
[workflow policy](#workflow-policy), [fast path](#fast-path), [successor](#successor);
[`workflows.md`](workflows.md) (the core workflows);
[`gates_and_workflows.md#declaration-passage-projection`](gates_and_workflows.md#declaration-passage-projection).
**Forbidden:** `workflow_definition` (retired), "pipeline" (one engine that runs workflows), "template".

### step
**Definition:** one declared position in a workflow's ordered list, carrying a name, an owner, a `required`
flag, and parallel-group and join fields, claimed by its owner on a passage and closed by its owner's
sign-off.
Step names are data (`pm`, `ux`, `arch`, `impl`, `pr_review`, `qa`, `legal`, `release`, and any a workflow
declares).
**Related:** [workflow](#workflow), [step owner](#step-owner), [sign-off](#sign-off), [step state](#step-state),
[stage](#stage);
[`gates_and_workflows.md#declaration-passage-projection`](gates_and_workflows.md#declaration-passage-projection).
**Forbidden:** "gate" (reserved for the execution gate), "phase" alone, "check" (a CI status), "checkpoint".

### stage
**Definition:** a named group of contiguous steps in a workflow, such as the review stage or the release
stage.
**Related:** [step](#step), [workflow](#workflow);
[`gates_and_workflows.md#declaration-passage-projection`](gates_and_workflows.md#declaration-passage-projection).
**Forbidden:** "stage" for a single step, "phase" when a group of steps is meant.

### step owner
**Definition:** the agent declared on a step as the principal that claims it on a passage and whose
sign-off closes it.
**Field:** `workflow.steps[].owner_agent`.
**Related:** [step](#step), [sign-off](#sign-off), [agent](#agent), [claim](#claim),
[workflow policy](#workflow-policy);
[`gates_and_workflows.md#two-policies-workflow-policy-and-execution-policy`](gates_and_workflows.md#two-policies-workflow-policy-and-execution-policy).
**Forbidden:** "gate owner" (retired), "owner" alone, "assignee".

### sign-off
**Definition:** the record a step owner writes to close a step on a passage, carrying the verdict,
timestamps, the agent, artifact refs, and the pinned `agent_definition` version.
A terminal write that supplies every field the schema requires; a rejected write is an error, never
swallowed.
**Related:** [step owner](#step-owner), [step state](#step-state), [passage](#passage), [terminal](#terminal),
[read-back](#read-back), [artifact](#artifact);
[`gates_and_workflows.md#declaration-passage-projection`](gates_and_workflows.md#declaration-passage-projection).
**Forbidden:** "LGTM", "approval" (an approval is on a `checkpoint_brief`), "green" without the record,
`step_run` and `participation_record` (retired; a step's state is derived, and the sign-off is the one
record written), "audit row".

### passage
**Definition:** the entity recording one passage of tasks through a workflow, to which the tasks attach by
`ADDRESSED_BY` edges.
Reads: "the passage is at `qa`", "tasks aggregated into one passage", "a task split out of a passage",
"a passage opens", "the passage advances to `impl`", "the passage closes naming `release`".
**Related:** [workflow](#workflow), [task](#task), [artifact](#artifact), [step state](#step-state),
[aggregation](#aggregation), [split](#split), [successor](#successor), [chain](#chain);
[`work_model.md#what-passes-through-a-workflow-is-a-passage-of-tasks`](work_model.md#what-passes-through-a-workflow-is-a-passage-of-tasks).
**Forbidden:** `workflow_run` (retired; `run` collides with the retired liveness vocabulary), "run",
"instance" unqualified, "pipeline run", "execution".

### step state
**Definition:** the state of one step within one passage, derived at read time from edges and never
stored: open (the passage and the step), claimed (a lease from the step owner to the step on that
passage), or signed (a sign-off).
**Related:** [step](#step), [passage](#passage), [lease](#lease), [sign-off](#sign-off), [step_status](#step_status);
[`gates_and_workflows.md#declaration-passage-projection`](gates_and_workflows.md#declaration-passage-projection).
**Forbidden:** `step_run` (retired), a stored per-step status row, "gate status" (the projection).

### step_status
**Definition:** the map on the task projecting each step's state on its passage for the hot path, derived
from the sign-offs and proved equal to them by a reconciler.
**Related:** [sign-off](#sign-off), [step state](#step-state), [hot path](#hot-path), [passage](#passage);
[`gates_and_workflows.md#declaration-passage-projection`](gates_and_workflows.md#declaration-passage-projection).
**Forbidden:** `gate_status` (retired), treating it as history, a second source of truth.

### fast path
**Definition:** a declared skip of steps that a workflow permits for a named class of tasks.
**Related:** [workflow](#workflow), [step](#step);
[`gates_and_workflows.md#declaration-passage-projection`](gates_and_workflows.md#declaration-passage-projection).
**Forbidden:** "hot path", "shortcut".

### successor
**Definition:** a workflow that a `workflow` declares in `successors` as one a passage of it may hand its
tasks to on closing, of which the closing sign-off selects exactly one or none.
The closing sign-off is the sign-off on the workflow's last step, which is always a single step.
**Related:** [workflow](#workflow), [passage](#passage), [sign-off](#sign-off), [chain](#chain),
[intake](#intake), [split](#split);
[`gates_and_workflows.md#sequencing-is-data-successors-and-the-chain`](gates_and_workflows.md#sequencing-is-data-successors-and-the-chain).
**Forbidden:** "next stage" (a stage is within a workflow), "downstream workflow", "handoff", two
successors at once (that is a split), a successor named by anything but the closing sign-off.

### chain
**Definition:** the derived, never stored, sequence of passages a task has passed through, read along
`FOLLOWS` edges from its live passage back to its intake passage.
**Related:** [passage](#passage), [successor](#successor), [intake](#intake), [task](#task);
[`gates_and_workflows.md#sequencing-is-data-successors-and-the-chain`](gates_and_workflows.md#sequencing-is-data-successors-and-the-chain).
**Forbidden:** "program", "super-workflow", "pipeline" for the sequence, "lifecycle", a stored list of
passages on the task.

### issue
**Definition:** a GitHub issue, recorded as an `issue` entity, which is an artifact a passage produces or
references, linked to the passage and its tasks by edge.
**Related:** [artifact](#artifact), [passage](#passage), [task](#task);
[`work_model.md#artifacts-are-records-a-passage-leaves-never-its-subject`](work_model.md#artifacts-are-records-a-passage-leaves-never-its-subject).
**Forbidden:** "ticket", "task" for the issue, "work item", the subject of a step.

### gate
**Definition:** short for the execution gate, and nothing else.
**Related:** [execution gate](#execution-gate), [step](#step);
[`gates_and_workflows.md#the-execution-gate-is-pr-independent`](gates_and_workflows.md#the-execution-gate-is-pr-independent).
**Forbidden:** "gate" for a step or a stage, "gate owner", "gates green".

### execution gate
**Definition:** the decision, taken by a principal evaluating one action against the execution policy,
whether that action executes or checkpoints.
Inputs are the action's class, blast radius, confidence, and successful recurrences; no PR, issue, or
repository.
**Related:** [action](#action), [execution_policy](#execution_policy), [blast radius](#blast-radius),
[confidence](#confidence), [checkpoint_brief](#checkpoint_brief), [gate](#gate);
[`gates_and_workflows.md#the-execution-gate-is-pr-independent`](gates_and_workflows.md#the-execution-gate-is-pr-independent).
**Forbidden:** "merge gate" as a synonym (merge is one boundary among several), "gate" alone when a step
is meant.

### execution_policy
**Definition:** the policy a principal evaluates the execution gate against, listing the low- and
high-blast action classes, the confidence threshold, the recurrence count that graduates a series, the
always-checkpoint boundaries, and the permission scope.
**Related:** [execution gate](#execution-gate), [action_type](#action_type), [blast radius](#blast-radius),
[workflow policy](#workflow-policy);
[`gates_and_workflows.md#two-policies-workflow-policy-and-execution-policy`](gates_and_workflows.md#two-policies-workflow-policy-and-execution-policy).
**Forbidden:** "config", "settings", "workflow policy".

### workflow policy
**Definition:** the rule set stating which principals may claim which steps of which workflows, composed
of the workflow's step owners and the `agent_grant`s in force.
**Related:** [workflow](#workflow), [step owner](#step-owner), [grant](#grant), [claim](#claim),
[execution_policy](#execution_policy);
[`gates_and_workflows.md#two-policies-workflow-policy-and-execution-policy`](gates_and_workflows.md#two-policies-workflow-policy-and-execution-policy).
**Forbidden:** "execution policy", "permissions".

### action
**Definition:** an entity representing one intended effect outside the Ateles system, such as a send, a
publish, a merge, a payment, or a release, related to the task it serves.
Created when the effect becomes known, which may be mid-workflow; a task may produce many, most unknown at
creation; an internal operational write to Neotoma is not an action.
**Related:** [task](#task), [action_type](#action_type), [execution gate](#execution-gate),
[execute (an action)](#execute-an-action), [effect dedup](#effect-dedup), [artifact](#artifact) (the
record the effect leaves);
[`gates_and_workflows.md#actions-are-entities-only-actions-execute`](gates_and_workflows.md#actions-are-entities-only-actions-execute).
**Forbidden:** "side effect" (unrecorded), "task" for the effect, "operation".

### execute (an action)
**Definition:** to carry out an action's effect outside the system once the execution gate permits it.
**Related:** [action](#action), [execution gate](#execution-gate), [work (a task)](#work-a-task);
[`gates_and_workflows.md#actions-are-entities-only-actions-execute`](gates_and_workflows.md#actions-are-entities-only-actions-execute).
**Forbidden:** "execute" for a task (a task is worked), "fire".

### action_type
**Definition:** the class an action belongs to, on which blast radius keys, and which a task declares at
creation as the classes of action it expects to produce.
Values include `build`, `docs`, `publish`, `send_external_comms`, and `operator_only`; a declared but
unclassified value fails closed.
**Related:** [action](#action), [blast radius](#blast-radius), [operator_only](#operator_only),
[execution_policy](#execution_policy);
[`gates_and_workflows.md#confidence-and-three-blast-tiers`](gates_and_workflows.md#confidence-and-three-blast-tiers).
**Forbidden:** inferring it from the handling agent, "category", "kind".

### blast radius
**Definition:** the tier an action_type resolves to under an execution_policy, one of `LOW`, `HIGH`, or
`NEVER`.
`LOW` executes at or above the confidence threshold or once a recurring series graduates; `HIGH`
checkpoints until a recurring series graduates; `NEVER` is cleared by no confidence and no recurrence.
**Related:** [action_type](#action_type), [execution_policy](#execution_policy), [confidence](#confidence),
[recurring series](#recurring-series), [operator_only](#operator_only);
[`gates_and_workflows.md#confidence-and-three-blast-tiers`](gates_and_workflows.md#confidence-and-three-blast-tiers).
**Forbidden:** "risk level" (unbounded), "severity".

### confidence
**Definition:** the proposing agent's score that an action is right, compared with the policy's threshold.
**Related:** [action](#action), [execution gate](#execution-gate), [blast radius](#blast-radius);
[`gates_and_workflows.md#confidence-and-three-blast-tiers`](gates_and_workflows.md#confidence-and-three-blast-tiers).
**Forbidden:** a default of zero standing in for a score.

### recurring series
**Definition:** a series of successful executions of one action class that, on reaching the policy's count,
graduates that class from checkpointing to auto-execution.
**Related:** [blast radius](#blast-radius), [execution_policy](#execution_policy), [action_type](#action_type);
[`gates_and_workflows.md#confidence-and-three-blast-tiers`](gates_and_workflows.md#confidence-and-three-blast-tiers).
**Forbidden:** "streak", "history" unqualified.

### operator_only
**Definition:** the action_type marking an effect an agent structurally cannot carry out, which resolves to
`NEVER` ahead of any policy.
The task that carries it is still claimable, by the operator-facing agent.
**Related:** [action_type](#action_type), [blast radius](#blast-radius),
[operator-facing agent](#operator-facing-agent);
[`gates_and_workflows.md#confidence-and-three-blast-tiers`](gates_and_workflows.md#confidence-and-three-blast-tiers).
**Forbidden:** "high blast" (a louder `HIGH` delays the wrong outcome rather than preventing it),
"unclaimable".

### checkpoint_brief
**Definition:** the entity the execution gate writes when an action cannot auto-execute, holding the
action in an interrupted state that awaits a principal's decision and records whom it awaits and who
resolved it.
To checkpoint an action is to write one and hold.
**Related:** [execution gate](#execution-gate), [action](#action), [approval](#approval),
[principal](#principal);
[`gates_and_workflows.md#the-approval-object`](gates_and_workflows.md#the-approval-object).
**Forbidden:** "approval request" without the entity name, "checkpoint" for a step.

### steward
**Definition:** the pipeline role that merges a pull request once every required step is signed off and
the execution gate permits the merge action.
**Related:** [pipeline](#pipeline), [sign-off](#sign-off), [action](#action), [execution gate](#execution-gate);
[`gates_and_workflows.md#the-execution-gate-is-pr-independent`](gates_and_workflows.md#the-execution-gate-is-pr-independent).
**Forbidden:** "merger", "bot".

### review panel
**Definition:** the set of lenses the pipeline runs on a pull request, each by its step owner.
**Related:** [lens](#lens), [step owner](#step-owner), [pipeline](#pipeline);
[`conformance.md#always-read`](conformance.md#always-read).
**Forbidden:** "reviewers" unqualified, "CI".

### effect dedup
**Definition:** the rule that every outbound effect is idempotent or deduplicated on its own key, so a
re-claimed task never repeats an effect that already ran.
**Related:** [action](#action), [lapsed](#lapsed), [claim](#claim);
[`work_model.md#at-least-once-implies-effect-dedup`](work_model.md#at-least-once-implies-effect-dedup).
**Forbidden:** "replay protection" (replay is refused outright), "retry".

## Core workflows (`workflows.md`)

### intake
**Definition:** the workflow every task passes through first, whose steps classify, link, dedupe,
prioritize, and route the task, and whose closing sign-off names the successor workflow, or none, or
operator-only.
A task with no intake passage is unrouted by that fact; no unrouted state is stored.
**Related:** [task](#task), [passage](#passage), [successor](#successor), [chain](#chain),
[operator-facing agent](#operator-facing-agent), [action_type](#action_type);
[`workflows.md#intake`](workflows.md#intake),
[`work_model.md#intake-is-every-tasks-first-passage`](work_model.md#intake-is-every-tasks-first-passage).
**Forbidden:** "triage" for the whole workflow (its first stage), "undispatched", "unrouted" as a stored
status, "routing" by a router (the `route` step is a sign-off by a step owner).

## Authority model (`authority_model.md`)

### authority
**Definition:** the right to take an action, expressed as `principal + domain + scope + action +
conditions + time`.
**Related:** [principal](#principal), [grant](#grant), [action](#action), [delegation](#delegation);
[`authority_model.md#the-tuple`](authority_model.md#the-tuple).
**Forbidden:** "permission" alone (a scope term), "access".

### principal
**Definition:** an actor, human or agent, that authority is attributed to, recorded as an entity that
edges point to.
**Related:** [operator](#operator), [agent](#agent), [credential](#credential), [tenant](#tenant);
[`authority_model.md#principals`](authority_model.md#principals).
**Forbidden:** "owner" unless ownership is meant, "identity" (the credential, not the actor), "user" (the
store's authenticated credential).

### credential
**Definition:** a binding from a login, key, address, or chat id to a principal, many-to-one, and never the
principal itself.
**Related:** [principal](#principal), [grant](#grant), [agent](#agent);
[`authority_model.md#principals`](authority_model.md#principals).
**Forbidden:** "identity" for the principal, "account".

### operator
**Definition:** a human principal who directs agents.
**Related:** [principal](#principal), [approval](#approval), [operator-facing agent](#operator-facing-agent);
[`authority_model.md#principals`](authority_model.md#principals).
**Forbidden:** "user" when authority is meant, "admin".

### agent
**Definition:** a non-human principal defined by an `agent_definition` and acting as a bound principal.
**Related:** [principal](#principal), [runner](#runner), [grant](#grant), [step owner](#step-owner);
[`authority_model.md#principals`](authority_model.md#principals).
**Forbidden:** "bot", "worker" (the process running an agent is a runner).

### tenant
**Definition:** the isolation boundary, an organization or a solo operator, that no read, write, routing,
or key crosses.
**Related:** [principal](#principal), [grant](#grant);
[`authority_model.md#principals`](authority_model.md#principals).
**Forbidden:** "account", "workspace" alone.

### grant
**Definition:** an `agent_grant` holding the domain and scope a principal may act in, matched on its
credential, as operation × entity types × repositories with parameter constraints and an expiry.
Zero grants is deny.
**Related:** [principal](#principal), [credential](#credential), [authority](#authority),
[workflow policy](#workflow-policy), [enforcement point](#enforcement-point);
[`authority_model.md#grants`](authority_model.md#grants).
**Forbidden:** "permissions" (a capability is one row of a grant), "allowlist" (one enforcement of it).

### decision point
**Definition:** the function, the execution gate or the grant checker, that returns `Permit`, `Deny`, or
`Indeterminate` for one request.
**Related:** [enforcement point](#enforcement-point), [execution gate](#execution-gate), [grant](#grant),
[unknown](#unknown);
[`authority_model.md#the-tuple`](authority_model.md#the-tuple).
**Forbidden:** "policy engine", "checker" unqualified.

### enforcement point
**Definition:** a call site that acts on a decision point's answer and treats `Indeterminate` as `Deny`.
**Related:** [decision point](#decision-point), [unknown](#unknown), [halt](#halt);
[`authority_model.md#the-tuple`](authority_model.md#the-tuple).
**Forbidden:** "advisory check", "passthrough".

### ownership
**Definition:** named accountability for a workflow, domain, queue, or configuration entity, carried as an
`ownership_grant` edge to a principal.
**Related:** [principal](#principal), [workflow](#workflow), [step owner](#step-owner);
[`authority_model.md#principals`](authority_model.md#principals).
**Forbidden:** "assignee" alone.

### delegation
**Definition:** a scoped, time-bounded transfer of action rights, recorded as a `delegation_edge` from
delegator to delegate, in which each hop holds a subset of the delegator's authority.
Delegation is A acting for B and recorded as such; impersonation is A indistinguishable from B (RFC 8693).
**Related:** [authority](#authority), [principal](#principal), [authority_chain](#authority_chain),
[grant](#grant);
[`authority_model.md#delegation`](authority_model.md#delegation).
**Forbidden:** "assign", "handoff" without scope, "impersonation".

### authority_chain
**Definition:** the derived, never stored, read model over delegation edges, grants, and checkpoints that
gives the path from a principal through each delegation hop to the approver for one action.
**Related:** [delegation](#delegation), [grant](#grant), [checkpoint_brief](#checkpoint_brief),
[approval](#approval);
[`authority_model.md#delegation`](authority_model.md#delegation).
**Forbidden:** "audit log" alone.

### approval
**Definition:** an explicit yes, no, or veto by a required principal on a checkpoint_brief, ending in a
terminal state.
A timeout is a terminal state that never continues.
**Related:** [checkpoint_brief](#checkpoint_brief), [principal](#principal), [terminal](#terminal),
[quorum](#quorum), [separation of duties](#separation-of-duties);
[`authority_model.md#approval`](authority_model.md#approval).
**Forbidden:** "LGTM", silent continuation, "resolved" without who, "sign-off" (that closes a step).

### quorum
**Definition:** a structural check requiring m-of-n named principals on one approval object.
**Related:** [approval](#approval), [separation of duties](#separation-of-duties), [principal](#principal);
[`authority_model.md#structural-checks-quorum-and-separation-of-duties`](authority_model.md#structural-checks-quorum-and-separation-of-duties).
**Forbidden:** "required reviewers" (1-of-n is not a quorum), "sign-off".

### separation of duties
**Definition:** a structural check requiring disjointness between the roles on one approval object, such as
raiser and resolver or proposer and approver.
**Related:** [approval](#approval), [quorum](#quorum), [checkpoint_brief](#checkpoint_brief);
[`authority_model.md#structural-checks-quorum-and-separation-of-duties`](authority_model.md#structural-checks-quorum-and-separation-of-duties).
**Forbidden:** "four eyes" unqualified, "sign-off".

### initiative
**Definition:** a proposed change to what the organization pursues.
**Related:** [proposal](#proposal), [reprioritization](#reprioritization), [principal](#principal);
[`authority_model.md#initiative-proposal-reprioritization`](authority_model.md#initiative-proposal-reprioritization).
**Forbidden:** "project", "epic".

### proposal
**Definition:** the ask that an initiative be accepted, made under proposal rights that are distinct from
execution rights.
**Related:** [initiative](#initiative), [approval](#approval), [authority](#authority);
[`authority_model.md#initiative-proposal-reprioritization`](authority_model.md#initiative-proposal-reprioritization).
**Forbidden:** "PR", "RFC" alone.

### reprioritization
**Definition:** the explicit "what stops?" recorded when an initiative is accepted, confirmed by a
principal.
**Related:** [initiative](#initiative), [proposal](#proposal), [principal](#principal);
[`authority_model.md#initiative-proposal-reprioritization`](authority_model.md#initiative-proposal-reprioritization).
**Forbidden:** "priority bump", "re-plan".

## Failure posture (`failure_posture.md`)

### halt
**Definition:** the state in which the swarm does no work because its record is unreachable, while it keeps
observing and announces itself off-Neotoma.
**Related:** [reachability probe](#reachability-probe), [unknown](#unknown), [escalation](#escalation);
[`failure_posture.md#the-decision`](failure_posture.md#the-decision).
**Forbidden:** "degraded mode", "fallback", "offline mode".

### reachability probe
**Definition:** one real read, at the moment a task is claimed, of what the work will read.
**Related:** [halt](#halt), [claim](#claim), [unknown](#unknown);
[`failure_posture.md#the-rules`](failure_posture.md#the-rules).
**Forbidden:** "health check" (`/health` can be green while every read hangs), "ping".

### read-back
**Definition:** the retrieval, after any write that carries a decision, that asserts the field holds the
value written.
**Related:** [observation](#observation), [claim](#claim), [sign-off](#sign-off);
[`principles.md`](principles.md#2-a-write-that-reports-success-has-not-necessarily-happened-read-it-back).
**Forbidden:** treating a 2xx or `success: true` as evidence.

### unknown
**Definition:** the third state of any gate, grant, drift, or reachability reader, meaning the value could
not be determined.
Never coerced to pending or to clear; at an enforcement point it resolves to deny.
**Related:** [enforcement point](#enforcement-point), [decision point](#decision-point), [halt](#halt);
[`failure_posture.md#the-rules`](failure_posture.md#the-rules).
**Forbidden:** "pending" or "clear" for a failed read, "legacy fail-open" (no such category exists).

### escalation
**Definition:** an `escalation` entity recording a moment the swarm needs a human, with reason, needed
input, options, and status.
**Related:** [watchdog](#watchdog), [lapsed](#lapsed), [halt](#halt), [operator](#operator);
[`failure_posture.md#repeated-lapse-escalates`](failure_posture.md#repeated-lapse-escalates).
**Forbidden:** "page" (one delivery of it), "alert".

## Conformance (`conformance.md`)

### kernel document
**Definition:** a foundation document read on every review.
**Related:** [keyed document](#keyed-document), [lens](#lens), [design basis](#design-basis);
[`conformance.md#always-read`](conformance.md#always-read).
**Forbidden:** "core docs", "the P1 docs".

### keyed document
**Definition:** a foundation document read when a changed path matches its key.
Each header says which kind it is.
**Related:** [kernel document](#kernel-document), [lens](#lens);
[`conformance.md#read-when-these-paths-changed`](conformance.md#read-when-these-paths-changed).
**Forbidden:** "optional docs", "secondary docs".

### lens
**Definition:** one reviewing perspective on the review panel (pm, ux, arch, qa, and the rest), run by its
step owner.
**Related:** [review panel](#review-panel), [step owner](#step-owner), [kernel document](#kernel-document);
[`conformance.md#always-read`](conformance.md#always-read).
**Forbidden:** "reviewer" unqualified.

### design basis
**Definition:** the foundation document and section an issue or PR conforms to, or the statement `no
design applies` with a reason, checked mechanically and judged by reading.
**Related:** [kernel document](#kernel-document), [keyed document](#keyed-document), [lens](#lens);
[`conformance.md#design-basis`](conformance.md#design-basis).
**Forbidden:** "reference", "see also".

### status
**Definition:** the dated measurement of the gap between the foundation and a checkout, held in
`status.md` and regenerated rather than maintained.
**Allowed:** naming `status.md` as the state home (for example, "what is built is `status.md`").
**Related:** [design basis](#design-basis), [kernel document](#kernel-document);
[`conformance.md#phases-and-implementation-state`](conformance.md#phases-and-implementation-state).
**Forbidden:** embedding dated figures, counts, or checkout claims from it into a foundation document;
treating it as design evidence.

## Verbs

Each subject has its verb. The pairs are canonical; the phrases in the last column are replaced by them
wherever they appear in a document, a schema, a prompt, or an error message.

| Subject | Verb | Not |
|---|---|---|
| a task, with respect to a workflow | **passes through** it; a passage **opens** for it and **closes** | "runs through", "goes through", "moves through", "enters" a workflow or a passage |
| a passage, from step to step | **advances** | "moves", "progresses", "transitions" |
| a step, within a passage | **opens**; **closes** by sign-off | "fires", "clears", "is satisfied", "goes green" |
| a lease | is **claimed**, **renewed**, **returned**; it **lapses** on its own | "acquired", "released", "freed", "expired and released" |
| an action | is **executed** | "fired", "run", "performed" |
| a task | is **worked** | "executed", "run", "processed" |
| a passage, on closing | **hands** its tasks to one successor, or closes with none | "flows into", "triggers", "enters" the next workflow |

## Owner: five meanings, one word forbidden alone

`owner` on its own is forbidden. Sources use it for five things (C10); each has its own term:

| Meaning | Term | Field |
|---|---|---|
| the agent whose sign-off closes a step | **step owner** | `workflow.steps[].owner_agent` |
| the step a passage is at | **current step** | derived from the passage's step states; projected as `current_owner` |
| the agent a finding is routed to | **routed agent** | `proposed_skill_update.owning_agent` |
| the operator with the book of business for a customer | **book-of-business owner** | `multi_tenant.md` section 5 |
| named accountability for a workflow, domain, or queue | **ownership** (above) | `ownership_grant` |

The principal holding a task's lease is its [claimant](#claimant), never its owner; the principal an
assignment names is its assignee, who is not yet its claimant.
