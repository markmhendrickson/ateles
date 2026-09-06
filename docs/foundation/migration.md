# Migration: carrying the record from the checkout's names to the design's

**Authored companion (not on the review reading list; never keyed):** the design of the second leg of the
population plan (`ent_0916804d07280d1751106d82`): how the record an instance already holds, written under
the names and shapes the checkout uses, is carried into the types, fields, and edges the foundation
defines. Reviewers load the kernel and the keyed documents instead (`conformance.md`). **Kind:** foundation
companion; states, for every type the design renames, retires, splits, merges, or introduces, the
disposition, the record primitive that carries it, the order, what is reversible, and how the carrying
itself is governed — and never the state of any instance. Which types exist on an instance, in what
numbers and shapes, is measured in `status.md`, which is where every figure this document relies on
lives. **Derived from:** `work_model.md`, `gates_and_workflows.md`, `data_model.md`, `authority_model.md`,
`failure_posture.md`, `adapters.md`, `conformance.md#direction-of-truth-per-class-of-record`, the
population plan `ent_0916804d07280d1751106d82` (2026-09-04), the operator's 2026-09-05 direction that the
migration and the conformance suite be designed before either is built so that the design surfaces gaps
and contradictions, the rulings of 2026-09-05 (decisions 13–18 and 23–30; in particular 17, the institutionalization task
entering intake on its own; 18, governance writes reserved by default; and 30, the recurring task as one
live instance whose completion creates the next), and
a read-only inventory of the production record taken 2026-09-05 through the Neotoma MCP (recorded in
`status.md`), the operator's 2026-09-05 direction that the plan also cover the migration of skills into the swarm, the skill
inventory at both harness roots read the same day and the recurring-work extraction over it (both in `status.md`,
revision 32), and the two renderers' stated contracts (`render_agent_docs.py`, `sync_skills.py`). The bootstrap sequence this document orders against is the
conformance suite's (`conformance_suite.md`); where the two disagree, this document says so. Revised by the simplification pass of 2026-09-05 (revision 29: gap G14 closed; `workflow policy` retired). Revised by the memo-gap pass of 2026-09-06 (revision 31: gaps G1 and G12 closed). Revised 2026-09-06 (revision 32: the skills leg — five classes of skill, the mapping of each to its target, stage 11, gaps G28–G31, and open decision 42). Revised by the testability pass of 2026-09-06 (revision 37: gaps G6, G7, G8, G11, G13, and G15 closed). Revised by the rulings pass of 2026-09-06 (revision 38: decisions 31 and 42 ruled — the merge form for a re-type, and a skill's harness mechanics split among the grant, a `vendor_binding`, and the harness's own configuration; gap G19 closed). Revised by the second rulings pass of 2026-09-06 (revision 39: leg one grants the governance types to the engine alone, decision 56). Revised by the planning pass of 2026-09-06 (revision 40: gaps G9, G10, and G31 closed by `planning_model.md`; the planning types' dispositions; the plan family mapped to `workflows.md#planning`). Revised by the minimization-recalibration pass of 2026-09-06 (revision 50: `conversation`, `conversation_message` introduced and registered, decision 63; `session_digest`'s row marked registered and drift-carrying). Revised by the close-out pass of 2026-09-06 (decision 64 closed in `conformance.md`; the `conversation`, `conversation_message` row repointed from decision 64 "still open" to its ruled writer/reader/mark and its authored remainder; a new subsection, *Session types: the field-by-field shape decision 64 left to the schema*, states what must be authored, who owns it, what constrains it, and the existing rows it carries forward without restating their figures). Revised by the sign-off-provenance pass of 2026-09-06 (revision 57: `session_digest`'s row carries the zero-existing-edges drift decision 40's new `REFERS_TO` ← sign-off edge leaves behind). Revised by the schema-drift pass (revision 58, 2026-09-06, **derived from** the operator's 2026-09-06 12:56 memo, via `conformance.md`'s decision 67): G25 cross-referenced as the design-side symptom of the same missing registry read neotoma#1972's diverging `relationship_type` copies show; no gap number added, no figure restated. Revised by the undefined-term pass of 2026-09-06 (revision 60: a new section, *Substrate field names the design reads and never adopts as terms*, records `user_id`, `sub`, `iss`, `conversation`, `raw_fragments`, and `reducer_config` — six names cited across five or more foundation documents whose concepts the design already names, kept out of `vocabulary.md` so the design outlives the substrate's field names; no gap number added, since none of the six is a place the foundation fails to say what the migration needs).

## Purpose

State how the swarm gets from the record it has to the record the design describes without a flag day,
without a delete-and-recreate, and without a side door: every carrying write is one of the record's own
primitives — a correction, a merge, a re-pointing of observations, an interpretation, a relationship — with provenance, with an
idempotency key, and with a read-back; every write that changes what the swarm may do is a governance
write and passes the action gate, or is one of the enumerated operator acts the bootstrap limitation
admits (`work_model.md#changing-the-swarm-is-work-and-it-goes-through-a-workflow-like-any-other`). The
second purpose is equal to the first: a mapping that has to find a home for every source field is the
sharpest test a design gets before anything is built, and every place this document could not find one is
listed as a gap for the foundation to close.

## Scope

The record: the entity types, fields, relationships, and registry entries on an instance that the four
models name or that the built swarm wrote in their place; and the skills — the files the harnesses load by
name at the repository and user roots, and the two entity types behind them — because the operator's direction
places them in the same plan and because they are where the swarm's procedures are written today. Out of scope: the code that reads and writes
them, which follows the record and is measured in `status.md`; the adapters' redeployment, which
`adapters.md#admitting-a-new-adapter` governs; and the declarations of the twelve core workflows, which
the population plan's phases 3 and 4 make and this document only orders against.

This document describes types, counts as classes, and shapes — which fields are populated and what they
hold structurally — and never an entity's contents. It names no operator, payee, contact, channel, host,
or client.

### The name

`migration` is the word the foundation already uses for this: `gates_and_workflows.md#one-step-set-defined-once-tested-for-parity`
states that migration is incremental and never a flag day, and `data_model.md#record-conventions` states
what a migration may never be — a bulk rewrite that lets readers narrow. Both are constraints on this
document, and taking the name is taking the constraints. The alternative, a "carry-forward" document,
would have said the same thing under a word no other document uses.

## Two legs of one plan

The population plan (`ent_0916804d07280d1751106d82`) builds the target state: it inventories what exists
(phase 0), maps recurring work to roles (phase 1), reconciles the eight existing workflow declarations
with the foundation (phase 2), declares the workflows the foundation settles (phase 3) and the rest (phase
4), and writes the governance the declarations require (phase 5). This document is that plan's second
leg: population says what the target record contains; migration says how what is already there gets to
it, in which order, and under whose authority. Neither duplicates the other, and the table below is the
join.

| Population phase | What it produces | What this document adds beside it |
|---|---|---|
| 0, inventory | one analysis of skills, agents, workflows, and recurring shapes | the **record** inventory: every type the design touches, its count class and populated fields, measured into `status.md` (stage 0 below); population's phase 0 inventories what the swarm does, this one inventories what the record holds |
| 1, the gap map | recurring work by role, declared or not | nothing; the gap map is population's |
| 2, reconcile the eight existing declarations | the corrected content of each declaration: step names, roles for agent names, the three divergences, `successors` and `on_fail` | the **mechanism** by which a declaration under the retired name becomes a `workflow` entity (stage 4 below), which population assumed was a correction and which the record's primitives do not support as one (gap G2); population supplies the content, this document the carrying |
| 3 and 4, declare the workflows | the `workflow` entities the foundation settles, then the rest | the ordering constraint: none may be declared before the registry holds the type (stage 1), and the `intake` and the migration's own workflow are declared first because every later stage goes through them (stage 2) |
| 5, the governance the declarations require | `action_policy` per project; the step-owner roles each declaration names; matching grants | the derivation of the first `action_policy` from the two retired policies that already carry blast lists (stage 6), the widening of every grant that names a retired type **before** its holder writes the new one (stage 3), and the classes the migration's own writes carry, which that policy must list before any agent may take them |

What population does not have and migration needs, and therefore adds: the bootstrap act (stage 1 and
2), the engine halt and cutover (stage 4), the re-typing of the retired instance types — the held
decisions, the step records, the retired escalations (stages 5 and 7) — the treatment of the task
population and the artifact types, which population never touches (stages 8 and 9), and the freeze
of the operational records the retired engine wrote (stage 10). What migration does not have and points to:
the content of every declaration, the per-role gap map, and the governance values themselves, which are
population's phase 5 and the operator's.

**The one line the operator adds to the population plan's `next_steps`**, since this document may not
write to that entity: *"Second leg: `docs/foundation/migration.md` (the PR that carries this document) carries the existing record
into the declared state; its stage 4 depends on phase 2's content and its stage 6 on phase 5's values;
stages 0–3 can start when the registry has the types."*

## Dispositions, and the primitive that carries each

Every source type takes exactly one of five dispositions. The primitive is named per disposition so that
the mapping table can name the disposition alone.

**Keep.** The type stays under its name, its entities keep their ids, and no bulk write touches them. Where
the design renames a *field* of a kept type, the rule is `data_model.md#record-conventions`: a tolerant
reader over every known spelling and a canonical writer of the new one, permanently. Keep is the default,
and it covers every type the four models do not name: the design persists what it persists, and a type
outside its scope is not migrated because it is not in the target.

**Re-type.** The type is renamed by the design. The record has no primitive that changes an entity's type
in place — `correct` takes a field and `update_schema_incremental` changes a schema's fields and identity
rule, not an entity's type — so a rename is carried by three primitives together: `register_schema` for
the target type, with its `reducer_config` and one `ownership_grant`; `create_interpretation` over the
**same source** the legacy entity's observations came from, producing the target-typed entity with
provenance to that source and to the interpretation that produced it; and `merge_entities` from the legacy
entity into the target, which rewrites the legacy observations onto the survivor and marks the legacy id
as merged, so the old id keeps resolving through its merge pointer and an as-of read on either id
reconstructs the same history. The survivor's id is new; the legacy id redirects. Every inbound edge is
repointed by the merge rather than re-derived (`data_model.md#record-conventions`, on merges). That this is
the mechanism — and not a type alias the record would have to gain so that ids never change — is decision
31, ruled 2026-09-06 (`#how-a-registered-entity-type-is-renamed-on-a-live-record`): re-type means the
three-primitive form, and stages 4 and 7 no longer wait on it.

**Derive.** A target concept exists in the design and has no source type, but the information that would
populate it is spread across a source type's fields — a held action inside a retired approval record, a
parent edge inside a `parent_task_id` field, a principal binding inside an agent's credential field. The
carrying primitive is `create_interpretation` over the legacy entity's source, producing the new entities
and `create_relationships` for the edges, and the legacy entity **stays**: a derivation reads it and
leaves it, so the derived rows carry provenance back to it and the original remains an as-of read.

**Retire.** The type is superseded and nothing is derived from it, because what it recorded is either
carried by a derived read in the design or is not worth carrying. Retire means **freeze**: no writer
produces the type after the stage that retires it, its entities stay readable forever, and no entity is
deleted. Where the registry can mark a type deprecated it is marked; where it cannot, the freeze is held
by the writers having been redeployed and is verified by the count not moving (stage 10).

**Introduce.** A target type with no source at all: registered, owned, and populated only by the design's
own writers from the moment they exist. Nothing is migrated into it.

**What no disposition admits.** No entity is deleted and recreated: a recreated entity has a new id, no
provenance, and no correction history, and every reference to the old one is silently broken. No verdict
is written for a principal that did not write it: a legacy step record is never turned into a `sign_off`
attributed to the step owner whose name it carries, because no principal signs for another
(`gates_and_workflows.md#declaration-batch-projection`). No field is bulk-rewritten to a new spelling: the
reader stays tolerant (`data_model.md#record-conventions`). And no write is made to an entity while a
process that does not know the new shape is still writing it (the hazard section below).

## The mapping

One table per model. *Disposition* is one of the five above. *Target* is the design's type or edge. The
right-hand column says where the information the source carried ends up, so that every retirement is
accountable for what it drops. Count classes and populated-field measurements per row are `status.md`.

### The work model

| Source type or field (retired names marked) | Disposition | Target | Where the information goes, and why the disposition is safe |
|---|---|---|---|
| — (no source type) | introduce | `intake_rule` | a type the design adds (revision 30) with no counterpart on the instance. The predicates in daemon code that create a task on an entity change are its drift and not its source: each is restated as a rule by an operator's governance write, read back, before the daemon's predicate is retired (`work_model.md#an-intake-rule-turns-a-described-change-in-the-record-into-a-task-and-nothing-else`). Writes to it take the reserved default of decision 18 |
| `task` | keep | `task` | ids stable; no bulk write. The live status vocabulary stays as written and the claim predicate reads every spelling it carries onto `open` or terminal (`work_model.md#what-a-claim-predicate-treats-as-claimable`); the canonical writer uses one spelling per meaning from the set the registered type declares (gap G7, closed). `assigned_to` keeps its design meaning, eligibility. `action_type` stays as the declared classes. `priority` stays |
| `task.blocked_reason`, and the statuses that name a wait for a principal (an approval, an input) | derive | `checkpoint` on the task, reason class per the reason's kind | a task waiting on a principal is a task with an open checkpoint, not a status; the derivation reads each non-terminal task whose status or reason names a wait, and raises one checkpoint whose subject is the task. Terminal tasks are left as written. The field stays for the tolerant reader, and a live `blocked` status is read as a wait and derived the same way (gap G8, closed: `blocked` is retired as a status) |
| `task.confidence` (confidence scored on the task) | derive, where an action is derived; else keep as history | `action.confidence` | the design scores confidence on the action at the moment it would be taken; a task-level score is the retired engine's estimate before any action existed. It is carried onto a derived action where one is derived (below) and otherwise stays readable and unused |
| `task.parent_task_id` | derive | `PART_OF` child → parent | one edge per populated field; the field stays. Parent completion is then a derived read |
| `task.recurrence`, and the tasks that carry it | keep; hold the writer | `task` with its `recurrence` rule; the next instance created by the closing sign-off, `FOLLOWS` task → task (`work_model.md#a-recurring-task-is-one-live-instance-and-its-completion-creates-the-next`) | the live instance the retired pattern left — one open task per obligation whose `due_date` a daemon moves after each completion — is already the design's one live instance, so nothing is rewritten and no `FOLLOWS` edge is fabricated for occurrences the retired pattern overwrote (their history was lost when the fields were). What changes is the writer: the daemon that moves the date must stop before the first instance completes under the design, or a completed instance is reopened beside the one its sign-off created — two live instances, which the ruling forbids. The hazard section names it |
| `task.project_id`, `task.project_ids`, `task.outcome_ids`, and every plan-membership edge | keep the edges; derive from the fields; hold a task with two | `PART_OF` task → planning record (`planning_model.md#the-hierarchy-is-edges-and-a-task-has-one-line-upward`) | gap G9 is closed: a task's one `PART_OF` targets its parent task or the planning record it is under, so the edges the instance holds are already the design's and are not rewritten. A task whose `PART_OF` edges name two planning records, or a planning record and a parent task, is held: the derivation writes nothing for it and lists it for the `planning` batch of each record to judge — one edge stays, the other becomes `REFERS_TO` — because which is the task's line is a judgement. The membership fields (`project_id`, `project_ids`, `outcome_ids`) are derived to one `PART_OF` edge where they name a registered planning record and the task has none, and stay readable history where they name a record that is not one (the imported grouping type below) |
| the retired liveness status values on a task (the value the retired engine wrote while a runner held a task, and the age of `updated_at` it read as the hold) | retire, with a dual-read window | `LEASE` edge; `active` derived | no lease can be fabricated for a hold the retired engine took: the design's lease has a runner, a claim time, and an expiry, and the retired record has none of them. Until the old engine is halted (stage 4) the claim predicate treats a task carrying the retired liveness value within the retired engine's own age window as held; after the halt, the value is history and a task with no `LEASE` edge is claimable. This is the hazard section's first rule |
| `agent_session` | keep | `agent_session` | the design names the type. Its design fields (`runner_id`, `host`, `checkout`, `branch`, `head`, `started_at`, `last_seen_at`) and the source's (`native_session_id`, `cwd`, `git_head_sha`, `holder`, `task_id`, a step name and a declaration reference) are a tolerant-reader case; the canonical writer uses the design's. The fields that named a held task and a step become the `LEASE` edge and its `step_name` for new sessions, and stay readable history on old ones |
| `transcription`, `meeting_transcription`, `transcription_run` | keep | entities in the record; a `task` that concerns one attaches it by edge | a transcript in the record is not an artifact: it is not reached through an adapter and has no `system` or `external_id` (`work_model.md#artifacts-are-records-a-batch-leaves-never-its-subject`). The meeting-processing workflow's entry condition already names the `transcription` entity as what the task references (`workflows.md#meeting-processing`). Nothing is re-typed. Two gaps: the same section lists the transcript file as an artifact (G11), and the design names no edge from a task to an entity in the record that it concerns (G12). A transcription enters intake only as the reference of a task a self-triggering daemon creates; the entity's existence creates no task |
| `session_digest` | keep, now registered (decision 63) | `session_digest`, the `digest` step's output in `workflows.md#session-digestion` | the design names it as an entity in the record. Existing digests predate the workflow and belong to no batch; they are history, and the `verify` step's states already live in their claims. Decision 63 makes it eligible for the special-category mark; the ~369 rows on prod predate any mark or owner and are drift this row carries, not a reason the type stayed unregistered (`data_model.md#record-conventions`). Decision 40 additionally gives it a `REFERS_TO` ← sign-off edge, required on an agent's sign-off where a digest exists for its session; zero such edges exist among the ~369 rows or among sign-offs on prod today, since neither side wrote the other before this ruling — carried here as the same kind of migration drift as the mark and owner gap, not an argument that the edge is wrong: a rule ruled today binds writes from today, and backfilling the edge onto pre-ruling sign-offs is a population-plan question, not one this ruling answers |
| `conversation`, `conversation_message` | introduce, registered (decision 63) | `conversation`, `conversation_message` | the design previously named neither type nor carried a row for them (`status.md` revision 46 found the gap by scan). ~10,800 and ~59,300 rows respectively on prod predate any owner, merge policy, or mark, and `conversation`'s 51 fields already show the unowned-type drift `data_model.md#record-conventions` warns of — turn-tracking fields (`turn_key`, `harness`, `session_id`) alongside unrelated ones from other writers (`cpu_cores`, `applecare_price_eur`, `order_date`) with no accountable principal separating them. Decision 63 registers the types so the special-category mark, admission by grant, and reference-never-value reach them; decision 64 rules the writer (the runner), the reader (any declaring step), and the mark's reach (the type, not the row) — its field-by-field remainder is authored below, under *Session types: the field-by-field shape decision 64 left to the schema*, and this row does not itself do that authoring |
| `plan`, its `decisions` and `todos` fields | keep the type, marked as a planning level; derive from both fields | `plan` as a planning record; `decision` entities `PART_OF` it; `task` entities `PART_OF` it (`planning_model.md#downward-state-is-derived-upward-content-is-authored-as-entities`) | gap G10 is closed: a todo is a task, and a decision is an entity. Each entry of a `todos` array whose status is not done is derived to one task `PART_OF` the plan, with the entry's text and any `notes` it carries, entering intake like every created task; a done entry stays history on the field, because the artifact it cites was never read back and a terminal task cannot be fabricated for it (principle 10). Each key of a `decisions` map is derived to one `decision` entity `PART_OF` the plan, the key as its idempotency key and the value as its text, dated from the observation that wrote it; the map stays for the tolerant reader and is written by no canonical writer. `todos_pending` and `todos_completed` are derived reads and are written by nothing. The plan's `body`, `overview`, `completion_criteria`, `success_criteria`, and `out_of_scope` are its statement, kept as written; `status`, `outcome`, `next_steps`, and `decision_blockers` are the stored progress the design derives, kept for the tolerant reader. The plans this directory's own render targets mirror (`conformance.md#direction-of-truth-per-class-of-record`) are touched only in these derivations |
| `plan.parent_plan_entity_id`, `plan.is_project` | derive; keep as history | `PART_OF` plan → plan; a `project`-level record where the flag holds and the operator registers that level (decision 57) | the parent field becomes one edge where it names a plan; the flag marks a plan the operator treated as a longer-horizon record, and whether that is a level of its own or a plan with a longer horizon is decision 57's |
| `project` (the imported grouping), `outcome`, `goal`, and the fields that chain them (`project.outcome_id`, `outcome.goal_id`) | keep; hold the level mark | planning records at the levels decision 57 registers, or kept outside the hierarchy as history | the instance's grouping type carries a stored `status` and a field-based chain to an outcome and a goal, which is the tool-shaped inversion `planning_model.md#prior-art` names: nothing is derived from it until decision 57 says which of the three are levels. A record the operator marks as a level gains its `PART_OF` edge from its chaining field; one left out keeps its fields as history and no task is derived under it |
| `strategy` and its `altitude` field; `business_strategy`, `domain_strategy`, `growth_strategy`, `content_strategy`, `agent_strategy` | keep; hold the level mark | one or more planning levels above the project, as decision 57 registers them; `PART_OF` between them where the instance already writes it | the instance carries its top levels as one type discriminated by an `altitude` field (its values name a mission, a domain strategy, a tactic, and a tenet) and as four sibling types; the design marks a level on the type, so either the field's values become levels of one type or the sibling types do, and which is decision 57's. The `PART_OF` edges the instance already writes between them are the design's and stay. A tenet or a principle the operator never amends may be registered as a level read on every ascent whose `amend_<level>` class is never listed |
| `plan_contribution` | derive; retire the writer | `finding` on a `planning` batch, `PART_OF` its sign-off (`planning_model.md#maintenance-is-work-the-planning-workflow`) | the review, concern, question, and amendment kinds are findings a review step of the `planning` declaration records, and a sign-off kind is that step's sign-off; the rows the instance holds predate any `planning` batch and stay history, as the gate-model table's retired contribution rows do (gap G31 closed) |
| `decision`, `decision_record`, `architectural_decision` | keep `decision`; re-type the other two into it by the merge form (decision 31) | `decision` `PART_OF` a planning record; `SUPERSEDES` between decisions | three types for one concept; the design keeps the one whose name is the concept's. A decision the instance holds with no plan to be under is a decision under the root the operator names, or history, and the choice is made per record by the `planning` batch that first reads it |
| `strategy_drift_signal`, `strategy_evaluation_report`, `metric_contract`, `plan_prioritization` | keep as history; hold | a `finding` at `judge` against a record's `completion_criteria[]`; a `priority_rubric` reading the ascent | the impact-loop types the instance registered and left fallow are the `planning` workflow's findings under this model: a drift signal is a finding that a decision under the record has been contradicted by the work, an evaluation is a `planning` batch's sign-off, and a prioritization is what `prioritize` does reading the ascent. Nothing is derived from the rows; a `metric_contract` bound to a plan is the one that may become a criterion on the plan's statement, by the plan's own `planning` batch |

### Session types: the field-by-field shape decision 64 left to the schema

Decision 63 registered `conversation`, `conversation_message`, and `session_digest`
(`conformance.md#the-register-of-open-design-decisions`); decision 64 then ruled their writer (the runner,
`vocabulary.md#runner`), their reader (any step that declares the type as a read), and the special-category
mark's reach (the type, per revision 36's F23 — a session type carrying third-party Article 9 content
inherits the mark, no per-row flag needed). Decision 64 did not rule the field-by-field shape, and its
register row does not stay open for that shape either: naming `conversation`'s fields, normalizing them, and
writing a `reducer_config` for each of the three types is schema authoring for the owner just named, the same
kind of write `#type-registration-is-an-owned-decision-write-read-back-tests-never-register-into-the-shared-registry`
already requires of any registration — not a design fork the register exists to hold open.

**What must be authored.** One schema row per type — `conversation`, `conversation_message`, and
`session_digest` — declaring the fields each canonically carries and the `reducer_config` each needs
(`data_model.md#concepts`, `conformance_suite.md`'s row 1: nothing is written until its type exists, and a
type registered later leaves earlier writes in `raw_fragments` forever). `conversation`'s ~51 existing
fields are the concrete work: which of them the type declares going forward, which are retired to the
tolerant reader as another writer's drift, and which never belonged to the type at all.

**Who owns it.** The runner — decision 64's ruled writer — under the same `ownership_grant` principle every
other registration in this table observes: one principal is accountable for a type's shape, and the write is
made by or on behalf of that principal, read back against the registry before it lands.

**What constrains it.** The special-category mark applies at the type, not per field: a shape that carries
third-party Article 9 content anywhere in `conversation_message` or `session_digest` is covered by the type's
mark as decided, and the schema does not need to (and must not) re-derive that coverage field by field.
Decision 41's default-deny admission applies the same way it applies to every other write: a field the
schema does not declare has no writer with standing to populate it, the same fail-closed reading decision 63
already applied to the type as a whole, applied here to the type's shape.

**What already exists to carry forward.** The pre-existing rows on prod predate any owner, mark, or
declared shape, and are carried as migration drift by the rows above, not restated here: ~10,800
`conversation` rows and ~59,300 `conversation_message` rows (the `conversation`, `conversation_message` row
above) and ~369 `session_digest` rows (the `session_digest` row above). The schema-authoring write does not
retroactively reshape or minimize what these rows already hold; it declares the shape new writes take.

### The gate model

| Source type or field (retired names marked) | Disposition | Target | Where the information goes, and why the disposition is safe |
|---|---|---|---|
| `workflow_definition` (retired name) | re-type | `workflow` | the content of each declaration is population phase 2's: `gates[]` become `steps[]` with `step_name`, `join_step`, and `owner_role` — a role obtained by inverting the roster's role-to-agent map for each agent name, which fails loudly for a retired agent name and is the phase-2 correction; the in-line final release step becomes a `successors` entry; `stale_threshold_days` becomes the declared interval after which an unclaimed step raises `unclaimed_step` (`failure_posture.md#checkpoints-on-tasks-one-queue-one-protocol`); `legal_required` becomes an `applies_when` condition on the optional step; a fast path whose condition is a label an external system carries is not carried, because the design forbids it (`workflows.md#how-to-read-a-workflow-section`), and where the label names a workflow type that already has its own declaration the fast path was redundant. A declaration for a smoke-test project is retired rather than re-typed: a test declaration in the shared registry is the case `data_model.md#record-conventions` forbids |
| `participation_record` (retired name), rows at a terminal satisfied status | derive, as history only | none | a satisfied row is the retired engine's record that a step was judged. It is **not** turned into a `sign_off`: the design's sign-off is pinned to an artifact head the retired record never captured, carries a pinned agent version it never captured, and is attributed to a principal that never wrote it. The rows stay as the as-of read of what the retired engine recorded. Where the work they judged is still open, the operator's `waived` sign-off per step, carrying the legacy row as its reason, is the design's own way to carry the judgement forward (stage 4) |
| `participation_record` (retired name), rows at a non-terminal status | retire | step state derived from batch, `LEASE`, and `sign_off` | a row saying a step was opened for a step owner is, in the design, the existence of a batch at that step (open) or a lease on it (claimed), both derived. No batch exists for the work these rows name and no lease can be fabricated, so the rows are frozen history. Nothing is lost that the design would have kept: the design deliberately has no per-step status row |
| `gate_status` (retired name), `workflow_state`, `workflow_run` (retired name), `workflow_gate`, `release_gate`, `task_action`, `owner_history_entry` | retire | `step_status` projection on the task; the batch's sign-offs | the per-artifact step maps the retired engines wrote. The design says step state or verdicts on an artifact belong to the batch and its sign-offs and are deliberately not fields of the artifact (`data_model.md#concepts`). The maps were written by two engines that did not read each other, so their content is not trusted enough to derive from; they are frozen and readable |
| `checkpoint_brief` (retired name), open, with a task and a held class | derive, then re-type | `action` (`PRODUCES` from the task; `action_type`, `confidence` from the brief's fields) and `checkpoint` (`CHECKPOINTS` → the action, reason `gate_hold`, `AWAITS` → the operator principal, `RAISED_BY` → the agent that wrote the brief) | the brief held a task before any action entity existed; the design's `gate_hold` presumes an action (gap G4). The derivation creates the action the gate would have written, then the checkpoint on it, then merges the brief into the checkpoint so the brief's id redirects. The operator then decides each as they decide any checkpoint |
| `checkpoint_brief` (retired name), open, with a merge held on a pull request | derive, then re-type | `artifact` (kind pull request) if the record does not yet hold one; `action` (class merge, `REFERS_TO` → the artifact); `checkpoint` | as above, with the artifact minted from the record's own reference to the pull request, not from an adapter read (gap G16: the design says only an adapter mints an artifact, and a migration is not one) |
| `checkpoint_brief` (retired name), open, with no determinable subject | correct to terminal | the brief itself, terminal | a checkpoint has exactly one subject, an action or a task; a brief naming neither cannot become one. The design already has the terminal state for a decision nobody took: a timeout is terminal and never continues (`gates_and_workflows.md#the-checkpoint`). Each such brief is corrected to that terminal state with a resolution note naming this migration. Across the population this is a bulk correction and therefore a lossy record mutation at the action gate (stage 5) |
| `checkpoint_brief` (retired name), terminal | keep as history | none | the decision was taken under the retired engine and its record is the as-of read of that. Nothing is derived |
| `escalation` (retired type), open, about an entity that is neither a task nor an action | derive | a `task` entering intake, referring to the entity concerned; a `checkpoint` on that task only where the reason class calls for a principal's decision | the retired type's subject was frequently a configuration or a checkout, which the design admits as no checkpoint's subject (gap G3). A daemon that cannot act on something the record says it should act on has produced work for someone, and work is a task. The retired entity stays readable and is referred to by the task |
| `escalation` (retired type), written by a test process into the shared instance | correct to terminal | the entity itself, dismissed | test rows in the production registry are the case `data_model.md#record-conventions` forbids; they are closed terminal with a note, never deleted, and the finding goes to the test suite (`conformance_suite.md`). Bulk, therefore gated (stage 5) |
| `execution_policy` (retired name) | derive one per project from the declarations that carry blast lists; retire the rest | `action_policy` per project | the fields that are the design's — `low_blast_action_types[]`, `high_blast_action_types[]`, `confidence_threshold`, the recurrence count — are carried into one policy per project. The named checkpoints a plan-scoped policy declared become `always_checkpoint_boundaries[]` where they name a class boundary. The fields with no target — a plan reference, an operator autonomy level, quality criteria, an agent list, a fallback instruction — are gap G5; the plan-scoped policies stay readable and are not re-typed, because the design's policy is per project and theirs were per plan |
| `finding` | keep | — | the source type records the attribution of a code defect to the change that introduced it — a different concept from the design's finding, which a step owner records against a batch with a severity. The design's finding has no row in `data_model.md` at all (gap G15), so there is nothing to migrate into; the source type is outside the four models and stays |
| `feedback`, `task_policy` | keep | `task_policy` (`conformance.md#direction-of-truth-per-class-of-record`); `feedback` outside the four models | `task_policy` is named by the design as the home of operator preferences. The `feedback` type on the instance holds third-party product feedback, which the design does not name at all: the operator's input on reviewed work is a finding, not a feedback entity (`gates_and_workflows.md#a-finding-is-one-off-or-standing-and-a-standing-one-obliges-a-change-to-what-produced-it`), and the direction-of-truth row that once named `feedback` beside `task_policy` was removed for that reason (gap G14, closed) |
| `proposed_skill_update`, `strategy_drift_signal`, `agent_improvement_proposal` | keep; derive a task from each open one | a `task` entering intake, proposing a change to what produced a finding | each is a standing finding's proposed change under the retired engine's names. The design makes the proposal an institutionalization task entering intake on its own, with no batch waiting on it (`gates_and_workflows.md#a-finding-is-one-off-or-standing-and-a-standing-one-obliges-a-change-to-what-produced-it`, decision 17). The source entity stays as the proposal's record and the task refers to it |
| `batch`, `sign_off`, `action`, `artifact`, `checkpoint`, and the edges `ADDRESSED_BY`, `FOLLOWS`, `LEASE`, `CLOSES`, `SIGNED_BY`, `PRODUCES`, `CHECKPOINTS`, `AWAITS`, `RESOLVED_BY`, `RAISED_BY` | introduce | themselves | none exists on any instance. The derivations above populate `checkpoint`, `action`, and `artifact` for the open held decisions only; everything else is written by the design's own writers from the cutover. A batch is never opened by the migration: a batch comes into existence only when a closing sign-off names a successor (`work_model.md#how-a-batch-is-formed-and-what-chooses-its-workflow`), and every pre-existing task has no intake batch and so meets intake's entry condition exactly as a new task does (`workflows.md#intake`). That is how work part-way through a retired passage is carried: it re-enters intake, is routed, and the steps already judged under the retired engine are either judged again or waived per step by the operator with the legacy record as the reason (stage 4) |

### The authority model

| Source type or field (retired names marked) | Disposition | Target | Where the information goes, and why the disposition is safe |
|---|---|---|---|
| `agent_definition` (retired name) | re-type | `agent` | the design's fields — `name`, `prompt_markdown`, `context_entity_types[]`, version — carry over. The credential field becomes the credential's binding to the `agent` (gap G17: no edge type for a credential binding is named). A status of planned on the source is a declaration-time fact the design already checks: a role the roster resolves to an agent with no runner raises `unspawnable_assignee` at declaration (`failure_posture.md#checkpoints-on-tasks-one-queue-one-protocol`). The tool allowlist, the write-set field, the tier, the harness preferences, and the model tier have no design field (gap G19) and ride in the interpretation as declared-but-unmodelled, never in `raw_fragments`. The coarse grant label is retired: the real grant is the `agent_grant` entity. Undeclared fields already sitting in the source's `raw_fragments` are read out per entity and either declared or dropped with a note before the merge, because a merge carries them nowhere (`data_model.md#record-conventions`) |
| the swarm's roster | keep; correct | the roster the design names as the role resolver | one of the governance types (`gates_and_workflows.md#two-questions-who-may-claim-a-step-and-whether-an-action-may-be-taken`); its role-to-agent map is what inverts the retired declarations' agent names into roles (stage 4) and what every `owner_role` resolves through at claim time. Every role must resolve to an `agent` with a credential before any workflow is declared against it; roles that resolve to a planned agent are named in the read-back (stage 3). The design gives it no row in `data_model.md` (gap G21) and says it binds per project where the instance's is global (gap G20) |
| `agent_grant` | keep; correct | `agent_grant` | the design's fields (`sub`, `iss`, `capabilities[]`, `param_constraints`, `expires_at`) and the source's (`match_sub`, `match_iss`, and a linked host login on the grant) are a tolerant-reader case. Two corrections are governance writes: every capability naming a retired type is widened to name the new type **alongside** the old, before the holder's first write of the new type, and narrowed to the new type only after the holder is redeployed — the staged, dual-admit form `authority_model.md#grants` requires of a rotation, applied to a type name (gap G23: the design never says a type rename re-keys the grants that name it). And the human-device grants that carry wildcards are the fail-open shape the design forbids for a human; narrowing them is the operator's own act. The host login recorded on a grant is a credential binding living on the wrong entity (G17) |
| `agent_policy` | keep | `agent_policy` | the design names it as the authoritative home of skill bodies and agent behavioural rules, and as one target of a standing finding. Prompts and skills rendered from it still carry retired words; the correction is to the entities, then a re-render (`conformance.md#direction-of-truth-per-class-of-record`) |
| `agent_strategy` | keep | — | outside the four models. Each names an agent by credential rather than by role, which population's phase 1 will want to read; not migrated |
| `operator_profile` | keep | `operator_profile`, descriptive | the design keeps it descriptive and hangs no authority edge on it (`authority_model.md#principals`) |
| `operator` (the human principal), `principal_binding`, `ownership_grant`, `delegation_edge` | introduce | themselves | the one `operator` entity is the first write of the bootstrap and the first operator act. Each `agent` then carries a `principal_binding` to it; each registered type carries one `ownership_grant` to it. Credentials (a store identity, a host login, an address, a chat identity) bind to it many-to-one — through an edge the design has not named (G17) |

### Operational records

| Source type | Disposition | Target | Where the information goes, and why the disposition is safe |
|---|---|---|---|
| `harness_event`, rows that record an external system's delivery | retire at adapter cutover | observations on the `artifact` the delivery concerned, keyed on the delivery id | the design deliberately gives an artifact no delivery log: deliveries are its observations (`data_model.md#concepts`). The rows are not backfilled — no artifact exists for most of them to land on, and a backfill of that size is a lossy record mutation whose value is nil — and the adapter writes observations from its cutover. The rows stay readable |
| `harness_event`, rows that record a session's tool calls and the session-integrity check | keep | — | outside the four models: the session-integrity mechanism's own record. Not migrated. That one type carries two unrelated meanings is noted for population's phase 0 |
| `daemon_report` | keep, and hold | — | the write contract says a self-triggering daemon writes the tasks its poll produces and observations carrying its provenance, and nothing else (`data_model.md#write-contract`), which retires the daemon's own report. But the report is what a watchdog reads to tell a daemon with nothing to say from a daemon that has halted (gap G13). Frozen only when the design says what carries a successful empty poll |
| the retired engine's own state entities beyond the gate model's (per-artifact inheritance checks, step-inheritance results) | retire | — | frozen with their gate-model siblings (stage 10) |

### Context entities the design retrieves and never migrates

`payment_profile`, `vendor_binding`, `channel_config`, `deployment_configuration`, `locale_profile`,
`brand_voice`, `constitution`, `priority_rubric`, `confidence_rubric`, `tax_profile`, and the rest of the
per-operator context are **keep**: the design reads them by type at runtime and gives them no new shape.
The artifact types the built adapters already write — issues, pull requests, reviews, mail
messages and threads, calendar events, posts, transactions, payment events — are **keep** with a tolerant
reader keyed on the external system and identifier: the design's `artifact` is introduced and minted by
adapters from cutover, no existing typed artifact is bulk re-typed (the population would make it the
largest lossy mutation in the plan for no reader's benefit), and intake's `link` step attaches whichever
row exists. This is gap G2 in its second form: the tolerant-reader rule is written for field names and the
design needs it stated for types.

## The skills: source state the harnesses hold, and where each kind goes

A skill is a file a harness loads by path — `SKILL.md` under a repository root or a user root — carrying
what an agent or a session is to do when it is invoked by that name. The operator's direction of 2026-09-05
was that the migration plan cover the skills too, and the mapping is the same exercise as the tables above
applied to a source the four models never name: **the design has no `skill` type.** Nothing in
`work_model.md`, `gates_and_workflows.md`, `data_model.md`, or `authority_model.md` names one, and the
direction-of-truth table (`conformance.md#direction-of-truth-per-class-of-record`) already places every
skill file on the mirror side. A skill is therefore **source state and never a target**: it is where the
swarm's knowledge of how work gets done is written today, under the harness's shapes, and the question this
section answers for each is which target the design does have — an `agent`, a `workflow` declaration, a
`task_policy`, an `agent_policy`, an adapter document — and which disposition carries it there. Where a
skill has no target, the gap is listed with the others. Which skills exist, in what numbers, of which class,
and how many carry which shape is measured in `status.md` (revision 32); this section states classes and
rules and names no count.

The record holds two entity types behind the files, and the two are already on opposite sides of the
direction of truth. A role's file is rendered from the agent entity's `prompt_markdown` by
`render_agent_docs.py`, whose header says it is generated and not authored; a procedure's file is rendered
from a `skill` entity's `content` by `sync_skills.py`, under the same header. Both renderers run with
`--check`. What neither renderer changes is that the swarm's own runner reads the role's **file** at claim
time and prepends the entity's prompt to it (`status.md`), so the file is on the swarm's load path, which is
the one place a mirror must not be.

### Five classes of skill

Every skill takes exactly one class, and the class decides its target. The classification test for each is
stated so that it can be applied to a skill this section never saw.

**Role skill.** The file mirrors an agent entity: its front matter carries the entity id and the header says
so. Its body is a prompt — who the agent is, what it judges, how it reports — not a procedure anyone invokes
by name. Target: the `agent` row of the authority table above; the file's own disposition is below.

**Procedure skill.** The file is invoked by name, by the operator in a session or by a daemon, and its body
is a sequence — read this, decide that, store this, confirm before that — with the recurring work it owns
stated or implied by the phrases that invoke it. Target: a `workflow` declaration, or a step of one, or an
adapter's operation; the mapping is below.

**Mixed.** A role skill carrying a procedure verbatim, or a procedure skill that defines a role. The first
form is the three duplicated sections the role files carry (below); the second is a procedure that is one
daemon's whole duty, such as the unattended mail sweep, which is that daemon's `agent` and the mail adapter's
mapping and nothing besides. Each part takes its own class's target.

**Harness plumbing.** The file tells a harness how to reach the record or a tool: install it, configure
it, recover its local store, sync secrets into an environment, query it, store into it, read a
session's own present state. No step owner would claim it and no sign-off would close it, because it does no
work of the swarm's — it makes a harness able to do work. Target: **none, and that is correct.** A skill
that only tells a harness how to call the record is not a workflow, and declaring one would put the record's
own API into a step list. It stays a skill, kept (as every type outside the four models is kept), and its
`skill` entity is its record.

**Obsolete.** A file that duplicates another at a second root, a near-duplicate pair, a skill for an agent
loop the roster does not contain, and the probe rows the instance holds under the `skill` type. Target:
retire — the file removed from the roots at the freeze, the entity frozen and readable; a probe row in the
production registry is the case `data_model.md#record-conventions` forbids and is closed terminal with a note,
as the retired test rows are (stage 5).

### Role skill → `agent`, and what the file becomes

The prompt is canonical on the record — `agent.prompt_markdown` after stage 7, the retired type's field
until then — and the file is its render target. That is already the stated contract and the built state
(`conformance.md#direction-of-truth-per-class-of-record`, *Agent prompt text*). Two things are decided here
that the contract does not say.

**The file is retired as a load path and kept as a render target.** The runner that reads the file at claim
time is reading a mirror as if it were the source, and the two are one until somebody edits the mirror in
place — which the direction-of-truth section names as the case that destroys work. After stage 7 the runner
reads `agent.prompt_markdown` alone, and a claim by a runner that cannot read the entity is `unknown` and
holds (`data_model.md#what-each-actor-reads-and-writes`, the agent claiming a step): it never falls back to
the file. The file itself is not deleted, for the reason `work_model.md#the-four-execution-mechanisms`
gives: the interactive session is a named mechanism, its harness loads a skill by exactly this path, and a
mirror removed from a harness that hard-requires the path is re-created by hand within the week, which is
the side door. So the mirror stays, rendered and checked, for the harnesses that read it, and nothing that
claims a step reads it. Verification: after the runner's redeployment, a claim with the mirror absent from
the runner's checkout produces the same prompt as a claim with it present, and the runner's `agent_session`
records the entity version it read, never a file path.

**The prompt text is corrected on the entity and re-rendered, never edited in the file.** Almost every role
prompt names a retired type or field — a step map on an artifact, a per-step status row, a held-decision
record — in the call shapes it carries (`status.md`). Stage 7 already reads each retired entity's undeclared
fields out and decides them before the merge; the prompt body is decided in the same pass, as an
interpretation over the same source, and the mirror changes only when the renderer runs. A re-render that
changes anything but names is the verification failure stage 7 already lists.

**A role that has never executed is a design review, not a migration.** Roughly half the roles on the
instance are `planned`: no runner has held a task for them, no daemon writes under their credential, no
sign-off or observation carries their name (`status.md`). A migration carries a record; for these there is
no record to carry — a prompt nobody has executed is a proposal about what an agent should be, and
re-typing it into an `agent` would create a principal on the strength of a document. The rule this document
takes, refining stage 7's "each agent under the retired name": **stage 7 re-types the roles that have
executed** — a credential that has written, a task a runner has held, a sign-off or observation carrying the
name. **A `planned` role is a phase-1 item of the population plan**: phase 1 maps recurring work to roles,
and a planned role either receives recurring work there — in which case a declaration will name its role as
a step owner, and creating the `agent` is the governance write that declaration needs, taken at the gate
under decision 18's reserved default before the declaration is made — or receives none, and is retired in
place, frozen and readable, its prompt kept as the proposal it was. Until one or the other, the roster's
role for it resolves to no `agent`, and any declaration naming that role fails at declaration time with
`unspawnable_assignee` (`failure_posture.md#checkpoints-on-tasks-one-queue-one-protocol`), which is the
design's own check doing exactly the work a migration rule would otherwise have to. The seven planned roles
that carry a procedure verbatim and have never executed are the clearest case: their procedure is the same
one the executed roles carry (below), so nothing in them is unique to the role but the predicate, and the
predicate is a declaration's `applies_when`, not an agent's.

### Procedure skill → `workflow` declaration, step, or adapter operation

The extraction of 2026-09-05 read every skill file at both roots and produced one recurring-work candidate
per procedure, with the skill it came from as the frequency evidence (`status.md` names the counts). The
table maps each procedure's candidate to the target the design has. *Target* is a core workflow of
`workflows.md`, a step of one, an adapter document's operation, or a gap. Where several skills map to one
target, they are one row: the collapse is the point, and the count of rows against the count of skills is
the measure of how much the harness duplicated. A skill named here is named as source state; none of them
is the design.

| Procedure skills (source) | Target | What carries it, and what is dropped |
|---|---|---|
| the session digestion sequence: the mid-session status read-out, the sweep of sessions, the verification of claims, the reconciliation against the inventory, the readiness grading, the batch archival, the session-end audit, the cross-harness resumption | `workflows.md#session-digestion` | one declaration for what seven skills stage by hand: the read-out and the sweep are `digest`, the claim check is `verify`, the inventory merge is `reconcile`, the filing is `file`. The readiness grading is dropped as a step: grading a filed task for what may execute it is intake's `classify` and `route`, already declared for every task, and a second grading beside intake is the second router principle 6 forbids. The archival of sessions and the resumption of one are harness acts over the digestion's outputs and stay plumbing. The rendered task board is a projection over tasks (`data_model.md#concepts`) and is published as one, not as a step |
| the meeting family: recording start and stop, the live transcript stream, audio import, transcript analysis, the transcript import | `workflows.md#meeting-processing`; the capture daemon's `agent` | recording and streaming are the capture daemon's self-trigger and produce the `transcription` entity the workflow's entry condition names; they are not steps. Import and ingest are `ingest`; the analysis is `summarize`, `extract`, `persist`; the recap is `deliver`, which creates an outreach task and never sends. The consent disclosure the recording skill carries is the entry condition's `classify` failure, already declared |
| the analyses: the comparative analysis of a target, the feedback corpus analysis, the category definition and its rollout audit, the portfolio review, the liquidity scorecard | `workflows.md#research-and-analysis` | each is a question answered from sources and persisted as an `analysis` entity: `brief`, `gather`, `synthesize`, `persist`, `deliver`. The rollout audit's per-surface tasks are `deliver`'s successor, a `copy` task per surface. What is dropped: the idempotency each skill implements on its own (a key on the target's identity) is the record's `dedup_key` rule, stated once |
| the writing family: the general writer, the blog post, the comparative series post, the social share material | `workflows.md#copy`; `workflows.md#social-content` | the words are `copy`'s `copy` step against `brand_voice`; the share drafts are `social_content`'s `draft`, `draft_lint`, `consent`, `post`. The style enforcement each skill carries is the `brand_voice` entity retrieved by type and the lint runner's checks, never a prompt's list |
| the outreach family: the rendered proposal page, the prospect intake to a preview, the interactive inbox triage's reply drafts | `workflows.md#outreach` | `draft`, `review`, `consent`, `send`, `follow_up`. The triage's storage of every thread is the mail adapter's inbound mapping (`gmail.md`) and not a step; its archive-on-approval is an outbound operation of the same adapter, taken through the gate |
| the unattended mail sweep | the mail daemon's `agent`; `gmail.md` | mixed class: the procedure is one daemon's whole duty, so it is that daemon's prompt and the adapter's declared mapping of signals to tasks, and no workflow of its own. Its "never send, never archive" is the adapter document's write contract, stated there once |
| the code family: the feature unit's spec, prototype, and final review, the feature run, the bug fix, the error-report queue and its filing, the cross-repo report | `workflows.md#feature`; `workflows.md#bug` | the spec is `pm`, the prototype and the run are `impl`, the final review is `qa` and the consent the batch's `merge` carries; the bug fix and the error queue are `bug`'s `pm` (reproduce) and `impl`; filing an error report is a task entering intake, which the reporting skill already does under another name |
| the release family: the release plan, the publish, the personal-site deploy, the deploy verification | `workflows.md#release` | `criteria`, `release`, `verify_deployed`, one declaration per project; the site's deploy is the release workflow of its project with a `release_criteria` entity of its own |
| the source-control trio: commit, pull, and the branch upload | the `impl` and `merge` steps; `github.md` | step work, not workflows: the operations of the implementer at `impl` and of the steward at `merge`, whose per-step operation the code host's adapter document declares. A commit message convention is a `task_policy` where it is the operator's and an `agent_policy` where it is every agent's |
| the plan family: the execution plan, the plan reconciliation at close, the task sync from a plan's todos | `workflows.md#planning` | the reconciliation at close is the `planning` workflow entered by the record's live instance, pulled forward by the closing task; the task sync is nothing, because a todo is a task from creation and a plan holds no second list; the execution plan's creation is the `amend` step of the parent record's `planning` batch, or the operator's at the root. What is dropped: the session binding, the re-read-and-merge of a map, and every direct correction of a plan's fields from a session (`planning_model.md#binding-dissolves-a-tasks-ascent-is-its-binding`) |
| the session-start clarification batch | `workflows.md#intake` | a batch of clarifications before work starts is intake's `classify` over the tasks a session creates, and its approval gate is a checkpoint on the first action, not a step |
| the learning pair: the behaviour miss turned into a rule, the record-instruction gap repaired | the standing finding and its institutionalization task (`gates_and_workflows.md#a-finding-is-one-off-or-standing-and-a-standing-one-obliges-a-change-to-what-produced-it`, decision 17) | each is a standing finding: a defect that will recur until what produced it changes. The design already routes it — a task enters intake and its governance writes reach the gate. What is dropped is the skills' "apply the fix immediately in the harness", which is an agent mutating what produced a finding on its own finding, the side door the ruling names |
| the record imports: calendar, codebase, contacts, conversations, mail, finances, meetings | the adapters' inbound sourcing (`adapters.md`, `gmail.md`, `calendar.md`) where a document exists; gap G30 where none does | an import is an adapter's inbound operation run by hand, and its result is what the adapter would have produced. Mail and calendar have documents; contacts, conversations, finances, and codebase have none, and a hand-run import of those has no declared mapping to conform to. The calendar skill's outbound half is the calendar document's one event-write path |
| the record maintenance: the leads-graph grooming; the order extraction from a message; the fitness backfill; the technician-slot search; the interview administration; the feedback triage against a release stage | gap G30 | recurring work the extraction found with no candidate workflow and no adapter document: graph curation is a series of lossy record mutations at the gate with no declared step set; the order extraction is a mail-adapter mapping the document does not carry; the slot search is a calendar read for an operator-only task; the rest are one-off procedures whose recurrence the extraction could not establish. Each stays a skill until a declaration exists, and is listed so that its absence from the declared set is visible rather than assumed |
| the plumbing: install and connectivity check, memory query, generic store, the session-storage store, database recovery, secret sync, editor rule creation, editor-copy sync, the background-watcher control, disk cleanup, language check, website scaffold, the present-tense orientation | none (harness plumbing, kept) | no step, no owner, no sign-off; each makes a harness able to reach the record or a tool. Kept as files and as `skill` entities |
| the obsolete: the three files duplicated at two roots, the near-duplicate report pair, the three agent-loop skills, the probe rows under the `skill` type | retire | the duplicates collapse to the root the entity names; the pair to one; the loop skills describe an agent with its own wallet and registration that no roster role names, and are frozen; probe rows are closed terminal (stage 5) |

**What the table shows.** Ten of the declared workflows and three adapter documents absorb the procedures that
own recurring work; the residue is plumbing that should stay a skill, a plan family held on G10, and one row
of extraction gaps. No procedure maps to a workflow the design lacks in kind — every gap is an adapter
document or a declaration not yet written, never a mechanism the design is missing — with one exception
argued below as G31.

### The format gap: where a skill's harness mechanics go

A skill carries what a declaration does not: the names of tools, the shape of a call to the record, the
syntax of a command-line client for mail or the code host, file paths, an environment variable. A `workflow`
declaration carries steps, owner roles, closing conditions, read dependencies, and action classes; an `agent`
carries a prompt and its context entity types; a grant carries operations over entity types and
repositories. None of them has a field for a tool name, and the mapping above would silently drop the
mechanics if it did not say where they go. They go to four places, and the fourth is the operator's.

**What must be read and written goes to the declaration and the write contract.** A skill's "retrieve the
plan, then correct its `todos`" is a `reads_to_enter` and a closing condition; the record's own primitives
— a correction, an interpretation, a relationship — are named in the design already, and a step that says
"the sign-off is written and read back" has said everything the call shape said, minus the harness.

**Which external system, and what operation, goes to the adapter document.** A skill's mail-client syntax
or code-host command is the per-step operation the adapter document declares for that system
(`gmail.md`, `github.md`, `calendar.md`); the per-instance binding of the system to this operator is the
`channel_config` or `vendor_binding` entity, which binds and never redefines the mapping
(`conformance.md#direction-of-truth-per-class-of-record`). A skill that says which client to use for mail is
stating a vendor binding's capability slot, not an instruction.

**How a harness reaches the record goes nowhere in the design, and that is right.** The MCP tool names and
their argument shapes are the record's own interface, harness-neutral and versioned by the record's project,
and they are documented where the record documents them. A prompt that spells them out is a copy of that
documentation which rots on the next interface change — the pattern `status.md` measured: the call shapes
in the role prompts are where the retired field names live. The design's rule is that an agent reaches the
record through its primitives and reaches an external system only through an adapter
(`gates_and_workflows.md#external-systems-are-reached-only-through-adapters`); the harness's way of issuing
those calls is the harness's, and it changes with the harness.

**The residue is decision 42, ruled below.** After the three moves above, a skill still
carries mechanics that bind a harness rather than the record: which tools an agent may invoke (the tool
allowlist that gap G19 had found homeless), which harness a role prefers and which model tier, the
environment variables the harness router reads, hook wiring. These are not the record's and not an
external system's; they are properties of the harness that executes an agent. Whether they belong in the
record at all — as a context entity the design would have to introduce, bound per harness the way a
`vendor_binding` is bound per system — or stay outside it, in the harness's own configuration, read by no
principal and governed by no gate, is not a mapping question. It changes what the record knows about how its
agents execute, and it is ruled below (`#where-a-skills-harness-mechanics-live`): the tools an agent may
invoke are a dimension of its grant, the harness preference and the model tier are a `vendor_binding`'s,
and hook wiring and environment stay the harness's.

### Standing rules inside skills go to `task_policy`, by kind, and never by value

Skills at both roots carry standing rules of the operator's: how correspondence is voiced and signed off,
which timezone a calendar write uses, which client is used for which system, what a payment must never
carry, which task kinds are never marked complete, how a draft is shown before it is sent. The design's
home for an operator preference is the `task_policy` entity, and a harness memory file is a cache of one
and never its home (`conformance.md#direction-of-truth-per-class-of-record`, *Operator preferences*). The
migration derives one `task_policy` per rule **kind** from wherever the rule is stated today — a prompt, a
session instruction file, a procedure skill — and the rule's value is written on the entity, read at
runtime by every agent whose `context_entity_types[]` names the type, and reproduced in no prompt and in no
foundation document, this one included. The kinds this document names so that the derivation is
accountable: correspondence voice and sign-off; channel and client routing per external system; scheduling
defaults; payment constraints; storage and completion discipline per task kind; presentation of drafts to the
operator. A rule that binds every agent's behaviour rather than one operator's preference — how a
sign-off is worded, what a finding must carry, that a response code is not evidence — is an `agent_policy`,
the type the design names as the authoritative home of behavioural rules, and the instance already holds
those (`status.md`). A rule that binds a harness — a hook that refuses a send without an inline approval —
is neither and stays a hook. The derivation is reversible (the derived entities are new and the sources
stay) and the sources are then corrected to cite the type rather than carry the value, which is stage 7's
prompt correction for a prompt and a re-render for its mirror.

### The duplicated procedure, and what the roles collapse to

The role files carry three procedures near-verbatim, differing only in the agent's name and one predicate
(`status.md` gives the counts and the names): a **plan-participation** protocol — check the plan against a
domain predicate, decide whether there is input, file a contribution record with a sign-off or a concern; a
**consultation** protocol for asking another role a question; and, on the review roles, a **per-step
sign-off** on the issue entity — correct the artifact's step map, append to its owner history, check the
join with the parallel step, store a contribution record. The extraction found the same thing from the
other side: one candidate for the plan-participation pattern cited eight roles as its frequency evidence (the
file scan finds a dozen),
and one candidate per review role described the same sign-off with a different judgement in the middle.

That is the evidence the roles collapse. **What differs between the copies is the judgement and the
predicate; what is identical is the procedure, and the procedure is the design's one review step.** A
review step is a step whose work is a judgement of the batch's change, closed by its owner's sign-off like
any other step (`vocabulary.md#review-step`); the sign-off is one entity with one write contract
(`data_model.md#what-each-actor-reads-and-writes`, the agent claiming a step); the join with a parallel
step is the declaration's `parallel_group` and `join_step`; the waive is the operator principal's `waived`
verdict and nobody else's. Each role's copy of the procedure is a hand-written implementation of that one
step, written before the step existed as data, and every copy writes the retired shapes — the step map on
the artifact, the owner history, the contribution record — that the gate-model table above retires.

So the migration does three things with the duplication, and none of them is a merge of roles. **The
procedure is dropped from every prompt** at stage 7: the interpretation that produces the `agent` carries
the judgement — what the `ux` owner looks for, what the `legal` owner refuses — and not the mechanics of
signing, which are the step's and are stated once. **The predicate becomes an `applies_when` condition**
on the optional review step of the declaration that seats it (`workflows.md#how-to-read-a-workflow-section`,
*Applicability*): whether the `ux` step opens on this batch is a property of the declaration read against
what the change touches, and it is the same condition the prompt carried, moved to the one place that
decides which steps open. **The consultation protocol becomes nothing**: a role that cannot judge a step
without another role's answer raises the condition and does not sign (`data_model.md`, the retrieval
contract's absence rule), and a question that needs another role's work is a task; no protocol for asking
is declared because asking is not a step. The roles themselves stay distinct `agent` entities with distinct
prompts, because the judgement differs, and the roster binds each role to one; what the duplication had
been standing in for is one declared review step with different owners, and that is what the declarations
of population phases 3 and 4 write.

**The plan-participation protocol has no target, and this is G31.** A plan is not a batch, a plan is not a
task, and the design has no workflow whose subject is a plan (G10). A dozen roles subscribing to plan events
and reviewing plans against their predicates is recurring work the extraction confirmed, with a shape the
design does not admit: the review of a plan is neither a review step on a batch nor an analysis with a
question. Until G10 says what a plan is in the target model, this procedure is dropped from the prompts with
the rest and recorded as undeclared work, not silently re-declared as something it is not.

### Ordering and the cutover, for the skills

The order is the dependency order the rest of this document uses, and it adds one stage.

**An `agent` exists before a declaration names its role as an owner.** Stage 7 re-types the executed
roles; population phases 3 and 4 then declare, and a declaration whose `owner_role` resolves to no `agent`
is a declaration-time defect (stage 2's check). So the procedure-to-workflow mapping above cannot land a
declaration for a role that stage 7 has not carried, which orders phases 3 and 4 after stage 7 for every
role they name.

**A procedure's workflow exists before its skill is retired, and the two are available together across a
cutover test.** For each procedure row above: the declaration is made (a governance write at the gate,
reserved under decision 18); the skill stays invocable at both roots; the workflow is then executed **end
to end through one real batch** — a task enters intake, is routed to it, every step is claimed by the
principal the roster resolves, every sign-off is read back, the closing sign-off names its successor or
none — and only when that batch has closed is the skill retired: its mirror removed from the roots (or
reduced to a rendered pointer at the one root whose harness hard-requires the path), its `skill` entity
corrected to name the `workflow` that superseded it, the correction read back. A retirement before the
cutover batch has closed is the flag day this document forbids: the swarm would hold neither the old path
nor a proven new one. The cutover batch is the parity test `gates_and_workflows.md#one-step-set-defined-once-tested-for-parity`
asks for, applied to a procedure instead of a step set, and it is the same test the conformance suite runs
for any declaration.

**Stage 11 — the skills (workflow; declarations, then retirements).** For each procedure row: declare,
dual-run, cut over on one closed batch, retire the skill. For each role: the runner reads
`agent.prompt_markdown` alone after its redeployment, verified as above; the mirror stays rendered. For the
plumbing: nothing. For the obsolete: retire at the freeze. **Depends on** stage 7 (the agents), population
phases 3 to 5 (the declarations and the values), and each runner's redeployment. Reversible per skill: the
mirror is regenerated from the entity, and the entity's correction is corrected back; the declaration, being
a governance write, is retired by another. The `task_policy` derivation above belongs to this stage too and
depends on nothing but stage 1.

**Verification for stage 11.** Per procedure: the declaration is retrievable; one batch of its type has
closed with every required step signed and read back; the skill's entity names the workflow; the file is
absent from the roots that no longer hold it, and present and unchanged by a re-render at the root that does.
Per role: the runner's `agent_session` names an entity version and no path; a claim with the mirror absent
produces the same prompt. For the whole: the count of procedure skills at both roots equals the plumbing
count plus the held rows, and the `skill` type's count has stopped moving except for corrections.

**What this stage does not do.** It does not merge two roles into one agent, declare a workflow for a
procedure whose recurrence the extraction could not establish, retire a plumbing skill, or write a
preference's value anywhere but on a `task_policy`. And it does not touch a user-root skill's file before
the operator has confirmed which root each duplicated skill is to keep, because the user root is the
operator's own harness configuration and this document may not write to it.

## How the migration is governed

The design forbids the side door this document would be if it were executed by hand: every write to a
governance type — the closed list is
`gates_and_workflows.md#two-questions-who-may-claim-a-step-and-whether-an-action-may-be-taken`'s — is a
governance write and an action at the gate; every merge, split, and bulk correction is a lossy record mutation and an action at
the gate (`gates_and_workflows.md#two-questions-who-may-claim-a-step-and-whether-an-action-may-be-taken`). A migration that
registered the types, re-typed the agents, and rewrote the grants from a script with a bearer token would
be exactly the ungoverned self-change the design exists to prevent, executed once at the largest blast
radius the swarm will ever see.

The design also states the recursion: the gate that would govern the first governance write is made of
types that write registers, and the first workflow has no workflow to come through
(`work_model.md#changing-the-swarm-is-work-and-it-goes-through-a-workflow-like-any-other`, bootstrapping).
The foundation resolves it by naming the first declaration an operator act, out of band, of the same kind
as issuing a credential. This document takes that resolution and draws the line as narrowly as the
recursion requires, in two legs.

**Leg one, the bootstrap, is an operator act.** It is the smallest set of writes without which no step of
any workflow can open: the registry entries for the design's types and edge types, each with its
`ownership_grant`; the `operator` entity and the credential bindings that let a later write resolve to a
principal; the first `action_policy` for the project, which must list the classes the migration's own
writes carry — a registration, a re-typing merge, a bulk correction, a grant widening, a declaration —
because without a value each is the unclassified case and resolves to `NEVER`, and the leg-two agent could
take nothing; the `intake` declaration and the declaration of the migration's own workflow; the roster
binding for the roles those two declarations name; and the grants those roles need to write the new types —
under decision 56 the sole grant over the governance types goes to the engine, and the roles' grants admit the
ordinary types their steps write (`gates_and_workflows.md#where-the-enforcement-point-for-a-governance-write-sits`).
Every write in this leg is enumerated in stage 1 and 2 below, carries an idempotency key built from the
source's own stable identity, and is read back. It is an operator act because it cannot be anything else,
and it is kept to this list because every write that could have gone through the gate and did not is a
precedent for the next side door. What makes it a control rather than a convention is the conformance
suite's bootstrap test (`conformance_suite.md`): the leg is complete when that test's read-backs hold, and
not before.

**Leg two, the migration proper, is a workflow.** Everything after stage 2 is tasks: one per re-typing
class, one per derivation class, one per freeze, each entering intake, each routed to the migration's
declared workflow, each claimed by the principal the roster resolves for its steps, and each producing its
writes as actions. The writes are then governed by the mechanism the design already has, and they are
governed *per class*: the merge of the eight declarations is one action series; the bulk correction of the
subjectless held decisions is one; each grant widening is one governance write. The `action_policy`
resolves each class to a tier, and the tier decides whether the operator sees a checkpoint per batch or
sees none. Which tier each class takes is a policy value and the operator's to write, class by class —
that is decision 18's ruling, reserved by default
(`work_model.md#changing-the-swarm-is-work-and-it-goes-through-a-workflow-like-any-other`) — and the
section on the policy values below states the two postures and their costs without choosing between them.
Under either posture the design's floors hold without being written here: `operator_only` classes resolve
to `NEVER` ahead of any policy, an unclassified class resolves to `NEVER`, and no class graduates by
recurrence during a migration because a migration's series has no history to graduate on.

**Why not all operator, and why not all workflow.** All operator would mean the operator executing, by
hand, the re-typing of every held decision, every step record, every agent, and every grant — a volume
that makes hand execution a fiction and a script the reality, and a script run by the operator's own
credential is the shared-bearer attribution the authority model forbids. All workflow would mean a
workflow declaring the types it is made of, which the recursion forbids. The line between the legs is
therefore not a preference; it is the smallest set of writes the recursion cannot avoid, and everything
past it goes through the gate. Decision 18's ruling closes the remaining seam: under the reserved default a
class the operator has not granted is not a class leg two skips, it is a class whose action is
`operator_only` — the batch is claimed by the operator-facing agent and the operator makes that write by
hand, inside the workflow, with the batch's record of it
(`work_model.md#changing-the-swarm-is-work-and-it-goes-through-a-workflow-like-any-other`). So leg two
goes through the workflow whether or not the operator grants a single class; what the grants decide is
who takes each action, never whether the record shows it was taken through one.

**The migration's workflow.** Declared by the operator in leg one under the name `record_migration`, one
declaration for the project. Its steps, each with an owner role the roster resolves: `inventory`
(analyst; closes on the stage's pre-flight measurement existing in the record and in `status.md`), `map`
(arch review step; closes on the stage's mapping being reviewed against this document and every write's
idempotency key being derivable from a source's stable identity), `apply` (implementer; closes on every
write of the stage landed and read back), `verify` (qa review step; closes on the stage's verification
below holding, independently re-read), `record` (analyst; closes on `status.md` re-measured for the types
the stage touched, and the batch's closing sign-off naming no successor). `on_fail` on `verify` opens
`apply` again; a declared cap escalates with reason `rounds_exhausted`. Its `reads_to_enter` name the
retired types the stage reads; its `reads_to_close` name the target types it writes, so an unregistered
target is a declaration error caught before any stage opens. The workflow's only successor is none.

## Ordering, dependencies, and what each stage depends on

Stages are ordered by dependency, not by preference, and each names the stage it cannot precede. The
bootstrap sequence the conformance suite derives (`conformance_suite.md`) covers stages 1 and 2; this
document orders against it and states one disagreement at the end of the section. Every stage's writes
carry an idempotency key derived from the source entity's stable identity and the stage's name — never
from a clock — so a stage interrupted and re-run lands once.

**Stage 0 — measure, write nothing.** The record inventory: counts by type for every type this document
names; the status distribution of tasks; the open and terminal distribution of the held decisions and the
retired escalations; the step-record statuses; per re-typed type, which entities carry `raw_fragments`;
every grant capability that names a retired type; the roster's roles against the agents they resolve to
and their credentials; each retired declaration's agent names inverted through the roster, with the ones
that resolve to no role listed. Recorded in `status.md` with its date and instrument. Depends on nothing.
Also here, the instrument is validated (principle 3): the parity of the two retired engines' step records
against each other is measured before either is treated as evidence of anything.

**Stage 1 — the registry (operator act).** Register `operator`, `agent`, `workflow`, `batch`, `sign_off`,
`action`, `checkpoint`, `artifact`, `action_policy`, each with `reducer_config` and version, and the
relationship types `LEASE`, `ADDRESSED_BY`, `FOLLOWS`, `CLOSES`, `SIGNED_BY`, `PRODUCES`, `CHECKPOINTS`,
`AWAITS`, `RESOLVED_BY`, `RAISED_BY`, `principal_binding`, `ownership_grant`, `delegation_edge`
(`DEPENDS_ON`, `PART_OF`, `REFERS_TO`, and `DUPLICATE_OF` the record already has). Create the `operator`
entity; write one `ownership_grant` from each registered type to it. **Depends on** the record admitting
new relationship types at all: the relationship-type vocabulary the record exposes is a closed list that
holds one of the design's new edges and none of the other thirteen, and the record offers no primitive
that registers one (gap G25). Until that is shipped by the record's own project, stage 1 cannot complete and nothing
below it can start; it is the first dependency in the plan and it is not this repository's to resolve.
Additive: the retired engine reads none of these types.

**Stage 2 — the first declarations and the first policy (operator act).** Declare `*|intake` and the
project's `record_migration` workflow; write the project's `action_policy` with a tier for every class the
migration's writes carry (the policy values section below); bind the roster's roles for `pm` step owner, analyst, arch and qa
review steps, and implementer to agents that have credentials; write the grants those agents need for the
new types, and widen the grants of every daemon that will write a new type alongside its retired one.
**Depends on** stage 1. Additive. The declaration-time check runs here: every `owner_role` in both
declarations resolves to an `agent` with a credential, or the declaration is a defect and the stage is not
complete. Leg one ends when the conformance suite's bootstrap read-backs hold.

**Stage 3 — bindings and grants (workflow; governance writes).** Every `agent_grant` naming a retired type
is widened to name the new type alongside it. Each roster role is checked against an `agent` with a
credential, and the ones resolving to a planned agent are listed for the operator. **Depends on** stage 2.
Reversible by correction. **Must precede** any daemon's first write of a new type, else the write is
denied at the enforcement point and, under the design, raises `capability_denied`.

**Stage 4 — the engines: halt, re-type the declarations, cut over (workflow; governance writes and merges).**
The one stage with a moment in it. (a) The operator halts the retired sequencing engines — the daemon
that routes tasks and writes the retired liveness value, the daemon that writes step records, and the
sweep that back-fills step state on artifacts — and the halt is confirmed by read-back, not by the command
returning (`failure_posture.md#the-operator-invoked-halt-and-what-undoes-an-action-already-taken`): no new
retired liveness value on any task and no new step record over a window. (b) For each retired declaration,
in population phase 2's corrected content: create the `workflow` entity as an interpretation of the same
source, merge the retired entity into it, read back both ids. (c) Enable the design's claim predicate for
the project. **Depends on** stages 2 and 3 and population phase 2. (a) is reversible until (b) begins; (b)
is reversible by `split_entity` back onto the retired id; (c) is the cutover and is not reversed except by
repeating (a). The order inside the stage is the hazard section's second rule and is not negotiable: the
retired routing engine falls back to a hardcoded step list when its declaration is not found, so a
declaration merged while that engine is live is a declaration the engine replaces with a literal.

**Stage 5 — the held decisions (workflow; derivations, merges, one bulk correction).** For each open
held decision with a subject: derive the action, the checkpoint, and where needed the artifact; merge the
retired record into the checkpoint. For the subjectless: one bulk correction to terminal, which is a lossy
record mutation and raises its own checkpoint to the operator before it is taken. For the retired
escalations: a task per real one, entering intake; a bulk terminal correction for the test rows.
**Depends on** stage 4 (the operator-facing agent must be reading `checkpoint` before the queue moves, or
the operator sees two queues). Derivations are reversible (the derived rows are new and the source stays);
merges by split; the bulk correction by a further correction.

**Stage 6 — the policies (workflow; governance writes).** Derive the project's `action_policy` content
from the two retired policies that carry blast lists, merging population phase 5's values; retire the rest
in place. **Depends on** stage 2 (the policy exists) and population phase 5 (its values). Reversible by
correction.

**Stage 7 — the agents (workflow; re-typing merges).** For each agent under the retired name: read out
its `raw_fragments`, decide each field, create the `agent` as an interpretation of the same source, write
its `principal_binding`, merge the retired entity into it, read back both ids, and re-render the mirrors
(`conformance.md#direction-of-truth-per-class-of-record`). **Depends on** stages 1 and 3; decision 31 is ruled (the merge form), so nothing here waits on it.
Reversible by split. The retired loader keeps reading the retired name until the daemons are redeployed,
which is why stage 3 widened the grants to both names and why the merged id must keep resolving: a daemon
that loads its definition by the old id mid-migration must get the same content.

**Stage 8 — the tasks (workflow; derivations only).** `PART_OF` from each `parent_task_id`; a checkpoint
on each non-terminal task whose status or reason names a wait for a principal. No status is rewritten.
**Depends on** stage 4 (the claim predicate must already be tolerant, or a derived checkpoint's subject is
claimed while held). Reversible: derived rows and edges only.

**Stage 9 — the artifacts (adapter cutover; no migration writes).** The adapters mint `artifact` from
their cutover; the tolerant reader keyed on system and identifier spans the typed artifacts already
present. **Depends on** stages 1 and 3, and on each adapter's redeployment, which is admission
(`adapters.md#admitting-a-new-adapter`). Nothing to reverse.

**Stage 10 — the freeze (workflow; registry writes where the registry admits them).** The retired
operational types stop being written: verified by counts not moving over a window after every writer is
redeployed; marked deprecated in the registry where a primitive exists to do so, which the record does not
presently expose (a schema can lose fields and gain them; it cannot be retired) — gap G26. The retired
grants' old type names are narrowed away here, completing stage 3's dual-admit window. **Depends on**
every daemon's redeployment. Reversible: nothing is deleted.

**Stage 11 — the skills (workflow; declarations, then retirements).** Stated in full in the skills section above:
per procedure, declare, run both, cut over on one closed batch, retire the skill; per role, the runner reads the
entity alone and the mirror stays rendered. **Depends on** stage 7, population phases 3 to 5, and each runner's
redeployment. Reversible per skill.

**Where this order and the conformance suite's bootstrap may differ.** The suite derives the bootstrap
from the design alone: what must exist for the first step to open. This document adds two things from the
record: the grant widening (stage 3) must precede the first new-type write by a daemon that still runs its
retired code, and the halt (stage 4a) must precede the first declaration merge. Neither follows from the
design, both follow from what is live, and if the suite's sequence places a declaration before a halt,
this document's order stands for an instance with a retired engine live and the suite's for a fresh one.

## The live daemons, and how the plan sequences around them

The daemons keep operating throughout, and a half-migrated record they read is the hazard this section
names. Three shapes of it, and the rule for each.

**Two claim predicates over one task pool.** The retired engine holds a task by writing a liveness value
and reads the age of `updated_at` as the hold; the design holds a task by a `LEASE` edge and reads
nothing on the task. A task the retired engine holds carries no edge, so the design's predicate reads it as
claimable: two runners on one task, which is the failure the claim primitive exists to prevent. Rule: the
design's claim predicate, from the day it is enabled, is a **tolerant reader** over both holds — a task
carrying the retired liveness value inside the retired engine's own age window counts as held — and the
window closes only when the halt of stage 4a is confirmed by read-back. The tolerant predicate is
permanent, not transitional: a task written under the retired engine and never touched again still carries
the value, and a predicate narrowed on the strength of the halt would read it as held forever or as free
depending on which spelling it forgot (`data_model.md#record-conventions`).

**Two sequencing engines over one declaration.** The design forbids two engines that cannot see each other
(`gates_and_workflows.md#declaration-batch-projection`), and an incremental cutover per workflow would
recreate exactly that for the duration. Worse, the retired routing engine substitutes a hardcoded step
list when it cannot find a declaration, so merging one declaration while that engine is live does not
disable the engine for that workflow; it hands the workflow to a literal. Rule: the engine cutover is one
moment per instance, not one per workflow — halt confirmed, then merge, then enable — and what is
incremental is the tasks, each of which enters intake fresh and is carried by its own chain. Migration is
incremental in the design's sense, over the work; it is not incremental over the engines.

**Two queues in front of the operator.** Until the operator-facing agent reads `checkpoint`, and until the
daemons that write the retired held-decision type are redeployed, decisions arrive under two types. Rule:
the operator-facing agent reads both, open, for the dual-write window, and the window is closed by
redeployment and verified by the retired type's open count not moving (stage 10). No decision is answered
in one queue and left open in the other: the merge of stage 5 makes the retired id redirect, so a
resolution written on either resolves the one entity.

**Two writers of a recurring task's next occurrence.** The retired pattern reopens a completed recurring
task by moving its `due_date`; the design closes the instance and has the closing sign-off create the next
(`work_model.md#a-recurring-task-is-one-live-instance-and-its-completion-creates-the-next`). If both run,
the first completion under the design leaves two live instances: the one the sign-off created and the one
the daemon reopened. Rule: the daemon that moves the date stops writing before the first recurring task is
routed through a declared workflow, and the stop is verified the way the halt is — no `due_date` on a
terminal task moves over a window — so that a recurring task has one writer of its succession at any moment.
Until then, recurring tasks stay under the retired pattern and are not routed.

And two hazards that are not the daemons' but arrive with them. A daemon's grant that names only the
retired type denies the daemon's first write of the new one at the enforcement point — silently today,
because the enforcement points fail open, and loudly under the design — which is why stage 3 precedes every
cutover. And a daemon that loads its own definition by the retired id after stage 7 must find it: the merge
pointer is what makes that hold, and the read-back that proves it is an as-of read on the retired id.

## Reversibility

| Write | Reversible | How, or what is checked first |
|---|---|---|
| a schema registration (stage 1) | no | the registry is permanent; a field can be removed and restored, a type cannot be retired (G26). Checked first: the type's name and fields against `data_model.md#concepts`, the `reducer_config` against the field's writers, and the `ownership_grant` present; registered once, by the operator, read back against the registry |
| a correction (stages 3, 5, 6, 10) | yes | a further correction; the superseded value stays readable as an observation |
| a relationship (stages 1, 5, 7, 8) | yes | a soft delete, restorable; the edge's own timestamps record the interval it held |
| a derivation (stages 5, 7, 8) | yes | the derived rows are new entities with provenance to the source, and the source is untouched; reversing is ending them |
| a merge (stages 4, 5, 7) | partly, and only deliberately | `split_entity` re-points the merged observations back onto the retired id by their source or by their time; the survivor remains. Checked first: every field read back against both sources after the merge (`data_model.md#record-conventions`), and a dry read that lists what the merge will repoint. A merge is a lossy record mutation and is held at the gate before it is taken |
| the halt (stage 4a) | yes, until 4b begins | restarting the retired engines. After the first declaration merge, restarting them hands merged workflows to a literal (above), so the check before 4b is the halt's read-back |
| the cutover (stage 4c) | only by repeating the halt | enabling the design's claim predicate is reversed by disabling it under a confirmed halt of the design's engine; there is no state to unwind because the predicate stored nothing |
| a skill's retirement (stage 11) | yes | the mirror is regenerated from its entity by the renderer; the entity's superseded-by correction is corrected back; the declaration it pointed at is retired by another governance write. Checked first: the cutover batch of that workflow has closed with every required step signed and read back |
| a bulk terminal correction (stage 5) | yes, per entity | each is a correction; but it is taken once for the population and is held at the gate as a lossy record mutation before it is |

## Verification

Response codes are not evidence (principle 2). Each stage's read-back is the retrieval that proves it, and
the `verify` step of the migration's workflow re-reads independently of the `apply` step's own read-back.

| Stage | What proves it landed |
|---|---|
| 0 | the inventory in `status.md` carries a date and an instrument for every figure, and the parity measurement of the two retired step records reports a number rather than an absence |
| 1 | the registry describes each type with the fields and version written; a relationship of each new edge type can be written and read back on two test-owned entities that are then ended, never on production entities; each type's `ownership_grant` resolves to the `operator` |
| 2 | both declarations are retrievable, every `owner_role` in them resolves through the roster to an `agent` with a credential, and the `action_policy` resolves every migration class to a tier that is not the unclassified default; the conformance suite's bootstrap read-backs hold |
| 3 | every grant that named a retired type now names both; a read of each grant lists the new type; the roster's unresolved roles are listed, and the list is what the operator expects |
| 4 | (a) no task's retired liveness value changes and no step record is written over the window; (b) each `workflow` resolves by its new id, each retired id resolves with a merge pointer to it, and an as-of read on the retired id at a time before the merge returns the pre-merge content; the count of `workflow` equals the count of retired declarations less the retired smoke-test one; (c) the first claim under the design writes a `LEASE` edge whose `runner_id` the lease holder reads back as its own |
| 5 | every open held decision with a subject has exactly one `checkpoint` whose `CHECKPOINTS` edge names an action or a task and whose `AWAITS` names the operator; the retired open count is zero for those with a subject and terminal for those without; the operator's queue lists them once, not twice |
| 6 | the project's `action_policy` lists a tier for every class the two retired policies listed, and the retired policies' content is unchanged |
| 7 | each `agent` resolves by its new id and by the retired id through the merge pointer; every roster role resolves to an `agent`; every `agent` has a `principal_binding` to the `operator`; a re-render of the mirrors changes nothing but the names |
| 8 | `PART_OF` count equals the count of populated `parent_task_id` fields; each derived checkpoint's subject is a non-terminal task; no task's status changed |
| 9 | the first adapter delivery after cutover produces an observation on an `artifact` keyed by the delivery id, and redelivery produces none |
| 10 | the count of each retired type is unchanged across a window after the last writer's redeployment; each grant names only the new types |
| 11 | per procedure, its `workflow` is retrievable, one batch of its type has closed with every required step signed and read back, and its `skill` entity names that workflow; per role, the runner's `agent_session` names an entity version and no file path, and a claim with the mirror absent yields the same prompt; the `skill` type's count has stopped moving except for corrections |

## Substrate field names the design reads and never adopts as terms

Six names appear across five or more foundation documents with no `vocabulary.md` entry, and none of them
should get one: each is a field or a claim the record's substrate defines, cited by its own spelling so a
reader can write it into a query, and each already has a design term that names the concept. Recording
them here rather than in `vocabulary.md` is what lets the design outlive the substrate's field names — a
substrate that renames one of these changes this table and nothing a foundation document argues.

The rule the corpus already follows, stated once: **a substrate name is set in code font and appears only
where a reader needs to write it into a query; the design's word for the concept is what prose uses.**

| Substrate name | What defines it | The design's term for the concept | Where the mapping is argued |
|---|---|---|---|
| `user_id` | the record store's authenticated account field | [credential](vocabulary.md#credential) — a binding to a [principal](vocabulary.md#principal), many-to-one, never the principal | `authority_model.md#principals`. On a shared instance it collapses every writer onto one value, so a write whose only identity is `user_id` **resolves to no principal and is recorded as unattributed** |
| `sub` | the AAuth token's subject claim | [credential](vocabulary.md#credential), reaching the human principal through the agent's [principal binding](vocabulary.md#principal-binding) | `authority_model.md#principals`; `adapters.md#aauth-is-the-internal-credential-not-a-second-identity-system`. A claim abbreviation is opaque to a reader who does not already know the token format, and the design never argues in terms of it |
| `iss` | the AAuth token's issuer claim | the other half of the `(sub, iss)` pair a [grant](vocabulary.md#grant) is matched on — a [credential](vocabulary.md#credential) qualified by who issued it | `authority_model.md#grants`. Named only where the matching pair is stated; nothing else in the design keys on the issuer alone |
| `conversation` | the harness's transcript container, a registered type (decision 63) | no design term, and none is wanted: what a [sign-off](vocabulary.md#sign-off) references is the [session digest](vocabulary.md#session_digest), never the container or its turns | `data_model.md#record-conventions`; `#session-types-the-field-by-field-shape-decision-64-left-to-the-schema`. Its ~51 fields are the schema-authoring work that section names |
| `raw_fragments` | where the store puts a write carrying an undeclared field, reporting success | no design term: it is the mechanism behind a rule the design does state — a [read-back](vocabulary.md#read-back) asserts the declared field and never the response | `data_model.md#record-conventions`. A field only ever found here is a schema the design has not declared, and a schema version never migrates what already sits in it |
| `reducer_config` | the per-type merge policy every registered type declares | no design term: which fields are last-write and what breaks a tie is the type's own schema authoring, not a design decision (decision 64) | `data_model.md#record-conventions`; `#session-types-the-field-by-field-shape-decision-64-left-to-the-schema` |

**`sub` and `iss` never stand bare as design terms.** Where prose means the thing, it says credential; the
claim names appear only in the two places the pair is being matched or the two credential systems joined,
and both are cited above. This is the same discipline the [principal](vocabulary.md#principal) entry
already states in its ban on "user for a principal (the store's authenticated credential)".

## Gaps and contradictions the mapping exposed

Each is a place the foundation does not say what a migration needs it to say, or says two things. They
are numbered for reference from the register and from the population plan; the register's decision
numbers are separate and only two of these are opened as decisions below.

- **G1 — the governance list is stated three ways.** `gates_and_workflows.md#two-questions-who-may-claim-a-step-and-whether-an-action-may-be-taken`
  names six governance types: `agent`, `action_policy`, `agent_grant`, the roster, the schema
  registry, and (since revision 30) `intake_rule`, and calls the list closed. `work_model.md#changing-the-swarm-is-work-and-it-goes-through-a-workflow-like-any-other`
  names `agent`, `agent_policy`, and `workflow` writes as the governance writes that section "already
  names". `gates_and_workflows.md#a-finding-is-one-off-or-standing-and-a-standing-one-obliges-a-change-to-what-produced-it`
  names `agent`, `agent_policy`, and a `workflow` declaration. A closed list checkable by inspection must
  be one list; a `workflow` declaration and an `agent_policy` are either governance writes or they are
  not, and the migration's stage 4 and stage 7 need to know which. Closed by the memo-gap pass of
  2026-09-05: the list is stated once, in the first of those sections, as eight types — the six (with revision
  30's `intake_rule`), plus `agent_policy` and the `workflow` declaration, each admitted by the test that
  section states — and the
  other two cite it; stage 4's re-typing of the declarations and stage 7's policy writes are governance
  writes.
- **G2 — the design renames types and the record cannot rename a type.** Three types are renamed and the
  record's primitives change fields, identity rules, and observations' owners, never an entity's type.
  The tolerant-reader rule (`data_model.md#record-conventions`) is written for field names; nothing says
  whether it applies to type names, whether a rename is a merge into a new-typed entity (id changes), or
  whether the record should gain an alias. Opened as decision 31, and closed by its ruling of 2026-09-06: a
  rename is the merge form, no alias is built, and the tolerant reader the field rule requires is kept for
  the type name too (`#how-a-registered-entity-type-is-renamed-on-a-live-record`).
- **G3 — a retired escalation's subject is often neither a task nor an action.** The checkpoint admits
  exactly two subjects; the retired type's subjects include configuration entities and checkouts. The
  design should say that a condition about an entity that is not work is a task for intake, or admit a third
  subject; this document assumes the former.
- **G4 — `gate_hold` presumes an action that the held decisions never had.** The retired approval record
  held a task before any action existed. The design has no reason class for a held task whose action is
  not yet an entity; this document derives the action retroactively, which is a choice the design should
  either endorse or replace.
- **G5 — `action_policy` is per project; the retired policy was per plan.** A plan reference, an
  operator-set autonomy level, quality criteria, an agent list, and a fallback instruction have no home.
  Quality criteria in particular: `gates_and_workflows.md#declaration-batch-projection` says a batch
  carries acceptance criteria, and the `batch` row of `data_model.md#concepts` has no field or edge for
  them (G6).
- **G6 — a batch's acceptance criteria have no row.** As above; a migration cannot carry criteria into a
  field that does not exist. Closed by the testability pass of 2026-09-06: the criteria are the tasks' —
  `pm` states them on the task — and the `task` row carries `acceptance_criteria[]`, amended only by a step
  owner's correction naming the finding it answers (`gates_and_workflows.md#declaration-batch-projection`).
- **G7 — the terminal status set is measured, never declared.** `work_model.md#what-a-claim-predicate-treats-as-claimable`
  makes the predicate read the record's live vocabulary, which is right for the reader; the canonical
  writer has no stated terminal spelling to write. Without one, every new writer chooses its own and the
  tolerant reader grows forever. Closed by the testability pass of 2026-09-06: the registered `task` type
  declares its terminal set, one spelling per meaning; the writer writes from it, and the reader stays
  tolerant permanently (`work_model.md#what-a-claim-predicate-treats-as-claimable`).
- **G8 — `blocked` is a stored status and a checkpoint is the held state.** Both exist in the design. It
  does not say which conditions write `blocked` rather than raise a checkpoint, nor what clears `blocked`
  — if a checkpoint's resolution does, `blocked` is a projection and needs the reconciler the design
  requires of one. Closed by the testability pass of 2026-09-06: `blocked` is retired as a status; a task
  the swarm cannot advance is held by an open checkpoint on it, and claimability is read from that edge,
  `unclaimed_step` excepted (`failure_posture.md#checkpoints-on-tasks-one-queue-one-protocol`). The
  derivation row above already carries the waiting statuses onto checkpoints; the field stays for the
  tolerant reader.
- **G9 — `PART_OF` is reserved for child-to-parent and is in use for task-to-plan.** The design gives a
  task at most one `PART_OF`; the instance uses the same edge for plan membership. No edge is named for
  the latter. Closed by the planning pass of 2026-09-06: one edge, one parent, the target a parent task or
  a planning record, and between planning records upward in level
  (`planning_model.md#the-hierarchy-is-edges-and-a-task-has-one-line-upward`); the instance's edges are the
  design's and stay.
- **G10 — plans and their `todos` have no home in the target model.** A plan's `todos` field is a task
  list beside the `task` type. The design does not say whether a todo is a task that failed to be filed
  or a plan's own record, and the migration leaves plans untouched until it does. Closed by the planning
  pass of 2026-09-06: a plan is a planning record, a todo is a task `PART_OF` it, and a decision is a
  `decision` entity `PART_OF` it (`planning_model.md#downward-state-is-derived-upward-content-is-authored-as-entities`);
  the work-model table above carries the derivation.
- **G11 — the transcript file is called an artifact.** `workflows.md#meeting-processing` lists the
  transcript file under *Artifacts* while its entry condition names the `transcription` entity in the
  record; a local file is reached through no adapter and has no `system`, so it is not an artifact by the
  design's own test. Closed by the testability pass of 2026-09-06: the Artifacts line names the calendar
  event alone, and the transcript is a source in the record (`workflows.md#meeting-processing`).
- **G12 — no edge from a task to an entity in the record it concerns.** `REFERS_TO` in the design is task
  to artifact. A task that concerns a transcription, a contact, or a payment configuration — the shape
  every self-triggering daemon produces — has no named edge. Closed by the memo-gap pass of 2026-09-06:
  `REFERS_TO` task → entity is the edge, written by intake's `link` for what the task names and by a step
  for what it discovers (`workflows.md#what-link-attaches-and-what-it-leaves-to-hydration`).
- **G13 — retiring `daemon_report` removes the only signal that distinguishes an idle daemon from a
  halted one.** The write contract retires it; the failure posture says a silently halted swarm is
  indistinguishable from an idle one. What carries a successful empty poll is unstated. Closed by the
  testability pass of 2026-09-06: a successful empty poll writes one observation per declared window on the
  daemon's own `agent_session`, carrying the poll's coverage and the dispositions counted, so a daemon silent
  past its window is a derived read (`adapters.md#what-the-adapter-does-with-every-event`).
- **G14 — `feedback` names two things.** The direction-of-truth table's operator feedback and the
  instance's third-party product feedback shared a type name; the operator's input on reviewed work is,
  by `gates_and_workflows.md`, a finding and not a feedback entity at all. Closed by the simplification
  pass: the table now names `task_policy` alone and points the operator's input at the finding.
- **G15 — the design's finding has no row.** Findings bind, carry severity, and are what a verdict may
  not contradict, and `data_model.md#concepts` gives the sign-off no `findings[]` field and names no
  `finding` entity. The type of that name on the instance records something else. Closed by the
  testability pass of 2026-09-06: the `finding` row — severity, kind, scope, evidence, text — `PART_OF` the
  sign-off that carries it and `REFERS_TO` the batch it judges (`data_model.md#concepts`); the instance's
  type of that name is what stage 1's registration versions over, and the carrying is stage 1's.
- **G16 — only an adapter mints an artifact, and the migration must mint some.** The held decisions on
  merges name pull requests the record holds as typed rows but not as `artifact`; deriving their
  checkpoints needs an artifact that no adapter read produced. Either the derivation is allowed to mint
  from the record's own reference, or those held decisions cannot be carried.
- **G17 — the credential binding has no edge type.** `data_model.md#concepts` says credentials bind to a
  principal many-to-one; the relationships table names no edge for it, and the instance holds the
  bindings as fields on the agent and on the grant.
- **G18 — `agent_grant.expires_at`, `sub`, `iss` versus the source's field names.** A tolerant-reader
  case the design covers, listed because the rotation rule (`authority_model.md#grants`) makes the
  dual-admit window load-bearing for the migration too.
- **G19 — the harness tool allowlist has no home.** `authority_model.md#grants` reasons about a stub
  with a wildcard tool allowlist; the `agent` row has no such field and the grant models operations,
  entity types, and repositories, not harness tools. Closed by the ruling of decision 42 (2026-09-06): the
  tools a principal may invoke are a dimension of its `agent_grant`'s capabilities, and a harness's own
  allowlist is a copy derived from the grant or held equal to it by a parity test
  (`#where-a-skills-harness-mechanics-live`; `data_model.md#concepts`, the grant row).
- **G20 — the roster binds per project in `workflows.md` and is one global map on the instance.**
- **G21 — the roster has no row in `data_model.md`.** It is one of the governance types and the
  resolver every `owner_role` depends on, and its fields and edges are stated nowhere.
- **G22 — incremental migration versus one engine.** The design says migration is incremental and that
  two blind engines are the defect; an incremental engine cutover is two blind engines. This document
  resolves it as incremental over tasks and atomic over engines; the design should say so.
- **G23 — a type rename does not re-key the grants that name it.** Capabilities name entity types as
  strings; the C4 rule (a rename leaves no stale name) does not mention grants, and a grant that names
  only the retired type denies the holder's first new write.
- **G24 — formation admits no way to carry work part-way through a retired passage.** A batch opens only
  on a closing sign-off; nothing lets a migration open a batch at a step. The design's own answer —
  every pre-existing task has no intake batch and re-enters intake, with the operator waiving per step
  what was already judged — is correct and costly, and the design should state it as the rule for
  carried work rather than leave it to be inferred.
- **G25 — the record's relationship-type vocabulary is closed and contains one of the design's edges.**
  Of the edge types the design introduces, only `DEPENDS_ON` (and `PART_OF`, `REFERS_TO`, `DUPLICATE_OF`,
  which the design reuses) is in the vocabulary the record exposes; `LEASE`, `ADDRESSED_BY`, `FOLLOWS`,
  `CLOSES`, `SIGNED_BY`, `PRODUCES`, `CHECKPOINTS`, `AWAITS`, `RESOLVED_BY`, `RAISED_BY`,
  `principal_binding`, `ownership_grant`, and `delegation_edge` cannot be written on the instance, and no
  primitive registers a relationship type. This is the first dependency of the whole plan and belongs to
  the record's project, not this one. `conformance.md`'s decision 67 names this the same shape as
  neotoma#1972's three hand-kept `relationship_type` copies already diverging (28, 28, 8 members) — one
  registry read replacing several literals — and assigns it to the substrate on that basis; this gap is
  the design-side symptom of the same missing mechanism, not a second instance of it to fix separately.
- **G26 — a type cannot be retired in the registry.** Fields can be removed and restored; a type,
  once registered, has no deprecated state. The freeze of stage 10 is therefore held by writers and
  verified by counts, which is weaker than the design's own rule about test types in the shared registry
  implies it wanted.
- **G27 — the design's `agent_session` fields and the source's overlap partially and the design's set
  is smaller.** The source's fields that named a held task and a step are the lease's; the rest is a
  tolerant-reader case. Listed because the design's row is the one place `LEASE` and `agent_session`
  meet and it does not say which of the two carries `runner_id` authoritatively.
- **G28 — one file path is named as the mirror of two canonicals, and the record holds a third.** The
  direction-of-truth table names `.claude/skills/<name>/SKILL.md` as the mirror of `agent_policy` (*Skill bodies*)
  and of `agent.prompt_markdown` (*Agent prompt text*); on the instance the procedure files at that path are rendered
  from `skill` entities by a third renderer. A path with three authoritative sides has none, and the runner's reading
  of the file at claim time is the consequence. The skills section above retires the file as a load path; the table
  should say which entity renders which file.
- **G29 — the record holds a `skill` type the design does not name.** The plumbing skills the migration keeps have a
  canonical entity under a type with no row in `data_model.md` and no place in the four models. Either the design
  admits the type as the record of a harness's own instructions — outside the four models, kept, like `feedback` — or
  it says the file is canonical for plumbing and the entity is the mirror, which would be the one class of record
  whose direction of truth points at a harness. This document keeps the entity and the file and decides neither.
- **G30 — recurring work the extraction found with neither a declaration nor an adapter document.** The hand-run
  imports of contacts, conversations, finances, and codebase state; the leads-graph curation; the order extraction
  from a message; the operator's slot search; the interview administration; the feedback triage. Each is listed in
  the procedure table as a gap row rather than mapped to the nearest workflow, because a procedure mapped to a
  workflow that does not describe it would conform to the letter of the declaration and to nothing.
- **G31 — a dozen roles review plans by predicate, and the design has no workflow whose subject is a plan.** The
  plan-participation protocol the role files carry is recurring work the extraction confirmed, and it is neither a
  review step on a batch nor an analysis answering a question. G10 leaves plans outside the target model; until it
  is closed this work is dropped from the prompts and recorded as undeclared, never re-declared as something else.
  Closed by the planning pass of 2026-09-06, with G10: the work is the `planning` workflow, and a role's
  predicate is an `applies_when` on an optional review step of its declaration whose sign-off carries that
  role's findings on the record (`workflows.md#planning`;
  `planning_model.md#maintenance-is-work-the-planning-workflow`).

## The decisions this document opened, and how each was ruled

Registered in `conformance.md#the-register-of-open-design-decisions` in the change that opened each, as
that section requires, and moved to ruled there on 2026-09-06. Two decisions were opened — 31 by the record
mapping, 42 by the skills leg — and both are ruled below, in the idiom of the earlier rulings: the ruling,
its reason, the cost accepted, and what would reopen it. The other question a reader might expect here —
which tier the migration's classes take — is a policy value under decision 18's ruling and is stated as one
below.

### How a registered entity type is renamed on a live record

**Ruled (decision 31, 2026-09-06): the merge form.** Registered as ruled in
`conformance.md#the-register-of-open-design-decisions`. A rename is carried by the three primitives the
dispositions section already names — register the target type, interpret over the same source, merge the
retired entity into the survivor — so the retired id resolves through its merge pointer and every inbound
edge is repointed; and the tolerant reader that `data_model.md#record-conventions` requires of every reader
for a field name is kept, permanently, for the type name too. No alias is built, and the reader alone is not
the answer.

**The question, and the three answers it had.** Three answers, each with
a cost the others do not have. **Merge into a new-typed entity** (the form this document assumes): the
target entity is created as an interpretation of the same source and the retired entity is merged into
it, so observations, provenance, and every inbound edge move to the survivor and the retired id resolves
through its merge pointer; the cost is that every entity's id changes, so every reference held outside
the record — in a grant's notes, a prompt, a configuration file, a person's memory — points at a
redirect, and every merge is a lossy record mutation held at the gate. **A type alias in the registry**:
the retired name resolves to the new type for reads and refuses new writes, no id changes, and no entity is
touched; the cost is a capability the record does not have and would have to build, and an alias is a
second name for one thing that the tolerant-reader rule was written to avoid needing. **A permanent
tolerant reader over both types**: nothing is written, every reader of `agent` reads `agent` and the
retired type, and the new type is populated only by new writes; the cost is that the design's own claim
that a rename leaves no stale name is false for the record forever, and that every reader carries the
union. **What would decide it,** as the question was opened: whether any reader outside the record holds an
entity id it cannot be told about; if none does, the merge form's cost is nil and it is the answer, because
it is made of primitives the record already has.

**Why the merge, and why not the other two.** Principle 6: the merge is made of primitives the record
already has (`#dispositions-and-the-primitive-that-carries-each`), and the alias is a capability the record
lacks and would have to gain — a parallel mechanism for one case, kept alive by every later rename.
Principle 9: an alias is two names for one thing by construction, which is what the tolerant-reader rule was
written so that the record would never need; and a reader-only rename leaves two live types in the record
forever, so the C4 rule — a rename leaves no stale name
(`gates_and_workflows.md#contradictions-this-document-settles`) — would be false by design rather than false
until a stage completes. Ruled decision 6 makes the cost the merge form was charged with — that every merge
is a lossy record mutation held at the gate — a control already in place rather than a new one: the
re-typing stages are governance writes and merges through the gate
(`#ordering-dependencies-and-what-each-stage-depends-on`), which is where a lossy mutation belongs. And
ruled decision 12 already implies the rename this decision was opened for, the retired agent type to
`agent`, so what was open was only the mechanism. The deciding test is answered by the merge pointer: an
outside holder of the retired id resolves through the redirect to the survivor and its whole history, so it
need not be told; a holder that must write to the survivor is told by the redirect it reads.

**Cost accepted.** Every retired id becomes a redirect, and every reference held outside the record — a
grant's capability, a prompt, a configuration file, a private note — points at a redirect until it is
corrected; the grant corrections are governance writes under ruled decision 18, and stage 3 already makes
them. How many entities and grants that is on an instance is `status.md`'s (the record inventory), not this
section's. The reader stays tolerant of both type names forever, as the field rule already asks of it.

**What would reopen it.** A reader outside the record that holds an entity id, cannot follow a merge
pointer, and cannot be corrected — a system the swarm does not own that has stored the id as a foreign key
with no redirect on read. None is known; one would argue for the alias, and for building it once rather
than per rename.

**What it unblocks.** Stages 4 and 7 (`#ordering-dependencies-and-what-each-stage-depends-on`), which
waited on this ruling; the pending mark on MG-2 and the MG-12 pointer row in `conformance_suite.md`.

### Where a skill's harness mechanics live

**Ruled (decision 42, 2026-09-06): split by what each mechanic is — the tools a principal may invoke are a
dimension of its grant; the harness a role prefers and its model tier are a `vendor_binding` for the harness
as a vendor; hook wiring and environment stay outside the record, as harness plumbing.** Registered as ruled
in `conformance.md#the-register-of-open-design-decisions`. No new context type is introduced.

**The question, and the three answers it had.** After the skills section's three moves — what is read and written to the declaration, which system and operation
to the adapter document, how the record is called to the record's own interface — a skill still carries what binds
a **harness** rather than the record or an external system: the tool allowlist an agent runs under (gap G19), the
harness a role prefers and its model tier, the environment the harness selector reads, hook wiring. Three answers,
each with a cost. **A harness binding in the record**: a context entity, introduced, bound per harness the way a
`vendor_binding` is bound per external system, retrieved by the runner at claim time and by the review of any change
to it; the cost is a new type outside the four models, a per-harness dimension the design has nowhere else, and the
question of whether a write to it is a governance write — it changes what an agent may invoke, which is what the
closed governance list was meant to bound. **Fields on the `agent`**: the allowlist and the preferences ride on the
principal they constrain, as the retired type carried them (G19 lists them as declared-but-unmodelled); the cost is a
public, generic prompt entity carrying harness-specific and instance-specific values, which the public-prompt
constraint forbids, and a `data_model.md` row that grows with every harness. **Outside the record**: the harness's own
configuration, read by no principal and governed by no gate, as it is today; the cost is that the record cannot say
under what tool bounds an agent executed, so a sign-off's pinned agent version pins the prompt and not the reach, and
a change to an agent's reach is invisible to the mechanism the design built to see changes to agents. **What would
decide it,** as the question was opened: whether the tool bound is part of what a sign-off attests — if a review of a
batch may legitimately ask under what allowlist the implementer executed, the bound is in the record; if not, it is the
harness's.

**Why the three go three ways.** The three mechanics are not one kind of thing, and the question treated them as one. **Which
tools a principal may invoke is a bound on what it may do**, and the design has exactly one home for such a bound:
the tuple's [permission scope](vocabulary.md#permission-scope) is "the operations within the domain, with
per-tool parameter constraints", carried by
`agent_grant.capabilities` and `param_constraints` (`authority_model.md#the-tuple`), and `authority_model.md#grants`
already reasons about "a stub with a wildcard tool allowlist" as the fail-open grant shape — so the bound was a
grant's in the design's own words before the grant had a field for it. Principle 6 extends the grant rather than
introducing a per-harness type; ruled decision 41 makes the grant the allowlist read at every enforcement point,
and a wildcard over tools is the same fail-open shape as a wildcard over types. Where a harness needs an allowlist
of its own in its configuration, that list is a copy: derived from the grant at load, or held equal to it by a
parity test, and never a second home (principle 9). **Which harness a role prefers and which model tier** binds
the role to a vendor's instance and settings, which is what a `vendor_binding` is: the design already sends "which
client to use" to a vendor binding's capability slot (`#the-format-gap-where-a-skills-harness-mechanics-go`), and a
harness is a vendor the swarm runs its agents on. **Hook wiring and the environment the harness selector reads** are
how a harness executes, not what an agent may do or where it runs; they are read by no principal and govern no
write, and they change with the harness — the same reasoning that keeps the harness's way of calling the record out
of the design (above). The deciding test is met, and met without a new entity: ruled decision 40 has a sign-off pin
the agent version and name what it read, and "under what allowlist did the implementer execute" is then an as-of
read of the grant at `signed_at`, along ingestion time (`adapters.md#what-the-record-supplies-and-what-an-adapter-therefore-never-builds`) — the grant is in the record already,
so the bound is attestable the moment it lives there. The public-prompt constraint rejects fields on the `agent`: a
harness-specific, instance-specific value on a public, generic entity is the leak that constraint forbids.

**Cost accepted.** `agent_grant.capabilities[]` gains a tool dimension (`data_model.md#concepts`), a schema change
and a governance write per grant that names tools. A harness that cannot enforce a grant-derived allowlist leaves
the bound reporting-only for that harness, and principle 1 names it as such rather than hiding it. The format-gap
section had framed the residue as the operator's; it is ruled here because two ruled decisions and the tuple's own
definition of `scope` answer it, and no operator lean was recorded to contradict.

**What would reopen it.** A harness mechanic that is none of the three — neither a bound on what an agent may do,
nor a binding to a vendor's instance, nor plumbing — would need a home this ruling does not give it; and a
sign-off found to be judged on hook wiring or environment would move that mechanic from plumbing to something the
record must hold.

**What it unblocks.** Stage 11's runner redeployment narrows what a runner may invoke from the grant rather than
from a skill file; the pending mark on MG-13 in `conformance_suite.md` resolves; gap G19 closes.

## The policy values leg two needs, and the two postures

Not a design decision, by decision 18's ruling: which tier each class of migration write takes is a value
in the project's `action_policy`, written by the operator class by class, and reserved — `NEVER` — until
written. This section states the classes and the two postures so that the value is written knowingly,
and chooses neither. The classes are a registration, a re-typing merge, a derivation, a bulk correction, a
grant widening, and a declaration. **Every class left reserved** makes leg two a sequence of checkpoints
the operator approves one by one; the record shows a principal's decision on every write, and the cost is
the queue the design already warns about, where held work is approved in bulk and a tier that holds
becomes a tier that delays. **Registrations and declarations reserved, the rest high blast**: the writes
that change what the swarm is stay the operator's, and the writes that carry existing information into
new shapes are held once per batch and then taken; the cost is that a re-typing merge is reversible only
partly, and a class held per batch is a class the operator sees once for many entities. What would decide
between them is whether the operator will consume a checkpoint per entity for the held decisions and the
agents, which is a measured property of the queue and not a design one (`status.md`). Until the values
are written, every migration class resolves to `NEVER` and leg two cannot start — the reserved posture
the ruling intends, and the safe direction to be waiting in.
