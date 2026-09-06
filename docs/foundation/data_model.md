# Data model: how the foundation's concepts are recorded

**Keyed document:** read when the Neotoma client, a schema script, the data-model renderer, or this
document changes (`conformance.md`). **Kind:** foundation; maps each concept the other documents define
onto the record (entity type, fields, edges, derived reads, projections) and never states which of them
a checkout has registered. **Derived from:** `work_model.md`, `gates_and_workflows.md`,
`failure_posture.md`, `authority_model.md`, principle 9 and principle 11 of `principles.md`, PR #745
operator review (2026-09-04), the operator's 2026-09-05 terminology review (revision 17: the one boundary and the term `external system`, the `action series` rename, `subject` defined, and the two-part `checkpoint`), and the operator memos of 2026-09-05 (the `undetermined_scope` reason
class), and the operator's 2026-09-05 review of review relevance (revision 19: the `applies_when` condition on an optional step, and two terms retired in favour of `review step`), and PR #745 operator review (2026-09-05, rulings 13–14, 16–18, 23–29: `DEPENDS_ON` from a batch to a task it holds on; `PART_OF` between artifacts; the consent tolerance on `action_policy`), and the operator's 2026-09-05 proposal on recurring tasks (revision 27, decision 30: `recurrence` and `due_date` on the task; `FOLLOWS` task → task), and the operator's 2026-09-05 22:02–22:13 memos on how tasks come into existence (revision 30, 2026-09-06: the `intake_rule` row). Which types and edge types the registry holds is `status.md`. Revised by the simplification pass of 2026-09-05 (revision 29: `claimant` and `workflow policy` retired; the checkpoint's reason classes cited from their one home). Revised by the memo-gap pass of 2026-09-06 (revision 31: `REFERS_TO` from a task to a record entity it concerns and from a sign-off to what it read; the source kept in the record; what an `agent_session` is not). Revised by the workflow-format pass of 2026-09-06 (revision 34: the `workflow` row carries every field the declaration declares — `applies_when`, the read dependencies, `freshness`, and the two intervals). Revised by the second workflow-format pass of 2026-09-06 (revision 36: a step's two intervals may each be the task's `due_date`; the special-category mark on a registered type, and the three mechanisms that read it). Revised by the testability pass of 2026-09-06 (revision 37, the conformance suite's findings carried back into their homes: the `finding` row; `sign_off` carrying its findings by edge, `tasks_attached[]`, and the per-kind pinned state, `SIGNED_BY` → principal; `DUPLICATE_OF`; `acceptance_criteria[]` on the task; `recoveries` and `lapse_cap` on `action_policy`; `rounds_cap` and `none_permitted` on the declaration; the daemon's window observation; `blocked` retired as a status). Revised by the rulings pass of 2026-09-06 (revision 38: the `verdict` as the sign-off's reconciled projection; a sign-off's lease check as a derived read; the tool dimension on a grant's capabilities and the budget shape on `param_constraints` and `delegation_edge.scope`; `quorum` and `disjoint_roles[]` per class on `action_policy`; a host's `process` and `checkout` artifact kinds; what an `ownership_grant` confers; no `credit`, `initiative`, or `proposal` type). Revised by the second rulings pass of 2026-09-06 (revision 39: `self_resolved` on the checkpoint, decision 47; `metered_resources[]` per class on `action_policy`, decision 53; the governance types on the engine's grant alone, decision 56; the initiative-class constraint on a `task` write capability as the right to propose, decision 52; a work-model type in `subject_types[]` refused at the write, decision 36). Revised by the planning pass of 2026-09-06 (revision 40: the planning record and the `decision` rows; `PART_OF` from a task to its planning record and between levels; `SUPERSEDES`; the ascent as a derived read on the task). Revised by the Human Inversion mapping pass of 2026-09-06 (revision 44: a sign-off's attribution stops at the agent and its version — the model or harness observed at the write is absent, and named as such beside `vendor_binding`, decision 42, and open decision 59, rather than added as a field). Revised by the minimization-recalibration pass of 2026-09-06 (revision 50: the broadened capture purpose stated in record conventions, and the special-category bullet marked as not reached by it). Revised by the transport-and-delivery pass of 2026-09-06 (revision 51: a paragraph added under Concepts stating why `observation` has no row of its own — not the `finding`/G15 shape, since nothing references it by edge — and why `delivery` is deliberately never stored, both already implied by existing rules and now stated in the one place a reader of this table would look). Revised by the rulings-61-62-64 pass of 2026-09-06 (decision 64 ruled in part — the runner as writer, any declaring step as reader, and the special-category mark's reach to the type per revision 36's F23; the field-by-field shape kept open as the row's remainder). Revised by the sign-off-provenance pass of 2026-09-06 (revision 57: `sign_off` and `REFERS_TO` gain a `session_digest` target, decision 40; the decision-64 note updated for the close-out pass's full ruling — `session_digest`'s field list is schema authoring, not an open decision).

## Purpose

Give every concept exactly one home in the record, so that a schema change can be judged against the
design it implements and a design change can name the schema it needs. The definitions in
`vocabulary.md` say what a concept is; this document says how it is written down: the entity type that
carries it, the fields that are stored, the edges that relate it, the reads that are derived and never
stored, the projections that are stored and reconciled, and, for each, what is deliberately not a field.

## Scope

The concepts of the work model, the gate model, the failure posture, and the authority model. Neotoma is
the record (principle 9): every type here is a Neotoma entity type or relationship type, and a concept
with no row here is a concept the design does not persist. Field names are the design's; a checkout may
carry older names, and the gap is `status.md`.

## Concepts

The rule that decides each row (principle 11): state that would need a watchdog, a sweeper, or a
reconciler to stay correct is an edge with its own timestamps or a derived read, never a field. A
projection is the one exception, and it is reconciled.

One row is easy to over-read. `artifact` names a thing that an **external** system holds, identified by
`system` and `external_id` and reached only through that system's adapter; it is not the general row for
anything the swarm produces. What the swarm writes into the record — a sign off, a checkpoint, an
analysis, a draft, a rendered page held here — is an entity of its own type, and the question that tells
them apart is where the thing lives and how it is read, never how much it looks like an output
(`work_model.md#artifacts-are-records-a-batch-leaves-never-its-subject`).

Two words a reader of the adapters documents leans on hard enough to expect a row here, and neither gets
one, for two different reasons. **`observation`** is not an entity type; it is the write itself — every
mutating write in the record is an observation, on whichever entity it targets, carrying a timestamp and
provenance (`#record-conventions`, above). It has no row because it is not a *kind of thing held*, it is
*how anything is held*: an artifact's `state`, a task's `priority`, a finding's `text` are each read as the
latest observation of that field, so giving `observation` its own row would be a second, competing home for
fields the owning type's row already states. This is not the `finding` gap the testability pass closed —
`finding` needed a row because other rows referenced it by edge (`PART_OF`, `REFERS_TO`) and no row meant
those edges pointed at nothing with a shape; nothing references an observation by edge, because an
observation is never the target, only ever the mechanism a target's field is read through. **`delivery`**
is deliberately never stored as an entity of its own either: the `artifact` row's own Deliberately-not-a-field
column already forbids "the delivery log of the events that updated it," naming a delivery as a real
temptation and refusing it. A delivery is transient by design — it resolves, within the write that handles
it, to one of the four inbound outcomes (an observation, an artifact, a sign-off, or a task) or to
`dropped`, and it is the *outcome* that is recorded, never a receipt of the delivery that produced it
(`adapters.md#what-the-adapter-does-with-every-event`). Even a drop is not a per-delivery row: drops are
counted per window and the count, with its reasons, is itself one observation on the adapter's own
`agent_session` (`adapters.md#what-the-adapter-does-with-every-event`) — aggregate, and reconstructable from
what it produced, never a table of deliveries an adapter has seen. A stored delivery log would be exactly
the sync bookkeeping principle 11 forbids adapters from keeping (`#what-each-actor-reads-and-writes`,
below): a second ledger, beside the record's own observations, that a process would have to keep consistent
with them.

<!-- rendered: data_model concepts -->

| Concept | Entity type | Key fields | Edges (type, direction, target) | Derived reads | Projections | Deliberately not a field |
|---|---|---|---|---|---|---|
| task | `task` | `status` (`open`, or a terminal value from the set the registered type declares — one spelling per meaning, `work_model.md#what-a-claim-predicate-treats-as-claimable`); `title`, `description`; `acceptance_criteria[]` (stated at `pm`; amended only by a correction attributed to a step owner of the batch whose idempotency key names the finding it answers — `gates_and_workflows.md#declaration-batch-projection`); `action_type[]` (declared classes); `assigned_to` (eligibility; a principal's write, never an adapter's from a host's assignment — `work_model.md#assignment-restricts-eligibility-it-never-creates-a-lease`); `priority`; `due_date`; `recurrence` (the rule, on a recurring task; copied to the next instance) | `ADDRESSED_BY` → batch; `PART_OF` → parent task, or → the planning record the task is under (one edge, one parent — `planning_model.md#the-hierarchy-is-edges-and-a-task-has-one-line-upward`); `PRODUCES` → action; `REFERS_TO` → artifact, or a record entity the task concerns (a `finding` the task was produced from among them); `DUPLICATE_OF` → task (the task this one duplicates, written at intake's `dedupe`); `LEASE` ← principal; `CHECKPOINTS` ← checkpoint; `FOLLOWS` → task (the instance whose completion created this one) | claimable (not terminal, no held lease, no open checkpoint holding it, and `assigned_to` unset or naming the claiming principal — `vocabulary.md#claimable`, `work_model.md#what-a-claim-predicate-treats-as-claimable`); active; chain; the ascent (the planning records above it along `PART_OF`), and unplanned where the ascent is empty; current batch; parent completion; the live instance of a recurring task, and its history along `FOLLOWS`; landed (the chain ended under a declaration that permits its ending there — `work_model.md#a-task-is-executed-only-through-a-workflow`) | `step_status` | the lease holder, `claimed_at`, any lease field; a liveness flag; a `blocked` status (a task the swarm cannot advance is held by an open checkpoint on it, and its claimability is read from that edge — `failure_posture.md#checkpoints-on-tasks-one-queue-one-protocol`); the workflow it is in; the list of batches it has gone through; a parent's stored status; a series id, an occurrence count, a next due date computed from completion, or a reopened status (an occurrence is a task; the next is created); an `initiative` or `proposal` type beside it (an initiative is a task by class, and its acceptance is a checkpoint's resolution — `authority_model.md#initiative-approval-is-the-checkpoint`); a `plan_id`, a `project_id`, or any copy of what the records above it say (the ascent is read along the edge) |
| planning record | a registered type the registry marks as a planning type, with a level (`planning_model.md#the-hierarchy-is-edges-and-a-task-has-one-line-upward`; which types an instance registers is decision 57) | the **statement** — purpose, scope and what is out of it, `completion_criteria[]`; `cadence` (the recurrence rule its live `planning` task copies); `title`; the statement is on the engine's grant alone and written as the effect of a permitted `amend_<level>` action (decision 56's shape) | `PART_OF` → planning record of a higher level (at most one); `PART_OF` ← task, ← planning record, ← `decision`; `REFERS_TO` ← task (a task that concerns it and is not under it); `REFERS_TO` ← action (the `amend_<level>` action that amends it); `DEPENDS_ON` ← batch (a planning batch holding on a descendant); `ownership_grant` → principal | completion (every descendant task terminal); the open and terminal counts; the descendants held by an open checkpoint; the most recent activity beneath it; the fraction of descendants landed; the open descendants in priority order; its decisions (the `decision` entities `PART_OF` it); whether it is maintained (one live `planning` task under it); whether a task is unplanned (no planning record on its ascent) | — | a stored `status`, `outcome`, or progress; a `todos` list or its counts (a todo is a task `PART_OF` the record); a `next_steps` list; a `decisions` map; a `parent` id field (the edge); a level field on the entity (the mark is the type's) |
| planning decision | `decision` | the decision, its reason, its date; written once and never edited into another | `PART_OF` → planning record (the record it was taken under); `SUPERSEDES` → `decision` (the one it reverses); `REFERS_TO` ← action (the `amend_<level>` action that wrote it) | which decisions stand under a record (those no later decision supersedes); the history of a reversal | — | a `status` a process would move; a key in a map on the record; a copy on the record |
| batch | `batch` | `project`, `workflow_type`; `status` (open or terminal); `opened_at`, `closed_at`; `successor` named by the closing sign-off, or none | `ADDRESSED_BY` ← task; `FOLLOWS` → batch (the one it follows); `CLOSES` ← sign-off; `PRODUCES` → artifact; `LEASE` ← principal (on a step, `step_name` on the edge); `DEPENDS_ON` → task (a task the batch holds on, `created_at` and `ended_at` on the edge) | step state per step (open, claimed, signed); current step; the successor's batch; whether a step is holding (a held lease, a finding naming a condition, no sign-off); open dependencies | — | the list of attached tasks (edges); a per-step status row; a sequence of workflows above it; a held, paused, or waiting state; a `blocked_by` list |
| lease | relationship `LEASE` | `claimed_at`, `expires_at`, `returned_at`; `runner_id`; `step_name` when the lease is on a step of a batch | principal → task, or principal → batch (step) | `held` (`expires_at` future, no `returned_at`); `lapsed` (`expires_at` past, no `returned_at`); `returned`; whether a sign-off's signer holds it at the write (a `signed` or blocking sign-off from a lease not `held` is refused — `conformance_suite.md#whether-a-sign-off-from-a-step-owner-whose-lease-has-lapsed-closes-the-step`) | — | a stored state; a lock; anything on the task |
| sign-off | `sign_off` | `step_name`; `verdict` (`signed`, a blocking verdict, or `waived` — the operator principal's close of an unsigned required step, carrying the reason; the sign-off's own projection of its findings and its author, reconciled at the write by the refusal below — `gates_and_workflows.md#whether-the-verdict-is-a-stored-field-or-a-read-over-the-findings-and-the-author`); `signed_at`; `agent`, `agent_version` (the signing agent's; absent on the operator principal's `waived`); `artifact_refs[]`, each carrying the artifact's **pinned state** as observed at the moment the verdict was made — `head` for a code artifact, and per kind for the rest (`#record-conventions`); `tasks_attached[]` (the tasks this sign-off attached to the batch part-way, each asserted to inherit the sign-offs already written — `work_model.md#how-a-batch-is-formed-and-what-chooses-its-workflow`); `successor` (closing sign-off only, one or none where the declaration permits none) | `CLOSES` → batch; `SIGNED_BY` → principal (the step owner's agent, or the operator principal on `waived`); `PART_OF` ← finding (the findings it carries); `REFERS_TO` → artifact, or a record entity the step read (its state at `signed_at` is an as-of read), or → `session_digest` (the session that produced the sign-off, required where `SIGNED_BY` is an agent and a digest exists for that session, permitted otherwise — decision 40, `gates_and_workflows.md#what-a-step-leaves-at-close-what-it-produced-and-a-reference-to-what-it-read`) | the signed step state; whether the verdict agrees with the severities of the findings it carries (a blocking finding under a non-blocking verdict is refused at submission — `gates_and_workflows.md#findings-verdicts-and-what-a-blocking-finding-obliges`); whether a referenced artifact's current pinned state differs from the one judged; what the step read, resolved as of `signed_at`; whether the signer's lease on the step is `held` at the write (required for `signed` and a blocking verdict, and the write is refused otherwise; `waived` needs none — `conformance_suite.md#whether-a-sign-off-from-a-step-owner-whose-lease-has-lapsed-closes-the-step`) | the `verdict`, over its own findings and author; contributes to `step_status` | a verdict on an issue or a PR (the subject is the batch's tasks); a `findings[]` copy (findings are entities, `PART_OF` the sign-off); a condition; a stored stale-or-current flag; a copy of what was read; the session's transcript |
| finding | `finding` | `severity` (`blocking`, `non_blocking`); `kind` (`implementation_only` or `decision_or_attestation` on a blocking finding; `hold` on the non-blocking finding that names a condition the step cannot yet judge and what would resolve it — `work_model.md#a-batch-may-hold-on-a-condition-discovered-mid-flight`; absent otherwise); `scope` (`batch`, `step`, `workflow`, `agent`, or `unknown` — the standing axis, one-off being `batch`; `unknown` raises `undetermined_scope` and is never coerced); `evidence` (the executed check and its output, or the mechanism that executed it and the result read; required on a blocking finding, whose write is refused without it); `text` (the one defect, objection, or condition); `step_name`; `recorded_at` | `PART_OF` → sign-off (the sign-off that carries it, where one is written; a hold's finding has none while the hold stands); `REFERS_TO` → batch (the work it judges); `REFERS_TO` ← task (a task the finding produced: a routed remedy, an institutionalization task, a redo — `gates_and_workflows.md#a-finding-is-one-off-or-standing-and-a-standing-one-obliges-a-change-to-what-produced-it`) | whether it blocks (its severity, never the verdict it sits under); whether its remedy is routable (its kind); where its change lands, and whether the change made went there (its scope against the type the produced task's write lands in); a hold's duration (its observations); whether a standing finding was discharged (the task it produced reached a terminal status) | — | a verdict; a condition on a later step (`gates_and_workflows.md#findings-verdicts-and-what-a-blocking-finding-obliges`); a stored discharged or resolved flag; a remark on an artifact (it carries no severity and reaches no step) |
| action | `action` | `action_type`; `confidence`; `dedup_key` (idempotency of the effect); `taken_at` and `result_ref` (the action confirmation, written by the adapter as an observation naming the artifact the effect left) | `PRODUCES` ← task; `REFERS_TO` → artifact it acts on; `CHECKPOINTS` ← checkpoint | blast radius under the policy; whether it may be taken | — | a stored gate decision; the artifact it leaves (an entity of its own) |
| checkpoint | `checkpoint` | `reason` (a class from the one list in `failure_posture.md#checkpoints-on-tasks-one-queue-one-protocol`, or a policy-declared class); `needed_input`; `options[]`; `status` (open, or a terminal approval: approved, denied, vetoed, timed out); `deferral_until`; `raised_at`, `resolved_at`; `resolution_note`; `self_resolved` (the resolver's own assertion at the write that it is the principal `RAISED_BY` names, admitted for the operator alone; a resolution whose resolver is its raiser under the counting rule is refused without it, and any other principal's is refused with it — `authority_model.md#the-raiser-of-a-checkpoint-does-not-resolve-it-and-the-operators-self-resolution-is-marked`) | `CHECKPOINTS` → action or task (the subject, exactly one); `AWAITS` → principal (one or more; a role the raiser names is resolved to principals through the roster when the checkpoint is raised, and the role itself is named in `needed_input` — `failure_posture.md#checkpoints-on-tasks-one-queue-one-protocol`); `RESOLVED_BY` → principal; `RAISED_BY` → principal or agent | the queue (open checkpoints whose `AWAITS` names the reader); quorum and separation of duties over its principals — count and disjointness over `AWAITS`, `RESOLVED_BY`, and `RAISED_BY`, an agent counting as its bound principal, against the class's `quorum` and `disjoint_roles[]` on the `action_policy` (`authority_model.md#structural-checks-are-reads-over-the-checkpoints-principal-edges`); whether its subject task is held from claim (every task-subject class but `unclaimed_step` — `work_model.md#what-a-claim-predicate-treats-as-claimable`); whether its resolver is its raiser, against the mark | — | the subject as free text; a resolver as a bare status write; an unmarked self-resolution; a page or notification record; a vote, tally, or approval-set entity beside it |
| artifact | `artifact` | `kind` (`issue`, `pull_request`, `release`, `page`, `message`, `thread`, `event`, `transfer`, and a host's `process` and `checkout` — `adapters.md#whether-the-host-a-daemon-runs-on-is-an-external-system` — …); `system`; `external_id`; `url`; `state` (per kind: `open`, `closed`, `merged`, `sent`, `settled`, …); `labels[]`; `head`; `checks` (`passing`, `failing`, `pending`, or `unknown`) — the last four written by the adapter as observations (`adapters.md`) | `PRODUCES` ← batch; `REFERS_TO` ← task; `REFERS_TO` ← action; `REFERS_TO` ← sign-off; `PART_OF` → containing artifact (a message to its thread; an occurrence to its series) | whether the record tracks it (any batch or task edge); its container, and the members the record holds | — | step state or verdicts (they belong to the batch and its sign-offs); a workflow instruction; the delivery log of the events that updated it (they are its observations) |
| workflow | `workflow` | `project`, `workflow_type`; `steps[]` (`step_name`, `owner_role` — a role the roster resolves at claim time, never an agent name; `parallel_group`, `join_step`, `required`, `applies_when`, `on_fail`, `rounds_cap` (the rounds an `on_fail` loop may take before `rounds_exhausted`), `reads_to_enter[]`, `reads_to_close[]`, `freshness`, `unclaimed_after`, `hold_bound` — each of the two an interval or the batch's tasks' `due_date`; `gates_and_workflows.md#declaration-batch-projection`); `fast_paths[]`; `successors[]`, `none_permitted` (whether the closing sign-off may name no successor — `gates_and_workflows.md#sequencing-is-data-successors-and-the-chain`) | — | which batches are of it | — | a copy of the step list in code; a floor list; a `phase` field (decision 33: no `steps[].phase` exists — `vocabulary.md#stage`) |
| action policy | `action_policy` | `low_blast_action_types[]`, `high_blast_action_types[]`; `confidence_threshold`; `recurrence_count`; `always_checkpoint_boundaries[]`; `permission_scope`; `consent_tolerance` per action class (the change to an action's consented figures that may be taken without a new checkpoint; absent reads as zero — `payments.md#tolerance-is-an-action_policy-value-and-its-default-is-zero`); `recoveries` (for every class listed in either tier, the class its recovery is taken under, or `forward_only`, or `none`; a policy write listing a class with no entry is refused — `failure_posture.md#the-operator-invoked-halt-and-what-undoes-an-action-already-taken`); `lapse_cap` (the per-task lapse count at which `repeated_lapse` is raised; undeclared raises none — `failure_posture.md#repeated-lapse-raises-a-checkpoint`); `quorum` and `disjoint_roles[]` per action class (the count of awaited principals a resolution needs, and the role pairs on one checkpoint that must resolve to distinct principals; absent, every awaited principal and every named pair — `authority_model.md#the-thresholds-home-is-the-action_policy-per-class`); `metered_resources[]` per action class (the resources a class's actions are counted against a budget term in; absent, none — the permission stays the gate's, and a metered class with no budget term written resolves to `NEVER` — `authority_model.md#budget-is-a-scope-term-that-attenuates`) | — | blast radius for a class (a governance class with no value resolves to `NEVER` — `work_model.md#changing-the-swarm-is-work-and-it-goes-through-a-workflow-like-any-other`); whether a series has graduated; whether a re-quoted action is within tolerance; whether a checkpoint's resolutions meet the class's structural checks; which resources a class's actions are counted in | — | `operator_only` as a policy value (it is `NEVER` ahead of any policy); a tolerance the design supplies |
| intake rule | `intake_rule` | `subject_types[]` (entity types; never a work-model record type, a rule naming one refused at the write — decision 36, `work_model.md#whether-an-intake-rule-may-key-on-the-work-models-own-records`); `change_kinds[]` (`created`, `updated`, `corrected`); `predicate` (over the entity's fields after the change); `provenance_predicate` (system, instance, writer); `task_title`, `task_description` (the text the created task carries, naming the entity that fired it); `ceiling`, `window`; `ended_at` (a rule is ended by correction, never deleted) | — (the tasks it created carry provenance naming the rule and the change; no edge) | the tasks a rule created, by provenance; fires and drops per window; whether a rule is live | — | a last-evaluated cursor; a fired count the evaluator maintains; a successor, a workflow, a step, an action class, or an `assigned_to` for the created task (intake's); a batch it opens (the created task's intake batch opens on creation, as every task's does — `work_model.md#an-intake-rule-turns-a-described-change-in-the-record-into-a-task-and-nothing-else`) |
| agent session | `agent_session` | `runner_id`; `host`, `checkout`, `branch`, `head`; `started_at`, `last_seen_at`; on a daemon's session, one observation per declared window carrying the window, the coverage of the polls or deliveries made in it, and the dispositions counted — the write a successful empty poll makes (`adapters.md#what-the-adapter-does-with-every-event`) | `REFERS_TO` → task | active (with the lease); silent (no window observation past the declared window, while the record is reachable — `failure_posture.md#the-rules`, rule 2) | — | a history of runners; the session's transcript or reasoning; a copy of what the step read (`gates_and_workflows.md#what-a-step-leaves-at-close-what-it-produced-and-a-reference-to-what-it-read`) |
| agent | `agent` | `name`, `prompt_markdown`, `context_entity_types[]`, version | `principal_binding` → principal; `LEASE` → task or batch | — | — | the lease holder as a field on the task |
| adapter | `agent` (a daemon; `adapters.md`) | `name`; the `system` it adapts | `principal_binding` → principal; provenance on every write it makes (the adapter, the system, the delivery id) | which artifacts it tracks (by `system`) | — | a per-artifact map of satisfied steps; an event log beside the artifact's observations; a workflow it reads |
| principal | `operator` (human) or `agent` (non-human) | identity only — the type exists to be a principal; the identifier's form is `multi_tenant.md` section 7 | credentials → principal (many-to-one: `user_id` and a host login to the `operator`; an AAuth `sub` to the `agent`, reaching the human principal through that agent's `principal_binding`); `ownership_grant` ← object; `delegation_edge` → principal | authority chain; whether a write resolves to a principal at all | — | a login string, an address, or a magic value standing in for the principal; `operator_profile` (the descriptive record beside the `operator`, carrying no authority edges); locale or preferences on the principal; a stored credit (a read model over attribution — `authority_model.md#credit-is-a-read-model-over-attribution`) |
| grant | `agent_grant` | `sub`, `iss`; `capabilities[]` (operation × entity types × repositories, and the tools a principal may invoke — `migration.md#where-a-skills-harness-mechanics-live`; a governance type on the engine's grant alone — `gates_and_workflows.md#where-the-enforcement-point-for-a-governance-write-sits`; the right to propose an initiative as the `task` write capability constrained to the initiative class — `authority_model.md#what-stops-is-a-task-the-owner-seat-confirms-it-through-the-checkpoint-and-proposing-is-a-grant-capability`); `param_constraints` (per-tool parameter constraints; a field allowlist on a write capability — `authority_model.md#grants`; a budget, the bound on a resource a capability may consume, attenuating down a delegation chain — `authority_model.md#budget-is-a-scope-term-that-attenuates`); `expires_at` | — | permit, deny, or indeterminate for one request; what has been consumed against a budget (a read over confirmed actions) | — | a wildcard for a human; a harness preference or a model tier (a `vendor_binding`'s capability slot for the harness); a balance or a consumed amount; a governance type on any grant but the engine's |
| delegation | relationship `delegation_edge` | `scope` (a subset of the delegator's; a budget among its terms — `authority_model.md#budget-is-a-scope-term-that-attenuates`); `expires_at` | delegator → delegate | authority chain; attenuation | — | a prose note on a task; a stored balance |

<!-- /rendered -->

## Relationships

<!-- rendered: data_model relationships -->

| Edge type | Source → target | Meaning | What derives from it |
|---|---|---|---|
| `PART_OF` | child task → parent task; task → planning record; planning record → planning record of a higher level; `decision` → planning record; artifact → containing artifact; finding → sign-off | the child is part of the parent's work; the task is under the planning record, and the record under the one above it, one edge per source and always upward in level (`planning_model.md#the-hierarchy-is-edges-and-a-task-has-one-line-upward`); the decision was taken under the record; the contained record is part of the containing one (a message of its thread, an occurrence of its series), where the external system gives ids to both levels; the finding is carried by the sign-off that judged the batch (a hold's finding has no sign-off while the hold stands — `work_model.md#a-batch-may-hold-on-a-condition-discovered-mid-flight`) | parent completion (all children terminal), and completion at every planning level above; a task has at most one parent, and a planning record at most one; the ascent, read upward from a task to the root; a record's descendants and every derived read over them; a record's decisions; an artifact's container and its held members; a message regrouped by the system ends one edge and writes another; the findings a sign-off carries, whose severities its verdict must agree with |
| `SUPERSEDES` | `decision` → `decision` | the source decision reverses or replaces the target, which stays readable as what was decided then (`planning_model.md#downward-state-is-derived-upward-content-is-authored-as-entities`) | which decisions stand under a planning record; the history of a reversal |
| `DUPLICATE_OF` | task → task | the source task duplicates the target and closed terminal for that reason at intake's `dedupe` (`workflows.md#intake`) | which task carries the work; a duplicate's chain ends at intake, and its batch closes with no successor |
| `ADDRESSED_BY` | task → batch | the task is attached to the batch and goes through the workflow with it | a task's current batch (at most one non-terminal); a batch's task set; attach and detach are the writing and ending of this edge |
| `FOLLOWS` | batch → batch; task → task | the source batch opened for the tasks the target batch closed on, naming the source's workflow as successor; the source task is the next instance of a recurring task, created when the target instance's last batch closed, copying its `recurrence` rule (`work_model.md#a-recurring-task-is-one-live-instance-and-its-completion-creates-the-next`) | the chain, read from a live batch back to its intake batch; a recurring task's history, read from its live instance back, and the live instance itself (the one non-terminal task on the path) |
| `DEPENDS_ON` | batch → task | the batch holds on the task it created, and its step owner's sign-off is refused while the edge is unended and the task non-terminal (`work_model.md#a-batch-may-depend-on-a-task-it-created`) | a step's hold; every batch a task is holding up; the cycle walk (task → its live batch → its dependencies), refused at write and at attach where it would close a loop, escalated as `dependency_cycle` where found after |
| `LEASE` | principal → task; principal → batch (with `step_name`) | the principal holds the task, or the step of the batch | held / lapsed / returned; claimable; the claimed step state; active (with activity entities) |
| `CLOSES` | sign-off → batch (with `step_name`) | the step owner closed that step on the batch | the signed step state; the batch's closing sign-off and successor; `step_status` |
| `SIGNED_BY` | sign-off → principal | who signed: the step owner's agent, or the operator principal on a `waived` sign-off (`gates_and_workflows.md#declaration-batch-projection`) | attribution of the sign-off to its step owner, or to the operator who waived |
| `PRODUCES` | task → action; batch → artifact | the task intends the effect; the batch left the record | an action's task for the gate and the dedup key; a batch's artifacts |
| `CHECKPOINTS` | checkpoint → action or task | the held subject | the decision queue; what resumes on resolution (the action is taken or refused; the task is re-claimed or closed) |
| `AWAITS` / `RESOLVED_BY` / `RAISED_BY` | checkpoint → principal | whose decision is needed (a role named by the raiser is resolved to principals through the roster when the checkpoint is raised; the edge never targets a role — `failure_posture.md#checkpoints-on-tasks-one-queue-one-protocol`); who gave it; who raised it | quorum, separation of duties, the queue's scoping |
| `REFERS_TO` | task → artifact, or → a record entity it concerns (a finding it was produced from, or a planning record it bears on and is not under, among them); action → artifact, or → the planning record an `amend_<level>` action amends; sign-off → artifact, or → a record entity it read, or → `session_digest` (the session that produced it — decision 40, required where the signer is an agent and a digest exists, permitted otherwise); finding → batch (the work it judges); agent session → task | the source concerns, acts on, or was judged on the target | intake's `link` step, which attaches what the task names and nothing on relevance alone (`workflows.md#what-link-attaches-and-what-it-leaves-to-hydration`); the anchors hydration resolves a step's reads from, grown by the context a step writes back; the task an adapter creates for intake, to the artifact it concerns; an action's target; a sign-off's evidence and the read set it judged on, reproducible as of `signed_at`, and the session that produced it, resolved as of the same time; the tasks a finding produced, and the scope each was made at against the finding's own |
| `principal_binding` | agent → principal | the principal the agent acts as | attribution; delegation chains |
| `ownership_grant` | object → principal | named accountability | who is asked when the object needs a decision — the required seat on any checkpoint whose subject concerns the object, and nothing more (`authority_model.md#what-owning-confers-the-required-seat`) |
| `delegation_edge` | principal → principal | scoped, time-bounded transfer of rights | the authority chain; attenuation (a budget in `scope` narrows down the chain — `authority_model.md#budget-is-a-scope-term-that-attenuates`) |

<!-- /rendered -->

## Record conventions

- **Observations are the history.** Every write is an append-only observation with a timestamp and
  provenance; an entity's history is read from its observations, and no parallel log, transition event, or
  assignment record is kept (`work_model.md`, principle 9). The current value of a field is the latest
  observation of it.
- **Corrections replace a field, and say so.** A correction is a write that supersedes a field's value
  with an idempotency key naming its intent; it is itself an observation, so the superseded value stays
  readable. A correction never rebuilds a field from a stale copy: the field is re-read and merged first.
- **Idempotency keys on every mutating write.** A write that may be retried carries a key; a mismatch on
  an existing key is refused, and a refusal is stronger evidence of a prior commit than a success response
  is of the present one (`failure_posture.md`, rule 6). An action's `dedup_key` is this convention applied
  to the effect on the external system.
- **Read-back after every write that carries a decision** (principle 2): the retrieval that asserts the
  field holds the value written. A response code is not evidence.
- **A sign-off is pinned to the artifact state it judged.** Each entry in a sign-off's `artifact_refs[]`
  carries the artifact's **pinned state** as observed when the verdict was made, so a verdict against a
  superseded state is readable as one instead of being indistinguishable from a live verdict. What the
  pinned state is depends on the artifact's kind, and the list is closed with the kinds the design names:
  `head` for a code artifact (a pull request, a branch, a release's tag); the message itself for a mail or
  chat message, whose edits are observations and never re-identify it (`gmail.md#messages`,
  `telegram.md#conditions-that-are-not-updates`); the message set the read returned, with its coverage, for
  a thread; the dated fact for a calendar occurrence and the declaration as read for a series (decisions 23
  and 24 — `gmail.md#a-thread-and-its-messages-are-each-artifacts-related-by-part_of`,
  `calendar.md#a-series-and-its-occurrences-are-each-artifacts-related-by-part_of`); the rail state as read
  for a transfer (`payments.md#terminal-is-not-permanent-and-the-design-must-not-assume-it-is`). A kind
  admitted later states its pinned state in its system's document, under linkage
  (`adapters.md#what-an-adapters-document-must-contain`), so the derived staleness read below has a value to
  compare on every kind. For the code artifact, which the rest of this bullet is written against, the head is resolved
  before the work of the step begins, so new commits arriving mid-step cannot re-anchor a verdict onto a
  commit the step owner never read; a round that reopens after a blocking verdict starts from the artifact's
  recorded head, not from the earlier round's branch point. The second half, which `adapters.md` states
  from the event side and this document states from the record side: **a later head does not invalidate a
  sign-off automatically.** A new-commits event is an observation updating the artifact's `head`, and open
  sign-offs are unaffected; a workflow that wants a step to open again on a new head declares that on the
  step, and the event does not do it. A reader that needs to know whether a verdict is current derives it
  by comparing the pinned head to the artifact's — a derived read, never a stored freshness flag that a
  process would have to keep true (principle 11).
- **A sign-off pins the agent version and names what it read; it does not pin the model that read it.**
  `sign_off.agent_version` is the signing [agent](vocabulary.md#agent)'s pinned version, and `artifact_refs[]`
  names what was judged, at what state (above) — together, attribution to the agent, its declared version,
  and its rules in scope. What is absent is the model or harness observed at the write: harness preference
  and model tier are a `vendor_binding`'s, bound to the role the runner fills, not a per-write observation
  (decision 42, `migration.md#where-a-skills-harness-mechanics-live`), so two sign-offs from the same
  `agent_version` may have run under different models with nothing on either row distinguishing them. This
  is not an oversight the schema needs to close by itself: pinning a model per write would add a field the
  registry does not declare, which is a design question — whether a sign-off's attestation extends from
  "which agent, at which version" to "which model, observed at this write" — and not a docs correction.
  Open decision 59 (`gates_and_workflows.md#blast-radius-selects-the-gate-nothing-yet-selects-the-model-a-step-runs-at`)
  is the adjacent, already-open question of whether a class's minimum model tier is checked at the gate;
  a per-sign-off model pin would be the attribution-side counterpart, and is named here as absent rather
  than added.
- **Tolerant readers, canonical writers.** Where one concept has been written under several field names,
  the remedy is a tolerant reader and a canonical writer, never a bulk migration. Every new write uses the
  canonical name only; every reader checks all known spellings, canonical first. A migration cannot make
  readers safely narrow, because the record is append-only: the superseded spelling stays readable in the
  observations that carried it, and any writer or checkout not yet updated keeps producing it. So the
  tolerant reader is permanent, not transitional, and a reader narrowed on the strength of a completed
  migration silently stops seeing everything written before it. The reader also may not assume the query
  layer's coercions hold in process: a server filter that matches `{"value":459}` and `{"value":"459"}`
  alike says nothing about an in-process comparison of the two.
- **A registered type declares how competing observations resolve.** Registration sets the per-field merge
  policy (`reducer_config`): which fields are last-write, what breaks a tie between two writes (the
  `observed_at` the source states, never the arrival time we happened to record), and which types need no
  reducer at all because they are immutable and never corrected. This is why registration is explicit
  rather than inferred from the first store: an inferred type has no declared merge behaviour, and the
  first concurrent writer is the one who discovers that. A field with several concurrent writers is
  written through the correction path, which re-reads and merges; a whole-field store clobbers whatever
  landed in between.
- **Schema versions.** Every entity type is registered with a version, and a sign-off pins the
  `agent` version it was made under; a write that names a field the registered version does not
  declare is not silently accepted as that field.
- **`raw_fragments` for undeclared fields.** A write carrying a field the schema does not declare lands
  in `raw_fragments`, not in the field, and the store reports success. A read-back therefore asserts the
  declared field, never the response; a field that is only ever found in `raw_fragments` is a schema that
  was never registered, and the linter for that shape is named in `principles.md` (invariant 2).
- **Adapter writes are observations, keyed on the delivery id.** An inbound event's write carries the
  external system's delivery id as its idempotency key and provenance naming the adapter and the system,
  so a redelivery lands once. An action confirmation is an observation on the action (`taken_at`,
  `result_ref`), read back from the external system. No edge type is introduced for either: the record
  the effect left is `PRODUCES` from the batch, and the artifact an event concerns is found by `system`
  and `external_id` (`adapters.md`).
- **Sourcing rides on provenance; freshness is a read over it, never a maintained field.** The record
  already carries, for every observation, where it came from, when, and through which interpretation — so
  an adapter records its own sourcing through that mechanism rather than inventing bookkeeping beside it.
  Every observation an adapter writes carries its source (the external system and the adapter), the time
  it was sourced from that system rather than the time it was stored, and the **coverage** of the read
  that produced it: what range the adapter asked for and what it actually got back, so a partial,
  truncated, or paged read is distinguishable from a complete one. That last part is what makes silence
  readable — without it, a page cut short and a system with nothing to report produce identical records,
  and the gap is invisible until something downstream depends on it. How stale a system's picture is, and
  whether it was ever completely read, are then derived by reading provenance across those observations:
  a derived read (principle 11), not a freshness or last-synced field that a process would have to keep
  true and that goes quietly wrong the moment that process stops. The source an observation was interpreted
  from is itself kept in the record, not only named, so that a mapping corrected later is re-applied to
  what was actually read and a past state is traceable to the raw things read at the time
  (`adapters.md#what-the-record-supplies-and-what-an-adapter-therefore-never-builds`).
- **Edges carry their own timestamps and fields.** A relationship is written with `created_at` and,
  where it can end, an explicit end (`returned_at`, `ended_at`); a state derived from an edge is read from
  those, never from a status the edge would have to be transitioned to.
- **A registered entity type carries at most one `ownership_grant`.** One principal is accountable for
  the type's shape and answers when it needs a decision (`authority_model.md#grants`). A type with no
  owner is the one that accumulates use-specific fields from whichever writer arrived first, so that a
  later generic write lands undeclared; a type with two owners has neither. Ownership is the edge the
  record already has for named accountability, not a new field.
- **The purpose the minimization rule below tests against is stated here, once.** Minimization
  (RGPD Art. 5(1)(c)) bounds capture to what is "adequate, relevant and limited to what is necessary for
  the purposes" — a test with no fixed point until the purpose is itself stated, not assumed. The
  purpose this design captures for is an assistant conducting the operator's personal and professional life
  at granularity, where a piece of context with no use today is the input to work not yet conceived
  (`CLAUDE.md`, people-data processing, operator direction 2026-09). Under that purpose capture is
  generous by design, on the same Art. 6(1)(f) legitimate-interest basis the record already operates
  under: more is necessary once the stated purpose is broader, and a broader purpose is the legitimate way
  to make more data necessary, provided it is written down rather than backed into. What this does not
  relax is Art. 9 for a third party (below) or what leaves the record about one
  (`gmail.md#what-this-adapter-refuses`, refusal 1; `workflows.md#outreach`) — the generous side of the
  purpose is capture; the constraint moves to what a write, a draft, or a channel carries outward, stated
  at each of those surfaces rather than at the point of capture.
- **A registered type may be marked special-category, and the mark is read by three mechanisms that already
  exist.** Some types hold what the RGPD's Article 9 names — health, and the other categories the
  people-data rule lists — about the operator or about a person the record concerns: an episode, a result, a
  diagnosis. **The purpose bullet above does not reach this mark.** Article 9 data about a third party sits
  on a different legal basis than the Art. 6(1)(f) interest the broadened purpose extends — a person's
  health, sex life, and religious or political belief are not the operator's to make "useful for future
  work" by stating a broader purpose for his own; a broader purpose changes what is *necessary*, not whose
  data a legitimate-interest basis can reach. The operator's own data in these categories is his to hold
  under the same purpose as everything else; the mark exists for what the record holds about someone else.
  The mark is a property of the **type**, set at registration by the principal accountable for the type, as the merge
  policy and the version are; never a marker on a step's read
  (`gates_and_workflows.md#declaration-batch-projection`), and never a per-subject fact (a person's objection
  is an observation on that person's entity, `workflows.md#intake`), because what differs is how every value
  of the type travels, whichever step reads it and whoever it is about. Three mechanisms read the mark, and
  none is new. **Admission.** Write and read admission per type are by grant (`authority_model.md#grants`,
  decision 41; `#what-each-actor-reads-and-writes`), so a marked type is admitted only to the roles whose
  declarations name it as a read dependency, and a grant naming a marked type for a role no declaration gives
  a reason to read it is the reviewable defect a wildcard grant is. **Reference, never value.** A sign-off
  names what it read and copies nothing (decision 40,
  `gates_and_workflows.md#what-a-step-leaves-at-close-what-it-produced-and-a-reference-to-what-it-read`); for
  a marked type that rule extends to every surface that travels — a task's `title` and `description`, an
  intake rule's `task_title` and `task_description`, a checkpoint's `needed_input` and `options`, a finding,
  a digest, and anything a channel carries
  (`gates_and_workflows.md#work-is-reviewed-on-the-record-and-a-channel-carries-only-what-awaits-the-operator-or-cannot-wait`)
  — each of which names the entity by reference and says what the work is, and carries no value read from
  the type; the value is read on the record, under the reader's grant, and a channel adapter refuses to carry
  one as the payment adapter refuses metadata a policy suppressed. **Minimization at write.** The adapters'
  capture rule (`gmail.md#what-this-adapter-refuses`, refusal 1) and the meeting workflow's
  (`workflows.md#meeting-processing`, `extract`) are the rule for every writer of a marked type — what serves
  the work, summarized where the detail is incidental — stated here once rather than per writer. The mark's
  shape is the registry's, as every registered type's is; what the design fixes is that it lives on the type
  and what reads it.
- **The harness's own transcript store is registered (decision 63, ruled).** `conversation`,
  `conversation_message`, and `session_digest` are not registered types in this design's registry
  (`status.md#revision-46-2026-09-06-the-session-reconciliation-pass--decision-40-against-sanctioned-practice-one-proposal-one-open-question`
  found this by scan; `migration.md`'s population table carries no row for the first two at all). That
  gap means the special-category mark, admission by grant, reference-never-value, and minimization at
  write — every mechanism above — do not reach roughly 70,000 rows of turn-by-turn content, including
  `session_digest`, whose own field description instructs "summarize, never transcribe sensitive
  content" while sampled rows carry direct quotes, named counterparties, and project-identifying detail.
  **This is not the minimization-at-capture rule being loosened for these types; it is an instruction with
  no mechanism behind it, on types the registry does not know about, being replaced by one that reaches.**
  The alternative — leaving the harness's transcript store outside the design's opinion, as `status.md`'s
  revision 46 left it — was tested and rejected: principle 5's default is deny, and a type nobody
  registered is the type an owner never claimed, which is the shape a wildcard grant is refused for
  at the write (`authority_model.md#grants`, decision 41), applied here to a type instead of a write.
  Fail-closed is the position that does not need a second argument:
  registering these three types is strictly a widening of what the existing mechanisms cover, costs
  nothing to a type already correctly shaped, and is the only reading under which "a registered type may
  be marked special-category" is a claim about the record rather than a claim about the subset of it this
  design happened to enumerate. **What is ruled:** the three types are registered, owned, and (for
  `conversation_message` and `session_digest`, which hold turn content) eligible for the special-category
  mark on the same basis as any type whose rows may carry Article 9 data about a third party incidentally
  — the mark does not make every row special-category, it makes the three mechanisms watch the type.
  **What is not ruled here, and is a separate write:** the owner, the merge policy, and the field-by-field
  shape a registration carries (decision 64, below) — `conversation`'s 51 fields already show the
  unowned-type drift this document warns of elsewhere, mixing turn-tracking fields with unrelated ones
  from other writers, and normalizing that shape is its own decision, not a consequence this one carries.
  **The migration consequence, not an argument against.** Roughly 70,000 existing rows predate any mark
  or owner; registering the type does not retroactively minimize what they already hold, exactly as
  a schema version never migrates `raw_fragments` (below) — this is drift to carry in `migration.md`,
  the same way every other keep-and-backfill row in that document's population table already is, not a
  reason to leave the type unregistered.
  **`session_digest`'s "summarize, never transcribe" instruction is the stale thing, not the broadened
  purpose.** Under a minimization-at-capture posture that rule made sense: a digest is read more widely
  than the session it compresses, so verbatim content in it multiplied exposure for no purpose stated
  anywhere. Under the purpose this document now states, a digest's job is different — it is itself
  future work's input, and a paraphrase loses exactly the specific wording, names, and figures that make
  a past decision reconstructible later, which is the failure mode principle 2 (read back what was
  written) already warns against for any record. The correct fix is not to keep the instruction and
  under-enforce it, which is what the sampled rows already show happening; it is to retire the
  instruction for ordinary content and let the registered type's mechanisms — the special-category mark,
  admission by grant, reference-never-value on every surface a digest's fields reach — carry the
  third-party protection instead, the same division of labour refusal 1 states for the mail adapter
  (`gmail.md#what-this-adapter-refuses`). A digest may transcribe; what travels off the digest through a
  channel, task title, or checkpoint still never carries a marked type's value, only a reference to it.
- **Decision 64, ruled in part (2026-09-06): the owner and reader of the three registered session
  types, and the mark's reach — not their field-by-field shape.** Decision 63 registered `conversation`,
  `conversation_message`, and `session_digest`; this rules what is derivable from mechanisms already in the
  design and no more. **The writer is the runner**: it is the process that holds the lease and produces the
  turns (`vocabulary.md#runner` — "the process that runs an agent and holds a lease on the agent's behalf"),
  so a `conversation_message` or a `conversation` observation is written by the runner whose lease was live
  when the turn happened, the same attribution `agent_session` already carries for a step
  (`#a-sign-off-pins-the-agent-version-and-names-what-it-read-it-does-not-pin-the-model-that-read-it`). **The
  reader is any step that declares the read**: nothing above restricts who may read a session or its digest
  beyond the ordinary declared-read discipline every type carries
  (`workflows.md#what-link-attaches-and-what-it-leaves-to-hydration`) — a session's content is
  reachable exactly as any other entity's is, by a step that names it. **The special-category mark applies
  at the type**, per the mark's own rule stated above and per revision 36's F23 (a read dependency on a
  special-category type carries no marker of its own): `conversation_message` and `session_digest` inherit
  the mark decision 63 already granted them at the type, so a session carrying third-party Article 9 content
  is covered by the type's mark and needs no per-row flag — the same division decision 63 draws between "the
  mark reaches the type" and "every row is special-category," applied here to say a row needs no separate
  declaration to be covered.

  **What this does not decide, and stays with the schema.** The field-by-field shape — `conversation`'s 51
  fields, which mix turn-tracking with unrelated writers' fields, and what `conversation_message` and
  `session_digest` declare beyond what decision 63 already named — is the registered type's own business,
  under `ownership_grant` (above: "one principal is accountable for the type's shape"), not this ruling's:
  normalizing three sprawling, organically-grown types is a schema-authoring write with its own read-back,
  the same as any other registration, and deciding the owner and reader first is what gives that write a
  principal to be made by or on behalf of. The ~70,000 existing rows decision 63 already carries as
  migration drift are unchanged by this ruling — assigning a writer and a reader going forward does not
  retroactively attribute what predates either.

  **The field-by-field shape is not a further design decision.** The close-out pass found no open half for
  decision 64 to keep: which fields `conversation`, `conversation_message`, and `session_digest` declare,
  which of `conversation`'s 51 existing fields survive normalization, and the `reducer_config` each needs is
  schema authoring for the newly-named owner (the runner's `ownership_grant` principal, or whoever the
  operator names), made through the ordinary registration path
  (`#type-registration-is-an-owned-decision-write-read-back-tests-never-register-into-the-shared-registry`)
  and authored in `migration.md#session-types-the-field-by-field-shape-decision-64-left-to-the-schema` — not
  a design question this document decides in the abstract, and decision 64 is ruled in full, not in part.
  **One incoming edge is settled independently of that authoring:** decision 40 gives `session_digest` a
  `REFERS_TO` ← sign-off edge (above, the `sign_off` row and the `REFERS_TO` row), required where the
  signing principal is an agent and a digest exists for its session, permitted otherwise. This is a
  consequence of decision 40's ruling, not a claim on the schema-authoring write: the edge's existence is
  settled, `session_digest`'s own field list — including whether it gains a field naming the sign-offs that
  reference it, or leaves that to the reverse read — is the authoring owner's to write.
- **Type registration is an owned decision write, read back; tests never register into the shared
  registry.** Registering a type, or a new version of one, is a write carrying a decision, so it is read
  back against the registry (principle 2) and made by or on behalf of the type's `ownership_grant` principal. A test run that
  registers a type — most visibly one whose name carries a timestamp to keep runs from colliding — leaves
  that type in the production registry permanently, where nothing distinguishes it from a designed one.
  Tests register into their own registry or use a type that already exists.
- **A schema version does not migrate values already sitting in `raw_fragments`.** Declaring a field
  changes what *subsequent* writes may land in it; every earlier write that carried that field is still in
  `raw_fragments`, and stays there until something reads it out and re-writes it as the declared field.
  So registering the schema does not close a gap it appeared to close: a read-back of the declared field
  on an old entity still finds nothing, and the backfill is separate work with its own read-back.
- **Key on what the source says, never on when we happened to look.** An idempotency key, a dedup key, or
  any identity derived for a write is built from the source's own stable values. A wall-clock value inside
  a key permanently poisons the row it keys, because no later write can reproduce it. The converse error
  is as costly: a key derived from *(entity, field, value)* silently refuses any re-submission of a value
  the field has held before while reporting success, so a field that alternates between two values becomes
  unwritable after its first change — the key must distinguish the write, not the value.
- **Merging duplicate entities is a write that carries decisions.** A merge chooses, per field, which of
  two recorded values survives, and each choice is a decision — so every field is read back against both
  sources afterwards, not merely the merge's success response. Edges are repointed from the absorbed
  entity to the survivor rather than re-derived: re-deriving asks whatever produced an edge to produce it
  again, which silently drops every edge whose producer is gone, and edge loss after a merge is invisible
  because the survivor still looks well-formed. The bounded retrieval below is what keeps most merges from
  being necessary at all.

## What each actor reads and writes

The tables above say where each concept lives. This section says who touches it: what an actor must
retrieve before it acts, what it may not read, what it writes to close its work, and what it may never
write. The rows are actor kinds, not named agents — a roster binds roles to agents, and a role appears
here once however many agents fill it. Two documents supply the boundaries the columns enforce:
`adapters.md` (the engine never reads an external system; the adapter never reads a workflow) and
`authority_model.md#grants` (an actor reads what its grant admits, and nothing else).

### Retrieval contract

The rows below are the general form of a per-step **read dependency**
(`gates_and_workflows.md#declaration-batch-projection`): what an actor must be able to read before it acts,
and what absence means when it cannot. A `workflow`'s steps state the same thing per step, in
`reads_to_enter` and `reads_to_close`, with a required freshness for adapter-sourced types — so these five
rows are the actor-kind default and the step declaration is the specific one. Where they differ, the step's
declaration is the operative requirement, and it may only narrow what the actor's grant and its `agent`
definition already admit.

| Actor kind | Retrieves before acting | Must not read | What absence means |
|---|---|---|---|
| agent claiming a step | the batch and its tasks; the `workflow` declaring the step, its `required` and `on_fail`; the sign-offs already on the batch; the artifacts the tasks refer to, with their current `head` and `checks`; the context entity types its own `agent` names | another step owner's in-progress reasoning (there is none in the record — only sign-offs); the external system directly, except through an adapter's observations | the step is not judgeable: `unknown` holds the step, and the owner raises the condition rather than signing. An absent `on_fail`, an absent artifact, an unreadable workflow are each `unknown`, never a permissive default |
| adapter | the artifact for the event, by `system` and `external_id`; the credential binding that resolves the event's actor to a principal; the open steps and open checkpoints that principal owns, to decide which of the four outcomes applies; the action and its `dedup_key` for an outbound operation | **a `workflow`** — an adapter never reads step declarations, sequencing, `on_fail`, or fast paths, because mapping an event to a step's meaning is the engine's judgement, not the boundary's; another adapter's deliveries | the event is unmapped: an observation saying so, or `dropped` with the reason (`adapters.md`). An unresolvable credential is never resolved to the operator; an artifact that is not found is an untracked record, not an implicit new one |
| operator-facing agent | the open checkpoints whose `AWAITS` names the operator, with each subject and its options; the tasks whose `assigned_to` names the operator principal; the `task_policy` and preference entities that govern how the operator is addressed | the operator's grants as a source of what to present (the checkpoint carries its own options); another principal's checkpoint queue | there is nothing to present. An empty queue is reported as an empty queue, never inferred from a failed read — a read that failed is `unknown` and is announced as such |
| self-triggering daemon | its own `agent` and grant; the record's reachability, by a real read of what its work will read; the tasks or artifacts its poll produces work about; where it evaluates intake rules, every rule naming the changed entity's type, and the change's provenance (`work_model.md#an-intake-rule-turns-a-described-change-in-the-record-into-a-task-and-nothing-else`); the `dedup_key` space of any effect it may repeat | step state, which it neither derives nor advances; a workflow; another daemon's leases | the record is unreachable: it halts work and keeps observing (`failure_posture.md` rule 1), announcing on the path that survives the outage. An empty poll is an empty poll only if the poll itself succeeded, and it is written as one — the window observation the write contract names |
| reviewing step owner | the batch's tasks and their acceptance conditions; the artifact at a resolved `head`, and that head recorded for its own sign-off; earlier sign-offs on the batch, to see what has already been judged and at which head; the design basis its review claims to apply | the host's own review state as a substitute for the record's sign-offs; another step owner's verdict as grounds for its own | it cannot judge: the step stays open with the condition announced. A check it could not run is not a passing check, and a blocking verdict rests on what the step owner executed |

### Write contract

| Actor kind | Writes to close its work | Reads back | Never writes |
|---|---|---|---|
| agent claiming a step | one `sign-off` on the step, carrying the verdict, the artifact refs with their judged heads, and the pinned `agent` version; the observations its work produced | the sign-off, retrieved and asserted to hold the verdict written — a response code is not evidence | another principal's sign-off; step state as a field; a verdict anywhere but the record (`failure_posture.md` rule 4); a parallel log of what it did |
| adapter | observations on artifacts; action confirmations on actions (`taken_at`, `result_ref`); a task for intake, where a new-record event concerns a record the swarm does not track; a `dropped` disposition with its reason; a sign-off or a checkpoint resolution it carried in, attributed to the principal the event's credential binds to and to itself as the carrier (A-for-B, never as B — `adapters.md#no-external-event-advances-a-step-by-itself`); one observation per declared window on its own `agent_session` carrying the window's coverage and dispositions (`adapters.md#what-the-adapter-does-with-every-event`) | every write that carries a decision — a sign-off it carried in, a checkpoint resolution, an action confirmation — before it acknowledges the event to the external system | **step state, in any form** — it never opens, claims, closes, or initializes a step, and never writes `not_required`, `not_applicable`, or clear on an artifact with no batch; a task's status; a binding it inferred |
| operator-facing agent | the checkpoint's resolution, attributed to the operator principal and to itself as the agent that carried it (A-for-B, never as B); tasks the operator's instructions produce | the resolution, before it reports to the operator that the decision landed | the operator's decision where the operator gave none; a sign-off on a step it does not own; the operator's `waived` verdict on its own initiative |
| self-triggering daemon | the tasks its poll produces, each entering intake; observations carrying its provenance, among them one per declared window on its own `agent_session` carrying the window, the coverage of the polls made in it, and the dispositions counted — the write a successful empty poll makes, with zero tasks and the poll's coverage (`adapters.md#what-the-adapter-does-with-every-event`); nothing else — an effect it wants is a task it creates, never an action of its own (`work_model.md#a-task-is-executed-only-through-a-workflow`) | each task it created, so a poll that appears to have produced work but wrote none is caught; the window observation, so a daemon silent past its window is a derived read and not an absence | step state; a claim on a task it created (creating is publishing, not claiming); a routing decision that hands work to a named runner; an action `PRODUCES` from no task |
| reviewing step owner | one `sign-off` on its review step, with the head it judged, the findings it recorded, and the evidence it executed | the sign-off | a verdict on a step another principal owns; a comment on the host in place of the sign-off; a verdict rewritten in place (a later judgement is a **new** sign-off, stated as such, and the latest per step owner per head is the one that stands); a verdict contradicting its own findings (the write is rejected at submission); a verdict carrying a condition |

**Bounded retrieval before creating an entity.** Any actor about to create an entity first retrieves for
the one that may already exist, by identifier where it has a concrete one and by type-and-search where it
has a category. The retrieval is bounded — it names the type and the identifying values, rather than
scanning — and its result decides between creating and correcting. Duplicates in the record arise almost
entirely where this step was skipped: two actors resolve the same artifact, the same person, or the
same task independently, and each creates. Merging them afterwards is a write carrying decisions, with
edge loss as its failure mode (above), so the retrieval is cheaper than every path that follows from
omitting it.

**An agent's context entity types come from its own definition.** What an agent may retrieve is
`agent.context_entity_types[]` — the types its role needs — and a type it was not granted is
not read, whether or not a grant would admit the read. The definition is where an agent's information
diet is declared and reviewed; reading past it makes the declaration decorative and makes an agent's
behaviour depend on what happened to be reachable. Where an agent needs a type its definition does not
name, the definition is corrected, which is an owned, reviewed write — never circumvented at runtime. The
grant is the outer bound and the definition the inner one: a read must satisfy both, and a grant that
admits more than the definition names is not permission to read more
(`authority_model.md#grants`).

## Rendering

The concept and relationships tables above are render targets. The operative source is the schema
registry on the record: the registered entity types, their fields and versions, and the relationship
types in use. `execution/scripts/render_data_model.py` produces the two tables from the registry, between
the markers that name them; its `--check` mode exits non-zero when a table on disk differs from the
registry (the pattern of `render_workflow_docs.py` and `render_plan_docs.py`, `conformance.md`). The
prose around the tables (the rule that decides each row, the conventions, this section) is authored here
and reviewed in PRs. A row whose type the registry does not yet hold is hand-authored with the same
marker and becomes the check's expected content the day the type is registered. This section is the
contract for the renderer; whether the script exists is `status.md`.
