# Data model: how the foundation's concepts are recorded

**Keyed document:** read when the Neotoma client, a schema script, the data-model renderer, or this
document changes (`conformance.md`). **Kind:** foundation; maps each concept the other documents define
onto the record (entity type, fields, edges, derived reads, projections) and never states which of them
a checkout has registered. **Derived from:** `work_model.md`, `gates_and_workflows.md`,
`failure_posture.md`, `authority_model.md`, principle 9 and principle 11 of `principles.md`, PR #745
operator review (2026-09-04), the operator's 2026-09-05 terminology review (revision 17: the one boundary and the term `external system`, the `action series` rename, `subject` defined, and the two-part `checkpoint`), and the operator memos of 2026-09-05 (the `undetermined_scope` reason
class), and the operator's 2026-09-05 review of review relevance (revision 19: the `applies_when` condition on an optional step, and two terms retired in favour of `review step`), and PR #745 operator review (2026-09-05, rulings 13–14, 16–18, 23–29: `DEPENDS_ON` from a batch to a task it holds on; `PART_OF` between artifacts; the consent tolerance on `action_policy`). Which types and edge types the registry holds is `status.md`.

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

One row is easy to over-read. `artifact` is the record of a thing in an **external** system, identified by
`system` and `external_id` and reached only through that system's adapter; it is not the general row for
anything the swarm produces. What the swarm writes into the record — a sign off, a checkpoint, an
analysis, a draft, a rendered page held here — is an entity of its own type, and the question that tells
them apart is where the thing lives and how it is read, never how much it looks like an output
(`work_model.md#artifacts-are-records-a-batch-leaves-never-its-subject`).

<!-- rendered: data_model concepts -->

