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
definition, the terms it depends on, and the words it bans, grouped by the document that owns it.

## Scope

A term that names an entity type is written as the entity type (`checkpoint`). Every definition is one
sentence and names the concept; how the concept is recorded (entity type, fields, edges) is
`data_model.md`. Every entry links the terms it depends on and the owning section. Terms carry no phase
marker: the roadmap is `status.md`, and a definition does not change when its implementation lands.

Each entry ends with two lists, read by `execution/scripts/check_foundation_vocabulary.py`
(`conformance.md#mechanical-checks-on-this-directory`):

- **Never:** bare words and phrases banned in all foundation prose, in every sense. A hit fails the check.
  An item written `/…/` is a regular expression; every other item is matched as a whole word, case
  insensitive; a phrase matches across a space or a hyphen. A line that carries a Never or Not-for list,
  or the word "retired", is not scanned, and neither is a table row of this file (the Verbs, Owner, and
  Retired tables name banned words on purpose).
- **Not for:** words allowed in some senses and banned in the stated one. A hit is advisory: the check
  lists it, and the author judges the sense.

## Work model (`work_model.md`)

### task
**Definition:** the atomic unit of accountable work.
**Related:** [claim](#claim), [action](#action), [artifact](#artifact), [parent task](#parent-task),
[batch](#batch), [intake](#intake), [chain](#chain);
[`work_model.md#the-transition-vocabulary`](work_model.md#the-transition-vocabulary),
[`work_model.md#there-is-no-task-lifecycle-there-are-batches`](work_model.md#there-is-no-task-lifecycle-there-are-batches).
**Never:** "chip", "work item", "work entity".
**Not for:** "ticket" for a task (a GitHub issue is an `issue`, an artifact; a task may refer to one);
"lifecycle" for the task's states (its only states are its status and its edges; there is no task state
machine, C1).

### execute (a task)
**Definition:** to claim a task, do its work, and complete it.
Tasks are executed; actions are taken. Plain synonyms in prose: do, work on.
**Related:** [task](#task), [claim](#claim), [take (an action)](#take-an-action);
[`gates_and_workflows.md#actions-are-entities-only-actions-are-taken`](gates_and_workflows.md#actions-are-entities-only-actions-are-taken).
**Never:** /\bworked\b(?!\s+on\b)/, /\bwork(?:s|ing)?\s+(?:a|an|the|that|its|each|every|one|this|those|these)\s+tasks?\b/,
/\btasks?\s+(?:is|are|was|were|be|been|being)\s+worked\b/, /`executing`/,
/\b(?:is|are|was|were|status|state|stays?|stayed)\s+executing\b/, /\bexecuting\s+(?:status|state|flag)\b/ (executing as
a state).
**Not for:** run for a task; process for a task.

### artifact
**Definition:** a record in an external system that a batch produces or references, such as a GitHub issue,
a pull request, a release, a published page, or a sent message, linked to the batch and its tasks by edge
and never the subject of a step.
An action is the intended effect; the artifact is the record the effect leaves.
**Related:** [batch](#batch), [task](#task), [action](#action), [issue](#issue), [sign-off](#sign-off),
[adapter](#adapter), [signal](#signal);
[`work_model.md#artifacts-are-records-a-batch-leaves-never-its-subject`](work_model.md#artifacts-are-records-a-batch-leaves-never-its-subject),
[`adapters.md#the-two-invariants`](adapters.md#the-two-invariants).
**Never:** —
**Not for:** "deliverable" for the record; task for the artifact.

### claim
**Definition:** the act by which an agent takes the lease on a task, or on a step of a batch, itself,
atomic among concurrent claimants and keyed on the task or the step.
**Use:** "Corvus claims a task that is eligible for it: `assigned_to` is unset or names Corvus, and no
lease is held. The claim, not the assignment, makes Corvus the claimant."
**Related:** [lease](#lease), [claimant](#claimant), [claimable](#claimable), [assign](#assign);
[`work_model.md#the-claim-and-the-lease-are-one-primitive`](work_model.md#the-claim-and-the-lease-are-one-primitive).
**Never:** /\bdispatch\w*/, /\bpick(?:s|ed|ing)?[\s-]up\b/, /\bhand(?:s|ed|ing)?[\s-]off\b/,
/\bpush(?:es|ed|ing)?\b/, /\bspawn\w*\b/ (a runner is started; work is claimed).
**Not for:** assign for a claim (an eligibility constraint, which creates no lease).

### claimant
**Definition:** the principal that holds the lease on a task, read back from the persisted lease and never
from a task field.
**Related:** [claim](#claim), [lease](#lease), [runner](#runner), [assign](#assign);
[`work_model.md#the-lease-is-a-relationship-not-a-set-of-task-fields`](work_model.md#the-lease-is-a-relationship-not-a-set-of-task-fields).
**Never:** "assignee" (say: the principal the assignment names, who has not necessarily claimed).
**Not for:** /(?<!step )(?<!plan )(?<!grant )(?<!business )(?<!current )(?<!routed )\bowners?\b/ alone, for the
claimant or anything else; /(?<!lease )\bholders?\b/ ("holder" without the lease).

### assign
**Definition:** the act by which a principal restricts a task's eligibility to one named principal by
writing `assigned_to`, a field write like any other, creating no lease.
An assignment is the resulting state; it is not delivery, and the named principal still claims. Pull is the
only delivery.
**Related:** [claim](#claim), [claimant](#claimant), [claimable](#claimable), [step owner](#step-owner);
[`work_model.md#assignment-restricts-eligibility-it-never-creates-a-lease`](work_model.md#assignment-restricts-eligibility-it-never-creates-a-lease).
**Never:** —
**Not for:** delivery for an assignment; `setAssignee` (Camunda's, which installs a holder without a
claim).

### lease
**Definition:** a relationship between a principal and a task, or between a step owner and a step on a
batch, carrying `claimed_at` and `expires_at`, that lapses without cooperation from its holder.
The claim and the lease are one primitive; renewal is the heartbeat; the task carries no lease fields.
**Related:** [claim](#claim), [claimant](#claimant), [held](#held), [lapsed](#lapsed), [returned](#returned),
[active](#active), [step state](#step-state);
[`work_model.md#the-lease-is-a-relationship-not-a-set-of-task-fields`](work_model.md#the-lease-is-a-relationship-not-a-set-of-task-fields).
**Never:** —
**Not for:** "claim fields" for the lease (the task carries none); "lock" for a lease (a lock outlives its holder); "heartbeat" for the lease (the heartbeat
renews the lease; it is not the lease).

### held
**Definition:** the lease state, derived at read time, in which `expires_at` is in the future.
**Related:** [lease](#lease), [lapsed](#lapsed), [returned](#returned), [claimable](#claimable);
[`work_model.md#the-transition-vocabulary`](work_model.md#the-transition-vocabulary).
**Never:** —
**Not for:** "locked" for a held lease; /\bstatus\s+(?:of\s+|=\s*|is\s+)?`?claimed`?/ ("claimed" as a stored task status).

### lapsed
**Definition:** the lease state, derived at read time, in which `expires_at` has passed and the claimant
has not returned the lease.
A lapsed lease does not count for claimability, so the task is claimable again without any process acting
on the lease.
**Related:** [lease](#lease), [held](#held), [returned](#returned), [watchdog](#watchdog);
[`work_model.md#a-lapsed-lease-is-not-reaped-repeated-lapse-raises-a-checkpoint`](work_model.md#a-lapsed-lease-is-not-reaped-repeated-lapse-raises-a-checkpoint).
**Never:** "stuck", "stranded", "expired and released".
**Not for:** —

### returned
**Definition:** the lease state in which the claimant ended the lease explicitly, on completion or on
failure.
**Related:** [lease](#lease), [held](#held), [lapsed](#lapsed), [claimant](#claimant);
[`work_model.md#the-transition-vocabulary`](work_model.md#the-transition-vocabulary).
**Never:** /\blease\w*\s+(?:is|are|was|were|be|been|being|gets?|got)\s+released\b/,
/\breleas(?:e|es|ed|ing)\s+(?:a|an|the|its|their|one|any|each|every|expired|lapsed)\s+(?:\w+\s+)?leases?\b/
(collides with the `release` step and the software release), "surrendered" (expiry is not volitional and
gets no such word).
**Not for:** /\breleas\w*\b[^.;:]{0,30}\bleases?\b/ and /\bleases?\b[^.;:]{0,30}\breleas\w*/ ("release"
for a lease).

### active
**Definition:** the derived read that a held lease has activity entities, such as an `agent_session` or
observations, related to the task within the lease window.
Never stored; a dashboard derives live-versus-quiet from it.
**Related:** [lease](#lease), [held](#held), [agent_session](#agent_session), [observation](#observation);
[`work_model.md#liveness-is-derived-from-activity-at-read-time-never-declared`](work_model.md#liveness-is-derived-from-activity-at-read-time-never-declared).
**Never:** "running", "in flight".
**Not for:** active as a status value.

### created
**Definition:** the task transition in which the task comes to exist in the record, publication being
creation.
The task's own transition vocabulary is `created` plus its status; lease transitions belong to the lease.
**Related:** [task](#task), [held](#held), [lapsed](#lapsed), [returned](#returned), [active](#active);
[`work_model.md#the-transition-vocabulary`](work_model.md#the-transition-vocabulary).
**Never:** —
**Not for:** published as a separate state; routed, claimed, or released as task transitions.

### claimable
**Definition:** the derived property of a task whose status is neither terminal nor `blocked`, whose
`assigned_to` is unset or names the would-be claimant, and on which no lease is held.
**Related:** [claim](#claim), [held](#held), [lapsed](#lapsed), [assign](#assign), [terminal](#terminal);
[`work_model.md#what-a-claim-predicate-treats-as-claimable`](work_model.md#what-a-claim-predicate-treats-as-claimable).
**Never:** —
**Not for:** "available" for claimable; /\bopen\s+(?:tasks?|pool)\b/ and /\btasks?\s+(?:is|are)\s+open\b/
("open" for claimable; `open` is a status value).

### terminal
**Definition:** a status value after which a task, a batch, or a checkpoint changes no further.
**Related:** [claimable](#claimable), [parent task](#parent-task), [approval](#approval);
[`work_model.md#what-a-claim-predicate-treats-as-claimable`](work_model.md#what-a-claim-predicate-treats-as-claimable).
**Never:** —
**Not for:** "final" for terminal.

### runner
**Definition:** the process that runs an agent and holds a lease on the agent's behalf, identified by a
runner id the persisted lease names.
**Related:** [agent](#agent), [claimant](#claimant), [lease](#lease), [agent_session](#agent_session);
[`work_model.md#the-claim-and-the-lease-are-one-primitive`](work_model.md#the-claim-and-the-lease-are-one-primitive).
**Never:** "worker", "bot".
**Not for:** agent when the process is meant.

### agent_session
**Definition:** the identity half of a runner's work that observations lack, such as host, checkout,
branch, and head, related to the task it executes.
**Related:** [runner](#runner), [active](#active), [observation](#observation);
[`work_model.md#no-assignment-log-history-is-the-tasks-own-observations`](work_model.md#no-assignment-log-history-is-the-tasks-own-observations).
**Never:** "run history", "dispatch record".
**Not for:** —

### observation
**Definition:** one append-only, timestamped, provenance-bearing write to an entity in the record, from
which the entity's history is read.
**Related:** [task](#task), [active](#active), [agent_session](#agent_session), [read-back](#read-back);
[`work_model.md#no-assignment-log-history-is-the-tasks-own-observations`](work_model.md#no-assignment-log-history-is-the-tasks-own-observations),
[`data_model.md#record-conventions`](data_model.md#record-conventions).
**Never:** "log line".
**Not for:** event for a stored change.

### hot path
**Definition:** a read path on which a decision must be taken from one entity read, for which a projection
such as `step_status` exists.
**Related:** [step_status](#step_status), [sign-off](#sign-off);
[`gates_and_workflows.md#declaration-batch-projection`](gates_and_workflows.md#declaration-batch-projection).
**Never:** —
**Not for:** fast path for a hot path (a fast path is a declared skip of steps); cache for a
projection.

### watchdog
**Definition:** the observer that counts lapses on a task and raises a checkpoint when the count reaches
its cap, holding no authority over any lease.
**Related:** [lapsed](#lapsed), [checkpoint](#checkpoint), [escalate](#escalate);
[`failure_posture.md#repeated-lapse-raises-a-checkpoint`](failure_posture.md#repeated-lapse-raises-a-checkpoint).
**Never:** "reaper", "retry loop".
**Not for:** "router" for the watchdog.

### batch
**Definition:** one or more tasks going through a workflow together, and the record of that.
A single task is a batch of one; only batches go through workflows, so there is no separate single-task
path. Tasks are attached to and detached from a batch; batches chain along `FOLLOWS`.
Reads: "the tasks entered the feature workflow", "the batch is at `qa`", "the batch advances to `impl`",
"a task attached to the batch", "a task detached from the batch", "the batch records who signed off
`qa`", "the tasks leave the workflow when `merge` is signed off".
**Related:** [task](#task), [workflow](#workflow), [artifact](#artifact), [step state](#step-state),
[successor](#successor), [chain](#chain), [attach / detach](#verbs);
[`work_model.md#what-goes-through-a-workflow-is-a-batch-of-tasks`](work_model.md#what-goes-through-a-workflow-is-a-batch-of-tasks),
[`data_model.md#concepts`](data_model.md#concepts).
**Never:** "passage", "workflow_run", "aggregation", /\b(?:a|an|the|one|this|that|each|every)\s+splits?\b/,
/\bsplit-?outs?\b/ (aggregation and split are retired as nouns; the verbs are attach and detach).
**Not for:** run for a batch; instance for a batch, unqualified; bundle for a batch.

### parent task
**Definition:** a task that groups child tasks through `PART_OF` edges from each child, whose completion is
derived from its children's terminal states and which never enters a workflow.
**Related:** [child task](#child-task), [task](#task), [terminal](#terminal), [batch](#batch);
[`work_model.md#parent-and-child-tasks`](work_model.md#parent-and-child-tasks).
**Never:** "epic", "umbrella".
**Not for:** a stored parent status.

### child task
**Definition:** a task with one `PART_OF` edge to a parent task, which goes through workflows independently
of its siblings.
**Allowed:** "subtask" in prose.
**Related:** [parent task](#parent-task), [task](#task), [batch](#batch);
[`work_model.md#parent-and-child-tasks`](work_model.md#parent-and-child-tasks).
**Never:** "story".
**Not for:** a child with two parents.

### operator-facing agent
**Definition:** the agent, defined by the `ateles` `agent_definition`, that claims operator-only tasks,
carries them and their checkpoints to the operator, and records the outcome.
**Related:** [operator_only](#operator_only), [claim](#claim), [checkpoint](#checkpoint);
[`work_model.md#operator-only-tasks-are-claimed-by-the-operator-facing-agent`](work_model.md#operator-only-tasks-are-claimed-by-the-operator-facing-agent).
**Never:** —
**Not for:** the operator for the agent; concierge for the agent, unqualified.

### daemon
**Definition:** a long-lived process that self-triggers on its own loop, producing tasks or actions
without receiving a task.
**Related:** [runner](#runner), [action](#action), [action gate](#action-gate);
[`work_model.md#the-three-execution-mechanisms`](work_model.md#the-three-execution-mechanisms).
**Never:** —
**Not for:** service for a daemon, unqualified.

### pipeline
**Definition:** the GitHub-hosted execution mechanism that opens each step of a workflow for a batch as
claimable step work, which the step owner claims, and never writes a task status.
It delivers nothing; it is the same pull, over steps.
**Related:** [step owner](#step-owner), [claim](#claim), [step state](#step-state), [steward](#steward),
[review panel](#review-panel);
[`work_model.md#the-three-execution-mechanisms`](work_model.md#the-three-execution-mechanisms).
**Never:** —
**Not for:** workflow for the pipeline (the declaration); CI for the pipeline (one of its checks).

## Gate model (`gates_and_workflows.md`)

### workflow
**Definition:** the declaration, per (project, workflow type), of an ordered list of steps, the fast paths
a batch may take, and the successors a closing sign-off may name.
**Related:** [step](#step), [stage](#stage), [batch](#batch), [step owner](#step-owner),
[workflow policy](#workflow-policy), [fast path](#fast-path), [successor](#successor);
[`workflows.md`](workflows.md) (the core workflows);
[`gates_and_workflows.md#declaration-batch-projection`](gates_and_workflows.md#declaration-batch-projection).
**Never:** "workflow_definition".
**Not for:** pipeline for a workflow (one engine that runs workflows); "template" for a workflow.

### step
**Definition:** one declared position in a workflow's ordered list, carrying a name, a step owner, a
`required` flag, an `on_fail` target, and parallel-group and join fields, claimed by its step owner on a
batch and closed by that owner's sign-off.
Step names are data (`pm`, `ux`, `arch`, `impl`, `pr_review`, `qa`, `legal`, `release`, and any a workflow
declares).
**Related:** [workflow](#workflow), [step owner](#step-owner), [sign-off](#sign-off), [step state](#step-state),
[stage](#stage);
[`gates_and_workflows.md#declaration-batch-projection`](gates_and_workflows.md#declaration-batch-projection).
**Never:** "gate owner".
**Not for:** /\b(?:pm|ux|arch|impl|pr_review|qa|legal|merge|review|release)\s+(?:gate|phase|check)\b/ and
/\bgates?\s+(?:owner|name|set|sequence|list)s?\b/ ("gate", "phase", "check" for a step; `gate` is the action
gate); /\bcheckpoint\s+step\b/ and /\bstep\s+(?:named\s+)?`?checkpoint`?/ ("checkpoint" for a step).

### stage
**Definition:** a named group of contiguous steps in a workflow, such as the review stage or the release
stage.
**Related:** [step](#step), [workflow](#workflow);
[`gates_and_workflows.md#declaration-batch-projection`](gates_and_workflows.md#declaration-batch-projection).
**Never:** —
**Not for:** stage for a single step; phase when a group of steps is meant.

### step owner
**Definition:** the **role** declared on a step, which the roster resolves to a principal at claim time;
that principal claims the step on a batch and its sign-off closes it. The declaration names a role so that
one workflow serves every project and a renamed or replaced agent leaves no stale name in it; the
resolution to a principal happens when the step is claimed, against `swarm_roster` for the batch's
project, and a step whose role resolves to no principal raises a checkpoint (reason
`unspawnable_assignee`) rather than falling through to any available agent.
**Field:** `workflow.steps[].owner_role` (the design's name; the field is `owner_agent` in the built
declarations and holds a role there too — `status.md`).
**Related:** [step](#step), [sign-off](#sign-off), [agent](#agent), [claim](#claim),
[workflow policy](#workflow-policy);
[`gates_and_workflows.md#two-policies-workflow-policy-and-action-policy`](gates_and_workflows.md#two-policies-workflow-policy-and-action-policy).
**Never:** —
**Not for:** owner alone.

### sign-off
**Definition:** the record a step owner writes to close a step on a batch, carrying the verdict,
timestamps, the agent, artifact refs, and the pinned `agent_definition` version.
A terminal write that supplies every field the schema requires; a rejected write is an error, never
swallowed.
**Verdict values:** `signed` (the step's condition is met), a blocking verdict (it is not, and the step's
`on_fail` says which earlier step opens again), and `waived` (the operator principal closed an unsigned
required step, carrying the reason). `waived` is the only verdict a principal other than the step owner
may write, and only the operator principal may write it
([`gates_and_workflows.md#declaration-batch-projection`](gates_and_workflows.md#declaration-batch-projection)).
**Related:** [step owner](#step-owner), [step state](#step-state), [batch](#batch), [terminal](#terminal),
[read-back](#read-back), [artifact](#artifact), [adapter](#adapter), [signal](#signal);
[`gates_and_workflows.md#declaration-batch-projection`](gates_and_workflows.md#declaration-batch-projection),
[`adapters.md#no-external-event-advances-a-step-by-itself`](adapters.md#no-external-event-advances-a-step-by-itself).
**Never:** "participation_record", "step_run", "LGTM", "audit row".
**Not for:** approval for a sign-off (an approval is on a checkpoint); "green" without the record.

### step state
**Definition:** the state of one step within one batch, derived at read time from edges and never stored:
open (the batch and the step), claimed (a lease from the step owner to the step on that batch), or signed
(a sign-off).
**Related:** [step](#step), [batch](#batch), [lease](#lease), [sign-off](#sign-off), [step_status](#step_status);
[`gates_and_workflows.md#declaration-batch-projection`](gates_and_workflows.md#declaration-batch-projection).
**Never:** "gate status".
**Not for:** a stored per-step status row.

### step_status
**Definition:** the map on the task projecting each step's state on its batch for the hot path, derived
from the sign-offs and proved equal to them by a reconciler.
**Related:** [sign-off](#sign-off), [step state](#step-state), [hot path](#hot-path), [batch](#batch);
[`gates_and_workflows.md#declaration-batch-projection`](gates_and_workflows.md#declaration-batch-projection).
**Never:** "gate_status".
**Not for:** history for the projection; a second source of truth.

### fast path
**Definition:** a declared skip of steps that a workflow permits for a named class of tasks.
**Related:** [workflow](#workflow), [step](#step);
[`gates_and_workflows.md#declaration-batch-projection`](gates_and_workflows.md#declaration-batch-projection).
**Never:** "shortcut".
**Not for:** hot path for a fast path.

### successor
**Definition:** a workflow that a `workflow` declares in `successors` as one a batch of it may enter on
closing, of which the closing sign-off selects exactly one or none.
The closing sign-off is the sign-off on the workflow's last step, which is always a single step.
**Related:** [workflow](#workflow), [batch](#batch), [sign-off](#sign-off), [chain](#chain),
[intake](#intake);
[`gates_and_workflows.md#sequencing-is-data-successors-and-the-chain`](gates_and_workflows.md#sequencing-is-data-successors-and-the-chain).
**Never:** "downstream workflow", "handoff".
**Not for:** next stage for a successor (a stage is within a workflow); two successors at once (that is a
detach); a successor named by anything but the closing sign-off.

### chain
**Definition:** the derived, never stored, sequence of batches a task has gone through, read along
`FOLLOWS` edges from its live batch back to its intake batch.
**Related:** [batch](#batch), [successor](#successor), [intake](#intake), [task](#task);
[`gates_and_workflows.md#sequencing-is-data-successors-and-the-chain`](gates_and_workflows.md#sequencing-is-data-successors-and-the-chain).
**Never:** "super-workflow".
**Not for:** pipeline for the sequence; "program" for the chain; a stored list of batches on the task.

### issue
**Definition:** a GitHub issue, an artifact a batch produces or references, linked to the batch and its
tasks by edge.
**Related:** [artifact](#artifact), [batch](#batch), [task](#task);
[`work_model.md#artifacts-are-records-a-batch-leaves-never-its-subject`](work_model.md#artifacts-are-records-a-batch-leaves-never-its-subject).
**Never:** —
**Not for:** "ticket" for an issue; task for the issue; the subject of a step.

### gate
**Definition:** short for the action gate, and nothing else.
**Related:** [action gate](#action-gate), [step](#step);
[`gates_and_workflows.md#the-action-gate-is-pr-independent`](gates_and_workflows.md#the-action-gate-is-pr-independent).
**Never:** "gates green".
**Not for:** gate for a step or a stage.

### action gate
**Definition:** the decision, taken by a principal evaluating one action against the action policy,
whether that action is taken or checkpointed.
Inputs are the action's class, blast radius, confidence, and successful recurrences; no PR, issue, or
repository.
**Related:** [action](#action), [action_policy](#action_policy), [blast radius](#blast-radius),
[confidence](#confidence), [checkpoint](#checkpoint), [gate](#gate);
[`gates_and_workflows.md#the-action-gate-is-pr-independent`](gates_and_workflows.md#the-action-gate-is-pr-independent).
**Never:** "execution gate".
**Not for:** "merge gate" for the action gate (merge is one boundary among several).

### action_policy
**Definition:** the policy a principal evaluates the action gate against, listing the low- and high-blast
action classes, the confidence threshold, the recurrence count that graduates a series, the
always-checkpoint boundaries, and the permission scope.
**Related:** [action gate](#action-gate), [action_type](#action_type), [blast radius](#blast-radius),
[workflow policy](#workflow-policy);
[`gates_and_workflows.md#two-policies-workflow-policy-and-action-policy`](gates_and_workflows.md#two-policies-workflow-policy-and-action-policy).
**Never:** "execution_policy", "execution policy".
**Not for:** "config" or "settings" for the policy; workflow policy for the action policy.

### workflow policy
**Definition:** the rule set stating which principals may claim which steps of which workflows, composed
of the workflow's step owners and the `agent_grant`s in force.
**Related:** [workflow](#workflow), [step owner](#step-owner), [grant](#grant), [claim](#claim),
[action_policy](#action_policy);
[`gates_and_workflows.md#two-policies-workflow-policy-and-action-policy`](gates_and_workflows.md#two-policies-workflow-policy-and-action-policy).
**Never:** —
**Not for:** action policy for the workflow policy; permissions for the workflow policy.

### action
**Definition:** one intended effect outside the Ateles system, such as a send, a publish, a merge, a
payment, or a release, related to the task it serves.
Created when the effect becomes known, which may be mid-workflow; a task may produce many, most unknown at
creation; an internal operational write to Neotoma is not an action.
**Related:** [task](#task), [action_type](#action_type), [action gate](#action-gate),
[take (an action)](#take-an-action), [effect dedup](#effect-dedup), [artifact](#artifact) (the record the
effect leaves), [adapter](#adapter), [action confirmation](#action-confirmation);
[`gates_and_workflows.md#actions-are-entities-only-actions-are-taken`](gates_and_workflows.md#actions-are-entities-only-actions-are-taken),
[`adapters.md#outbound-steps-produce-actions-adapters-take-them`](adapters.md#outbound-steps-produce-actions-adapters-take-them).
**Never:** "side effect" (unrecorded).
**Not for:** task for the effect; operation for an action.

### take (an action)
**Definition:** to carry out an action's effect outside the system once the action gate permits it.
Actions are taken; tasks are executed.
**Related:** [action](#action), [action gate](#action-gate), [execute (a task)](#execute-a-task);
[`gates_and_workflows.md#actions-are-entities-only-actions-are-taken`](gates_and_workflows.md#actions-are-entities-only-actions-are-taken).
**Never:** /\bexecut\w*\b[^.;:]{0,40}\bactions?\b/, /\bactions?\b[^.;:]{0,40}\bexecut\w*/,
/\bauto-?execut\w*/.
**Not for:** "fire" or "perform" for an action.

### action_type
**Definition:** the class an action belongs to, on which blast radius keys, and which a task declares at
creation as the classes of action it expects to produce.
Values include `build`, `docs`, `publish`, `send_external_comms`, and `operator_only`; a declared but
unclassified value fails closed.
**Related:** [action](#action), [blast radius](#blast-radius), [operator_only](#operator_only),
[action_policy](#action_policy);
[`gates_and_workflows.md#confidence-and-three-blast-tiers`](gates_and_workflows.md#confidence-and-three-blast-tiers).
**Never:** —
**Not for:** "category" or "kind" for the class; inferring it from the handling agent.

### blast radius
**Definition:** the tier an action_type resolves to under an action_policy, one of `LOW`, `HIGH`, or
`NEVER`.
`LOW` is taken at or above the confidence threshold or once a recurring series graduates; `HIGH` is
checkpointed until a recurring series graduates; `NEVER` is cleared by no confidence and no recurrence.
**Related:** [action_type](#action_type), [action_policy](#action_policy), [confidence](#confidence),
[recurring series](#recurring-series), [operator_only](#operator_only);
[`gates_and_workflows.md#confidence-and-three-blast-tiers`](gates_and_workflows.md#confidence-and-three-blast-tiers).
**Never:** "risk level" (unbounded).
**Not for:** "severity" for a tier.

### confidence
**Definition:** the proposing agent's score that an action is right, compared with the policy's threshold.
**Related:** [action](#action), [action gate](#action-gate), [blast radius](#blast-radius);
[`gates_and_workflows.md#confidence-and-three-blast-tiers`](gates_and_workflows.md#confidence-and-three-blast-tiers).
**Never:** —
**Not for:** a default of zero standing in for a score.

### recurring series
**Definition:** a series of one action class taken successfully that, on reaching the policy's count,
graduates that class from checkpointing to being taken without one.
**Related:** [blast radius](#blast-radius), [action_policy](#action_policy), [action_type](#action_type);
[`gates_and_workflows.md#confidence-and-three-blast-tiers`](gates_and_workflows.md#confidence-and-three-blast-tiers).
**Never:** "streak".
**Not for:** history for a series, unqualified.

### operator_only
**Definition:** the action_type marking an effect an agent structurally cannot carry out, which resolves to
`NEVER` ahead of any policy.
The task that carries it is still claimable, by the operator-facing agent.
**Related:** [action_type](#action_type), [blast radius](#blast-radius),
[operator-facing agent](#operator-facing-agent);
[`gates_and_workflows.md#confidence-and-three-blast-tiers`](gates_and_workflows.md#confidence-and-three-blast-tiers).
**Never:** "unclaimable".
**Not for:** "high blast" for `operator_only` (a louder `HIGH` delays the wrong outcome rather than
preventing it).

### checkpoint
**Definition:** the held state of a subject, an action held at the action gate or a task the swarm cannot
advance, awaiting a principal's decision.
Recorded as an entity linked to its subject, carrying a reason class, the needed input, the options, whom
it awaits, and who resolved it, and ending in a terminal approval. To checkpoint a subject is to write one
and hold. The reason classes are `gate_hold`, `repeated_lapse`, `unreadable_workflow`, `rounds_exhausted`,
`unspawnable_assignee`, and any a policy declares. "Brief" described its content, not its identity, and is
retired from the name for the same reason as `_record` and `_definition`.
**Related:** [action gate](#action-gate), [action](#action), [task](#task), [escalate](#escalate),
[approval](#approval), [principal](#principal), [operator-facing agent](#operator-facing-agent);
[`gates_and_workflows.md#the-checkpoint`](gates_and_workflows.md#the-checkpoint),
[`failure_posture.md#what-a-checkpoint-does-not-absorb`](failure_posture.md#what-a-checkpoint-does-not-absorb),
[`data_model.md#concepts`](data_model.md#concepts).
**Never:** "checkpoint_brief", "approval request".
**Not for:** /\bcheckpoint\s+step\b/ ("checkpoint" for a step); checkpoint for the halt (the halt is not a
checkpoint: nothing can be written).

### steward
**Definition:** the pipeline role that merges a pull request once every required step is signed off and
the action gate permits the merge action.
**Related:** [pipeline](#pipeline), [sign-off](#sign-off), [action](#action), [action gate](#action-gate);
[`gates_and_workflows.md#the-action-gate-is-pr-independent`](gates_and_workflows.md#the-action-gate-is-pr-independent).
**Never:** "merger".
**Not for:** "bot" for the steward.

### review panel
**Definition:** the set of lenses the pipeline runs on a pull request, each by its step owner.
**Related:** [lens](#lens), [step owner](#step-owner), [pipeline](#pipeline);
[`conformance.md#always-read`](conformance.md#always-read).
**Never:** —
**Not for:** "reviewers" for the panel, unqualified; CI for the panel.

### effect dedup
**Definition:** the rule that every outbound effect is idempotent or deduplicated on its own key, so a
re-claimed task never repeats an effect that already happened.
**Related:** [action](#action), [lapsed](#lapsed), [claim](#claim);
[`work_model.md#at-least-once-implies-effect-dedup`](work_model.md#at-least-once-implies-effect-dedup),
[`data_model.md#record-conventions`](data_model.md#record-conventions).
**Never:** "replay protection" (replay is refused outright).
**Not for:** "retry" for dedup.

## Core workflows (`workflows.md`)

### intake
**Definition:** the workflow every task enters first, whose steps classify, link, dedupe, prioritize, and
route the task, and whose closing sign-off names the successor workflow, or none, or operator-only.
A task with no intake batch is unrouted by that fact; no unrouted state is stored.
**Related:** [task](#task), [batch](#batch), [successor](#successor), [chain](#chain),
[operator-facing agent](#operator-facing-agent), [action_type](#action_type), [adapter](#adapter),
[signal](#signal);
[`workflows.md#intake`](workflows.md#intake),
[`work_model.md#intake-is-every-tasks-first-workflow`](work_model.md#intake-is-every-tasks-first-workflow),
[`adapters.md#no-external-event-advances-a-step-by-itself`](adapters.md#no-external-event-advances-a-step-by-itself).
**Never:** "undispatched".
**Not for:** "triage" for the whole workflow (its first stage); unrouted as a stored status; routing by
a router (the `route` step is a sign-off by a step owner).

## Adapters (`adapters.md`)

### adapter
**Definition:** the component that translates between one external system and the record in both
directions, inbound events into signals about artifacts and outbound actions into operations on that
system, and the only component that touches the system.
An adapter is a daemon in the work model's sense: it self-triggers on the external system and receives no
task; the engine reads only what the adapter wrote.
**Related:** [signal](#signal), [artifact](#artifact), [action](#action),
[action confirmation](#action-confirmation), [daemon](#daemon), [intake](#intake), [credential](#credential);
[`adapters.md#the-two-invariants`](adapters.md#the-two-invariants),
[`adapters.md#what-the-adapter-does-with-every-event`](adapters.md#what-the-adapter-does-with-every-event).
**Never:** "connector", "plugin".
**Not for:** the engine for the adapter (the engine reads the record; the adapter reads the system);
"gateway" for an adapter, unqualified.

### signal
**Definition:** what an inbound external event is to the record: information about an artifact, which an
adapter translates into a sign-off by a named principal, an observation on an artifact, an action
confirmation, or a new task for intake, and never into an instruction to a workflow.
**Related:** [adapter](#adapter), [artifact](#artifact), [observation](#observation), [sign-off](#sign-off),
[intake](#intake), [action confirmation](#action-confirmation);
[`adapters.md#no-external-event-advances-a-step-by-itself`](adapters.md#no-external-event-advances-a-step-by-itself).
**Never:** —
**Not for:** "trigger" for a signal (nothing outside the record opens a step); "command" for a signal.

### action confirmation
**Definition:** the observation an adapter writes on an action once its effect exists in the external
system, carrying `taken_at` and `result_ref`, read back from that system and never inferred from the
operation's return.
**Related:** [action](#action), [adapter](#adapter), [artifact](#artifact), [read-back](#read-back),
[effect dedup](#effect-dedup);
[`adapters.md#outbound-steps-produce-actions-adapters-take-them`](adapters.md#outbound-steps-produce-actions-adapters-take-them).
**Never:** —
**Not for:** sign-off for a confirmation (a confirmation closes no step); a success response for a
confirmation.

## Authority model (`authority_model.md`)

### authority
**Definition:** the right to take an action, expressed as `principal + domain + scope + action +
conditions + time`.
**Related:** [principal](#principal), [grant](#grant), [action](#action), [delegation](#delegation);
[`authority_model.md#the-tuple`](authority_model.md#the-tuple).
**Never:** —
**Not for:** "permission" alone for authority (a scope term); "access" for authority.

### principal
**Definition:** an actor, human or agent, that authority is attributed to.
**Related:** [operator](#operator), [agent](#agent), [credential](#credential), [tenant](#tenant);
[`authority_model.md#principals`](authority_model.md#principals).
**Never:** —
**Not for:** owner for a principal unless ownership is meant; identity for a principal (the
credential, not the actor); user for a principal (the store's authenticated credential).

### credential
**Definition:** a binding from a login, key, address, or chat id to a principal, many-to-one, and never the
principal itself.
**Related:** [principal](#principal), [grant](#grant), [agent](#agent);
[`authority_model.md#principals`](authority_model.md#principals).
**Never:** —
**Not for:** identity for the principal; account for a credential.

### operator
**Definition:** a human principal who directs agents.
**Related:** [principal](#principal), [approval](#approval), [operator-facing agent](#operator-facing-agent);
[`authority_model.md#principals`](authority_model.md#principals).
**Never:** "admin".
**Not for:** user when authority is meant.

### agent
**Definition:** a non-human principal defined by an `agent_definition` and acting as a bound principal.
**Related:** [principal](#principal), [runner](#runner), [grant](#grant), [step owner](#step-owner);
[`authority_model.md#principals`](authority_model.md#principals).
**Never:** —
**Not for:** worker for an agent (the process running an agent is a runner).

### tenant
**Definition:** the isolation boundary, an organization or a solo operator, that no read, write, routing,
or key crosses.
**Related:** [principal](#principal), [grant](#grant);
[`authority_model.md#principals`](authority_model.md#principals).
**Never:** —
**Not for:** account for a tenant; "workspace" alone for a tenant.

### grant
**Definition:** an `agent_grant` holding the domain and scope a principal may act in, matched on its
credential, as operation × entity types × repositories with parameter constraints and an expiry.
Zero grants is deny.
**Related:** [principal](#principal), [credential](#credential), [authority](#authority),
[workflow policy](#workflow-policy), [enforcement point](#enforcement-point);
[`authority_model.md#grants`](authority_model.md#grants).
**Never:** —
**Not for:** permissions for a grant (a capability is one row of a grant); "allowlist" for a grant (one
enforcement of it).

### decision point
**Definition:** the function, the action gate or the grant checker, that returns `Permit`, `Deny`, or
`Indeterminate` for one request.
**Related:** [enforcement point](#enforcement-point), [action gate](#action-gate), [grant](#grant),
[unknown](#unknown);
[`authority_model.md#the-tuple`](authority_model.md#the-tuple).
**Never:** "policy engine".
**Not for:** checker for a decision point, unqualified.

### enforcement point
**Definition:** a call site that acts on a decision point's answer and treats `Indeterminate` as `Deny`.
**Related:** [decision point](#decision-point), [unknown](#unknown), [halt](#halt);
[`authority_model.md#the-tuple`](authority_model.md#the-tuple).
**Never:** "advisory check", "passthrough".
**Not for:** —

### ownership
**Definition:** named accountability for a workflow, domain, queue, or configuration entity, carried as an
`ownership_grant` edge to a principal.
**Related:** [principal](#principal), [workflow](#workflow), [step owner](#step-owner);
[`authority_model.md#principals`](authority_model.md#principals).
**Never:** —
**Not for:** owner alone for the accountable principal.

### delegation
**Definition:** a scoped, time-bounded transfer of action rights, recorded as a `delegation_edge` from
delegator to delegate, in which each hop holds a subset of the delegator's authority.
Delegation is A acting for B and recorded as such; impersonation is A indistinguishable from B (RFC 8693).
**Related:** [authority](#authority), [principal](#principal), [authority_chain](#authority_chain),
[grant](#grant);
[`authority_model.md#delegation`](authority_model.md#delegation).
**Never:** —
**Not for:** "impersonation" for delegation; "handover" for delegation without scope.

### authority_chain
**Definition:** the derived, never stored, read model over delegation edges, grants, and checkpoints that
gives the path from a principal through each delegation hop to the approver for one action.
**Related:** [delegation](#delegation), [grant](#grant), [checkpoint](#checkpoint),
[approval](#approval);
[`authority_model.md#delegation`](authority_model.md#delegation).
**Never:** —
**Not for:** "audit log" alone for the chain.

### approval
**Definition:** an explicit yes, no, or veto by a required principal on a checkpoint, ending in a terminal
state.
A timeout is a terminal state that never continues.
**Related:** [checkpoint](#checkpoint), [principal](#principal), [terminal](#terminal),
[quorum](#quorum), [separation of duties](#separation-of-duties);
[`authority_model.md#approval`](authority_model.md#approval).
**Never:** "silent continuation".
**Not for:** resolved without who; sign-off for an approval (that closes a step).

### quorum
**Definition:** a structural check requiring m-of-n named principals on one checkpoint.
**Related:** [approval](#approval), [separation of duties](#separation-of-duties), [principal](#principal);
[`authority_model.md#structural-checks-quorum-and-separation-of-duties`](authority_model.md#structural-checks-quorum-and-separation-of-duties).
**Never:** —
**Not for:** "required reviewers" for a quorum (1-of-n is not a quorum); sign-off for a quorum.

### separation of duties
**Definition:** a structural check requiring disjointness between the roles on one checkpoint, such as
raiser and resolver or proposer and approver.
**Related:** [approval](#approval), [quorum](#quorum), [checkpoint](#checkpoint);
[`authority_model.md#structural-checks-quorum-and-separation-of-duties`](authority_model.md#structural-checks-quorum-and-separation-of-duties).
**Never:** —
**Not for:** "four eyes" for the check, unqualified; sign-off for the check.

### initiative
**Definition:** a proposed change to what the organization pursues.
**Related:** [proposal](#proposal), [reprioritization](#reprioritization), [principal](#principal);
[`authority_model.md#initiative-proposal-reprioritization`](authority_model.md#initiative-proposal-reprioritization).
**Never:** —
**Not for:** project or "epic" for an initiative.

### proposal
**Definition:** the ask that an initiative be accepted, made under proposal rights that are distinct from
execution rights.
**Related:** [initiative](#initiative), [approval](#approval), [authority](#authority);
[`authority_model.md#initiative-proposal-reprioritization`](authority_model.md#initiative-proposal-reprioritization).
**Never:** —
**Not for:** PR or RFC alone for a proposal.

### reprioritization
**Definition:** the explicit "what stops?" recorded when an initiative is accepted, confirmed by a
principal.
**Related:** [initiative](#initiative), [proposal](#proposal), [principal](#principal);
[`authority_model.md#initiative-proposal-reprioritization`](authority_model.md#initiative-proposal-reprioritization).
**Never:** "priority bump", "re-plan".
**Not for:** —

## Failure posture (`failure_posture.md`)

### halt
**Definition:** the state in which the swarm does no work because its record is unreachable, while it keeps
observing and announces itself off-Neotoma.
Not a checkpoint: a checkpoint is written to the record, and the halt is the state in which nothing can be.
**Related:** [reachability probe](#reachability-probe), [unknown](#unknown), [checkpoint](#checkpoint);
[`failure_posture.md#the-decision`](failure_posture.md#the-decision),
[`failure_posture.md#what-a-checkpoint-does-not-absorb`](failure_posture.md#what-a-checkpoint-does-not-absorb).
**Never:** "degraded mode", "fallback mode", "offline mode".
**Not for:** —

### reachability probe
**Definition:** one real read, at the moment a task is claimed, of what the work will read.
**Related:** [halt](#halt), [claim](#claim), [unknown](#unknown);
[`failure_posture.md#the-rules`](failure_posture.md#the-rules).
**Never:** "ping".
**Not for:** "health check" for the probe (`/health` can be green while every read hangs).

### read-back
**Definition:** the retrieval, after any write that carries a decision, that asserts the field holds the
value written.
**Related:** [observation](#observation), [claim](#claim), [sign-off](#sign-off);
[`principles.md`](principles.md#2-a-write-that-reports-success-has-not-necessarily-happened-read-it-back).
**Never:** —
**Not for:** treating a 2xx or `success: true` as evidence.

### unknown
**Definition:** the third state of any gate, grant, drift, or reachability reader, meaning the value could
not be determined.
Never coerced to pending or to clear; at an enforcement point it resolves to deny.
**Related:** [enforcement point](#enforcement-point), [decision point](#decision-point), [halt](#halt);
[`failure_posture.md#the-rules`](failure_posture.md#the-rules).
**Never:** "legacy fail-open" (no such category exists).
**Not for:** pending or clear for a failed read.

### escalate
**Definition:** to raise a checkpoint on a task the swarm cannot advance, with the reason class that says
why.
The watchdog escalates on repeated lapse; the engine escalates on an unreadable workflow; a bounded loop
escalates when its rounds are exhausted; a claim predicate escalates on an `assigned_to` nobody can run.
One decision queue, one resolution protocol: a checkpoint on a task is resolved as a checkpoint on an
action is (principle 6, do not build a second gate).
**Related:** [checkpoint](#checkpoint), [watchdog](#watchdog), [lapsed](#lapsed), [halt](#halt),
[operator](#operator);
[`failure_posture.md#repeated-lapse-raises-a-checkpoint`](failure_posture.md#repeated-lapse-raises-a-checkpoint),
[`failure_posture.md#what-a-checkpoint-does-not-absorb`](failure_posture.md#what-a-checkpoint-does-not-absorb).
**Never:** /`escalations?`/, /\bescalation\s+(?:entity|entities|record|schema|object)s?\b/,
/\b(?:an|one|raises?|raised|raising|writes?|written|wrote)\s+(?:aggregated\s+)?escalations?\b/ (the
entity is the checkpoint).
**Not for:** page for a checkpoint (one delivery of it); alert for a checkpoint.

## Data model (`data_model.md`)

### edge
**Definition:** a typed, directed relationship between two entities in the record, carrying its own
timestamps and fields.
**Related:** [lease](#lease), [batch](#batch), [derived read](#derived-read);
[`data_model.md#relationships`](data_model.md#relationships).
**Never:** —
**Not for:** link for an edge in schema text (a link is a URL); field for what an edge carries.

### derived read
**Definition:** a value computed from entities and edges at read time and never stored, such as `active`,
a step state, the chain, or a parent's completion.
**Related:** [edge](#edge), [projection](#projection), [active](#active), [step state](#step-state),
[chain](#chain);
[`data_model.md#concepts`](data_model.md#concepts).
**Never:** —
**Not for:** cached for a derived read; flag for a derived read.

### projection
**Definition:** a stored copy of a derived read, kept for a hot path and proved equal to its source by a
reconciler, such as `step_status`.
**Related:** [derived read](#derived-read), [step_status](#step_status), [hot path](#hot-path);
[`data_model.md#concepts`](data_model.md#concepts).
**Never:** —
**Not for:** source of truth for a projection; history for a projection.

## Conformance (`conformance.md`)

### kernel document
**Definition:** a foundation document read on every review.
**Related:** [keyed document](#keyed-document), [lens](#lens), [design basis](#design-basis);
[`conformance.md#always-read`](conformance.md#always-read).
**Never:** "core docs", "the P1 docs".
**Not for:** —

### keyed document
**Definition:** a foundation document read when a changed path matches its key.
Each header says which kind it is.
**Related:** [kernel document](#kernel-document), [lens](#lens);
[`conformance.md#read-when-these-paths-changed`](conformance.md#read-when-these-paths-changed).
**Never:** "optional docs", "secondary docs".
**Not for:** —

### lens
**Definition:** one reviewing perspective on the review panel (pm, ux, arch, qa, and the rest), run by its
step owner.
**Related:** [review panel](#review-panel), [step owner](#step-owner), [kernel document](#kernel-document);
[`conformance.md#always-read`](conformance.md#always-read).
**Never:** —
**Not for:** "reviewer" for a lens, unqualified.

### design basis
**Definition:** the foundation document and section an issue or PR conforms to, or the statement `no
design applies` with a reason, checked mechanically and judged by reading.
**Related:** [kernel document](#kernel-document), [keyed document](#keyed-document), [lens](#lens);
[`conformance.md#design-basis`](conformance.md#design-basis).
**Never:** —
**Not for:** reference or "see also" for a design basis.

### status
**Definition:** the dated measurement of the gap between the foundation and a checkout, held in
`status.md` and regenerated rather than maintained.
**Allowed:** naming `status.md` as the state home (for example, "what is built is `status.md`").
**Related:** [design basis](#design-basis), [kernel document](#kernel-document);
[`conformance.md#phases-and-implementation-state`](conformance.md#phases-and-implementation-state).
**Never:** —
**Not for:** embedding dated figures, counts, or checkout claims from it into a foundation document;
treating it as design evidence.

## Verbs

Each subject has its verb. The subject of a movement verb is a task or a batch, never the batch record:
"the batch records who signed off `qa`" is a fact about the record; "the batch advances" is the batch
of tasks moving. The pairs are canonical; the phrases in the last column are replaced by them wherever
they appear in a document, a schema, a prompt, or an error message.

| Subject | Verb | Not |
|---|---|---|
| tasks, or a batch, with respect to a workflow | **enter** it (which opens a batch record if none exists), **go through** it, and **leave** it when its last step is signed off | "run through", "flow through", "are carried through" |
| a batch, from step to step | **advances** | "moves", "progresses", "transitions" |
| a task, with respect to a batch | is **attached** to it, is **detached** from it; to **split** a task is to detach it and open a new batch for it | "aggregated into", "bundled", "forked", "re-run" |
| a step, within a batch | **opens**; **closes** by sign-off | "fires", "clears", "is satisfied", "goes green" |
| a lease | is **claimed**, **renewed**, **returned**; it **lapses** on its own | "acquired", "freed", "expired and released" |
| a task | is **executed** (plain: done, worked on) | "run", "processed" |
| an action | is **taken** | "fired", "run", "performed" |
| a subject that must wait | is **checkpointed**; a task the swarm cannot advance is **escalated** | "paused", "parked", "paged" |
| a batch, on closing | its tasks **enter** one successor, or the batch closes with none | "flows into", "triggers" the next workflow |

## Owner: five meanings, one word forbidden alone

`owner` on its own is forbidden. Sources use it for five things (C10); each has its own term:

| Meaning | Term | Field |
|---|---|---|
| the role the roster resolves to the principal whose sign-off closes a step | **step owner** | `workflow.steps[].owner_role` |
| the step a batch is at | **current step** | derived from the batch's step states; projected as `current_owner` |
| the agent a finding is routed to | **routed agent** | `proposed_skill_update.owning_agent` |
| the operator with the book of business for a customer | **book-of-business owner** | `multi_tenant.md` section 5 |
| named accountability for a workflow, domain, or queue | **ownership** (above) | `ownership_grant` |

The principal holding a task's lease is its [claimant](#claimant), never its owner; the principal an
assignment names is not yet its claimant.

## Retired names

Each name below is retired; the term that replaced it is the entry to read. A retired name appears in
foundation prose only on a line that says it is retired.

| Retired | Replaced by | Why |
|---|---|---|
| `passage` | [batch](#batch) | the thing going through a workflow is the tasks; a batch is one or more of them, and the record of their going through |
| `aggregation`, `split` (nouns) | attach, detach ([Verbs](#verbs)) | edges are written by verbs; the nouns named a field that never existed |
| `execution gate`, `execution_policy` | [action gate](#action-gate), [action_policy](#action_policy) | tasks are executed and actions are taken, so the gate on actions is the action gate |
| `checkpoint_brief` | [checkpoint](#checkpoint) | "brief" described the content, not the identity, like `_record` and `_definition` |
| `escalation` (entity) | [checkpoint](#checkpoint) with a reason class; verb [escalate](#escalate) | one decision queue, one resolution protocol (principle 6) |
| `workflow_definition`, `participation_record`, `workflow_run`, `step_run` | [workflow](#workflow), [sign-off](#sign-off), [batch](#batch), [step state](#step-state) | redundant qualifiers; `run` collided with the liveness vocabulary |
| `gate owner`, `gate_status` | [step owner](#step-owner), [step_status](#step_status) | `gate` names one decision |
| `work item`, `work entity` | [task](#task) (subject), [artifact](#artifact) (record) | the subject of a workflow is the task |
| `dispatch` | [assign](#assign), [claim](#claim), [intake](#intake) | it once named publication, claim, assignment, and execution at once |
| `reaper` | nothing | a lapsed lease already does not count; there is nothing to release |
| `executing`, `running` (as states) | [active](#active) (derived) | a stored liveness flag fails when the process that would clear it dies |
