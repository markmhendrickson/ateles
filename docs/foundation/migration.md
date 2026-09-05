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
`status.md`). The bootstrap sequence this document orders against is the
conformance suite's (`conformance_suite.md`); where the two disagree, this document says so. Revised by the simplification pass of 2026-09-05 (revision 29: gap G14 closed; `workflow policy` retired).

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
models name or that the built swarm wrote in their place. Out of scope: the code that reads and writes
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
population and the external-record types, which population never touches (stages 8 and 9), and the freeze
of the operational records the retired engine wrote (stage 10). What migration does not have and points to:
the content of every declaration, the per-role gap map, and the governance values themselves, which are
population's phase 5 and the operator's.

**The one line the operator adds to the population plan's `next_steps`**, since this document may not
write to that entity: *"Second leg: `docs/foundation/migration.md` (PR #745) carries the existing record
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
repointed by the merge rather than re-derived (`data_model.md#record-conventions`, on merges). Whether this
is the right mechanism, or whether the record should gain a type alias so that ids never change, is open
decision 31 (below); until it is ruled, re-type means the three-primitive form, and stage 4 onward waits on
the ruling.

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
| `task` | keep | `task` | ids stable; no bulk write. The live status vocabulary stays as written and the claim predicate reads every spelling it carries onto `open`, `blocked`, or terminal (`work_model.md#what-a-claim-predicate-treats-as-claimable`); the canonical writer uses one spelling per meaning (gap G7: the design names no canonical terminal value). `assigned_to` keeps its design meaning, eligibility. `action_type` stays as the declared classes. `priority` stays |
| `task.blocked_reason`, and the statuses that name a wait for a principal (an approval, an input) | derive | `checkpoint` on the task, reason class per the reason's kind | a task waiting on a principal is a task with an open checkpoint, not a status; the derivation reads each non-terminal task whose status or reason names a wait, and raises one checkpoint whose subject is the task. Terminal tasks are left as written. The field stays for the tolerant reader (gap G8: `blocked` is both a design status and a condition a checkpoint holds, and the design does not say which writes which) |
| `task.confidence` (confidence scored on the task) | derive, where an action is derived; else keep as history | `action.confidence` | the design scores confidence on the action at the moment it would be taken; a task-level score is the retired engine's estimate before any action existed. It is carried onto a derived action where one is derived (below) and otherwise stays readable and unused |
| `task.parent_task_id` | derive | `PART_OF` child → parent | one edge per populated field; the field stays. Parent completion is then a derived read |
| `task.recurrence`, and the tasks that carry it | keep; hold the writer | `task` with its `recurrence` rule; the next instance created by the closing sign-off, `FOLLOWS` task → task (`work_model.md#a-recurring-task-is-one-live-instance-and-its-completion-creates-the-next`) | the live instance the retired pattern left — one open task per obligation whose `due_date` a daemon moves after each completion — is already the design's one live instance, so nothing is rewritten and no `FOLLOWS` edge is fabricated for occurrences the retired pattern overwrote (their history was lost when the fields were). What changes is the writer: the daemon that moves the date must stop before the first instance completes under the design, or a completed instance is reopened beside the one its sign-off created — two live instances, which the ruling forbids. The hazard section names it |
| `task.project_id`, and every plan-membership edge | keep, and hold | — | the design names no edge from a task to a plan, and the edge in use for it collides with the design's `PART_OF` (gap G9). Nothing is written until the design says which edge relates a task to the plan that owns it |
| the retired liveness status values on a task (the value the retired engine wrote while a runner held a task, and the age of `updated_at` it read as the hold) | retire, with a dual-read window | `LEASE` edge; `active` derived | no lease can be fabricated for a hold the retired engine took: the design's lease has a runner, a claim time, and an expiry, and the retired record has none of them. Until the old engine is halted (stage 4) the claim predicate treats a task carrying the retired liveness value within the retired engine's own age window as held; after the halt, the value is history and a task with no `LEASE` edge is claimable. This is the hazard section's first rule |
| `agent_session` | keep | `agent_session` | the design names the type. Its design fields (`runner_id`, `host`, `checkout`, `branch`, `head`, `started_at`, `last_seen_at`) and the source's (`native_session_id`, `cwd`, `git_head_sha`, `holder`, `task_id`, a step name and a declaration reference) are a tolerant-reader case; the canonical writer uses the design's. The fields that named a held task and a step become the `LEASE` edge and its `step_name` for new sessions, and stay readable history on old ones |
| `transcription`, `meeting_transcription`, `transcription_run` | keep | entities in the record; a `task` that concerns one attaches it by edge | a transcript in the record is not an artifact: it is not reached through an adapter and has no `system` or `external_id` (`work_model.md#artifacts-are-records-a-batch-leaves-never-its-subject`). The meeting-processing workflow's entry condition already names the `transcription` entity as what the task references (`workflows.md#meeting-processing`). Nothing is re-typed. Two gaps: the same section lists the transcript file as an artifact (G11), and the design names no edge from a task to an entity in the record that it concerns (G12). A transcription enters intake only as the reference of a task a self-triggering daemon creates; the entity's existence creates no task |
| `session_digest` | keep | `session_digest`, the `digest` step's output in `workflows.md#session-digestion` | the design names it as an entity in the record. Existing digests predate the workflow and belong to no batch; they are history, and the `verify` step's states already live in their claims |
| `plan`, its `decisions` and `todos` fields | keep, and hold | — | plans are outside the four models; `conformance.md` makes a plan's `decisions` map the event log of when a decision was taken and this directory the reviewed statement. A `todos` field is a second task list beside the `task` type (principle 9); the design does not say whether a todo is a task, and this document does not decide it (gap G10). No plan is touched by the migration |