| Concept | Entity type | Key fields | Edges (type, direction, target) | Derived reads | Projections | Deliberately not a field |
|---|---|---|---|---|---|---|
| task | `task` | `status` (`open`, `blocked`, or a terminal value); `title`, `description`; `action_type[]` (declared classes); `assigned_to` (eligibility); `priority` | `ADDRESSED_BY` → batch; `PART_OF` → parent task; `PRODUCES` → action; `REFERS_TO` → artifact; `LEASE` ← principal; `CHECKPOINTS` ← checkpoint | claimable; active; chain; current batch; parent completion | `step_status` | claimant, `claimed_at`, any lease field; a liveness flag; the workflow it is in; the list of batches it has gone through; a parent's stored status |
| batch | `batch` | `project`, `workflow_type`; `status` (open or terminal); `opened_at`, `closed_at`; `successor` named by the closing sign-off, or none | `ADDRESSED_BY` ← task; `FOLLOWS` → batch (the one it follows); `CLOSES` ← sign-off; `PRODUCES` → artifact; `LEASE` ← principal (on a step, `step_name` on the edge); `DEPENDS_ON` → task (a task the batch holds on, `created_at` and `ended_at` on the edge) | step state per step (open, claimed, signed); current step; the successor's batch; whether a step is holding (a held lease, a finding naming a condition, no sign-off); open dependencies | — | the list of attached tasks (edges); a per-step status row; a sequence of workflows above it; a held, paused, or waiting state; a `blocked_by` list |
| lease | relationship `LEASE` | `claimed_at`, `expires_at`, `returned_at`; `runner_id`; `step_name` when the lease is on a step of a batch | principal → task, or principal → batch (step) | `held` (`expires_at` future, no `returned_at`); `lapsed` (`expires_at` past, no `returned_at`); `returned` | — | a stored state; a lock; anything on the task |
| sign-off | `sign_off` | `step_name`; `verdict` (`signed`, a blocking verdict, or `waived` — the operator principal's close of an unsigned required step, carrying the reason); `signed_at`; `agent`; `agent_version`; `artifact_refs[]`, each carrying the artifact's observed `head` at the moment the verdict was made; `successor` (closing sign-off only, one or none) | `CLOSES` → batch; `SIGNED_BY` → agent; `REFERS_TO` → artifact | the signed step state; whether a referenced artifact's current `head` differs from the one judged | contributes to `step_status` | a verdict on an issue or a PR (the subject is the batch's tasks); a stored stale-or-current flag |
| action | `action` | `action_type`; `confidence`; `dedup_key` (idempotency of the effect); `taken_at` and `result_ref` (the action confirmation, written by the adapter as an observation naming the external record the effect left) | `PRODUCES` ← task; `REFERS_TO` → artifact it acts on; `CHECKPOINTS` ← checkpoint | blast radius under the policy; whether it may be taken | — | a stored gate decision; the artifact it leaves (an entity of its own) |
| checkpoint | `checkpoint` | `reason` (`gate_hold`, `repeated_lapse`, `unreadable_workflow`, `rounds_exhausted`, `unspawnable_assignee`, `undetermined_scope`, or a policy-declared class); `needed_input`; `options[]`; `status` (open, or a terminal approval: approved, denied, vetoed, timed out); `deferral_until`; `raised_at`, `resolved_at`; `resolution_note` | `CHECKPOINTS` → action or task (the subject, exactly one); `AWAITS` → principal (one or more); `RESOLVED_BY` → principal; `RAISED_BY` → principal or agent | the queue (open checkpoints whose `AWAITS` names the reader); quorum and separation of duties over its principals | — | the subject as free text; a resolver as a bare status write; a page or notification record |
| artifact | `artifact` | `kind` (`issue`, `pull_request`, `release`, `page`, `message`, `thread`, `event`, `transfer`, …); `system`; `external_id`; `url`; `state` (per kind: `open`, `closed`, `merged`, `sent`, `settled`, …); `labels[]`; `head`; `checks` (`passing`, `failing`, `pending`, or `unknown`) — the last four written by the adapter as observations (`adapters.md`) | `PRODUCES` ← batch; `REFERS_TO` ← task; `REFERS_TO` ← action; `REFERS_TO` ← sign-off; `PART_OF` → containing artifact (a message to its thread; an occurrence to its series) | whether the record tracks it (any batch or task edge); its container, and the members the record holds | — | step state or verdicts (they belong to the batch and its sign-offs); a workflow instruction; the delivery log of the events that updated it (they are its observations) |
| workflow | `workflow` | `project`, `workflow_type`; `steps[]` (`phase`, `step_name`, `owner_role` — a role the roster resolves at claim time, never an agent name; `parallel_group`, `join_step`, `required`, `on_fail`); `fast_paths[]`; `successors[]` | — | which batches are of it | — | a copy of the step list in code; a floor list |
| action policy | `action_policy` | `low_blast_action_types[]`, `high_blast_action_types[]`; `confidence_threshold`; `recurrence_count`; `always_checkpoint_boundaries[]`; `permission_scope`; `consent_tolerance` per action class (the change to an action's consented figures that may be taken without a new checkpoint; absent reads as zero — `payments.md#tolerance-is-an-action_policy-value-and-its-default-is-zero`) | — | blast radius for a class (a governance class with no value resolves to `NEVER` — `work_model.md#changing-the-swarm-is-work-and-it-goes-through-a-workflow-like-any-other`); whether a series has graduated; whether a re-quoted action is within tolerance | — | `operator_only` as a policy value (it is `NEVER` ahead of any policy); a tolerance the design supplies |
| agent session | `agent_session` | `runner_id`; `host`, `checkout`, `branch`, `head`; `started_at`, `last_seen_at` | `REFERS_TO` → task | active (with the lease) | — | a history of runners |
| agent | `agent` | `name`, `prompt_markdown`, `context_entity_types[]`, version | `principal_binding` → principal; `LEASE` → task or batch | — | — | a claimant field on a task |
| adapter | `agent` (a daemon; `adapters.md`) | `name`; the `system` it adapts | `principal_binding` → principal; provenance on every write it makes (the adapter, the system, the delivery id) | which artifacts it tracks (by `system`) | — | a per-artifact map of satisfied steps; an event log beside the artifact's observations; a workflow it reads |
| principal | `operator` (human) or `agent` (non-human) | identity only — the type exists to be a principal; the identifier's form is `multi_tenant.md` section 7 | credentials → principal (many-to-one: `user_id` and a host login to the `operator`; an AAuth `sub` to the `agent`, reaching the human principal through that agent's `principal_binding`); `ownership_grant` ← object; `delegation_edge` → principal | authority chain; whether a write resolves to a principal at all | — | a login string, an address, or a magic value standing in for the principal; `operator_profile` (the descriptive record beside the `operator`, carrying no authority edges); locale or preferences on the principal |
| grant | `agent_grant` | `sub`, `iss`; `capabilities[]` (operation × entity types × repositories); `param_constraints`; `expires_at` | — | permit, deny, or indeterminate for one request | — | a wildcard for a human |
| delegation | relationship `delegation_edge` | `scope`; `expires_at` | delegator → delegate | authority chain; attenuation | — | a prose note on a task |

