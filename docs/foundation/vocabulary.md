# Vocabulary: canonical terms

**Keyed document:** read when a skill, an [agent](#agent) document, or the agent-doc renderer changes
(`conformance.md`). **Kind:** foundation; defines terms by what they are in the design, never by what a
checkout implements. **Derived from:** synthesis `ent_b0ce322f768e4fc676b73139` (PR-03, PR-08, C10), prior
art `ent_08460968e6f49dac21510f4a` (A2A `TaskState`, RFC 8693, Camunda), [task](#task)
`ent_da60df3beccb675ef8c8c0c5`, the ateles#378 glossary ([operator](#operator) section, and the ux-signed swarm section
cited as [proposal](#proposal)), `docs/multi_tenant.md` section 5, PR #745 operator review (2026-09-04),
and the operator memos of 2026-09-05 (the standing axis on a [finding](#finding)), and the operator's 2026-09-05 terminology review (revision 17: the one boundary and the term `external system`, the `action series` rename, `subject` defined, and the two-part `checkpoint`), and the operator's 2026-09-05 review of review relevance (revision 19: the `applies_when` condition on an optional [step](#step), and two terms retired in favour of `review step`), and PR #745 operator review (2026-09-05, rulings 13–14,
16–18, 23–29: the hold verb, a condition a step holds on, the `dependency_cycle` [reason class](#reason-class), the consent
tolerance on `action_policy`, and an [artifact](#artifact) `PART_OF` its containing artifact), and the operator's 2026-09-05 22:02–22:13 memos on how tasks come into existence (revision 30, 2026-09-06: the `intake rule` entry). Format
follows Neotoma's `docs/vocabulary/canonical_terms.md`. Revised by the simplification pass of 2026-09-05 (revision 29: `claimant`, `workflow policy`, and `hot path` retired; the [checkpoint](#checkpoint) reason classes cited from their one home; a code-era field removed from the Owner table). Revised by the memo-gap pass of 2026-09-06 (revision 31: the finding's `unknown` scope; what an `agent_session` is not for). Revised by the workflow-format pass of 2026-09-06 (revision 34: the two intervals on the step entry; where the set of `action_type` values lives). Revised by the consistency pass of 2026-09-06 (revision 35: the [operator-facing agent](#operator-facing-agent) defined by [role](#role); `merge` as an action-class name retired for `merge_pr`). Revised by the second workflow-format pass of 2026-09-06 (revision 36: a bound as the task's `due_date` on the step entry; the `operator_only` step; a rule keying on a field a step wrote; no marker on a read for a special-category type; decision 55 on the [external-system](#external-system) entry). Revised by the testability pass of 2026-09-06 (revision 37: `blocked` retired as a task status; the terminal set declared on the type; the `finding` and `sign_off` fields; `rounds_cap` and `none_permitted`). Revised by the rulings pass of 2026-09-06 (revision 38: the [verdict](#verdict) as the [sign-off](#sign-off)'s reconciled [projection](#projection); a `signed` or blocking sign-off written under a held [lease](#lease); what owning confers; the counting rule and the thresholds' home on the [quorum](#quorum) and separation entries; an initiative as a task by class; the host as an external system; a budget as a grant's and a [delegation](#delegation)'s scope term). Revised by the second rulings pass of 2026-09-06 (revision 39: the raiser never the resolver save the operator's marked self-resolution, on the [approval](#approval) entry; the right to propose as a grant capability and what stops as a task, on the proposal and [reprioritization](#reprioritization) entries; metered resources and the engine's sole governance grant, on the [grant](#grant) entry; a rule naming a work-model type refused at the write). Revised by the planning pass of 2026-09-06 (revision 40: the planning-model section — [planning record](#planning-record), [planning level](#planning-level), [ascent](#ascent), [unplanned](#unplanned), [planning decision](#planning-decision), [planner](#planner), and the [amend](#amend-a-planning-record) verb). Revised by the record-sense pass of 2026-09-06 (revision 41: the [artifact](#artifact) definition and five other sentences that read "record" against an external system rephrased to "an entry an external system holds," closing the [record](#record) Not-for collision; the Not-for scoped to a Never for `docs/foundation/adapters.md` and the five per-system [adapter](#adapter) documents). Revised by the Human Inversion mapping pass of 2026-09-06 (revision 44: two term collisions with a public essay series disambiguated — [reconciler](#reconciler) as this design's projection-parity check, never the essay's adjudicating role, and [replay](#replay) as the essay's [as-of-read](#as-of-read) sense, never this design's refused re-execution sense). Revised by the priority pass of 2026-09-06 (revision 47: the [`priority`](#priority) entry added, giving the term a home beside [`claimable`](#claimable) as a [derived read](#derived-read) a [principal](#principal) consults and is never bound to obey). Revised by the rulings pass of 2026-09-06 (revision 48: decision 34 ruled — the [pipeline](#retired-names) entry replaced by [engine](#engine), defined once; decision 33 ruled — the [stage](#stage) entry rewritten to authored prose only, with no `steps[].phase` field). Revised by the event/signal/delivery pass of 2026-09-06 (revision 49: the [event](#event) entry added — the delivery's payload, distinct from the [delivery](#delivery) that carried it and the [signal](#signal) the adapter reads it into — and the [delivery](#delivery) and [signal](#signal) entries cross-referenced against it; the operator's question of whether events, signals, and deliveries are properly distinguished). Revised by the transport-and-delivery pass of 2026-09-06 (revision 50: [mapping](#mapping), [receiver](#receiver), [signature](#signature), and [idempotency key](#idempotency-key) entries added; [effect dedup](#effect-dedup) tightened to name the key by reference; [cursor](#freshness) and capability left as prose on the [grant](#grant) entry, the latter checked for collision and found none). Revised by the operator's 2026-09-06 terminology review of role, domain, and scope (revision 52: the [role](#role) entry added, beside [step owner](#step-owner); the [domain](#domain) and [permission scope](#permission-scope) entries added for the [authority](#authority) tuple's second and third terms; [finding scope](#finding-scope) and [waiver scope](#waiver-scope) added as qualified compounds rather than one entry for bare `scope`'s four senses, with bare scope left to the author and the `## Scope` heading untouched). Revised by the adversarial term audit of 2026-09-06 (revision 53: the [governance write](#governance-write) entry added — cited by name across six documents with no entry of its own, its definition already stated verbatim in `gates_and_workflows.md`; ten Not-for/Never violations fixed by unambiguous rephrasing, the `record`-for-external-system pattern found again in three new sites and a stale `steps[].phase` field removed from `data_model.md`). Revised by the checker-mechanism pass of 2026-09-06 (revision 54: `check_foundation_vocabulary.py`'s `record` ban rebuilt as a structural, corpus-wide check — a bound term possessed by, or governed by a preposition or relative clause pointing at, a foreign-system noun — after the literal-phrase, six-document-scoped version missed two more rounds of paraphrase; 28 further Not-for/Never violations this rebuilt check found across `adapters.md`, `calendar.md`, `conformance.md`, `data_model.md`, `gmail.md`, `migration.md`, `payments.md`, `work_model.md`, and `workflows.md` fixed by the same unambiguous rephrasing; one `adapters.md` heading renamed with its four cross-references, its anchor confirmed unused outside `docs/foundation`; `as_read` extended to strip `**bold**`/`*italic*` emphasis, closing a second escape the fix found live in `data_model.md`). Revised by the peering pass of 2026-09-06 (revision 56, rebased onto the checker-mechanism and self-awareness passes: decision 55 ruled on the [external system](#external-system) entry — a second instance of the record is not one; the [governance write](#governance-write) entry's admission sentence extended to name a synced write's own, unattributed shape). Revised by the undefined-term pass of 2026-09-06 (revision 60: [reason class](#reason-class) added as the category entry a reader meeting `repeated_lapse` had no way to resolve, with [`repeated_lapse`](#repeated_lapse), [`rounds_exhausted`](#rounds_exhausted), [`unreadable_workflow`](#unreadable_workflow), [`capability_denied`](#capability_denied), and [`capability_unavailable`](#capability_unavailable) defined beneath it; [principal binding](#principal-binding) and [session_digest](#session_digest) added, each carrying a rule no existing term could hold — the counting rule and the agent sign-off's required reference; `lapse_cap`, `min_tier`, `confidence_threshold`, `external_api_write`, and `verify_deployed` deliberately NOT given entries, each named instead on the entry that already owns its set — [`action_policy`](#action_policy), [`action_type`](#action_type), and [`step`](#step) — since a value is not a term (principle 9); the drop reason named on the [dropped](#dropped) entry as the [adapter](#adapter)'s, not the design's; six substrate field names recorded in `migration.md#substrate-field-names-the-design-reads-and-never-adopts-as-terms` rather than promoted).

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

**Bare "scope" is not one of these eighteen and carries no entry of its own.** The corpus uses it for at
least four separate relations — a [finding](#finding-scope)'s reach, a [waiver](#waiver-scope)'s reach, a
[grant](#permission-scope)'s permitted operations, and this file's own `## Scope` section heading — and no
fifth, unifying sense exists to define. Where one of the first three is meant, name the qualified compound;
where the heading is meant, nothing is bound at all. A bare "scope" in prose that names none of the three
is the ordinary English word and is left alone, the same as the eighteen.

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
**Definition:** an entry an [external system](#external-system) holds, reached only through that system's
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
[`adapters.md#an-artifact-exists-only-once-its-external-systems-entry-does-and-the-interval-before-that-belongs-to-the-action`](adapters.md#an-artifact-exists-only-once-its-external-systems-entry-does-and-the-interval-before-that-belongs-to-the-action),
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
from a task field; it is the only [role](#role) the lease has, and it needs no term of its own. A held lease is also
what makes a [sign-off](#sign-off) the current step owner's: a `signed` or blocking sign-off from a lease not
held is refused at the write (decision 44).
**See:** [`work_model.md#the-lease-is-a-relationship-not-a-set-of-task-fields`](work_model.md#the-lease-is-a-relationship-not-a-set-of-task-fields),
[`conformance_suite.md#whether-a-sign-off-from-a-step-owner-whose-lease-has-lapsed-closes-the-step`](conformance_suite.md#whether-a-sign-off-from-a-step-owner-whose-lease-has-lapsed-closes-the-step).
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
**Definition:** the derived property of a [task](#task) whose status is not terminal, on which no [lease](#lease) is
held, which no open [checkpoint](#checkpoint) holds from claim (every task-subject [reason class](#reason-class) but
`unclaimed_step` — `failure_posture.md#checkpoints-on-tasks-one-queue-one-protocol`), and whose
`assigned_to` is unset or names the [principal](#principal) about to [claim](#claim).
**See:** [`work_model.md#what-a-claim-predicate-treats-as-claimable`](work_model.md#what-a-claim-predicate-treats-as-claimable).
**Never:** —
**Not for:** "available" for claimable; open for claimable, whether as an open task, an open pool, or a
task said to be open — `open` is a status value and means something else.

### terminal
**Definition:** a status value after which a [task](#task), a [batch](#batch), or a [checkpoint](#checkpoint) changes no further.
A task's terminal values are the set the registered `task` type declares, one spelling per meaning; a writer
writes from it, and a reader tolerates every spelling the record carries.
**See:** [`work_model.md#what-a-claim-predicate-treats-as-claimable`](work_model.md#what-a-claim-predicate-treats-as-claimable).
**Never:** —
**Not for:** "final" for terminal.

### priority
**Definition:** the ordering of the [claimable](#claimable) pool a [principal](#principal) reads at [claim](#claim) time,
derived from the [ascent](#ascent) (a higher-level record's declared standing), the [task](#task)'s own `due_date`,
the [workflow](#workflow)'s own declared urgency, and [blast radius](#blast-radius) — never a value a principal is obliged to obey.
Priority orders what a claim predicate presents; it does not narrow what the predicate permits, and a
principal may still decline a high-priority task it is not fit for as any other claimable task is declined.
The task carries a `priority` field, written once at [intake](#intake)'s `prioritize` from the `priority_rubric` entity
(`workflows.md#intake`) and occasionally corrected as a `reprioritization`'s stop
(`authority_model.md#what-stops-is-a-task-the-owner-seat-confirms-it-through-the-checkpoint-and-proposing-is-a-grant-capability`);
the claim-time ordering this entry names is a separate, [derived read](#derived-read) over the current ascent, consulted
where the two might disagree, for the reason principle 11, "state that needs a [watchdog](#watchdog) belongs
in a relationship, not a field" (`principles.md#11-state-that-needs-a-watchdog-belongs-in-a-relationship-not-a-field`), already gives.
**See:** [`work_model.md#priority-orders-the-claimable-pool-it-does-not-enter-it`](work_model.md#priority-orders-the-claimable-pool-it-does-not-enter-it).
**Never:** a router that chooses a principal's next task on priority alone; a principal blocked from declining
a high-priority task it judges unfit; a maintained `priority` recomputed by a sweeper on every ancestor
change.
**Not for:** the general cross-disciplinary rubric Part 4 of the Human Inversion series describes
(`principles.md#where-the-human-sits-what-it-protects-and-why-the-record-is-owned`) — `priority_rubric` is a narrow
policy input this entry's derived read consults, not that object; [`reprioritization`](#reprioritization)
for the governance-scale act of displacing another task's priority through a checkpoint.

### runner
**Definition:** the process that runs an [agent](#agent) and holds a [lease](#lease) on the agent's behalf, identified by a
runner id the persisted lease names.
**See:** [`work_model.md#the-claim-and-the-lease-are-one-primitive`](work_model.md#the-claim-and-the-lease-are-one-primitive).
**Never:** "worker", "bot".
**Not for:** agent when the process is meant.

### agent_session
**Definition:** the identity half of a [runner](#runner)'s work that [observations](#observation) lack, such as host, checkout,
branch, and head, related to the [task](#task) it executes.
**See:** [`work_model.md#no-assignment-log-history-is-the-tasks-own-observations`](work_model.md#no-assignment-log-history-is-the-tasks-own-observations),
[`gates_and_workflows.md#what-a-step-leaves-at-close-what-it-produced-and-a-reference-to-what-it-read`](gates_and_workflows.md#what-a-step-leaves-at-close-what-it-produced-and-a-reference-to-what-it-read).
**Never:** "run history", "dispatch record".
**Not for:** the session's transcript or reasoning as a field on it; a copy of what the step read (the sign-off names it, and an as-of read returns it).

### session_digest
**Definition:** the summarized content of one [runner](#runner)'s session — what the session covered and
concluded — as distinct from the [`agent_session`](#agent_session) that names where it ran and the raw turn
store that holds every message.
It carries one rule: **an [agent](#agent)'s [sign-off](#sign-off) `REFERS_TO` the digest of the session
that produced it**, required where a digest exists for that session and permitted otherwise, so a reader
can resolve what the signer was working from as of `signed_at`. Never the `agent_session` (which carries no
content) and never the raw turns (the wrong grain for a reference resolved as of a time). A registered type
whose rows may carry a third party's special-category content, so the mark applies at the type.
**See:** [`gates_and_workflows.md#what-a-step-leaves-at-close-what-it-produced-and-a-reference-to-what-it-read`](gates_and_workflows.md#what-a-step-leaves-at-close-what-it-produced-and-a-reference-to-what-it-read),
[`data_model.md#record-conventions`](data_model.md#record-conventions).
**Never:** "session summary" as a second name for it.
**Not for:** the [`agent_session`](#agent_session) for the digest (the first is identity, the second is
content); a transcript for a digest (a digest summarizes; the turns are `conversation_message` rows and are
not what a sign-off references).

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
A rule keys on no record of the work model (decision 36; one naming a work-model type is refused at the write) but may key on a field a [step](#step) wrote on
a type it may name — a classification recorded on an [artifact](#artifact) — with the writer in its provenance
predicate; it opens no [batch](#batch), names no
[workflow](#workflow), and takes no [action](#action); writing one is a [governance write](#governance-write), reserved to the
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
**Definition:** the [agent](#agent) the roster resolves to the operator-facing [role](#role) — bound per project in
`swarm_roster`, never named in the design — which [claims](#claim) operator-only [tasks](#task),
carries them and their [checkpoints](#checkpoint) to the [operator](#operator), and records the outcome.
**See:** [`work_model.md#operator-only-tasks-are-claimed-by-the-operator-facing-agent`](work_model.md#operator-only-tasks-are-claimed-by-the-operator-facing-agent).
**Never:** —
**Not for:** the operator for the agent; concierge for the agent, unqualified.

### daemon
**Definition:** a long-lived process that self-triggers on its own loop, without receiving a task, and takes
no [action](#action) of its own: it writes the [tasks](#task) its poll produces and [observations](#observation),
and nothing else — the announcement path of last resort
([`failure_posture.md#the-rules`](failure_posture.md#the-rules), rule 2) is the sole exception.
**See:** [`work_model.md#the-four-execution-mechanisms`](work_model.md#the-four-execution-mechanisms);
[`work_model.md#contradictions-this-document-settles`](work_model.md#contradictions-this-document-settles) (C2).
**Never:** an action `PRODUCES` from no task.
**Not for:** service for a daemon, unqualified.

### engine
**Definition:** the execution mechanism that opens each [step](#step) of a [workflow](#workflow) for a [batch](#batch) as
[claimable](#claimable) step work, which the [step owner](#step-owner) [claims](#claim), and reads the
[sign-offs](#sign-off) that close them; it never writes a [task](#task) status.
It delivers nothing; it is the same pull, over steps. Decision 34
(`work_model.md#whether-the-step-path-is-a-mechanism-of-its-own-and-what-the-engine-is-called`) is where
`pipeline` was retired for this sense: "GitHub-hosted" named a fact about the checkout, not a design
property, and this is the name used throughout `gates_and_workflows.md`, `adapters.md`, and `data_model.md`
for the same component, now defined once.
**See:** [`work_model.md#the-four-execution-mechanisms`](work_model.md#the-four-execution-mechanisms).
**Never:** "pipeline" for this sense (retired: see [Retired names](#retired-names)).
**Not for:** workflow for the engine (the declaration); CI for the engine (one of its checks).

### interactive session
**Definition:** the execution mechanism in which an [operator](#operator) works directly with an
[agent](#agent): a work **source** whose output becomes [tasks](#task), holding no [lease](#lease) and
receiving no task.
Because it holds no lease, none of the lease-borne [recovery](#recovery) reaches it — nothing lapses when a session
dies, and there is no task to make [claimable](#claimable) again. Work an interrupted session left
unfinished is recovered by **digestion**, reading the session back and filing what it left, which is a
declared [workflow](#workflow) with an owning [role](#role) rather than an emergent practice.
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
**Not for:** engine for a workflow (one engine that runs workflows); "template" for a workflow.

### step
**Definition:** one declared position in a [workflow](#workflow)'s ordered list, carrying a name, a [step owner](#step-owner), a
`required` flag, an `on_fail` target with its `rounds_cap`, its [read dependencies](#read-dependency), two intervals
(`unclaimed_after`, `hold_bound`, each an interval or the [task](#task)'s `due_date` —
`gates_and_workflows.md#declaration-batch-projection`), and
parallel-group and join fields, [claimed](#claim) by its step owner on a [batch](#batch) and closed by that
step owner's
[sign-off](#sign-off).
Step names are data (`pm`, `ux`, `arch`, `impl`, `pr_review`, `qa`, `legal`, `release`,
`verify_deployed`, and any a [workflow](#workflow) declares), and no name carries an entry of its own: each is
declared on one workflow, its [step owner](#step-owner)'s [role](#role) is tabled at
`workflows.md#roles-named-in-this-document`, and the reason the [workflow](#workflow) separates it from its
neighbours is argued in that workflow's own section — three homes a per-name entry would duplicate
(principle 9). Where a name's separation is the point, the section says so: `verify_deployed` is a step
apart from `release` because released and landed are different claims (`principles.md`, invariant 10), and
a [batch](#batch) that closed on the release [action](#action)'s success would record the first as the
second.
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
[agent](#agent)'s information diet, declared on the agent and not on the step); a marker on the read for a
type marked special-category (the mark is the type's — `data_model.md#record-conventions`).

### stage
**Definition:** authored prose only — a named group of contiguous [steps](#step) in a [workflow](#workflow)'s
**Stages** line, such as the review stage or the release stage. No `steps[].phase` field exists (decision
33, `workflows.md#whether-a-stage-names-anything-a-step-does-not`): a stage names a reporting grain no [gate](#gate),
[verdict](#verdict), [fast path](#fast-path), `applies_when`, [successor](#successor), or [checkpoint](#checkpoint) keys on, and where a [batch](#batch) is is its current
[step](#step).
**See:** [`workflows.md#whether-a-stage-names-anything-a-step-does-not`](workflows.md#whether-a-stage-names-anything-a-step-does-not).
**Never:** a `phase` field on a declared step.
**Not for:** stage for a single step; phase when a group of steps is meant.

### step owner
**Definition:** the **role** declared on a [step](#step), which the roster resolves to a [principal](#principal) at [claim](#claim) time;
that principal claims the step on a [batch](#batch) and its [sign-off](#sign-off) closes it. The declaration names a [role](#role) so that
one [workflow](#workflow) serves every project and a renamed or replaced [agent](#agent) leaves no stale name in it; the
resolution to a principal happens when the step is claimed, against `swarm_roster` for the batch's
project, and a step whose role resolves to no principal raises a [checkpoint](#checkpoint) (reason
`unspawnable_assignee`) rather than falling through to any available agent.
**Field:** `workflow.steps[].owner_role` (the design's name; the field is `owner_agent` in the built
declarations and holds a role there too — `status.md`).
**See:** [`gates_and_workflows.md#two-questions-who-may-claim-a-step-and-whether-an-action-may-be-taken`](gates_and_workflows.md#two-questions-who-may-claim-a-step-and-whether-an-action-may-be-taken).
**Never:** —
**Not for:** owner alone.

### role
**Definition:** a name a declaration carries in place of a [principal](#principal), resolved to one at the moment the
declaration is acted on — `swarm_roster` per project, and once for the levels above a project
(`vocabulary.md#planner`) — so that one declaration serves every project and a renamed or replaced
[agent](#agent) leaves no stale name in it.
[Step owner](#step-owner) is a role, resolved when a [step](#step) is [claimed](#claim); an [approval](#approval)'s required
principal may also be a role the roster resolves, carried in `needed_input` because an `AWAITS`
[edge](#edge)'s target is a principal and never a role (`authority_model.md#approval`); a role that
resolves to no principal raises a [checkpoint](#checkpoint) (reason `unspawnable_assignee`) rather than
falling through to any available agent. A role is declared once and reused; it is not created per [batch](#batch) or
per checkpoint.
**See:** [`gates_and_workflows.md#declaration-batch-projection`](gates_and_workflows.md#declaration-batch-projection),
[`authority_model.md#approval`](authority_model.md#approval).
**Never:** —
**Not for:** an [agent](#agent) for a role (the role is the name; the roster resolves it to an agent, and a
renamed agent must not touch the declaration — the confusion the corpus makes most often); a [grant](#grant)
or capability for a role (a role names who is asked or who claims; a grant names what a principal may do,
and the two are checked separately —
[`authority_model.md#initiative-proposal-reprioritization`](authority_model.md#initiative-proposal-reprioritization)
states this for proposal rights: "standing to propose is a capability and not a role"); the required seat an
[ownership](#ownership) [edge](#edge) names for a role (ownership resolves straight to the principal the
`ownership_grant` points at; nothing is looked up by name).

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
**[Waiver scope](#waiver-scope):** one [batch](#batch)'s unsigned required steps, one `waived` sign-off **per step**, each
naming its step and carrying its reason — so a waived step is queryable as waived rather than recorded as a
batch-level flag or as prose on an artifact.
**Terminal, and never revised in place:** a later judgement is a new sign-off, and the latest per step
owner per artifact head is the one that stands; the superseded one stays readable.
**Under a held lease:** a sign-off carrying `signed` or a blocking verdict is written by a signer whose
[lease](#lease) on the step is held at the write; one from a [lapsed](#lapsed) or [returned](#returned) lease, or from a [runner](#runner) that
does not hold it, is refused at submission, and the current lease holder's stands. `waived` is the operator
principal's and needs none (decision 44,
[`conformance_suite.md#whether-a-sign-off-from-a-step-owner-whose-lease-has-lapsed-closes-the-step`](conformance_suite.md#whether-a-sign-off-from-a-step-owner-whose-lease-has-lapsed-closes-the-step)).
**Evidence:** a blocking verdict names the executed check and the output it produced, or the mechanism
that executed it; unexecuted reasoning is a non-blocking [finding](#finding), never a block
([`gates_and_workflows.md#findings-verdicts-and-what-a-blocking-finding-obliges`](gates_and_workflows.md#findings-verdicts-and-what-a-blocking-finding-obliges)).
**Field:** `sign_off`; its findings are `finding` entities `PART_OF` it, `SIGNED_BY` names the principal —
the step owner's agent, or the operator on `waived` — `artifact_refs[]` carries each artifact's pinned
state by kind, and `tasks_attached[]` names the [tasks](#task) it attached part-way (`data_model.md#concepts`).
**See:** [`gates_and_workflows.md#declaration-batch-projection`](gates_and_workflows.md#declaration-batch-projection),
[`adapters.md#no-external-event-advances-a-step-by-itself`](adapters.md#no-external-event-advances-a-step-by-itself).
**Never:** "participation_record", "step_run", "LGTM", "audit row".
**Not for:** approval for a sign-off (an approval is on a [checkpoint](#checkpoint)); "green" without the
record.

### waiver scope
**Definition:** the reach of one `waived` [sign-off](#sign-off) — exactly one [batch](#batch)'s one
unsigned required [step](#step) — never a whole batch's every unsigned step at once and never a standing
exemption carried past the batch that needed it.
A batch with more than one unsigned required step needing a waiver takes one `waived` sign-off per step,
each naming its own step and reason, so a waived step is queryable as waived rather than recorded as a
batch-level flag or as prose on an [artifact](#artifact).
Not a [domain](#domain) or [permission scope](#permission-scope): a waiver scope names which step one
[operator](#operator) decision covers, never what a [principal](#principal) may act on or do.
**See:** [`gates_and_workflows.md#declaration-batch-projection`](gates_and_workflows.md#declaration-batch-projection).
**Never:** —
**Not for:** a waiver scope wider than one step; a waiver scope for the workflow or the agent (that reach
belongs to a standing [finding](#finding), not to a waiver); [permission scope](#permission-scope) or
[finding scope](#finding-scope) for a waiver's reach.

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
input on reviewed work is a finding and is judged on both axes. The [finding scope](#finding-scope) a
finding lands on is one of four, narrowest first — the batch (one-off), the step, the workflow, the agent —
or `unknown`, which raises a [checkpoint](#checkpoint) (reason `undetermined_scope`) and is never coerced to
one-off.
**Field:** `finding` — an entity of its own, `PART_OF` the sign-off that carries it and `REFERS_TO` the batch
it judges; a hold's finding stands with no sign-off while the hold does; its severity, kind, scope, and
evidence are the fields the rules bind on (`data_model.md#concepts`).
**See:** [`gates_and_workflows.md#findings-verdicts-and-what-a-blocking-finding-obliges`](gates_and_workflows.md#findings-verdicts-and-what-a-blocking-finding-obliges),
[`gates_and_workflows.md#a-finding-is-one-off-or-standing-and-a-standing-one-obliges-a-change-to-what-produced-it`](gates_and_workflows.md#a-finding-is-one-off-or-standing-and-a-standing-one-obliges-a-change-to-what-produced-it).
**Never:** —
**Not for:** a finding as the thing that closes a step (the [sign-off](#sign-off) closes it); a comment on
an [artifact](#artifact) as a finding (a remark carries no severity and reaches no step); a blocking
finding that names no executed check; a standing finding discharged by correcting only the work it was
filed against.

### finding scope
**Definition:** which of a defect's four possible reaches a [finding](#finding) names — narrowest first, the
[batch](#batch) (a one-off finding's), the [step](#step), the [workflow](#workflow), or the [agent](#agent)
(a standing finding's) — carried on the `finding` entity's `scope` field, or `unknown`, which raises a
[checkpoint](#checkpoint) (reason `undetermined_scope`) rather than being coerced to the batch.
Not a [domain](#domain) or a [permission scope](#permission-scope): a finding scope names what a defect is
wrong about, never what a [principal](#principal) may act on or do.
**Field:** `finding.scope`.
**See:** [`gates_and_workflows.md#a-finding-is-one-off-or-standing-and-a-standing-one-obliges-a-change-to-what-produced-it`](gates_and_workflows.md#a-finding-is-one-off-or-standing-and-a-standing-one-obliges-a-change-to-what-produced-it).
**Never:** —
**Not for:** [permission scope](#permission-scope) or [waiver scope](#waiver-scope) for a finding's scope;
a finding scope coerced to the batch when it cannot be determined (that is `undetermined_scope`, held for
the operator).

### verdict
**Definition:** the summary a [sign-off](#sign-off) carries, stating whether the [step](#step)'s
[condition](#condition) is met: `signed`, a blocking value, or `waived`.
Those three are the only values, and a host's own review tokens are the [adapter](#adapter)'s [inbound](#inbound)
[mapping](#mapping) onto them, never the record's vocabulary.
**Against its findings:** the [findings](#finding) bind. A verdict must agree with the findings its
[sign-off](#sign-off) carries, and a write whose verdict contradicts them — a blocking finding under a
non-blocking verdict — is **rejected at submission**, never swallowed; the step stays open until the step
owner re-submits.
**Stored, as a projection:** the verdict is a field of the sign-off, kept as its own [projection](#projection)
of its findings and its author and reconciled at the write by that refusal; under a derivation a sign-off with
no finding would read `signed`, and silence is not a [claim](#claim) (decision 32,
[`gates_and_workflows.md#whether-the-verdict-is-a-stored-field-or-a-read-over-the-findings-and-the-author`](gates_and_workflows.md#whether-the-verdict-is-a-stored-field-or-a-read-over-the-findings-and-the-author)).
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
[sign-offs](#sign-off) and proved equal to them by a [reconciler](#reconciler).
Written as the field the record names, in code font, because that is what a reader queries; the state it
projects is the spaced concept step state.
**See:** [`gates_and_workflows.md#declaration-batch-projection`](gates_and_workflows.md#declaration-batch-projection).
**Never:** "gate_status".
**Not for:** history for the projection; a second source of truth; `step_status` as the name of the
concept.

### reconciler
**Definition:** in this design, the mechanical check that proves a stored [projection](#projection) —
chiefly [step_status](#step_status) — agrees with the [sign-offs](#sign-off) it is derived from; not a [role](#role)
and not a [principal](#principal).
**Not for:** the sense of a public essay series (*The Human Inversion*, Part 4: a person, "often the most
senior cross-functional person on the team or the founder," who adjudicates cross-disciplinary tension
against a rubric). This design distributes that function rather than naming a role for it: the
[operator](#operator) is the required seat on any [checkpoint](#checkpoint) whose subject concerns an
object it owns (decision 46); the `arch` [review step](#review-step) checks a change against its cited
design basis; an unresolvable scope is put to the operator rather than guessed (`undetermined_scope`);
and the README states the divergence directly — "Reconciliation is a topology of domain owners, approvers,
and quorums, not a single human bottleneck." Where the essay's sense is meant, "the operator" or "the
owning principal" is this design's term, never "reconciler."
**See:** [`authority_model.md#what-owning-confers-the-required-seat`](authority_model.md#what-owning-confers-the-required-seat).
**Never:** —
**Not for:** the essay's sense above; a stored role or principal type (none exists).

### fast path
**Definition:** a declared skip of [steps](#step) that a [workflow](#workflow) permits for a named class of [tasks](#task).
**See:** [`gates_and_workflows.md#declaration-batch-projection`](gates_and_workflows.md#declaration-batch-projection).
**Never:** "shortcut".
**Not for:** a [projection](#projection) for a fast path (one is a stored read, the other a declared skip).

### successor
**Definition:** a [workflow](#workflow) that a `workflow` declares in `successors` as one a [batch](#batch) of it may enter on
closing, of which the closing [sign-off](#sign-off) selects exactly one, or none where the declaration
permits it (`none_permitted`).
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
always-checkpoint boundaries, the [permission scope](#permission-scope), and the consent tolerance per action class — the change
to an action's consented figures that may be taken without a new [checkpoint](#checkpoint), zero where the
policy declares none (`payments.md#tolerance-is-an-action_policy-value-and-its-default-is-zero`).
**Its fields are values, not terms.** The policy's every field is named and defined once in its
`data_model.md#concepts` row, and this file mints no term for any of them: `lapse_cap` (the per-task lapse
count at which [`repeated_lapse`](#repeated_lapse) is raised, undeclared raising none), `min_tier` (the
minimum model tier a class's [action](#action) may be taken at, evaluated at the same take as
[blast radius](#blast-radius) and set by the [operator](#operator) only for irreversible classes),
`confidence_threshold` (the policy floor a scored [confidence](#confidence) is compared with — not a second
term beside `confidence`), `recoveries`, `metered_resources[]`, `quorum`, and `disjoint_roles[]` are the
policy's data. A reader meets each on the policy's row; naming one here would be a second home for a value
that already has one (principle 9).
**See:** [`gates_and_workflows.md#two-questions-who-may-claim-a-step-and-whether-an-action-may-be-taken`](gates_and_workflows.md#two-questions-who-may-claim-a-step-and-whether-an-action-may-be-taken),
[`data_model.md#concepts`](data_model.md#concepts).
**Never:** "execution_policy", "execution policy", "workflow policy" (retired: the step owners a workflow
declares together with the grants in force decide who may claim a step, and nothing stands beside them
under a name of its own).
**Not for:** "config" or "settings" for the policy; the policy for who may claim a step (that is the
declaration and the grants).

### governance write
**Definition:** a write to one of eight closed, named record types — `agent`, `agent_policy`, a [workflow](#workflow)
declaration, [`action_policy`](#action_policy), an [agent grant](#grant), `swarm_roster`, the schema registry, or an
[intake rule](#intake-rule) — evaluated as an [action](#action) at the [action gate](#action-gate) under the project's
`action_policy`, because each of the eight defines what the swarm may do, what a [principal](#principal) is, or how
work reaches the swarm.
The list is stated once, at its one home, and never restated elsewhere in this document or any other; a type a
[step](#step) reads as an input rather than one of the eight — a `task_policy`, a `channel_config`, a `priority_rubric` —
is not a governance type. The write capability on a governance type is held by the [engine](#engine) alone
(decision 56); every other principal's write to one is refused at admission, and a write naming no
principal at all — a peer instance's, replicated by the record's own sync substrate — lands as an
[observation](#observation) and never takes effect, for the same reason (decision 55; `gates_and_workflows.md#a-synced-observation-on-a-governance-type-is-recorded-and-never-takes-effect`).
**See:** [`gates_and_workflows.md#two-questions-who-may-claim-a-step-and-whether-an-action-may-be-taken`](gates_and_workflows.md#two-questions-who-may-claim-a-step-and-whether-an-action-may-be-taken).
**Never:** —
**Not for:** a lengthened list (the eight are closed by construction, C-tested at WM-22); an internal
operational write for a governance write (only these eight named types qualify; every other record write
inside the boundary is not an action).

### action
**Definition:** one intended effect on an [external system](#external-system) — one the swarm does not own — such as a send, a
publish, a merge, a payment, or a release, related to the [task](#task) it serves.
Created when the effect becomes known, which may be mid-workflow; a task may produce many, most unknown at
creation. The record is inside the boundary, not across it, so an internal operational write to it is not
an action; the two exceptions, [governance writes](#governance-write) and lossy record mutations, are actions for what they can
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
once in the [action](#action)'s home section. The host a [daemon](#daemon) runs on is one: its processes and checkouts
are [artifacts](#artifact), and process control is its adapter's action classes (decision 45). A second
instance of the record's own software, owned by another party, is **not** one: decision 55 rules it the
same record, extended by replication, reached through the record's own peer-sync substrate rather than an
adapter, with no [artifact](#artifact) at the seam.
**See:** [`gates_and_workflows.md#actions-are-entities-only-actions-are-taken`](gates_and_workflows.md#actions-are-entities-only-actions-are-taken),
[`adapters.md#the-two-invariants`](adapters.md#the-two-invariants),
[`adapters.md#whether-the-host-a-daemon-runs-on-is-an-external-system`](adapters.md#whether-the-host-a-daemon-runs-on-is-an-external-system),
[`adapters.md#whether-a-second-instance-of-the-record-is-an-external-system`](adapters.md#whether-a-second-instance-of-the-record-is-an-external-system).
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
Values include `build`, `docs`, `publish`, `external_api_write` (a write to an
[external system](#external-system) that reaches nobody but the system itself), `send_external_comms`,
`merge_pr`, and `operator_only`; a declared but unclassified value fails closed.
The set of values is `action_policy` data and has no list in the foundation: each [adapter](#adapter)'s
document tables the classes its [outbound](#outbound) operations carry, every class an adapter can produce is listed in
the policy (`adapters.md#the-admission-contract`, obligation 6), and the values named here are examples.
**No individual class carries an entry of its own**, for the same reason the set has no list: a class is
policy data, its [blast radius](#blast-radius) is the policy's to resolve, and an entry per class would be
a second home for a value the policy already declares (principle 9). Where one class's boundary against
another is load-bearing — the same calendar write being `external_api_write` on a solo event and
`send_external_comms` on one with attendees, because the second mails people — the
[adapter](#adapter)'s own document argues it, at the row where the operation appears.
**See:** [`gates_and_workflows.md#confidence-and-three-blast-tiers`](gates_and_workflows.md#confidence-and-three-blast-tiers),
[`adapters.md#what-an-adapters-document-must-contain`](adapters.md#what-an-adapters-document-must-contain).
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
A [step](#step) of any [workflow](#workflow) whose [action](#action) carries it stays in that workflow: the step carries the
[checkpoint](#checkpoint), holds for the [action confirmation](#action-confirmation), and closes on it — never on
the resolution, which is the [operator](#operator)'s decision and not the fact.
**See:** [`gates_and_workflows.md#confidence-and-three-blast-tiers`](gates_and_workflows.md#confidence-and-three-blast-tiers),
[`gates_and_workflows.md#an-operator_only-action-is-taken-by-the-operator-and-the-step-that-carries-it-closes-on-the-confirmation-never-on-the-resolution`](gates_and_workflows.md#an-operator_only-action-is-taken-by-the-operator-and-the-step-that-carries-it-closes-on-the-confirmation-never-on-the-resolution).
**Never:** "unclaimable".
**Not for:** "high blast" for `operator_only` (a louder `HIGH` delays the wrong outcome rather than
preventing it).

### checkpoint
**Definition:** the held state of its [subject](#subject) — an [action](#action) held at the [action gate](#action-gate), or a [task](#task) the swarm
cannot advance — awaiting a [principal](#principal)'s decision.
Two cases, one term, because both are work stopped short of a decision only a principal can make; what
resumes differs and is read from the subject [edge](#edge), not from a second term. Recorded as an entity linked to
its subject, carrying a [reason class](#reason-class), the needed input, the options, whom it awaits, and who resolved it,
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
**Definition:** the [engine](#engine) [role](#role) that merges a pull request once every required [step](#step) is signed off and
the [action gate](#action-gate) permits the `merge_pr` [action](#action).
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
**Definition:** the rule that every [outbound](#outbound) effect is idempotent or deduplicated on its own
[idempotency key](#idempotency-key) — the [action](#action)'s `dedup_key` — so a re-claimed [task](#task)
never repeats an effect that already happened.
**See:** [`work_model.md#at-least-once-implies-effect-dedup`](work_model.md#at-least-once-implies-effect-dedup),
[`data_model.md#record-conventions`](data_model.md#record-conventions).
**Never:** "replay protection" (replay is refused outright).
**Not for:** "retry" for dedup; "idempotency" alone for the rule (the rule is effect dedup; the key it is
built on is the [idempotency key](#idempotency-key)).

### idempotency key
**Definition:** the value a write is keyed on so a retry of it lands once: an
[external system](#external-system)'s own delivery id, carried by every [inbound](#inbound)
[delivery](#delivery) as the idempotency key of the write it produces
(`data_model.md#record-conventions`), or an [action](#action)'s `dedup_key` on the [outbound](#outbound)
side, which [effect dedup](#effect-dedup) is the rule for. The two are one mechanism at two boundaries of
one [adapter](#adapter), not two mechanisms: a mismatch on an existing key is refused, and a refusal is
stronger evidence of a prior commit than a success response is of the present one
(`failure_posture.md`, rule 6). The key is built from the source's own stable values and never from a
wall-clock time or from when the record happened to look, because a key that could not be reproduced by a
later, identical write would not catch the redelivery it exists to catch.
**See:** [`data_model.md#record-conventions`](data_model.md#record-conventions),
[`adapters.md#what-the-adapter-does-with-every-event`](adapters.md#what-the-adapter-does-with-every-event),
[`adapters.md#the-admission-contract`](adapters.md#the-admission-contract).
**Never:** —
**Not for:** "idempotency" unqualified for the key (the noun is the key; "idempotent" describes an effect
that needs no key because repeating it changes nothing); a key derived from *(entity, field, value)*,
which refuses any re-submission of a value the field has held before and reports success
(`data_model.md#record-conventions`).

### replay
**Definition:** doing a prior [action](#action) or [step](#step) a second time from a stored trace; refused
outright in this design. [Effect dedup](#effect-dedup) exists so a re-claimed step or [task](#task) never repeats an
effect that already happened, and decision 40 refuses the adjacent shape — a stored reasoning trace a later
reader replays to reconstruct a step's judgement
(`gates_and_workflows.md#what-a-step-leaves-at-close-what-it-produced-and-a-reference-to-what-it-read`).
**Not for:** the sense of a public essay series (*The Human Inversion*, Part 4: reading a session's
reasoning chain months later). That sense is an as-of read over what a [sign-off](#sign-off) names it
read, resolved at its `signed_at` (decision 40) — never a stored trace and never a second doing. Where
"replay" is meant in that reading sense, "as-of read" is this design's term.
**See:** [`gates_and_workflows.md#what-a-step-leaves-at-close-what-it-produced-and-a-reference-to-what-it-read`](gates_and_workflows.md#what-a-step-leaves-at-close-what-it-produced-and-a-reference-to-what-it-read).
**Never:** "replay protection" (see [effect dedup](#effect-dedup)).
**Not for:** the essay's sense above; deterministic code replay (`docs/durable_execution_substrate.md`,
which this design also refuses in favour of explicit persisted state machines).

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
[task](#task); the [engine](#engine) reads only what the adapter wrote.
**See:** [`adapters.md#the-two-invariants`](adapters.md#the-two-invariants),
[`adapters.md#what-the-adapter-does-with-every-event`](adapters.md#what-the-adapter-does-with-every-event).
**Never:** "connector", "plugin".
**Not for:** the engine for the adapter (the engine reads the record; the adapter reads the system);
"gateway" for an adapter, unqualified.

### event
**Definition:** what an [external system](#external-system) says happened, carried by one [delivery](#delivery) and read by
an [adapter](#adapter) into a [signal](#signal) about an [artifact](#artifact) — the payload, never the arrival that brought it
and never the record's reading of it. The chain is one direction only: a delivery carries an event; an
adapter reads the event as a signal; the signal yields one of the four outcomes
(`adapters.md#no-external-event-advances-a-step-by-itself`). GitHub's webhook body, Gmail's history entry,
and a Telegram update are each an event in this sense, distinct from the transport occurrence that
delivered them and from what the adapter makes of them.
**See:** [`adapters.md#what-the-adapter-does-with-every-event`](adapters.md#what-the-adapter-does-with-every-event),
[`github.md`](github.md), [`gmail.md#what-arrives-and-what-must-be-asked-for`](gmail.md#what-arrives-and-what-must-be-asked-for).
**Never:** —
**Not for:** an event for the [delivery](#delivery) that carried it (the delivery is the arrival, the event
is what it carried); an event for the [signal](#signal) the adapter makes of it (the signal is the event
read as being about an artifact, never the event itself); a calendar event, which is a domain object of
`calendar.md`'s system and not this vocabulary term — read from context.

### mapping
**Definition:** an [adapter](#adapter)'s document, stated as the [external system](#external-system)'s own
event list rather than the subset the swarm subscribes to: the **inbound table**, every [event](#event) and [action](#action)
the system can deliver with its status (handled, deliberately ignored, unhandled) and its outcome or drop
reason, and the **outbound table**, per [step](#step) the operation, the [action class](#action_type), and
what confirms it landed. Together the two tables discharge admission obligation 1 — that every delivery
resolves to one of the four [inbound](#inbound) outcomes or to [dropped](#dropped) — and a delivery outside
the mapping is exactly what makes the drop counter a control rather than a promise: it resolves to
`dropped`, reason `unmapped`, counted per window, so an incomplete mapping is a number that rises on its
own instead of a silent gap.
**See:** [`adapters.md#the-admission-contract`](adapters.md#the-admission-contract),
[`adapters.md#what-an-adapters-document-must-contain`](adapters.md#what-an-adapters-document-must-contain),
[`github.md#the-property-that-makes-this-a-control-and-not-a-list`](github.md#the-property-that-makes-this-a-control-and-not-a-list).
**Never:** —
**Not for:** ordinary English for a correspondence the design does not check (a mapping is checked by the
drop counter; a resemblance nothing counts is prose, not this term); a data-model migration (`migration.md`'s
sense); a `vendor_binding`'s per-instance fields (those bind an instance, they do not enumerate a system's
events).

### signal
**Definition:** what an [inbound](#inbound) external [event](#event) is to the record: information about an [artifact](#artifact), which an
[adapter](#adapter) translates into a [sign-off](#sign-off) by a named [principal](#principal), an [observation](#observation) on an artifact, an [action](#action)
confirmation, or a new [task](#task) for [intake](#intake), and never into an instruction to a [workflow](#workflow).
**See:** [`adapters.md#no-external-event-advances-a-step-by-itself`](adapters.md#no-external-event-advances-a-step-by-itself).
**Never:** —
**Not for:** "trigger" for a signal (nothing outside the record opens a step); "command" for a signal; an
[event](#event) for a signal (the event is what arrived; the signal is the event read as being about an
artifact).

### action confirmation
**Definition:** the [observation](#observation) an [adapter](#adapter) writes on an [action](#action) once its effect exists in the external
system, carrying `taken_at` and `result_ref`, [read back](#read-back) from that system and never inferred from the
operation's return.
**See:** [`adapters.md#outbound-steps-produce-actions-adapters-take-them`](adapters.md#outbound-steps-produce-actions-adapters-take-them).
**Never:** —
**Not for:** sign-off for a confirmation (a confirmation closes no step); a success response for a
confirmation.

### receiver
**Definition:** the transport a [delivery](#delivery) lands on before an [adapter](#adapter) reads it —
a **webhook** (the [external system](#external-system) delivers to an endpoint the swarm exposes) or a **poller** (the swarm
asks the system for updates, by long polling or on an interval) are its two modes; a **subscription** over
the record's own entity changes is not a third mode, because it watches the record and has no visibility
into any external system
(`adapters.md#where-inbound-delivery-lands-the-adapter-verifies-and-identifies-it-and-the-records-own-subscriptions-are-not-it`).
Ruled (decision 16): the receiver may be one shared process for every adapter, built by the swarm or
consumed from a third party, because a socket is a socket and carries no per-system meaning. What it hands
the adapter is the delivery as the system sent it — headers and body intact, unparsed, unverified,
unacknowledged.
**See:** [`adapters.md#where-inbound-delivery-lands-the-adapter-verifies-and-identifies-it-and-the-records-own-subscriptions-are-not-it`](adapters.md#where-inbound-delivery-lands-the-adapter-verifies-and-identifies-it-and-the-records-own-subscriptions-are-not-it),
[`telegram.md#delivery-webhooks-long-polling-and-what-the-dedup-rule-keys-on`](telegram.md#delivery-webhooks-long-polling-and-what-the-dedup-rule-keys-on).
**Never:** —
**Not for:** a receiver that verifies a [signature](#signature), deduplicates, acknowledges, or parses —
those four are the adapter's alone (decision 16) and a receiver that does any of them has become part of
the adapter, sharing its per-system knowledge with nothing; "receiver" for the record's own subscription
mechanism (it watches the record, not an external system, and wakes a consumer on a write the record
already holds — that is downstream of an adapter, never a substitute for one).

### signature
**Definition:** the per-[external system](#external-system) authenticity check a [receiver](#receiver)
hands the [adapter](#adapter) the means to make, and the adapter alone performs, on every
[delivery](#delivery) before its [disposition](#disposition) is decided. Ruled (decision 16): verification is the
adapter's because the scheme is a fact about the system, not a general property a shared receiver could
check — a keyed hash over a shared secret, a secret token the system echoes back in a header, an envelope to verify
whose payload carries no event, a scheme that varies between rails, or, for a chain, no signature at all,
only a source the swarm chose to trust. A receiver that verified generically would accept what it could
not check (the fail-open shape principle 5 forbids at the one field that decides whether a delivery is the
system's at all) or refuse every scheme it did not know, which is the same failure moved.
**See:** [`adapters.md#where-inbound-delivery-lands-the-adapter-verifies-and-identifies-it-and-the-records-own-subscriptions-are-not-it`](adapters.md#where-inbound-delivery-lands-the-adapter-verifies-and-identifies-it-and-the-records-own-subscriptions-are-not-it),
[`payments.md`](payments.md).
**Never:** —
**Not for:** a foundation-level scheme (the scheme itself is per-system detail, stated in that system's
adapter document — `github.md`, `telegram.md`, `payments.md` — and never generalized here); a substitute
for identity (`adapters.md#what-the-adapter-does-with-every-event`), which resolves the signed actor to a
principal and is a separate step after verification passes.

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
**Definition:** one arrival of an external [event](#event) at an [adapter](#adapter), carrying the [external system](#external-system)'s
own delivery id, which is the [idempotency key](#idempotency-key) of the write it produces.
Every delivery resolves to one of the four [inbound](#inbound) outcomes or to [dropped](#dropped) with a reason; that
[disposition](#disposition), never receipt alone, is what is recorded.
**See:** [`adapters.md#what-the-adapter-does-with-every-event`](adapters.md#what-the-adapter-does-with-every-event).
**Never:** —
**Not for:** delivery for the handing of work to a principal (pull is the only delivery of work,
[`work_model.md#pull-is-the-only-delivery-assignment-constrains-eligibility`](work_model.md#pull-is-the-only-delivery-assignment-constrains-eligibility));
delivery for the [event](#event) it carries (the delivery is the arrival; the event is the payload).

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
**The drop reason is the [adapter](#adapter)'s value, declared per [external system](#external-system).**
Unlike a [reason class](#reason-class), whose members the design enumerates once, the reasons a drop
carries are declared in each adapter document's [inbound](#inbound) table beside the surface each covers, because what
an adapter can be delivered and chooses not to handle is that system's question and not the design's. The
one reason every adapter carries is `out_of_scope_class`: the delivered class says nothing about an
[artifact](#artifact) the work model names. It is counted like every other drop, so a class becoming
relevant appears as a rising count rather than as silence.
**See:** [`adapters.md#what-the-adapter-does-with-every-event`](adapters.md#what-the-adapter-does-with-every-event),
[`adapters.md#what-an-adapters-document-must-contain`](adapters.md#what-an-adapters-document-must-contain).
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
to forbid them); a last-seen **cursor table** standing in for coverage (the concrete shape the sync-log
ban takes at every system — `gmail.md`, `payments.md`, `telegram.md` — named here so "cursor" has one
home instead of a per-system restatement); freshness of a [sign-off](#sign-off) against an artifact's
head (that is its own derived read); a stored freshness flag.

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

## Planning model (`planning_model.md`)

### planning record
**Definition:** an entity of a registered type the registry marks as a planning type with a level, under
which [tasks](#task) and lower planning records sit by `PART_OF`, whose progress is a [derived read](#derived-read) over its
descendants and whose statement and [planning decisions](#planning-decision) are authored through the planning [workflow](#workflow).
**See:** [`planning_model.md#the-hierarchy-is-edges-and-a-task-has-one-line-upward`](planning_model.md#the-hierarchy-is-edges-and-a-task-has-one-line-upward),
[`data_model.md#concepts`](data_model.md#concepts).
**Never:** —
**Not for:** "roadmap item" or "portfolio item" for a planning record; a parent task for a planning record (a parent task groups tasks and holds no statement); a
batch for a planning record; a matter or a case for one (that is what work concerns, not what it is for).

### planning level
**Definition:** the rank the registry's planning mark carries on a type, by which a [planning record](#planning-record)'s
`PART_OF` [edge](#edge) points only to a record of a higher rank, and which names the `amend_<level>` [action](#action) class
an amendment at that rank takes.
**See:** [`planning_model.md#the-hierarchy-is-edges-and-a-task-has-one-line-upward`](planning_model.md#the-hierarchy-is-edges-and-a-task-has-one-line-upward),
[`planning_model.md#which-levels-an-instance-declares-and-what-it-calls-them`](planning_model.md#which-levels-an-instance-declares-and-what-it-calls-them).
**Never:** —
**Not for:** "tier" for a planning level (a tier is a blast tier); a field on the entity for its level
(the mark is the type's); a fixed count of levels.

### ascent
**Definition:** the [planning records](#planning-record) above a [task](#task), read upward along `PART_OF` from the task to the root,
a [derived read](#derived-read) a [step](#step) resolves at [hydration](#hydration) for the planning types its declaration names.
The ascent is where a task has been placed; the chain is where it has been.
**See:** [`planning_model.md#upward-context-is-a-declared-read-resolved-along-the-ascent-at-hydration`](planning_model.md#upward-context-is-a-declared-read-resolved-along-the-ascent-at-hydration).
**Never:** —
**Not for:** "context depth" as a number on a step (the depth is the set of planning types the step
declares); chain for the ascent (the chain is the batches along `FOLLOWS`); a stored `plan_id` or list of
records for the ascent.

### unplanned
**Definition:** the [derived read](#derived-read) of a [task](#task) whose [ascent](#ascent) is empty — no [planning record](#planning-record) above it — admitted
through [intake](#intake) like any task and counted where the swarm's instruments are counted.
**See:** [`planning_model.md#upward-context-is-a-declared-read-resolved-along-the-ascent-at-hydration`](planning_model.md#upward-context-is-a-declared-read-resolved-along-the-ascent-at-hydration).
**Never:** —
**Not for:** "orphan" for an unplanned task; "uncategorized" as a status; a status value for unplanned; a
bucket record that unplanned tasks are put under.

### planning decision
**Definition:** a `decision` entity `PART_OF` the [planning record](#planning-record) it was taken under, carrying the decision,
its reason, and its date, written once by the planning [workflow](#workflow)'s `amend` [step](#step) and reversed only by a
later decision that `SUPERSEDES` it.
**See:** [`planning_model.md#downward-state-is-derived-upward-content-is-authored-as-entities`](planning_model.md#downward-state-is-derived-upward-content-is-authored-as-entities),
[`data_model.md#relationships`](data_model.md#relationships).
**Never:** —
**Not for:** "decisions map" for a planning decision (a decision is an entity, never a key in a map); the
resolution of a checkpoint for a planning decision (a resolution is a principal's answer on a held action
or task); a register decision of this directory for a planning decision.

### planner
**Definition:** the [role](#role) that [claims](#claim) every [step](#step) of the planning [workflow](#workflow), resolved by the roster per
project, and once for the levels above a project.
**See:** [`workflows.md#planning`](workflows.md#planning),
[`planning_model.md#maintenance-is-work-the-planning-workflow`](planning_model.md#maintenance-is-work-the-planning-workflow).
**Never:** —
**Not for:** "plan owner" for the planner (ownership is the required seat on the record's checkpoints, and
the planner is the step owner of its batches); a session for the planner (a session binds no plan and
holds no lease).

### amend (a planning record)
**Definition:** to write a [planning record](#planning-record)'s authored content — a correction to its statement, a planning
decision under it, a record created beneath it — as an [action](#action) of the class `amend_<level>` for the
record's [planning level](#planning-level), taken at the planning [workflow](#workflow)'s `amend` [step](#step) through the [action gate](#action-gate).
**See:** [`planning_model.md#authority-per-level-an-amendment-is-an-action-and-its-class-is-the-levels`](planning_model.md#authority-per-level-an-amendment-is-an-action-and-its-class-is-the-levels).
**Never:** —
**Not for:** "update the plan" for an amendment made outside the planning workflow; correcting a task's
acceptance criteria (that amendment is a step owner's, on the task); a governance write for an amendment
(a planning type is not on the list of eight).

## Authority model (`authority_model.md`)

### authority
**Definition:** the right to take an [action](#action), expressed as `[principal](#principal) + [domain](#domain) +
[permission scope](#permission-scope) + action + conditions + time`.
**See:** [`authority_model.md#the-tuple`](authority_model.md#the-tuple).
**Never:** —
**Not for:** "permission" alone for authority (a [permission scope](#permission-scope) term); "access" for
authority.

### domain
**Definition:** the tuple term naming the region an [authority](#authority) covers — entity types,
repositories, a [workflow](#workflow), a queue — carried by `agent_grant.capabilities` and by an
`ownership_grant`'s object.
A [grant](#grant)'s domain and its [permission scope](#permission-scope) are separate tuple terms: the
domain says what the [principal](#principal) may act on, the permission scope what it may do there. Where a
domain is acted in by one principal alone, that is [grant](#grant) configuration — an **exclusive domain**
— not a property of [ownership](#ownership): owning confers the required seat on the domain's
[checkpoints](#checkpoint) and nothing else (decision 46,
`authority_model.md#what-owning-confers-the-required-seat`). `ownership`'s list — "a [workflow](#workflow),
domain, queue, or configuration entity" — names domain as one kind of thing owned among several, not as a
synonym for any of them.
**See:** [`authority_model.md#the-tuple`](authority_model.md#the-tuple),
[`authority_model.md#what-owning-confers-the-required-seat`](authority_model.md#what-owning-confers-the-required-seat).
**Never:** —
**Not for:** domain for the class of work a `intake_rule` or a standing [finding](#finding) routes by (that
routing is by governance class and by [ownership](#ownership) — which `agent`, `agent_policy`, or
`workflow` a change reaches — never by a stored domain field; nothing in the design keys a routing decision
on this term); domain for a [workflow](#workflow) or a [step](#step) alone (each is one of the things a
domain may name, not the term itself).

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

### principal binding
**Definition:** the [edge](#edge) from an [agent](#agent) to the [principal](#principal) whose interest it
acts in, recorded as `principal_binding`.
Not a [credential](#credential): a credential binds a login, key, or address **to** a principal, while
this binds one principal to another, and it is what joins the two credential systems — an AAuth `sub`
binds to the agent that presented it and reaches the human principal only through the agent's binding.
It carries one rule the design turns on: **for a [quorum](#quorum) or a
[separation-of-duties](#separation-of-duties) check, an agent counts as the principal its binding names**
— one interest, so two agents bound to one [operator](#operator) cannot satisfy a check meant to require
two. Attribution is unaffected: the agent is recorded as itself, A-for-B.
**See:** [`authority_model.md#principals`](authority_model.md#principals),
[`authority_model.md#the-counting-rule-an-agent-counts-as-its-bound-principal`](authority_model.md#the-counting-rule-an-agent-counts-as-its-bound-principal).
**Never:** —
**Not for:** a [credential](#credential) for the binding (the credential binds to a principal; this binds
between two); the binding for [delegation](#delegation) (a delegation is granted and expires, a binding is
what the agent is); the binding for `vendor_binding` (that binds a [role](#role) to a model and harness,
and carries no [authority](#authority)).

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
**Definition:** an `agent_grant` holding the [domain](#domain) and [permission scope](#permission-scope) a
[principal](#principal) may act in, matched on its [credential](#credential), as operation × entity types ×
repositories with parameter constraints and an expiry.
Zero grants is deny. A capability also names the tools a principal may invoke, and a harness's own allowlist is
one enforcement of that (decision 42); a budget — a bound on a resource a capability may consume — is a term
of its parameter constraints, narrowing down a [delegation](#delegation) chain, and which resources a class's
[actions](#action) are counted in is the `action_policy`'s `metered_resources[]`, none until written (decision 53). The
write capability on a governance type is held by the [engine](#engine) alone, and every other principal's write to one
is refused at admission (decision 56).
**See:** [`authority_model.md#grants`](authority_model.md#grants),
[`gates_and_workflows.md#where-the-enforcement-point-for-a-governance-write-sits`](gates_and_workflows.md#where-the-enforcement-point-for-a-governance-write-sits),
[`migration.md#where-a-skills-harness-mechanics-live`](migration.md#where-a-skills-harness-mechanics-live),
[`authority_model.md#budget-is-a-scope-term-that-attenuates`](authority_model.md#budget-is-a-scope-term-that-attenuates).
**Never:** —
**Not for:** permissions for a grant (a capability is one row of a grant); "allowlist" for a grant (one
enforcement of it).

### permission scope
**Definition:** the tuple term naming the operations permitted within a [domain](#domain), with per-tool
parameter constraints, carried by `agent_grant.capabilities`, `param_constraints`, and
`action_policy.permission_scope`.
A budget is a term of a permission scope's parameter constraints — a bound on a resource a capability may
consume, narrowing a [delegation](#delegation) chain — and attenuates what a permission scope otherwise
grants; it is not a fifth tuple term of its own (decision 53,
`authority_model.md#budget-is-a-scope-term-that-attenuates`). This is the sense meant wherever these
documents say bare "scope" beside "domain," or say a right is scoped, restricted, or narrowed to an
operation or a parameter.
**See:** [`authority_model.md#the-tuple`](authority_model.md#the-tuple),
[`authority_model.md#grants`](authority_model.md#grants).
**Never:** —
**Not for:** "permission" alone for the whole tuple (permission scope is one term of it, paired with
[domain](#domain)); a [finding](#finding)'s one-off-versus-standing reach ([finding scope](#finding-scope));
a `waived` [sign-off](#sign-off)'s reach ([waiver scope](#waiver-scope)); the `## Scope` section heading
these documents use to bound a document's own subject matter, which names no tuple term at all.

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
**Definition:** named accountability for a [workflow](#workflow), [domain](#domain), queue, or configuration entity, carried as an
`ownership_grant` [edge](#edge) to a [principal](#principal).
Owning confers the required seat — the accountable principal is asked on any [checkpoint](#checkpoint) whose
subject concerns the object — and nothing else; exclusivity in a domain is [grant](#grant) configuration
(decision 46).
**See:** [`authority_model.md#principals`](authority_model.md#principals),
[`authority_model.md#what-owning-confers-the-required-seat`](authority_model.md#what-owning-confers-the-required-seat).
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
A timeout is a terminal state that never continues. The principal that raised a checkpoint does not
resolve it; the one exception is the [operator](#operator) resolving a checkpoint the operator raised, admitted
with the `self_resolved` mark and refused without it (decision 47).
**See:** [`authority_model.md#approval`](authority_model.md#approval),
[`authority_model.md#the-raiser-of-a-checkpoint-does-not-resolve-it-and-the-operators-self-resolution-is-marked`](authority_model.md#the-raiser-of-a-checkpoint-does-not-resolve-it-and-the-operators-self-resolution-is-marked).
**Never:** "silent continuation".
**Not for:** resolved without who; sign-off for an approval (that closes a step); an unmarked self-resolution.

### quorum
**Definition:** a structural check requiring m-of-n named [principals](#principal) on one [checkpoint](#checkpoint).
Read over the checkpoint's `AWAITS` and `RESOLVED_BY` [edges](#edge), an [agent](#agent) counting as its bound
principal (decisions 48 and 49); the count is the class's `quorum` on the `action_policy`, and every awaited
principal where none is set (decision 50).
**See:** [`authority_model.md#structural-checks-quorum-and-separation-of-duties`](authority_model.md#structural-checks-quorum-and-separation-of-duties).
**Never:** —
**Not for:** "required reviewers" for a quorum (1-of-n is not a quorum); sign-off for a quorum.

### separation of duties
**Definition:** a structural check requiring disjointness between the [roles](#role) on one [checkpoint](#checkpoint), such as
raiser and resolver or proposer and approver.
Read as disjointness over the checkpoint's principal [edges](#edge), an [agent](#agent) counting as its bound
[principal](#principal) (decisions 48 and 49); the pairs a class requires are its `disjoint_roles[]` on the
`action_policy` (decision 50).
**See:** [`authority_model.md#structural-checks-quorum-and-separation-of-duties`](authority_model.md#structural-checks-quorum-and-separation-of-duties).
**Never:** —
**Not for:** "four eyes" for the check, unqualified; sign-off for the check.

### initiative
**Definition:** a proposed change to what the organization pursues, entering [intake](#intake) as a [task](#task) by
class; its acceptance is the resolution of the [checkpoint](#checkpoint) on the [action](#action) it implies,
and it has no entity type of its own (decision 51).
**See:** [`authority_model.md#initiative-proposal-reprioritization`](authority_model.md#initiative-proposal-reprioritization),
[`authority_model.md#initiative-approval-is-the-checkpoint`](authority_model.md#initiative-approval-is-the-checkpoint).
**Never:** —
**Not for:** project or "epic" for an initiative; an entity type for an initiative (it is a task by class).

### proposal
**Definition:** the ask that an initiative be accepted, made under proposal rights that are distinct from
execution rights.
The right is a capability of a [grant](#grant) — creating a [task](#task) of the initiative class — and is
default-deny (decision 52).
**See:** [`authority_model.md#initiative-proposal-reprioritization`](authority_model.md#initiative-proposal-reprioritization),
[`authority_model.md#what-stops-is-a-task-the-owner-seat-confirms-it-through-the-checkpoint-and-proposing-is-a-grant-capability`](authority_model.md#what-stops-is-a-task-the-owner-seat-confirms-it-through-the-checkpoint-and-proposing-is-a-grant-capability).
**Never:** —
**Not for:** PR or RFC alone for a proposal.

### reprioritization
**Definition:** the explicit "what stops?" recorded when an initiative is accepted, confirmed by a
[principal](#principal).
What stops is a [task](#task) — a [batch](#batch) closing naming no [successor](#successor), or a `priority` correction — and
the confirmation is the resolution of the [checkpoint](#checkpoint) whose subject concerns it, by the stopped
task's owner seat or the [operator](#operator), with the stop [read back](#read-back) (decision 52).
**See:** [`authority_model.md#initiative-proposal-reprioritization`](authority_model.md#initiative-proposal-reprioritization),
[`authority_model.md#what-stops-is-a-task-the-owner-seat-confirms-it-through-the-checkpoint-and-proposing-is-a-grant-capability`](authority_model.md#what-stops-is-a-task-the-owner-seat-confirms-it-through-the-checkpoint-and-proposing-is-a-grant-capability).
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

### reason class
**Definition:** the named value on a [checkpoint](#checkpoint) saying what stopped its
[subject](#subject) — `gate_hold` for an [action](#action) held at the [action gate](#action-gate), and one
of the classes a [task](#task) is [escalated](#escalate) under.
The set is enumerated once, each member with what raises it, in
`failure_posture.md#checkpoints-on-tasks-one-queue-one-protocol`; a project's
[`action_policy`](#action_policy) may declare more, and this file defines only the members whose meaning is
not readable from the name. The class is what a reader routes on — which classes hold a task from
[claim](#claim), and which merely reorder — so it is a value the design names rather than free text.
**See:** [`failure_posture.md#checkpoints-on-tasks-one-queue-one-protocol`](failure_posture.md#checkpoints-on-tasks-one-queue-one-protocol),
[`gates_and_workflows.md#the-checkpoint`](gates_and_workflows.md#the-checkpoint).
**Never:** "error code", "failure code".
**Not for:** a reason class for a [dropped](#dropped) [delivery](#delivery)'s reason (that is the
[adapter](#adapter)'s value, declared per [external system](#external-system) in its inbound table, not one
of the classes this term names).

### repeated_lapse
**Definition:** the [reason class](#reason-class) raised on a [task](#task) whose [lease](#lease) has
[lapsed](#lapsed) as many times as its project's [`action_policy`](#action_policy) declares in `lapse_cap`.
Raised by the [watchdog](#watchdog), carrying the count and the last lease holders; an undeclared
`lapse_cap` raises none, and the absence is visible in the policy rather than defaulted at runtime. It
holds the task from [claim](#claim), because a re-claim would restart the condition it exists to stop.
**See:** [`failure_posture.md#repeated-lapse-raises-a-checkpoint`](failure_posture.md#repeated-lapse-raises-a-checkpoint).
**Never:** —
**Not for:** a lapse count for the class (`lapse_cap` is the declared ceiling, the class is what reaching
it raises); [recovery](#recovery) for a re-claim after a lapse (no effect was taken).

### rounds_exhausted
**Definition:** the [reason class](#reason-class) raised when a bounded loop reaches its declared ceiling
without resolving — a deferral repeated to its bound, or a [step](#step)'s `on_fail` loop reaching the
`rounds_cap` its [workflow](#workflow) declares.
Carries the [finding](#finding) naming what the [batch](#batch) was waiting on. It holds the [task](#task) from
[claim](#claim): the loop stopped because repeating it changed nothing.
**See:** [`failure_posture.md#the-rules`](failure_posture.md#the-rules),
[`gates_and_workflows.md#declaration-batch-projection`](gates_and_workflows.md#declaration-batch-projection).
**Never:** "retry limit", "max retries".
**Not for:** a timeout for the class (nothing here is bounded by elapsed time — a round is a repetition,
not an interval); the ceiling itself for the class (that is `rounds_cap`, declared on the [step](#step)).

### unreadable_workflow
**Definition:** the [reason class](#reason-class) raised when the [workflow](#workflow) declaration a
[batch](#batch) needs cannot be read, so no [step](#step) of it opens or is [claimed](#claim).
One [checkpoint](#checkpoint) for the batch, never one per [task](#task), and never an empty step tuple
proceeding: an unreadable declaration is [unknown](#unknown), and unknown holds.
**See:** [`gates_and_workflows.md#an-unreadable-workflow-is-unknown-and-unknown-holds`](gates_and_workflows.md#an-unreadable-workflow-is-unknown-and-unknown-holds),
[`failure_posture.md#checkpoints-on-tasks-one-queue-one-protocol`](failure_posture.md#checkpoints-on-tasks-one-queue-one-protocol).
**Never:** —
**Not for:** a missing workflow for the class (a declaration that does not exist and one that will not
read are the same [unknown](#unknown), and both raise this); the class for a workflow whose steps read
fine but whose [read dependencies](#read-dependency) do not (that is `undeclared_dependency`).

### capability_denied
**Definition:** the [reason class](#reason-class) raised when a [principal](#principal) was refused a
capability its [step](#step) needed — the [grant](#grant) checker's `Deny`.
Names the principal, the exact capability, and what asked for it. The [step](#step) waits rather than
borrowing another principal's [credential](#credential). It holds the [task](#task) from
[claim](#claim), since a re-claim asks the same [grant](#grant) the same question.
**See:** [`authority_model.md#grants`](authority_model.md#grants),
[`failure_posture.md#checkpoints-on-tasks-one-queue-one-protocol`](failure_posture.md#checkpoints-on-tasks-one-queue-one-protocol).
**Never:** "permission error", "403".
**Not for:** the class for a capability that exists but cannot be reached
([`capability_unavailable`](#capability_unavailable) is that one); the class for a credential the swarm
never held (`no_credential`).

### capability_unavailable
**Definition:** the [reason class](#reason-class) raised when a capability a [runner](#runner) had is no
longer reachable mid-[step](#step) — the model or harness it started under going away.
Distinct from [`capability_denied`](#capability_denied) because the two put different questions to the
[operator](#operator): denial asks whether to grant something, unavailability asks whether to wait, switch
vendor, or accept the [step](#step) at a lower tier. Its timing is the existing `lapse_cap`, not a second
clock, and a re-[claim](#claim) reads the same `min_tier` floor a first claim does.
**See:** [`failure_posture.md#a-runners-model-or-harness-going-unavailable-mid-step`](failure_posture.md#a-runners-model-or-harness-going-unavailable-mid-step).
**Never:** —
**Not for:** the class for a refusal ([`capability_denied`](#capability_denied) is that one); "outage" for
the class (the [halt](#halt) is the swarm-wide state, and this is one [step](#step)'s capability); a second
lapse clock beside `lapse_cap`.

### escalate
**Definition:** to raise a [checkpoint](#checkpoint) on a [task](#task) the swarm cannot advance, with the [reason class](#reason-class) that says
why.
The [watchdog](#watchdog) escalates on repeated lapse; the [engine](#engine) escalates on an unreadable [workflow](#workflow); a bounded loop
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
proved equal to its source by a [reconciler](#reconciler), such as `step_status`.
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
| a step, on a condition its owner cannot yet judge | **holds**, under a held lease, with a finding naming the condition; the hold **ends** by sign-off, checkpoint, or lapse | "is paused", "is waiting", "is blocked" (a task with an open checkpoint on it is held by it; `blocked` as a status is retired) |
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
| `merge` (as an action class) | `merge_pr` | the step is `merge` and the action it takes is `merge_pr`, as `github.md` and the code workflows already named it; one word for the step and the class made the class read as the step, and the lint's step rules treat `merge` as a step name |
| `calendar_routing_config` (a binding type) | `channel_config` | `adapters.md#scope` names the per-instance binding types once; a third name for the same binding was a second home |
| `blocked` (as a task status) | an open [checkpoint](#checkpoint) on the task, from which [claimable](#claimable) is derived | nothing wrote it and nothing cleared it; a task the swarm cannot advance is held by a checkpoint, and a status beside the checkpoint was a second held state (principle 6) that needed a process to keep true (principle 11) |
| `pipeline` (the step-path publisher) | [engine](#engine) | decision 34, `work_model.md#whether-the-step-path-is-a-mechanism-of-its-own-and-what-the-engine-is-called`: "GitHub-hosted" named a fact about a checkout, not a design property, and `engine` was already used in three documents and defined in none |