### The gate model

| Source type or field (retired names marked) | Disposition | Target | Where the information goes, and why the disposition is safe |
|---|---|---|---|
| `workflow_definition` (retired name) | re-type | `workflow` | the content of each declaration is population phase 2's: `gates[]` become `steps[]` with `step_name`, `join_step`, and `owner_role` — a role obtained by inverting the roster's role-to-agent map for each agent name, which fails loudly for a retired agent name and is the phase-2 correction; the in-line final release step becomes a `successors` entry; `stale_threshold_days` becomes the declared interval after which an unclaimed step raises `unclaimed_step` (`failure_posture.md#checkpoints-on-tasks-one-queue-one-protocol`); `legal_required` becomes an `applies_when` condition on the optional step; a fast path whose condition is a label an external system carries is not carried, because the design forbids it (`workflows.md#how-to-read-a-workflow-section`), and where the label names a workflow type that already has its own declaration the fast path was redundant. A declaration for a smoke-test project is retired rather than re-typed: a test declaration in the shared registry is the case `data_model.md#record-conventions` forbids |
| `participation_record` (retired name), rows at a terminal satisfied status | derive, as history only | none | a satisfied row is the retired engine's record that a step was judged. It is **not** turned into a `sign_off`: the design's sign-off is pinned to an artifact head the retired record never captured, carries a pinned agent version it never captured, and is attributed to a principal that never wrote it. The rows stay as the as-of read of what the retired engine recorded. Where the work they judged is still open, the operator's `waived` sign-off per step, carrying the legacy row as its reason, is the design's own way to carry the judgement forward (stage 4) |
| `participation_record` (retired name), rows at a non-terminal status | retire | step state derived from batch, `LEASE`, and `sign_off` | a row saying a step was opened for a step owner is, in the design, the existence of a batch at that step (open) or a lease on it (claimed), both derived. No batch exists for the work these rows name and no lease can be fabricated, so the rows are frozen history. Nothing is lost that the design would have kept: the design deliberately has no per-step status row |
| `gate_status` (retired name), `workflow_state`, `workflow_run` (retired name), `workflow_gate`, `release_gate`, `task_action`, `owner_history_entry` | retire | `step_status` projection on the task; the batch's sign-offs | the per-artifact step maps the retired engines wrote. The design says step state or verdicts on an artifact belong to the batch and its sign-offs and are deliberately not fields of the artifact (`data_model.md#concepts`). The maps were written by two engines that did not read each other, so their content is not trusted enough to derive from; they are frozen and readable |
| `checkpoint_brief` (retired name), open, with a task and a held class | derive, then re-type | `action` (`PRODUCES` from the task; `action_type`, `confidence` from the brief's fields) and `checkpoint` (`CHECKPOINTS` → the action, reason `gate_hold`, `AWAITS` → the operator principal, `RAISED_BY` → the agent that wrote the brief) | the brief held a task before any action entity existed; the design's `gate_hold` presumes an action (gap G4). The derivation creates the action the gate would have written, then the checkpoint on it, then merges the brief into the checkpoint so the brief's id redirects. The operator then decides each as they decide any checkpoint |
| `checkpoint_brief` (retired name), open, with a merge held on a pull request | derive, then re-type | `artifact` (kind pull request) if the record does not yet hold one; `action` (class merge, `REFERS_TO` → the artifact); `checkpoint` | as above, with the artifact minted from the record's own reference to the external record, not from an adapter read (gap G16: the design says only an adapter mints an artifact, and a migration is not one) |
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
| the swarm's roster | keep; correct | the roster the design names as the role resolver | one of the six governance types; its role-to-agent map is what inverts the retired declarations' agent names into roles (stage 4) and what every `owner_role` resolves through at claim time. Every role must resolve to an `agent` with a credential before any workflow is declared against it; roles that resolve to a planned agent are named in the read-back (stage 3). The design gives it no row in `data_model.md` (gap G21) and says it binds per project where the instance's is global (gap G20) |
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
The external-record types the built adapters already write — issues, pull requests, reviews, mail
messages and threads, calendar events, posts, transactions, payment events — are **keep** with a tolerant
reader keyed on the external system and identifier: the design's `artifact` is introduced and minted by
adapters from cutover, no existing typed external record is bulk re-typed (the population would make it the
largest lossy mutation in the plan for no reader's benefit), and intake's `link` step attaches whichever
row exists. This is gap G2 in its second form: the tolerant-reader rule is written for field names and the
design needs it stated for types.

## How the migration is governed

The design forbids the side door this document would be if it were executed by hand: every write to
`agent`, `action_policy`, `agent_grant`, the roster, or the schema registry is a governance write and an
action at the gate; every merge, split, and bulk correction is a lossy record mutation and an action at
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
binding for the roles those two declarations name; and the grants those roles need to write the new types.
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
(`conformance.md#direction-of-truth-per-class-of-record`). **Depends on** stages 1 and 3 and decision 31.
Reversible by split. The retired loader keeps reading the retired name until the daemons are redeployed,
which is why stage 3 widened the grants to both names and why the merged id must keep resolving: a daemon
that loads its definition by the old id mid-migration must get the same content.

**Stage 8 — the tasks (workflow; derivations only).** `PART_OF` from each `parent_task_id`; a checkpoint
on each non-terminal task whose status or reason names a wait for a principal. No status is rewritten.
**Depends on** stage 4 (the claim predicate must already be tolerant, or a derived checkpoint's subject is
claimed while held). Reversible: derived rows and edges only.

**Stage 9 — the artifacts (adapter cutover; no migration writes).** The adapters mint `artifact` from
their cutover; the tolerant reader keyed on system and identifier spans the typed external records already
present. **Depends on** stages 1 and 3, and on each adapter's redeployment, which is admission
(`adapters.md#admitting-a-new-adapter`). Nothing to reverse.

**Stage 10 — the freeze (workflow; registry writes where the registry admits them).** The retired
operational types stop being written: verified by counts not moving over a window after every writer is
redeployed; marked deprecated in the registry where a primitive exists to do so, which the record does not
presently expose (a schema can lose fields and gain them; it cannot be retired) — gap G26. The retired
grants' old type names are narrowed away here, completing stage 3's dual-admit window. **Depends on**
every daemon's redeployment. Reversible: nothing is deleted.

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
  not, and the migration's stage 4 and stage 7 need to know which.
- **G2 — the design renames types and the record cannot rename a type.** Three types are renamed and the
  record's primitives change fields, identity rules, and observations' owners, never an entity's type.
  The tolerant-reader rule (`data_model.md#record-conventions`) is written for field names; nothing says
  whether it applies to type names, whether a rename is a merge into a new-typed entity (id changes), or
  whether the record should gain an alias. Opened as decision 31.
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
  field that does not exist.
- **G7 — the terminal status set is measured, never declared.** `work_model.md#what-a-claim-predicate-treats-as-claimable`
  makes the predicate read the record's live vocabulary, which is right for the reader; the canonical
  writer has no stated terminal spelling to write. Without one, every new writer chooses its own and the
  tolerant reader grows forever.
- **G8 — `blocked` is a stored status and a checkpoint is the held state.** Both exist in the design. It
  does not say which conditions write `blocked` rather than raise a checkpoint, nor what clears `blocked`
  — if a checkpoint's resolution does, `blocked` is a projection and needs the reconciler the design
  requires of one.
- **G9 — `PART_OF` is reserved for child-to-parent and is in use for task-to-plan.** The design gives a
  task at most one `PART_OF`; the instance uses the same edge for plan membership. No edge is named for
  the latter.
- **G10 — plans and their `todos` have no home in the target model.** A plan's `todos` field is a task
  list beside the `task` type. The design does not say whether a todo is a task that failed to be filed
  or a plan's own record, and the migration leaves plans untouched until it does.
- **G11 — the transcript file is called an artifact.** `workflows.md#meeting-processing` lists the
  transcript file under *Artifacts* while its entry condition names the `transcription` entity in the
  record; a local file is reached through no adapter and has no `system`, so it is not an artifact by the
  design's own test.
- **G12 — no edge from a task to an entity in the record it concerns.** `REFERS_TO` in the design is task
  to artifact. A task that concerns a transcription, a contact, or a payment configuration — the shape
  every self-triggering daemon produces — has no named edge.
- **G13 — retiring `daemon_report` removes the only signal that distinguishes an idle daemon from a
  halted one.** The write contract retires it; the failure posture says a silently halted swarm is
  indistinguishable from an idle one. What carries a successful empty poll is unstated.
- **G14 — `feedback` names two things.** The direction-of-truth table's operator feedback and the
  instance's third-party product feedback shared a type name; the operator's input on reviewed work is,
  by `gates_and_workflows.md`, a finding and not a feedback entity at all. Closed by the simplification
  pass: the table now names `task_policy` alone and points the operator's input at the finding.
- **G15 — the design's finding has no row.** Findings bind, carry severity, and are what a verdict may
  not contradict, and `data_model.md#concepts` gives the sign-off no `findings[]` field and names no
  `finding` entity. The type of that name on the instance records something else.
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
  entity types, and repositories, not harness tools.
- **G20 — the roster binds per project in `workflows.md` and is one global map on the instance.**
- **G21 — the roster has no row in `data_model.md`.** It is one of the six governance types and the
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
  the record's project, not this one.
- **G26 — a type cannot be retired in the registry.** Fields can be removed and restored; a type,
  once registered, has no deprecated state. The freeze of stage 10 is therefore held by writers and
  verified by counts, which is weaker than the design's own rule about test types in the shared registry
  implies it wanted.
- **G27 — the design's `agent_session` fields and the source's overlap partially and the design's set
  is smaller.** The source's fields that named a held task and a step are the lease's; the rest is a
  tolerant-reader case. Listed because the design's row is the one place `LEASE` and `agent_session`
  meet and it does not say which of the two carries `runner_id` authoritatively.

## The open decision this document opens

Registered in `conformance.md#the-register-of-open-design-decisions` in the same change, as that
section requires. One decision is opened; the other question a reader might expect here — which tier the
migration's classes take — is a policy value under decision 18's ruling and is stated as one below.

**Open decision 31: how a registered entity type is renamed on a live record.** Three answers, each with
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
union. **What would decide it:** whether any reader outside the record holds an entity id it cannot be
told about; if none does, the merge form's cost is nil and it is the answer, because it is made of
primitives the record already has. **Until it is taken**, stages 4 and 7 wait, and stages 0 to 3 do not.

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