<!-- /rendered -->

## Relationships

<!-- rendered: data_model relationships -->

| Edge type | Source → target | Meaning | What derives from it |
|---|---|---|---|
| `PART_OF` | child task → parent task; artifact → containing artifact | the child is part of the parent's work; the contained record is part of the containing one (a message of its thread, an occurrence of its series), where the external system gives ids to both levels | parent completion (all children terminal); a task has at most one parent; an artifact's container and its held members; a message regrouped by the system ends one edge and writes another |
| `ADDRESSED_BY` | task → batch | the task is attached to the batch and goes through the workflow with it | a task's current batch (at most one non-terminal); a batch's task set; attach and detach are the writing and ending of this edge |
| `FOLLOWS` | batch → batch | the source batch opened for the tasks the target batch closed on, naming the source's workflow as successor | the chain, read from a live batch back to its intake batch |
| `DEPENDS_ON` | batch → task | the batch holds on the task it created, and its step owner's sign-off is refused while the edge is unended and the task non-terminal (`work_model.md#a-batch-may-depend-on-a-task-it-created`) | a step's hold; every batch a task is holding up; the cycle walk (task → its live batch → its dependencies), refused at write and at attach where it would close a loop, escalated as `dependency_cycle` where found after |
| `LEASE` | principal → task; principal → batch (with `step_name`) | the principal holds the task, or the step of the batch | held / lapsed / returned; claimable; the claimed step state; active (with activity entities) |
| `CLOSES` | sign-off → batch (with `step_name`) | the step owner closed that step on the batch | the signed step state; the batch's closing sign-off and successor; `step_status` |
| `SIGNED_BY` | sign-off → agent | who signed | workflow-policy attribution |
| `PRODUCES` | task → action; batch → artifact | the task intends the effect; the batch left the record | an action's task for the gate and the dedup key; a batch's artifacts |
| `CHECKPOINTS` | checkpoint → action or task | the held subject | the decision queue; what resumes on resolution (the action is taken or refused; the task is re-claimed or closed) |
| `AWAITS` / `RESOLVED_BY` / `RAISED_BY` | checkpoint → principal | whose decision is needed; who gave it; who raised it | quorum, separation of duties, the queue's scoping |
| `REFERS_TO` | task → artifact; action → artifact; sign-off → artifact; agent session → task | the source concerns or acts on the target | intake's `link` step; the task an adapter creates for intake, to the external record it concerns; an action's target; a sign-off's evidence |
| `principal_binding` | agent → principal | the principal the agent acts as | attribution; delegation chains |
| `ownership_grant` | object → principal | named accountability | who is asked when the object needs a decision |
| `delegation_edge` | principal → principal | scoped, time-bounded transfer of rights | the authority chain; attenuation |

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
  carries the artifact's `head` as observed when the verdict was made, so a verdict against a superseded
  head is readable as one instead of being indistinguishable from a live verdict. The head is resolved
  before the work of the step begins, so new commits arriving mid-step cannot re-anchor a verdict onto a
  commit the step owner never read; a round that reopens after a blocking verdict starts from the artifact's
  recorded head, not from the earlier round's branch point. The second half, which `adapters.md` states
  from the event side and this document states from the record side: **a later head does not invalidate a
  sign-off automatically.** A new-commits event is an observation updating the artifact's `head`, and open
  sign-offs are unaffected; a workflow that wants a step to open again on a new head declares that on the
  step, and the event does not do it. A reader that needs to know whether a verdict is current derives it
  by comparing the pinned head to the artifact's — a derived read, never a stored freshness flag that a
  process would have to keep true (principle 11).
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
  true and that goes quietly wrong the moment that process stops.
