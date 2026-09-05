# Vocabulary: canonical terms

**Keyed document:** read when a skill, an [agent](#agent) document, or the agent-doc renderer changes
(`conformance.md`). **Kind:** foundation; defines terms by what they are in the design, never by what a
checkout implements. **Derived from:** synthesis `ent_b0ce322f768e4fc676b73139` (PR-03, PR-08, C10), prior
art `ent_08460968e6f49dac21510f4a` (A2A `TaskState`, RFC 8693, Camunda), [task](#task)
`ent_da60df3beccb675ef8c8c0c5`, the ateles#378 glossary ([operator](#operator) section, and the ux-signed swarm section
cited as [proposal](#proposal)), `docs/multi_tenant.md` section 5, PR #745 operator review (2026-09-04),
and the operator memos of 2026-09-05 (the standing axis on a [finding](#finding)), and the operator's 2026-09-05 terminology review (revision 17: the one boundary and the term `external system`, the `action series` rename, `subject` defined, and the two-part `checkpoint`), and the operator's 2026-09-05 review of review relevance (revision 19: the `applies_when` condition on an optional [step](#step), and two terms retired in favour of `review step`), and PR #745 operator review (2026-09-05, rulings 13–14,
16–18, 23–29: the hold verb, a condition a step holds on, the `dependency_cycle` reason class, the consent
tolerance on `action_policy`, and an [artifact](#artifact) `PART_OF` its containing artifact), and the operator's 2026-09-05 22:02–22:13 memos on how tasks come into existence (revision 30, 2026-09-06: the `intake rule` entry). Format
follows Neotoma's `docs/vocabulary/canonical_terms.md`. Revised by the simplification pass of 2026-09-05 (revision 29: `claimant`, `workflow policy`, and `hot path` retired; the [checkpoint](#checkpoint) reason classes cited from their one home; a code-era field removed from the Owner table).

## Purpose

One list of the terms the swarm's documents, schemas, prompts, and error messages use, each with a
definition, the section that owns it, and the words it bans, grouped by the document that owns it. A
definition links the other terms it leans on inline, at the point it uses them, rather than listing them
at the end: the link belongs where the reader meets the word.

## Scope

Every definition is one sentence and names the concept; how the concept is recorded (entity type, fields,
[edges](#edge)) is `data_model.md`. A definition links the terms it depends on inline, where they are mentioned,
and names its owning section. Terms carry no phase marker: the roadmap is `status.md`, and a definition
does not change when its implementation lands.

**How the inline links are applied.** One rule, so that a later editor follows the same one and the links
stay predictable rather than accumulating by taste:

- **The first mention of a term in an entry is linked; later mentions in that entry are not.** A short
  entry that links the same word three times is noisier than one that links it once.
- **A term is never linked inside its own entry.** A self-link says nothing.
- **The word is linked as it appears**, in whatever form the sentence needs — [task](#task) or tasks,
  [claim](#claim) or claimed, [sign-off](#sign-off) or sign-offs.
- **A multi-word term is linked whole, never as its parts**: [step owner](#step-owner), not [step](#step)
  followed by a bare owner.
- **Ban lists are never linked.** A `**Never:**` or `**Not for:**` line names words the foundation
  forbids; linking one would present it as canonical. Neither are code spans, headings, table rows, or
  the `**See:**` citation lists, which are links already.
- **A term that is also an ordinary English word is left to the author.** Eighteen entries — among them
  [status](#status), [active](#active), [held](#held), [condition](#condition), record, subject, and the
  two verb entries
  [execute (a task)](#execute-a-task) and [take (an action)](#take-an-action) — are words this file binds
  to a particular sense while English uses them for other things. Linking their every first occurrence
  would mislabel the ordinary use, so the linker leaves them alone and the author links them where the
  bound sense is meant.

`execution/scripts/link_vocabulary_terms.py` applies this mechanically and `--check` reports any first
mention that has gone unlinked, so adding a term relinks the file rather than leaving the new word
dangling.

### Two things a reader is looking at, and how they are written

This file names concepts; the record names rows and fields. The two are written differently on purpose,
so that a reader always knows which one is in front of them.

**A vocabulary term is written in plain words, with spaces:** [step](#step) status, [sign off](#sign-off), [action gate](#action-gate), blast
radius, [step owner](#step-owner), [fast path](#fast-path). It is prose and it is set as prose. Two consequences worth stating, because
this file previously mixed all three forms. First, no underscores: the concept is "step status", never
`step_status`, whatever the field is called. Second, a hyphen only where the term is a compound noun that
reads wrongly without one — **sign-off** as a noun (the thing a step owner writes) keeps its hyphen,
because "a sign off" reads as a verb phrase and misparses on first reading; the verb is always **to sign
off**, two words. That is the one hyphen this file keeps for readability, and it is applied consistently:
every other multi-word term is spaced.

**An entity type or a field name keeps its record spelling and is set in code font:** `step_status`,
`action_type`, `agent_grant`, `sign_off`, `dedup_key`, `owner_role`. That is the record's name for a row
or a column, not the vocabulary's name for a concept, and it is quoted exactly as the record spells it —
underscores included — because a reader who sees it needs to be able to write it into a query. Where a
concept and its recorded form differ only in spelling, both appear: the concept is step status, the
[projection](#projection) on the [task](#task) is `step_status`, and the entry says so.

So: `checkpoint` and `action` are set in code font when the entity type is meant and written plainly when
the concept is, and a heading that names an entity type is written as that entity type.

Each entry ends with two lists, read by `execution/scripts/check_foundation_vocabulary.py`
(`conformance.md#mechanical-checks-on-this-directory`):

- **Never:** bare words and phrases banned in all foundation prose, in every sense. A hit fails the check.
  Each quoted item is matched as a whole word, case insensitive; a phrase matches across a space or a
  hyphen. A line that carries a Never or Not-for list, or the word "retired", is not scanned, and neither
  is a table row of this file (the Verbs, Owner, and Retired tables name banned words on purpose).
- **Not for:** words allowed in some senses and banned in the stated one. A hit is advisory: the check
  lists it, and the author judges the sense.

Both lists are prose, and they are read as prose. Some bans need more than a phrase — an inflection set, a
sense distinction, a span between two words — and those are held as regular expressions in the checker's
own `PATTERNS` table, keyed by the entry heading above them, so that no regex syntax appears in this
document. The entry states the ban in words; the checker holds the machinery for matching it. A test
asserts every key in that table still names an entry here, so renaming a term fails loudly rather than
quietly dropping its ban.

## Work model (`work_model.md`)

### task
**Definition:** the atomic unit of accountable work.
**See:** [`work_model.md#the-transition-vocabulary`](work_model.md#the-transition-vocabulary),
[`work_model.md#there-is-no-task-lifecycle-there-are-batches`](work_model.md#there-is-no-task-lifecycle-there-are-batches).
**Never:** "chip", "work item", "work entity".
**Not for:** "ticket" for a task (a GitHub issue is an `issue`, an artifact; a task may refer to one);
"lifecycle" for the task's states (its only states are its status and its edges; there is no task state
machine, C1).

### execute (a task)
**Definition:** to [claim](#claim) a [task](#task), do its work, and complete it.
Tasks are executed; [actions](#action) are taken. Plain synonyms in prose: do, work on.
**See:** [`gates_and_workflows.md#actions-are-entities-only-actions-are-taken`](gates_and_workflows.md#actions-are-entities-only-actions-are-taken).
**Never:** worked, in any form, of a task — a task is executed, and the only permitted use of the verb is
work on; executing as a state, status, or flag — executing is what an agent is doing, never a value the
record holds.
**Not for:** run for a task; process for a task.

### artifact
**Definition:** a record living in an [external system](#external-system), reached only through that system's
[adapter](#adapter) and always identified by its `system` and `external_id`, that a [batch](#batch)
produces or references — a GitHub [issue](#issue), a pull request, a release, a sent message — linked to
the batch and its [tasks](#task) by [edge](#edge) and never the subject of a [step](#step).
An [action](#action) is the intended effect; the artifact is the record the effect leaves.
**The word is bound; it is not a catch-all for outputs.** Anything the swarm produces that lives in the
record is an **entity**, not an artifact: a [sign-off](#sign-off), a [checkpoint](#checkpoint), an
analysis, a draft, a page rendered into the record. The test is where the thing lives and how it is
reached — an external system through an adapter, or a retrieval from the record — never how
output-shaped it feels.
**Where the external system gives ids to two levels of one thing** — a thread and its messages, a recurring
series and its occurrences — each level is an artifact and the contained one is `PART_OF` the containing one;
an event links to the artifact whose id it carries, an action refers to the unit its operation needs, and a
task refers to whichever unit it names
(`adapters.md#what-the-adapter-does-with-every-event`).
**An artifact comes into existence with its `external_id` already known**, minted by the adapter from
the [read-back](#read-back) that confirms the effect landed. There is no artifact with a null or pending id: what
holds a composed-but-unsent thing is an entity, and what spans the interval between an effect's
submission and its confirmation is the [action](#action) and its `dedup_key`.
**See:** [`work_model.md#artifacts-are-records-a-batch-leaves-never-its-subject`](work_model.md#artifacts-are-records-a-batch-leaves-never-its-subject),
[`adapters.md#the-two-invariants`](adapters.md#the-two-invariants),
[`adapters.md#an-artifact-exists-only-once-its-external-record-does-and-the-interval-before-that-belongs-to-the-action`](adapters.md#an-artifact-exists-only-once-its-external-record-does-and-the-interval-before-that-belongs-to-the-action),
[`data_model.md#concepts`](data_model.md#concepts).
**Never:** —
**Not for:** "deliverable" for the record; task for the artifact; artifact for anything the swarm wrote
into the record (that is an entity of its own type).

### claim
**Definition:** the act by which an [agent](#agent) takes the [lease](#lease) on a [task](#task), or on a [step](#step) of a [batch](#batch), itself,
atomic among concurrent claims and keyed on the task or the step.
**Use:** "Corvus claims a task that is eligible for it: `assigned_to` is unset or names Corvus, and no
lease is held. The claim, not the assignment, makes Corvus the lease holder."
**See:** [`work_model.md#the-claim-and-the-lease-are-one-primitive`](work_model.md#the-claim-and-the-lease-are-one-primitive).
**Never:** dispatch, pick up, hand off, push, and spawn, in any of their forms — a runner is started; work
is claimed.
**Not for:** assign for a claim (an eligibility constraint, which creates no lease).

### assign
**Definition:** the act by which a [principal](#principal) restricts a [task](#task)'s eligibility to one named principal by
writing `assigned_to`, a field write like any other, creating no [lease](#lease).
An assignment is the resulting state; it is not delivery, and the named principal still [claims](#claim). Pull is the
only delivery.
**See:** [`work_model.md#assignment-restricts-eligibility-it-never-creates-a-lease`](work_model.md#assignment-restricts-eligibility-it-never-creates-a-lease).
**Never:** "assignee" (say: the principal the assignment names, who has not necessarily claimed).
**Not for:** delivery for an assignment; `setAssignee` (Camunda's, which installs a holder without a
claim).

### lease
**Definition:** a relationship between a [principal](#principal) and a [task](#task), or between a [step owner](#step-owner) and a [step](#step) on a
[batch](#batch), carrying `claimed_at` and `expires_at`, that lapses without cooperation from its holder.
The [claim](#claim) and the lease are one primitive; renewal is the heartbeat; the task carries no lease fields.
Its **lease holder** is the principal the persisted lease names, [read back](#read-back) from the lease and never
from a task field; it is the only role the lease has, and it needs no term of its own.
**See:** [`work_model.md#the-lease-is-a-relationship-not-a-set-of-task-fields`](work_model.md#the-lease-is-a-relationship-not-a-set-of-task-fields).
**Never:** "claimant" (retired: say lease holder).
**Not for:** "claim fields" for the lease (the task carries none); "lock" for a lease (a lock outlives its holder); "heartbeat" for the lease (the heartbeat
renews the lease; it is not the lease); owner standing alone, for the lease holder or for anything else — the word carries five meanings
and always takes its qualifier (step owner, plan owner, grant owner, business owner, current owner, routed
owner); holder without the lease in front of it.

### held
**Definition:** the [lease](#lease) state, derived at read time, in which `expires_at` is in the future.
**See:** [`work_model.md#the-transition-vocabulary`](work_model.md#the-transition-vocabulary).
**Never:** —
**Not for:** "locked" for a held lease; claimed as a stored task status (the lease is what is claimed, and
it is read back, never stored on the task).

### lapsed
**Definition:** the [lease](#lease) state, derived at read time, in which `expires_at` has passed and the lease holder
has not [returned](#returned) the lease.
A lapsed lease does not count for claimability, so the [task](#task) is [claimable](#claimable) again without any process acting
on the lease.
**See:** [`work_model.md#a-lapsed-lease-is-not-reaped-repeated-lapse-raises-a-checkpoint`](work_model.md#a-lapsed-lease-is-not-reaped-repeated-lapse-raises-a-checkpoint).
**Never:** "stuck", "stranded", "expired and released".
**Not for:** —

### returned
**Definition:** the [lease](#lease) state in which the lease holder ended the lease explicitly, on completion or on
failure.
**See:** [`work_model.md#the-transition-vocabulary`](work_model.md#the-transition-vocabulary).
**Never:** released, of a lease, in either voice — a lease is returned, because release collides with the
`release` step and with a software release; "surrendered" (expiry is not volitional and gets no such
word).
**Not for:** release, in any form, anywhere near a lease — a lease is returned.

### active
**Definition:** the [derived read](#derived-read) that a held [lease](#lease) has activity entities, such as an `agent_session` or
[observations](#observation), related to the [task](#task) within the lease window.
Never stored; a dashboard derives live-versus-quiet from it.
**See:** [`work_model.md#liveness-is-derived-from-activity-at-read-time-never-declared`](work_model.md#liveness-is-derived-from-activity-at-read-time-never-declared).
**Never:** "running", "in flight".
**Not for:** active as a status value.

### created
**Definition:** the [task](#task) transition in which the task comes to exist in the record, publication being
creation.
The task's own transition vocabulary is `created` plus its status; [lease](#lease) transitions belong to the lease.
**See:** [`work_model.md#the-transition-vocabulary`](work_model.md#the-transition-vocabulary).
**Never:** —
**Not for:** published as a separate state; routed, claimed, or released as task transitions.

### claimable
**Definition:** the derived property of a [task](#task) whose status is neither terminal nor `blocked`, whose
`assigned_to` is unset or names the [principal](#principal) about to [claim](#claim), and on which no [lease](#lease) is held.
**See:** [`work_model.md#what-a-claim-predicate-treats-as-claimable`](work_model.md#what-a-claim-predicate-treats-as-claimable).
**Never:** —
**Not for:** "available" for claimable; open for claimable, whether as an open task, an open pool, or a
task said to be open — `open` is a status value and means something else.

### terminal
**Definition:** a status value after which a [task](#task), a [batch](#batch), or a [checkpoint](#checkpoint) changes no further.
**See:** [`work_model.md#what-a-claim-predicate-treats-as-claimable`](work_model.md#what-a-claim-predicate-treats-as-claimable).
**Never:** —
**Not for:** "final" for terminal.

### runner
**Definition:** the process that runs an [agent](#agent) and holds a [lease](#lease) on the agent's behalf, identified by a
runner id the persisted lease names.
**See:** [`work_model.md#the-claim-and-the-lease-are-one-primitive`](work_model.md#the-claim-and-the-lease-are-one-primitive).
**Never:** "worker", "bot".
**Not for:** agent when the process is meant.

### agent_session
**Definition:** the identity half of a [runner](#runner)'s work that [observations](#observation) lack, such as host, checkout,
branch, and head, related to the [task](#task) it executes.
**See:** [`work_model.md#no-assignment-log-history-is-the-tasks-own-observations`](work_model.md#no-assignment-log-history-is-the-tasks-own-observations).
**Never:** "run history", "dispatch record".
**Not for:** —

### observation
**Definition:** one append-only, timestamped, provenance-bearing write to an entity in the record, from
which the entity's history is read.
**See:** [`work_model.md#no-assignment-log-history-is-the-tasks-own-observations`](work_model.md#no-assignment-log-history-is-the-tasks-own-observations),
[`data_model.md#record-conventions`](data_model.md#record-conventions).
**Never:** "log line".
**Not for:** event for a stored change.

### watchdog
**Definition:** the observer that counts lapses on a [task](#task) and raises a [checkpoint](#checkpoint) when the count reaches
its cap, holding no [authority](#authority) over any [lease](#lease).
**See:** [`failure_posture.md#repeated-lapse-raises-a-checkpoint`](failure_posture.md#repeated-lapse-raises-a-checkpoint).
**Never:** "reaper", "retry loop".
**Not for:** "router" for the watchdog.

### batch
**Definition:** one or more [tasks](#task) going through a [workflow](#workflow) together, and the record of that.
A single task is a batch of one; only batches go through workflows, so there is no separate single-task
path. Tasks are attached to and detached from a batch; batches chain along `FOLLOWS`. A batch is opened by a
closing [sign-off](#sign-off) naming a [successor](#successor), carries the tasks that sign-off carried, and goes through
exactly one [workflow](#workflow) for its whole life.
Reads: "the tasks entered the feature workflow", "the batch is at `qa`", "the batch advances to `impl`",
"a task attached to the batch", "a task detached from the batch", "the batch records who signed off
`qa`", "the tasks leave the workflow when `merge` is signed off".
**See:** [`work_model.md#what-goes-through-a-workflow-is-a-batch-of-tasks`](work_model.md#what-goes-through-a-workflow-is-a-batch-of-tasks),
[`work_model.md#how-a-batch-is-formed-and-what-chooses-its-workflow`](work_model.md#how-a-batch-is-formed-and-what-chooses-its-workflow),
[`data_model.md#concepts`](data_model.md#concepts).
**Never:** "passage", "workflow_run", "aggregation"; split as a noun, and split-out in any form
(aggregation and split are retired as nouns; the verbs are attach and detach).
**Not for:** run for a batch; instance for a batch, unqualified; bundle for a batch.

### parent task
**Definition:** a [task](#task) that groups [child tasks](#child-task) through `PART_OF` [edges](#edge) from each child, whose completion is
derived from its children's terminal states and which never enters a [workflow](#workflow).
**See:** [`work_model.md#parent-and-child-tasks`](work_model.md#parent-and-child-tasks).
**Never:** "epic", "umbrella".
**Not for:** a stored parent status.

### child task
**Definition:** a [task](#task) with one `PART_OF` [edge](#edge) to a [parent task](#parent-task), which goes through [workflows](#workflow) independently
of its siblings.
**Allowed:** "subtask" in prose.
**See:** [`work_model.md#parent-and-child-tasks`](work_model.md#parent-and-child-tasks).
**Never:** "story".
**Not for:** a child with two parents.

### recurring task
**Definition:** a [task](#task) carrying a recurrence rule, of which exactly one instance is non-terminal at a time, and whose
closing [sign-off](#sign-off) creates the next instance — a new task copying the rule, entering [intake](#intake), and linked
`FOLLOWS` to the instance whose completion created it.
Each instance is an ordinary task with its own [chain](#chain) and [terminal](#terminal) status; the next instance's `due_date` is
computed from the rule's schedule, never from the completion time; the history of the recurring task is
read along the task-to-task `FOLLOWS` [edges](#edge) and is never stored. Distinct from an [action series](#action-series), which is
made of [actions](#action) of one class and graduates at the [action gate](#action-gate): an instance's actions feed a series, and
graduation never changes whether the task recurs.
**See:** [`work_model.md#a-recurring-task-is-one-live-instance-and-its-completion-creates-the-next`](work_model.md#a-recurring-task-is-one-live-instance-and-its-completion-creates-the-next),
[`data_model.md#relationships`](data_model.md#relationships).
**Never:** "task reset", "reset to open", "rolled forward" of a task.
**Not for:** template for the rule's home (the live instance carries it); series id or occurrence count as
a field; reschedule for completion (a live instance may be postponed by correcting its `due_date`; an
occurrence that passed closes its instance and creates the next).

### intake rule
**Definition:** data on the record stating that a described change — to an entity of a named type, of a
named change kind, matching a predicate over its fields and over its provenance — is work: the rule's
evaluator, a [daemon](#daemon), writes one [task](#task) per matching change, entering [intake](#intake),
with provenance naming the rule and the change, and nothing else.
A rule keys on no record of the work model (open decision 36), opens no [batch](#batch), names no
[workflow](#workflow), and takes no [action](#action); writing one is a governance write, reserved to the
[operator](#operator) by default (decision 18). The operator's word for it was *listener*; the design keeps
that word for the socket a delivery lands on.
**See:** [`work_model.md#an-intake-rule-turns-a-described-change-in-the-record-into-a-task-and-nothing-else`](work_model.md#an-intake-rule-turns-a-described-change-in-the-record-into-a-task-and-nothing-else),
[`work_model.md#where-tasks-come-from-every-source-indexed`](work_model.md#where-tasks-come-from-every-source-indexed),
[`adapters.md#continual-inbound-is-the-inbound-side-and-an-intake-rule-evaluates-downstream-of-it`](adapters.md#continual-inbound-is-the-inbound-side-and-an-intake-rule-evaluates-downstream-of-it).
**Field:** `intake_rule`.
**Never:** —
**Not for:** "listener" for an intake rule (the transport listener is the shared socket a delivery lands on —
[`adapters.md#where-inbound-delivery-lands-the-adapter-verifies-and-identifies-it-and-the-records-own-subscriptions-are-not-it`](adapters.md#where-inbound-delivery-lands-the-adapter-verifies-and-identifies-it-and-the-records-own-subscriptions-are-not-it));
"trigger" for an intake rule (a rule creates a task; nothing but a sign-off opens a step); "task type" for a
rule, or "template" (the rule authors the created task's text; classification is intake's).

### operator-facing agent
**Definition:** the [agent](#agent), defined by the `ateles` `agent`, that [claims](#claim) operator-only [tasks](#task),
carries them and their [checkpoints](#checkpoint) to the [operator](#operator), and records the outcome.
**See:** [`work_model.md#operator-only-tasks-are-claimed-by-the-operator-facing-agent`](work_model.md#operator-only-tasks-are-claimed-by-the-operator-facing-agent).
**Never:** —
**Not for:** the operator for the agent; concierge for the agent, unqualified.

### daemon
**Definition:** a long-lived process that self-triggers on its own loop, producing [tasks](#task) or [actions](#action)
without receiving a task.
**See:** [`work_model.md#the-four-execution-mechanisms`](work_model.md#the-four-execution-mechanisms).
**Never:** —
**Not for:** service for a daemon, unqualified.

### pipeline
**Definition:** the GitHub-hosted execution mechanism that opens each [step](#step) of a [workflow](#workflow) for a [batch](#batch) as
[claimable](#claimable) step work, which the [step owner](#step-owner) [claims](#claim), and never writes a [task](#task) status.
It delivers nothing; it is the same pull, over steps.
**See:** [`work_model.md#the-four-execution-mechanisms`](work_model.md#the-four-execution-mechanisms).
**Never:** —
**Not for:** workflow for the pipeline (the declaration); CI for the pipeline (one of its checks).

### interactive session
**Definition:** the execution mechanism in which an [operator](#operator) works directly with an
[agent](#agent): a work **source** whose output becomes [tasks](#task), holding no [lease](#lease) and
receiving no task.
Because it holds no lease, none of the lease-borne [recovery](#recovery) reaches it — nothing lapses when a session
dies, and there is no task to make [claimable](#claimable) again. Work an interrupted session left
unfinished is recovered by **digestion**, reading the session back and filing what it left, which is a
declared [workflow](#workflow) with an owning role rather than an emergent practice.
**See:** [`work_model.md#the-four-execution-mechanisms`](work_model.md#the-four-execution-mechanisms).
**Never:** —
**Not for:** [daemon](#daemon) for a session (a daemon self-triggers; a session is driven by the operator);
[runner](#runner) for a session (the runner is the process); a session as something that [claims](#claim).

## Gate model (`gates_and_workflows.md`)

### workflow
**Definition:** the declaration, per (project, workflow type), of an ordered list of [steps](#step), the [fast paths](#fast-path)
a [batch](#batch) may take, and the [successors](#successor) a closing [sign-off](#sign-off) may name.
**See:** [`workflows.md`](workflows.md),
[`gates_and_workflows.md#declaration-batch-projection`](gates_and_workflows.md#declaration-batch-projection).
**Never:** "workflow_definition".
**Not for:** pipeline for a workflow (one engine that runs workflows); "template" for a workflow.

### step
**Definition:** one declared position in a [workflow](#workflow)'s ordered list, carrying a name, a [step owner](#step-owner), a
`required` flag, an `on_fail` target, its [read dependencies](#read-dependency), and parallel-group and
join fields, [claimed](#claim) by its step owner on a [batch](#batch) and closed by that owner's
[sign-off](#sign-off).
Step names are data (`pm`, `ux`, `arch`, `impl`, `pr_review`, `qa`, `legal`, `release`, and any a workflow
declares).
**See:** [`gates_and_workflows.md#declaration-batch-projection`](gates_and_workflows.md#declaration-batch-projection).
**Never:** "gate owner".
**Not for:** gate, phase, or check for a step — neither after a step name (a qa gate, an impl phase) nor
in front of a step's own attributes (a gate owner, a gate sequence); `gate` is the action gate. Also not
checkpoint for a step, in either order.

### read dependency
**Definition:** an entity type a [step](#step) declares it must be able to read — `reads_to_enter` before
the step opens, `reads_to_close` before its [sign-off](#sign-off) is written — with a required
[freshness](#freshness) for [adapter](#adapter)-sourced types.
A step that cannot read a type it declared does not proceed: the read returns `unknown`, the step holds,
the condition is announced off-record, and when the bounded hold reaches its bound one
[checkpoint](#checkpoint) names the dependency (reason `undeclared_dependency`). A step that reads a type
it did not declare is a **declaration error**, caught in review the way an undeclared
[action_type](#action_type) is. The point of the declaration is that a missing one is visible, where an
unstated dependency is invisible until a read fails silently and something proceeds on the gap.
**Field:** `workflow.steps[].reads_to_enter`, `reads_to_close`, `freshness`.
**See:** [`gates_and_workflows.md#declaration-batch-projection`](gates_and_workflows.md#declaration-batch-projection).
**Never:** —
**Not for:** a read dependency for a [grant](#grant)'s admitted types (the grant is the outer bound, the
dependency is what this step needs); a read dependency for `context_entity_types[]` (that is the
[agent](#agent)'s information diet, declared on the agent and not on the step).

### stage
**Definition:** a named group of contiguous [steps](#step) in a [workflow](#workflow), such as the review stage or the release
stage.
**See:** [`gates_and_workflows.md#declaration-batch-projection`](gates_and_workflows.md#declaration-batch-projection).
**Never:** —
**Not for:** stage for a single step; phase when a group of steps is meant.

### step owner
**Definition:** the **role** declared on a [step](#step), which the roster resolves to a [principal](#principal) at [claim](#claim) time;
that principal claims the step on a [batch](#batch) and its [sign-off](#sign-off) closes it. The declaration names a role so that
one [workflow](#workflow) serves every project and a renamed or replaced [agent](#agent) leaves no stale name in it; the
resolution to a principal happens when the step is claimed, against `swarm_roster` for the batch's
project, and a step whose role resolves to no principal raises a [checkpoint](#checkpoint) (reason
`unspawnable_assignee`) rather than falling through to any available agent.
**Field:** `workflow.steps[].owner_role` (the design's name; the field is `owner_agent` in the built
declarations and holds a role there too — `status.md`).
**See:** [`gates_and_workflows.md#two-questions-who-may-claim-a-step-and-whether-an-action-may-be-taken`](gates_and_workflows.md#two-questions-who-may-claim-a-step-and-whether-an-action-may-be-taken).
**Never:** —
**Not for:** owner alone.

### sign-off
**Definition:** the record a [step owner](#step-owner) writes to close a [step](#step) on a
[batch](#batch), carrying the [verdict](#verdict), the [findings](#finding) that produced it, timestamps,
the [agent](#agent), [artifact](#artifact) refs, and the pinned `agent` version.
A terminal write that supplies every field the schema requires; a rejected write is an error, never
swallowed. Written as a hyphenated noun (a sign-off); the act is to sign off, two words.
**Verdict values:** `signed` (the step's [condition](#condition) is met), a blocking verdict (it is not,
and the step's `on_fail` says which earlier step opens again), and `waived` (the [operator](#operator) [principal](#principal) closed
an unsigned required step, carrying the reason). `waived` is the only verdict a principal other than the
step owner may write, and only the operator principal may write it — the right is not delegable
([`gates_and_workflows.md#declaration-batch-projection`](gates_and_workflows.md#declaration-batch-projection)).
**Waiver scope:** one [batch](#batch)'s unsigned required steps, one `waived` sign-off **per step**, each
naming its step and carrying its reason — so a waived step is queryable as waived rather than recorded as a
batch-level flag or as prose on an artifact.
**Terminal, and never revised in place:** a later judgement is a new sign-off, and the latest per step
owner per artifact head is the one that stands; the superseded one stays readable.
**Evidence:** a blocking verdict names the executed check and the output it produced, or the mechanism
that executed it; unexecuted reasoning is a non-blocking [finding](#finding), never a block
([`gates_and_workflows.md#findings-verdicts-and-what-a-blocking-finding-obliges`](gates_and_workflows.md#findings-verdicts-and-what-a-blocking-finding-obliges)).
**See:** [`gates_and_workflows.md#declaration-batch-projection`](gates_and_workflows.md#declaration-batch-projection),
[`adapters.md#no-external-event-advances-a-step-by-itself`](adapters.md#no-external-event-advances-a-step-by-itself).
**Never:** "participation_record", "step_run", "LGTM", "audit row".
**Not for:** approval for a sign-off (an approval is on a [checkpoint](#checkpoint)); "green" without the
record.

### finding
**Definition:** one defect or objection a [step owner](#step-owner) records when judging a
[batch](#batch), carrying its own severity.
The severity of the finding, not the summary token of the [sign-off](#sign-off) that carries it, is what
blocks: a blocking finding filed under a non-blocking [verdict](#verdict) is still a blocking finding. A
blocking finding is either **implementation-only** — a named defect with a determinate fix, which may be
routed to an implementer, though the step owner still holds the terminal sign-off — or **decision or
attestation**, needing a judgement only a [principal](#principal) can make, which is not routable at all. A blocking
finding cites an executed command and its output; one reasoned about but not reproduced is filed as
non-blocking, stating what could not be verified. A finding may also record a **hold**: it names a
[condition](#condition) the step must satisfy and cannot yet judge, what would resolve it, and when it was
recorded; it is non-blocking, because it asserts no defect, and no sign-off is written while it stands
(`work_model.md#a-batch-may-hold-on-a-condition-discovered-mid-flight`).
**One-off or standing:** a second axis, judged separately from severity. A **one-off** finding is
discharged when the [batch](#batch)'s work is corrected; a **standing** finding names a defect that will
recur, and correcting the work alone does not discharge it — a change to the [agent](#agent), the
[workflow](#workflow), or the [step](#step) that produced it is owed besides. An [operator](#operator)'s
input on reviewed work is a finding and is judged on both axes.
**See:** [`gates_and_workflows.md#findings-verdicts-and-what-a-blocking-finding-obliges`](gates_and_workflows.md#findings-verdicts-and-what-a-blocking-finding-obliges),
[`gates_and_workflows.md#a-finding-is-one-off-or-standing-and-a-standing-one-obliges-a-change-to-what-produced-it`](gates_and_workflows.md#a-finding-is-one-off-or-standing-and-a-standing-one-obliges-a-change-to-what-produced-it).
**Never:** —
**Not for:** a finding as the thing that closes a step (the [sign-off](#sign-off) closes it); a comment on
an [artifact](#artifact) as a finding (a remark carries no severity and reaches no step); a blocking
finding that names no executed check; a standing finding discharged by correcting only the work it was
filed against.

### verdict
**Definition:** the summary a [sign-off](#sign-off) carries, stating whether the [step](#step)'s
[condition](#condition) is met: `signed`, a blocking value, or `waived`.
Those three are the only values, and a host's own review tokens are the [adapter](#adapter)'s [inbound](#inbound)
mapping onto them, never the record's vocabulary.
**Against its findings:** the [findings](#finding) bind. A verdict must agree with the findings its
[sign-off](#sign-off) carries, and a write whose verdict contradicts them — a blocking finding under a
non-blocking verdict — is **rejected at submission**, never swallowed; the step stays open until the step
owner re-submits.
**Terminal:** a verdict is never revised in place. A [step owner](#step-owner) reaching a different judgement writes a new
sign-off, and the latest per step owner per [artifact](#artifact) head stands.
**Unconditional:** a verdict carries no [condition](#condition); a requirement that must hold later is a
[task](#task) or an acceptance criterion, not a clause in a verdict.
**See:** [`gates_and_workflows.md#findings-verdicts-and-what-a-blocking-finding-obliges`](gates_and_workflows.md#findings-verdicts-and-what-a-blocking-finding-obliges),
[`gates_and_workflows.md#declaration-batch-projection`](gates_and_workflows.md#declaration-batch-projection).
**Never:** —
**Not for:** verdict for a whole [sign-off](#sign-off) (the verdict is one field of it); a verdict on an
[artifact](#artifact) (the subject is the batch's tasks); a verdict as what resolves a
[checkpoint](#checkpoint) (that is an [approval](#approval)).

### condition
**Definition:** a stated requirement that a [step](#step) must satisfy — its own, which its [verdict](#verdict)
states is met or not, or a later step's, which a verdict may not impose.
A verdict may **not** carry one. A [sign-off](#sign-off) that closed its step while binding
what follows would hand its own judgement to the party it was binding, and the guarantee that a closed step
was judged unconditionally is what makes a signed step readable. A requirement that must hold later is a
[task](#task) or an acceptance criterion of the [batch](#batch). A step's own condition may be **discovered
mid-flight** — a re-quote pending, a read returning `unknown`, a task the batch created still open — and
then the step **holds**: its owner records a [finding](#finding) naming the condition, writes no sign-off, and
renews its [lease](#lease); a hold is not a state and needs no field
(`work_model.md#a-batch-may-hold-on-a-condition-discovered-mid-flight`).
**See:** [`gates_and_workflows.md#findings-verdicts-and-what-a-blocking-finding-obliges`](gates_and_workflows.md#findings-verdicts-and-what-a-blocking-finding-obliges),
[`work_model.md#a-batch-may-hold-on-a-condition-discovered-mid-flight`](work_model.md#a-batch-may-hold-on-a-condition-discovered-mid-flight).
**Never:** —
**Not for:** condition for a [gate](#gate)'s inputs (those are the action's class, blast radius, and
confidence); "requirement" for an acceptance criterion of a [batch](#batch).

### step state
**Definition:** the state of one [step](#step) within one [batch](#batch), derived at read time from [edges](#edge)
and never stored: open (the batch and the step), [claimed](#claim) (a [lease](#lease) from the
[step owner](#step-owner) to the step on that batch), or signed (a [sign-off](#sign-off)).
The concept is written in spaced words, "step state"; the map that projects it onto the [task](#task) is the field
[`step_status`](#step_status), and the two are not the same thing.
**See:** [`gates_and_workflows.md#declaration-batch-projection`](gates_and_workflows.md#declaration-batch-projection).
**Never:** "gate status".
**Not for:** a stored per-step status row; `step_status` for step state (one is the derived state, the
other the projection of it).

### step_status
**Definition:** the map on the [task](#task) projecting each [step](#step)'s
[step state](#step-state) on its [batch](#batch) so that it is read in one retrieval, derived from the
[sign-offs](#sign-off) and proved equal to them by a reconciler.
Written as the field the record names, in code font, because that is what a reader queries; the state it
projects is the spaced concept step state.
**See:** [`gates_and_workflows.md#declaration-batch-projection`](gates_and_workflows.md#declaration-batch-projection).
**Never:** "gate_status".
**Not for:** history for the projection; a second source of truth; `step_status` as the name of the
concept.

### fast path
**Definition:** a declared skip of [steps](#step) that a [workflow](#workflow) permits for a named class of [tasks](#task).
**See:** [`gates_and_workflows.md#declaration-batch-projection`](gates_and_workflows.md#declaration-batch-projection).
**Never:** "shortcut".
**Not for:** a [projection](#projection) for a fast path (one is a stored read, the other a declared skip).

### successor
**Definition:** a [workflow](#workflow) that a `workflow` declares in `successors` as one a [batch](#batch) of it may enter on
closing, of which the closing [sign-off](#sign-off) selects exactly one or none.
The closing sign-off is the sign-off on the workflow's last [step](#step), which is always a single step.
**See:** [`gates_and_workflows.md#sequencing-is-data-successors-and-the-chain`](gates_and_workflows.md#sequencing-is-data-successors-and-the-chain).
**Never:** "downstream workflow", "handoff".
**Not for:** next stage for a successor (a stage is within a workflow); two successors at once (that is a
detach); a successor named by anything but the closing sign-off.

### chain
**Definition:** the derived, never stored, sequence of [batches](#batch) a [task](#task) has gone through, read along
`FOLLOWS` [edges](#edge) from its live batch back to its [intake](#intake) batch.
**See:** [`gates_and_workflows.md#sequencing-is-data-successors-and-the-chain`](gates_and_workflows.md#sequencing-is-data-successors-and-the-chain).
**Never:** "super-workflow".
**Not for:** pipeline for the sequence; "program" for the chain; a stored list of batches on the task.

### issue
**Definition:** a GitHub issue, an [artifact](#artifact) a [batch](#batch) produces or references, linked to the batch and its
[tasks](#task) by [edge](#edge).
**See:** [`work_model.md#artifacts-are-records-a-batch-leaves-never-its-subject`](work_model.md#artifacts-are-records-a-batch-leaves-never-its-subject).
**Never:** —
**Not for:** "ticket" for an issue; task for the issue; the subject of a step.

### gate
**Definition:** short for the [action gate](#action-gate), and nothing else.
**See:** [`gates_and_workflows.md#the-action-gate-is-pr-independent`](gates_and_workflows.md#the-action-gate-is-pr-independent).
**Never:** "gates green".
**Not for:** gate for a step or a stage.

### action gate
**Definition:** the decision, taken by a [principal](#principal) evaluating one [action](#action) against the action policy,
whether that action is taken or [checkpointed](#checkpoint).
Inputs are the action's class, [blast radius](#blast-radius), [confidence](#confidence), and successful recurrences; no PR, [issue](#issue), or
repository.
**See:** [`gates_and_workflows.md#the-action-gate-is-pr-independent`](gates_and_workflows.md#the-action-gate-is-pr-independent).
**Never:** "execution gate".
**Not for:** "merge gate" for the action gate (merge is one boundary among several).

### action_policy
**Definition:** the policy a [principal](#principal) evaluates the [action gate](#action-gate) against, listing the low- and high-blast
[action](#action) classes, the [confidence](#confidence) threshold, the recurrence count that graduates a series, the
always-checkpoint boundaries, the permission scope, and the consent tolerance per action class — the change
to an action's consented figures that may be taken without a new [checkpoint](#checkpoint), zero where the
policy declares none (`payments.md#tolerance-is-an-action_policy-value-and-its-default-is-zero`).
**See:** [`gates_and_workflows.md#two-questions-who-may-claim-a-step-and-whether-an-action-may-be-taken`](gates_and_workflows.md#two-questions-who-may-claim-a-step-and-whether-an-action-may-be-taken).
**Never:** "execution_policy", "execution policy", "workflow policy" (retired: the step owners a workflow
declares together with the grants in force decide who may claim a step, and nothing stands beside them
under a name of its own).
**Not for:** "config" or "settings" for the policy; the policy for who may claim a step (that is the
declaration and the grants).

### action
**Definition:** one intended effect on an [external system](#external-system) — one the swarm does not own — such as a send, a
publish, a merge, a payment, or a release, related to the [task](#task) it serves.
Created when the effect becomes known, which may be mid-workflow; a task may produce many, most unknown at
creation. The record is inside the boundary, not across it, so an internal operational write to it is not
an action; the two exceptions, governance writes and lossy record mutations, are actions for what they can
destroy rather than for where they go.
**See:** [`gates_and_workflows.md#actions-are-entities-only-actions-are-taken`](gates_and_workflows.md#actions-are-entities-only-actions-are-taken),
[`adapters.md#outbound-steps-produce-actions-adapters-take-them`](adapters.md#outbound-steps-produce-actions-adapters-take-them).
**Never:** "side effect" (unrecorded).
**Not for:** task for the effect; operation for an action.

### external system
**Definition:** a system the swarm does not own, on the far side of the design's one boundary, reached only
through an [adapter](#adapter).
The record is inside that boundary, not across it: the swarm's own state lives there, so writing to it
crosses nothing. This is the boundary every use of "outside" in these documents means, and it is stated
once in the [action](#action)'s home section.
**See:** [`gates_and_workflows.md#actions-are-entities-only-actions-are-taken`](gates_and_workflows.md#actions-are-entities-only-actions-are-taken),
[`adapters.md#the-two-invariants`](adapters.md#the-two-invariants).
**Never:** —
**Not for:** "the Ateles system" or "the Neotoma system" as the thing an effect is outside of (there is one
boundary, and the record is inside it); external for a component the swarm runs.

### record
**Definition:** Neotoma, the store the swarm reads and writes its own state in, inside the boundary an
[action](#action) crosses.
Every entity type the design names lives here (`data_model.md`); an [artifact](#artifact) is the record's
handle on a thing that does not. The word is also ordinary English for a thing written down — the record a
[step owner](#step-owner) writes, the record an effect leaves — and those uses stand; where the store is
meant and the sentence could be read either way, say the record and name what is in it.
**See:** [`data_model.md#scope`](data_model.md#scope),
[`failure_posture.md#the-decision`](failure_posture.md#the-decision).
**Never:** —
**Not for:** the record for an [external system](#external-system); "database" for the record in foundation
prose; the record as something an [adapter](#adapter) reaches across a boundary.

### take (an action)
**Definition:** to carry out an [action](#action)'s effect on an [external system](#external-system) once the [action gate](#action-gate) permits it.
Actions are taken; [tasks](#task) are executed.
**See:** [`gates_and_workflows.md#actions-are-entities-only-actions-are-taken`](gates_and_workflows.md#actions-are-entities-only-actions-are-taken).
**Never:** execute, in any form, anywhere near an action — an action is taken, never executed; and
auto-execute, in any form, spelled with or without the hyphen.
**Not for:** "fire" or "perform" for an action.

### action_type
**Definition:** the class an [action](#action) belongs to, on which [blast radius](#blast-radius) keys, and which a [task](#task) declares at
creation as the classes of action it expects to produce.
Values include `build`, `docs`, `publish`, `send_external_comms`, and `operator_only`; a declared but
unclassified value fails closed.
**See:** [`gates_and_workflows.md#confidence-and-three-blast-tiers`](gates_and_workflows.md#confidence-and-three-blast-tiers).
**Never:** —
**Not for:** "category" or "kind" for the class; inferring it from the handling agent.

### blast radius
**Definition:** the tier an [action_type](#action_type) resolves to under an [action_policy](#action_policy), one of `LOW`, `HIGH`, or
`NEVER`.
`LOW` is taken at or above the [confidence](#confidence) threshold or once an [action series](#action-series) graduates; `HIGH` is
[checkpointed](#checkpoint) until an action series graduates; `NEVER` is cleared by no confidence and no recurrence.
**See:** [`gates_and_workflows.md#confidence-and-three-blast-tiers`](gates_and_workflows.md#confidence-and-three-blast-tiers).
**Never:** "risk level" (unbounded).
**Not for:** "severity" for a tier.

### confidence
**Definition:** the proposing [agent](#agent)'s score that an [action](#action) is right, compared with the policy's threshold.
**See:** [`gates_and_workflows.md#confidence-and-three-blast-tiers`](gates_and_workflows.md#confidence-and-three-blast-tiers).
**Never:** —
**Not for:** a default of zero standing in for a score.

### action series
**Definition:** a series of successfully taken [actions](#action) of one class that, on reaching the policy's
count, graduates that class from [checkpointing](#checkpoint) to being taken without one.
Named for what the series is made of: the members are actions, and the class they share is what graduates.
**See:** [`gates_and_workflows.md#confidence-and-three-blast-tiers`](gates_and_workflows.md#confidence-and-three-blast-tiers).
**Never:** "streak".
**Not for:** history for a series, unqualified.

### operator_only
**Definition:** the [action_type](#action_type) marking an effect an [agent](#agent) structurally cannot carry out, which resolves to
`NEVER` ahead of any policy.
The [task](#task) that carries it is still [claimable](#claimable), by the [operator-facing agent](#operator-facing-agent).
**See:** [`gates_and_workflows.md#confidence-and-three-blast-tiers`](gates_and_workflows.md#confidence-and-three-blast-tiers).
**Never:** "unclaimable".
**Not for:** "high blast" for `operator_only` (a louder `HIGH` delays the wrong outcome rather than
preventing it).

### checkpoint
**Definition:** the held state of its [subject](#subject) — an [action](#action) held at the [action gate](#action-gate), or a [task](#task) the swarm
cannot advance — awaiting a [principal](#principal)'s decision.
Two cases, one term, because both are work stopped short of a decision only a principal can make; what
resumes differs and is read from the subject [edge](#edge), not from a second term. Recorded as an entity linked to
its subject, carrying a reason class, the needed input, the options, whom it awaits, and who resolved it,
and ending in a terminal approval. To checkpoint a subject is to write one and hold. The reason classes
— `gate_hold` for a held action, and the classes a task is [escalated](#escalate) under — are enumerated once, each
with what raises it, in `failure_posture.md#checkpoints-on-tasks-one-queue-one-protocol`; a policy may
declare more. "Brief" described its content, not its identity, and is
retired from the name for the same reason as `_record` and `_definition`.
**See:** [`gates_and_workflows.md#the-checkpoint`](gates_and_workflows.md#the-checkpoint),
[`failure_posture.md#what-a-checkpoint-does-not-absorb`](failure_posture.md#what-a-checkpoint-does-not-absorb),
[`data_model.md#concepts`](data_model.md#concepts).
**Never:** "checkpoint_brief", "approval request".
**Not for:** checkpoint for a step; checkpoint for the halt (the halt is not a checkpoint: nothing can be
written).

### subject
**Definition:** the thing a [checkpoint](#checkpoint) holds, exactly one, named by its `CHECKPOINTS`
[edge](#edge) and never by a free-text field: an [action](#action) or a [task](#task), and nothing else.
The word carries a second sense the work model owns — what a [step](#step) is taken on, which is always the
[batch](#batch)'s tasks and never an [artifact](#artifact). The two agree where it matters: in both, the
subject is the work itself, and the record an effect leaves is not it. Where a sentence could be read
either way, name the entity — the checkpoint's subject, or the subject of a step.
**See:** [`gates_and_workflows.md#the-checkpoint`](gates_and_workflows.md#the-checkpoint),
[`data_model.md#concepts`](data_model.md#concepts),
[`work_model.md#artifacts-are-records-a-batch-leaves-never-its-subject`](work_model.md#artifacts-are-records-a-batch-leaves-never-its-subject).
**Never:** —
**Not for:** an [artifact](#artifact) as the subject of anything; a [step](#step) or a [batch](#batch) as a
checkpoint's subject; two subjects on one checkpoint; the subject held as free text.

### steward
**Definition:** the [pipeline](#pipeline) role that merges a pull request once every required [step](#step) is signed off and
the [action gate](#action-gate) permits the merge [action](#action).
**See:** [`gates_and_workflows.md#the-action-gate-is-pr-independent`](gates_and_workflows.md#the-action-gate-is-pr-independent).
**Never:** "merger".
**Not for:** "bot" for the steward.

### review step
**Definition:** a [step](#step) whose work is a judgement of the [batch](#batch)'s change rather than a
change to it, closed by its [step owner](#step-owner)'s [sign-off](#sign-off) like any other step. `pm`,
`ux`, `arch`, `pr_review`, `qa`, and `legal` are review steps; nothing distinguishes one from a working
step but what its owner does, and no separate review concept exists in the design.
**See:** [`gates_and_workflows.md#declaration-batch-projection`](gates_and_workflows.md#declaration-batch-projection),
[`workflows.md`](workflows.md).
**Never:** "lens", "review panel", "panel".
**Not for:** "reviewer" for a review step's owner, unqualified; CI for a review step.

### effect dedup
**Definition:** the rule that every [outbound](#outbound) effect is idempotent or deduplicated on its own key, so a
re-claimed [task](#task) never repeats an effect that already happened.
**See:** [`work_model.md#at-least-once-implies-effect-dedup`](work_model.md#at-least-once-implies-effect-dedup),
[`data_model.md#record-conventions`](data_model.md#record-conventions).
**Never:** "replay protection" (replay is refused outright).
**Not for:** "retry" for dedup.

## Core workflows (`workflows.md`)

### intake
**Definition:** the [workflow](#workflow) every [task](#task) enters first, whose [steps](#step) classify, link, dedupe, prioritize, and
route the task, and whose closing [sign-off](#sign-off) names the [successor](#successor) workflow, or none, or operator-only.
A task with no intake [batch](#batch) is unrouted by that fact; no unrouted state is stored.
**See:** [`workflows.md#intake`](workflows.md#intake),
[`work_model.md#intake-is-every-tasks-first-workflow`](work_model.md#intake-is-every-tasks-first-workflow),
[`adapters.md#no-external-event-advances-a-step-by-itself`](adapters.md#no-external-event-advances-a-step-by-itself).
**Never:** "undispatched".
**Not for:** "triage" for the whole workflow (its first stage); unrouted as a stored status; routing by
a router (the `route` step is a sign-off by a step owner).

## Adapters (`adapters.md`)

### adapter
**Definition:** the component that translates between one [external system](#external-system) and the record in both
directions, [inbound](#inbound) events into [signals](#signal) about [artifacts](#artifact) and [outbound](#outbound) [actions](#action) into operations on that
system, and the only component that touches the system.
An adapter is a [daemon](#daemon) in the work model's sense: it self-triggers on the external system and receives no
[task](#task); the engine reads only what the adapter wrote.
**See:** [`adapters.md#the-two-invariants`](adapters.md#the-two-invariants),
[`adapters.md#what-the-adapter-does-with-every-event`](adapters.md#what-the-adapter-does-with-every-event).
**Never:** "connector", "plugin".
**Not for:** the engine for the adapter (the engine reads the record; the adapter reads the system);
"gateway" for an adapter, unqualified.

### signal
**Definition:** what an [inbound](#inbound) external event is to the record: information about an [artifact](#artifact), which an
[adapter](#adapter) translates into a [sign-off](#sign-off) by a named [principal](#principal), an [observation](#observation) on an artifact, an [action](#action)
confirmation, or a new [task](#task) for [intake](#intake), and never into an instruction to a [workflow](#workflow).
**See:** [`adapters.md#no-external-event-advances-a-step-by-itself`](adapters.md#no-external-event-advances-a-step-by-itself).
**Never:** —
**Not for:** "trigger" for a signal (nothing outside the record opens a step); "command" for a signal.

### action confirmation
**Definition:** the [observation](#observation) an [adapter](#adapter) writes on an [action](#action) once its effect exists in the external
system, carrying `taken_at` and `result_ref`, [read back](#read-back) from that system and never inferred from the
operation's return.
**See:** [`adapters.md#outbound-steps-produce-actions-adapters-take-them`](adapters.md#outbound-steps-produce-actions-adapters-take-them).
**Never:** —
**Not for:** sign-off for a confirmation (a confirmation closes no step); a success response for a
confirmation.

### inbound
**Definition:** the direction in which an external event reaches the record, as a [signal](#signal) an
[adapter](#adapter) translates into one of four outcomes and nothing else.
**See:** [`adapters.md#no-external-event-advances-a-step-by-itself`](adapters.md#no-external-event-advances-a-step-by-itself).
**Never:** —
**Not for:** inbound for a [task](#task) reaching a principal (work is claimed, never delivered); inbound
for a subscription over the record's own entity changes (that wakes a consumer on a write the record
already holds, and reports on no external system —
[`adapters.md#where-inbound-delivery-lands-the-adapter-verifies-and-identifies-it-and-the-records-own-subscriptions-are-not-it`](adapters.md#where-inbound-delivery-lands-the-adapter-verifies-and-identifies-it-and-the-records-own-subscriptions-are-not-it)).

### outbound
**Definition:** the direction in which the record reaches an [external system](#external-system), as an [action](#action) an
[adapter](#adapter) takes once the [action gate](#action-gate) permits it.
**It names a direction, not a class of action, which is why it is not redundant with `action`.** Every
action is outbound, so "outbound action" adds nothing and is not written; what the word earns its place on
is everything at the boundary that is *not* an action — the operation an adapter performs to take one, the
effect that operation leaves, and a [credential](#credential) held for reaching out
([`authority_model.md#grants`](authority_model.md#grants)) — each of which has an [inbound](#inbound)
counterpart it must be told apart from. The pair is the axis `adapters.md` is organized on: one direction
carries events into the record, the other carries effects out of it.
**See:** [`adapters.md#outbound-steps-produce-actions-adapters-take-them`](adapters.md#outbound-steps-produce-actions-adapters-take-them).
**Never:** —
**Not for:** outbound for an internal write to the record (only an effect on an [external
system](#external-system) is an action).

### delivery
**Definition:** one arrival of an external event at an [adapter](#adapter), carrying the [external system](#external-system)'s
own delivery id, which is the idempotency key of the write it produces.
Every delivery resolves to one of the four [inbound](#inbound) outcomes or to [dropped](#dropped) with a reason; that
[disposition](#disposition), never receipt alone, is what is recorded.
**See:** [`adapters.md#what-the-adapter-does-with-every-event`](adapters.md#what-the-adapter-does-with-every-event).
**Never:** —
**Not for:** delivery for the handing of work to a principal (pull is the only delivery of work,
[`work_model.md#pull-is-the-only-delivery-assignment-constrains-eligibility`](work_model.md#pull-is-the-only-delivery-assignment-constrains-eligibility)).

### disposition
**Definition:** what an [adapter](#adapter) resolved one [delivery](#delivery) to, recorded on every
delivery without exception: one of the four [inbound](#inbound) outcomes, or [dropped](#dropped) with the reason that
decided it.
There is no silent branch; receipt without a disposition is indistinguishable from handling.
**See:** [`adapters.md#what-the-adapter-does-with-every-event`](adapters.md#what-the-adapter-does-with-every-event).
**Never:** —
**Not for:** "handled" for a disposition (it names which outcome, not that there was one); a log line for
a disposition.

### dropped
**Definition:** the [disposition](#disposition) of a [delivery](#delivery) an [adapter](#adapter) resolved
to no outcome, recorded with the reason that decided it and counted per window.
A drop is announced on the same off-record path a [halt](#halt) uses, aggregated rather than one message each;
where the drop concerns a request a person made on the [external system](#external-system), the reason goes back to that
system as an [observation](#observation) the person can see.
**See:** [`adapters.md#what-the-adapter-does-with-every-event`](adapters.md#what-the-adapter-does-with-every-event).
**Never:** —
**Not for:** "swallowed" or "ignored" for a drop (each names the failure this disposition exists to
prevent, and each is used in the documents only to name it); dropped without a reason; dropped for an
event resolved to an observation.

### sourcing
**Definition:** what an [adapter](#adapter) records through the record's provenance about a read it made:
the [external system](#external-system) and adapter the [observation](#observation) came from, the time the system itself states for it, and
the [coverage](#coverage) of the read.
An adapter records sourcing through provenance and never through bookkeeping of its own.
**See:** [`adapters.md#what-the-adapter-does-with-every-event`](adapters.md#what-the-adapter-does-with-every-event),
[`data_model.md#record-conventions`](data_model.md#record-conventions).
**Never:** —
**Not for:** sourcing for the time a write landed (that is the record's own timestamp, not the source's).

### coverage
**Definition:** the part of a read's [sourcing](#sourcing) stating what an [adapter](#adapter) asked the
[external system](#external-system) for and what it actually got back, so a partial, truncated, or paged read is
distinguishable from a complete one.
Without it a cut-short page and a system with nothing to report produce the same record.
**See:** [`adapters.md#what-the-adapter-does-with-every-event`](adapters.md#what-the-adapter-does-with-every-event),
[`data_model.md#record-conventions`](data_model.md#record-conventions).
**Never:** —
**Not for:** coverage for test coverage; coverage as a completeness flag (it states the window asked and
returned, not a verdict on completeness).

### freshness
**Definition:** how current the record's picture of an [external system](#external-system) is, and whether an interval was
ever completely read — derived by reading [sourcing](#sourcing) and [coverage](#coverage) across an
[artifact](#artifact)'s [observations](#observation), never stored.
A stored freshness field would need a process to keep it true (principle 11) and goes stale into a
confident-looking value at the moment that process stops.
**See:** [`adapters.md#what-the-adapter-does-with-every-event`](adapters.md#what-the-adapter-does-with-every-event),
[`data_model.md#record-conventions`](data_model.md#record-conventions).
**Never:** —
**Not for:** `last_synced_at` or "sync status" as a field the record keeps (the documents name them only
to forbid them); freshness of a [sign-off](#sign-off) against an artifact's head (that is its own derived
read); a stored freshness flag.

### hydration
**Definition:** the phase that resolves a [step](#step)'s declared [read dependencies](#read-dependency)
before the step runs — reading from the record what the record holds, and importing through an
[adapter](#adapter) what an [external system](#external-system) holds, as [observations](#observation) on
[artifacts](#artifact) — so that the step begins only once every declared type is readable.
It runs before a step opens against `reads_to_enter`, and again before a [sign-off](#sign-off) is written
against `reads_to_close`; nothing is imported during the step itself. A read hydration cannot fulfil is
`unknown`, and the step holds, bounded, then [escalates](#escalate).
**See:** [`adapters.md#the-adapter-runs-before-and-after-a-step-never-during-it`](adapters.md#the-adapter-runs-before-and-after-a-step-never-during-it),
[`gates_and_workflows.md#declaration-batch-projection`](gates_and_workflows.md#declaration-batch-projection).
**Never:** —
**Not for:** hydration for an [adapter](#adapter)'s own scheduled polling (that produces
[signals](#signal), and answers to no step's declaration); hydration for a read a step performs mid-execution
(the design has none); "prefetch" or "warm-up" for hydration.

### as-of read
**Definition:** a read that reconstructs what the record held at a past moment rather than now, along
either of two axes: **event time**, the state implied by what had happened by that moment, or **ingestion
time**, the state that was actually readable then — which excludes [observations](#observation) describing
an earlier moment that arrived later.
Ingestion time is the axis that answers what a [step](#step) knew when it signed; event time answers what
was true. [Freshness](#freshness), the state a [sign-off](#sign-off) judged, and the reconstruction of a
past [drop](#dropped) or hold are all derived through it, which is why none of them is stored.
**See:** [`adapters.md#what-the-record-supplies-and-what-an-adapter-therefore-never-builds`](adapters.md#what-the-record-supplies-and-what-an-adapter-therefore-never-builds).
**Never:** —
**Not for:** an as-of read for replay of execution (reading history back is not re-running work —
[`failure_posture.md#refuse-resume-by-replay-where-actions-are-consent-gated`](failure_posture.md#refuse-resume-by-replay-where-actions-are-consent-gated));
an as-of read along event time for what was known (that is the look-ahead the ingestion axis exists to
prevent).

## Authority model (`authority_model.md`)

### authority
**Definition:** the right to take an [action](#action), expressed as `[principal](#principal) + domain + scope + action +
conditions + time`.
**See:** [`authority_model.md#the-tuple`](authority_model.md#the-tuple).
**Never:** —
**Not for:** "permission" alone for authority (a scope term); "access" for authority.

### principal
**Definition:** an actor, human or [agent](#agent), that [authority](#authority) is attributed to.
**See:** [`authority_model.md#principals`](authority_model.md#principals).
**Never:** —
**Not for:** owner for a principal unless ownership is meant; identity for a principal (the
credential, not the actor); user for a principal (the store's authenticated credential).

### credential
**Definition:** a binding from a login, key, address, or chat id to a [principal](#principal), many-to-one, and never the
principal itself.
**See:** [`authority_model.md#principals`](authority_model.md#principals).
**Never:** —
**Not for:** identity for the principal; account for a credential.

### operator
**Definition:** a human [principal](#principal) who directs [agents](#agent), recorded as an `operator`
entity whose only job is to be a principal.
The [authority](#authority) [edges](#edge) attach here: an `ownership_grant`, a [delegation](#delegation) endpoint, a [quorum](#quorum) seat,
a [separation-of-duties](#separation-of-duties) constraint. `operator_profile` is a **descriptive** record — identity details,
locale, preferences — and carries none of them. [Credentials](#credential) bind to the `operator`,
many-to-one, as they do to any principal.
**See:** [`authority_model.md#principals`](authority_model.md#principals).
**Never:** "admin".
**Not for:** user when authority is meant; `operator_profile` for the principal (it is the descriptive
record beside it).

### agent
**Definition:** a non-human [principal](#principal) defined by an `agent` entity and acting as a bound principal.
The entity type is `agent`: "definition" said nothing that "every entity is one" does not already say, and
the pair the model names is [operator](#operator) and agent — the human principal and the non-human one.
**See:** [`authority_model.md#principals`](authority_model.md#principals).
**Never:** "agent_definition".
**Not for:** worker for an agent (the process running an agent is a runner).

### tenant
**Definition:** the isolation boundary, an organization or a solo [operator](#operator), that no read, write, routing,
or key crosses.
**See:** [`authority_model.md#principals`](authority_model.md#principals).
**Never:** —
**Not for:** account for a tenant; "workspace" alone for a tenant.

### grant
**Definition:** an `agent_grant` holding the domain and scope a [principal](#principal) may act in, matched on its
[credential](#credential), as operation × entity types × repositories with parameter constraints and an expiry.
Zero grants is deny.
**See:** [`authority_model.md#grants`](authority_model.md#grants).
**Never:** —
**Not for:** permissions for a grant (a capability is one row of a grant); "allowlist" for a grant (one
enforcement of it).

### decision point
**Definition:** the function, the [action gate](#action-gate) or the grant checker, that returns `Permit`, `Deny`, or
`Indeterminate` for one request.
**See:** [`authority_model.md#the-tuple`](authority_model.md#the-tuple).
**Never:** "policy engine".
**Not for:** checker for a decision point, unqualified.

### enforcement point
**Definition:** a call site that acts on a [decision point](#decision-point)'s answer and treats `Indeterminate` as `Deny`.
**See:** [`authority_model.md#the-tuple`](authority_model.md#the-tuple).
**Never:** "advisory check", "passthrough".
**Not for:** —

### ownership
**Definition:** named accountability for a [workflow](#workflow), domain, queue, or configuration entity, carried as an
`ownership_grant` [edge](#edge) to a [principal](#principal).
**See:** [`authority_model.md#principals`](authority_model.md#principals).
**Never:** —
**Not for:** owner alone for the accountable principal.

### delegation
**Definition:** a scoped, time-bounded transfer of [action](#action) rights, recorded as a `delegation_edge` from
delegator to delegate, in which each hop holds a subset of the delegator's [authority](#authority).
Delegation is A acting for B and recorded as such; impersonation is A indistinguishable from B (RFC 8693).
**See:** [`authority_model.md#delegation`](authority_model.md#delegation).
**Never:** —
**Not for:** "impersonation" for delegation; "handover" for delegation without scope.

### authority_chain
**Definition:** the derived, never stored, read model over [delegation](#delegation) [edges](#edge), grants, and [checkpoints](#checkpoint) that
gives the path from a [principal](#principal) through each delegation hop to the approver for one [action](#action).
**See:** [`authority_model.md#delegation`](authority_model.md#delegation).
**Never:** —
**Not for:** "audit log" alone for the chain.

### approval
**Definition:** an explicit yes, no, or veto by a required [principal](#principal) on a [checkpoint](#checkpoint), ending in a terminal
state.
A timeout is a terminal state that never continues.
**See:** [`authority_model.md#approval`](authority_model.md#approval).
**Never:** "silent continuation".
**Not for:** resolved without who; sign-off for an approval (that closes a step).

### quorum
**Definition:** a structural check requiring m-of-n named [principals](#principal) on one [checkpoint](#checkpoint).
**See:** [`authority_model.md#structural-checks-quorum-and-separation-of-duties`](authority_model.md#structural-checks-quorum-and-separation-of-duties).
**Never:** —
**Not for:** "required reviewers" for a quorum (1-of-n is not a quorum); sign-off for a quorum.

### separation of duties
**Definition:** a structural check requiring disjointness between the roles on one [checkpoint](#checkpoint), such as
raiser and resolver or proposer and approver.
**See:** [`authority_model.md#structural-checks-quorum-and-separation-of-duties`](authority_model.md#structural-checks-quorum-and-separation-of-duties).
**Never:** —
**Not for:** "four eyes" for the check, unqualified; sign-off for the check.

### initiative
**Definition:** a proposed change to what the organization pursues.
**See:** [`authority_model.md#initiative-proposal-reprioritization`](authority_model.md#initiative-proposal-reprioritization).
**Never:** —
**Not for:** project or "epic" for an initiative.

### proposal
**Definition:** the ask that an initiative be accepted, made under proposal rights that are distinct from
execution rights.
**See:** [`authority_model.md#initiative-proposal-reprioritization`](authority_model.md#initiative-proposal-reprioritization).
**Never:** —
**Not for:** PR or RFC alone for a proposal.

### reprioritization
**Definition:** the explicit "what stops?" recorded when an initiative is accepted, confirmed by a
[principal](#principal).
**See:** [`authority_model.md#initiative-proposal-reprioritization`](authority_model.md#initiative-proposal-reprioritization).
**Never:** "priority bump", "re-plan".
**Not for:** —

## Failure posture (`failure_posture.md`)

### halt
**Definition:** the state in which the swarm does no work, while it keeps observing and announces itself
off-Neotoma.
Two causes, one state. It is **automatic** when the record is unreachable, since work with no record is
unaccountable work. It is **operator-invoked** on the [operator](#operator)'s word, at any time and for any
reason — and that one is confirmed stopped by a [read-back](#read-back) of the swarm's state, never by the command
returning, because a command that returns is a write reporting success (principle 2). In both, no
[task](#task) or [step](#step) is [claimed](#claim), no step opens, no [gate](#gate) decides, and nothing
is claimed complete; [watchdogs](#watchdog), forensic capture, and alerting stay live. Not a [checkpoint](#checkpoint):
a checkpoint is written to the record, and the automatic halt is the state in which nothing can be.
**See:** [`failure_posture.md#the-decision`](failure_posture.md#the-decision),
[`failure_posture.md#the-operator-invoked-halt-and-what-undoes-an-action-already-taken`](failure_posture.md#the-operator-invoked-halt-and-what-undoes-an-action-already-taken),
[`failure_posture.md#what-a-checkpoint-does-not-absorb`](failure_posture.md#what-a-checkpoint-does-not-absorb).
**Never:** "degraded mode", "fallback mode", "offline mode", "kill switch".
**Not for:** halt for a single blocked [step](#step) (that is the step staying open); halt for a
[recovery](#recovery) (a halt stops work, a recovery undoes an effect).

### recovery
**Definition:** the [action](#action) that undoes, or forward-fixes, an [action](#action) already taken.
Named per action class so the answer exists before it is needed: a merge is a revert; a publish is a
deprecate-and-supersede, since unpublishing is barred after a window; a release tag is a delete-and-retag;
a deploy is a rollback to the prior release. A recovery is itself an action through the
[action gate](#action-gate), recorded like any other — there is no privileged undo that bypasses the [gate](#gate).
**See:** [`failure_posture.md#the-operator-invoked-halt-and-what-undoes-an-action-already-taken`](failure_posture.md#the-operator-invoked-halt-and-what-undoes-an-action-already-taken).
**Never:** —
**Not for:** rollback as the general word for a recovery (it names the deploy class's recovery only, and
a merge's is a revert, a publish's a deprecate-and-supersede, a tag's a delete-and-retag); "undo" for a
recovery, which reads as a path outside the [action gate](#action-gate) and there is none; recovery for a
re-claim after a [lapsed](#lapsed) [lease](#lease) (no effect was taken); recovery for a restore drill
(that exercises a backup, it undoes nothing).

### reachability probe
**Definition:** one real read, at the moment a [task](#task) is [claimed](#claim), of what the work will read.
**See:** [`failure_posture.md#the-rules`](failure_posture.md#the-rules).
**Never:** "ping".
**Not for:** "health check" for the probe (`/health` can be green while every read hangs).

### read-back
**Definition:** the retrieval, after any write that carries a decision, that asserts the field holds the
value written.
**See:** [`principles.md`](principles.md#2-a-write-that-reports-success-has-not-necessarily-happened-read-it-back).
**Never:** —
**Not for:** treating a 2xx or `success: true` as evidence.

### unknown
**Definition:** the third state of any [gate](#gate), grant, drift, or reachability reader, meaning the value could
not be determined.
Never coerced to pending or to clear; at an [enforcement point](#enforcement-point) it resolves to deny.
**See:** [`failure_posture.md#the-rules`](failure_posture.md#the-rules).
**Never:** "legacy fail-open" (no such category exists).
**Not for:** pending or clear for a failed read.

### escalate
**Definition:** to raise a [checkpoint](#checkpoint) on a [task](#task) the swarm cannot advance, with the reason class that says
why.
The [watchdog](#watchdog) escalates on repeated lapse; the engine escalates on an unreadable [workflow](#workflow); a bounded loop
escalates when its rounds are exhausted; a [claim](#claim) predicate escalates on an `assigned_to` nobody can run.
One decision queue, one resolution protocol: a checkpoint on a task is resolved as a checkpoint on an
[action](#action) is (principle 6, do not build a second [gate](#gate)).
**See:** [`failure_posture.md#repeated-lapse-raises-a-checkpoint`](failure_posture.md#repeated-lapse-raises-a-checkpoint),
[`failure_posture.md#what-a-checkpoint-does-not-absorb`](failure_posture.md#what-a-checkpoint-does-not-absorb).
**Never:** escalation as a thing that is raised, written, or counted — as an entity, a record, a schema, or
an object, and as the code-font `escalation` type name. Escalate is the verb; the entity it writes is the
checkpoint.
**Not for:** page for a checkpoint (one delivery of it); alert for a checkpoint.

## Data model (`data_model.md`)

### edge
**Definition:** a typed, directed relationship between two entities in the record, carrying its own
timestamps and fields.
**See:** [`data_model.md#relationships`](data_model.md#relationships).
**Never:** —
**Not for:** link for an edge in schema text (a link is a URL); field for what an edge carries.

### derived read
**Definition:** a value computed from entities and [edges](#edge) at read time and never stored, such as `active`,
a [step state](#step-state), the chain, or a parent's completion.
**See:** [`data_model.md#concepts`](data_model.md#concepts).
**Never:** —
**Not for:** cached for a derived read; flag for a derived read.

### projection
**Definition:** a stored copy of a [derived read](#derived-read), kept where a decision must be taken from one entity read and
proved equal to its source by a reconciler, such as `step_status`.
**See:** [`data_model.md#concepts`](data_model.md#concepts).
**Never:** "hot path" (retired: the path had no property the projection kept for it does not state).
**Not for:** source of truth for a projection; history for a projection; cache for a projection; a
projection for a [fast path](#fast-path) (a declared skip of steps).

## Conformance (`conformance.md`)

### kernel document
**Definition:** a foundation document read on every review.
**See:** [`conformance.md#always-read`](conformance.md#always-read).
**Never:** "core docs", "the P1 docs".
**Not for:** —

### keyed document
**Definition:** a foundation document read when a changed path matches its key.
Each header says which kind it is.
**See:** [`conformance.md#read-when-these-paths-changed`](conformance.md#read-when-these-paths-changed).
**Never:** "optional docs", "secondary docs".
**Not for:** —

### design basis
**Definition:** the foundation document and section an [issue](#issue) or PR conforms to, or the statement `no
design applies` with a reason, checked mechanically and judged by reading.
**See:** [`conformance.md#design-basis`](conformance.md#design-basis).
**Never:** —
**Not for:** reference or "see also" for a design basis.

### status
**Definition:** the dated measurement of the gap between the foundation and a checkout, held in
`status.md` and regenerated rather than maintained.
**Allowed:** naming `status.md` as the state home (for example, "what is built is `status.md`").
**See:** [`conformance.md#phases-and-implementation-state`](conformance.md#phases-and-implementation-state).
**Never:** —
**Not for:** embedding dated figures, counts, or checkout claims from it into a foundation document;
treating it as design evidence.

## Verbs

Each subject has its verb. The subject of a movement verb is a [task](#task) or a [batch](#batch), never the batch record:
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
| a step, on a condition its owner cannot yet judge | **holds**, under a held lease, with a finding naming the condition; the hold **ends** by sign-off, checkpoint, or lapse | "is paused", "is waiting", "is blocked" (blocked is a task status) |
| a batch, on closing | its tasks **enter** one successor, or the batch closes with none | "flows into", "triggers" the next workflow |

## Owner: five meanings, one word forbidden alone

`owner` on its own is forbidden. Sources use it for five things (C10); each has its own term:

| Meaning | Term | Field |
|---|---|---|
| the role the roster resolves to the principal whose sign-off closes a step | **step owner** | `workflow.steps[].owner_role` |
| the step a batch is at | **current step** | derived from the batch's step states; projected as `current_owner` |
| the implementer a blocking finding's remedy is routed to | **routed agent** | none; the remedy is a task entering intake, and the step owner keeps the sign-off (`gates_and_workflows.md#findings-verdicts-and-what-a-blocking-finding-obliges`) |
| the operator with the book of business for a customer | **book-of-business owner** | `multi_tenant.md` section 5 |
| named accountability for a workflow, domain, or queue | **ownership** (above) | `ownership_grant` |

The [principal](#principal) holding a [task](#task)'s [lease](#lease) is its lease holder, never its owner; the principal an
assignment names holds no lease until it [claims](#claim).

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
| `recurring series` | [action series](#action-series) | "recurring" restated what a series already is, and named nothing about the members; the series is made of actions of one class, and that is what graduates |
| `lens` | [review step](#review-step), whose owner is a [step owner](#step-owner) | on owner, sequence, verdict and blocking a lens was identical to a step; a second term for one thing (principle 9) |
| `review panel` | the [review steps](#review-step) a [workflow](#workflow) declares | the set of steps a workflow declares is the panel; naming it separately implied a second sequencing mechanism beside the declaration |
| `reaper` | nothing | a lapsed lease already does not count; there is nothing to release |
| `executing`, `running` (as states) | [active](#active) (derived) | a stored liveness flag fails when the process that would clear it dies |
| `claimant` | lease holder ([lease](#lease)) | the claim and the lease are one primitive, so the principal that claimed is the principal the lease names; a second noun for the same principal (principle 9) |
| `workflow policy` | the [step owners](#step-owner) a [workflow](#workflow) declares, with the [grants](#grant) in force | a collective name for two mechanisms that already answer who may claim a step; no entity, field, or rule carried the name |
| `hot path` | [projection](#projection) | it named the reason a projection exists, and the projection's definition already states it |
| `operator_preview` (a step name) | `consent` | three step names — `operator_preview`, `consent`, `present` — for the step that carries the gate's checkpoint to the operator; the two whose work is identical now share the name |
| `calendar_routing_config` (a binding type) | `channel_config` | `adapters.md#scope` names the per-instance binding types once; a third name for the same binding was a second home |