- **Edges carry their own timestamps and fields.** A relationship is written with `created_at` and,
  where it can end, an explicit end (`returned_at`, `ended_at`); a state derived from an edge is read from
  those, never from a status the edge would have to be transitioned to.
- **A registered entity type carries at most one `ownership_grant`.** One principal is accountable for
  the type's shape and answers when it needs a decision (`authority_model.md#grants`). A type with no
  owner is the one that accumulates use-specific fields from whichever writer arrived first, so that a
  later generic write lands undeclared; a type with two owners has neither. Ownership is the edge the
  record already has for named accountability, not a new field.
- **Type registration is an owned decision write, read back; tests never register into the shared
  registry.** Registering a type, or a new version of one, is a write carrying a decision, so it is read
  back against the registry (principle 2) and made by or on behalf of the type's owner. A test run that
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
| self-triggering daemon | its own `agent` and grant; the record's reachability, by a real read of what its work will read; the tasks or artifacts its poll produces work about; the `dedup_key` space of any effect it may repeat | step state, which it neither derives nor advances; a workflow; another daemon's leases | the record is unreachable: it halts work and keeps observing (`failure_posture.md` rule 1), announcing on the path that survives the outage. An empty poll is an empty poll only if the poll itself succeeded |
| reviewing step owner | the batch's tasks and their acceptance conditions; the artifact at a resolved `head`, and that head recorded for its own sign-off; earlier sign-offs on the batch, to see what has already been judged and at which head; the design basis its review claims to apply | the host's own review state as a substitute for the record's sign-offs; another step owner's verdict as grounds for its own | it cannot judge: the step stays open with the condition announced. A check it could not run is not a passing check, and a blocking verdict rests on what the step owner executed |

### Write contract

| Actor kind | Writes to close its work | Reads back | Never writes |
|---|---|---|---|
| agent claiming a step | one `sign-off` on the step, carrying the verdict, the artifact refs with their judged heads, and the pinned `agent` version; the observations its work produced | the sign-off, retrieved and asserted to hold the verdict written — a response code is not evidence | another principal's sign-off; step state as a field; a verdict anywhere but the record (`failure_posture.md` rule 4); a parallel log of what it did |
| adapter | observations on artifacts; action confirmations on actions (`taken_at`, `result_ref`); a task for intake, where a new-record event concerns a record the swarm does not track; a `dropped` disposition with its reason | every write that carries a decision — a sign-off it carried in, a checkpoint resolution, an action confirmation — before it acknowledges the event to the external system | **step state, in any form** — it never opens, claims, closes, or initializes a step, and never writes `not_required`, `not_applicable`, or clear on an artifact with no batch; a task's status; a binding it inferred |
| operator-facing agent | the checkpoint's resolution, attributed to the operator principal and to itself as the agent that carried it (A-for-B, never as B); tasks the operator's instructions produce | the resolution, before it reports to the operator that the decision landed | the operator's decision where the operator gave none; a sign-off on a step it does not own; the operator's `waived` verdict on its own initiative |
| self-triggering daemon | the tasks its poll produces, each entering intake; observations carrying its provenance; nothing else | each task it created, so a poll that appears to have produced work but wrote none is caught | step state; a claim on a task it created (creating is publishing, not claiming); a routing decision that hands work to a named runner |
| reviewing step owner | one `sign-off` on its review step, with the head it judged, the findings it recorded, and the evidence it executed | the sign-off | a verdict on a step another principal owns; a comment on the host in place of the sign-off; a verdict rewritten in place (a later judgement is a **new** sign-off, stated as such, and the latest per step owner per head is the one that stands); a verdict contradicting its own findings (the write is rejected at submission); a verdict carrying a condition |

**Bounded retrieval before creating an entity.** Any actor about to create an entity first retrieves for
the one that may already exist, by identifier where it has a concrete one and by type-and-search where it
has a category. The retrieval is bounded — it names the type and the identifying values, rather than
scanning — and its result decides between creating and correcting. Duplicates in the record arise almost
entirely where this step was skipped: two actors resolve the same external record, the same person, or the
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
