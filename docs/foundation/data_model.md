# Data model: how the foundation's concepts are recorded

**Keyed document:** read when the Neotoma client, a schema script, the data-model renderer, or this
document changes (`conformance.md`). **Kind:** foundation; maps each concept the other documents define
onto the record (entity type, fields, edges, derived reads, projections) and never states which of them
a checkout has registered. **Derived from:** `work_model.md`, `gates_and_workflows.md`,
`failure_posture.md`, `authority_model.md`, principle 9 and principle 11 of `principles.md`, and PR #745
operator review (2026-09-04). Which types and edge types the registry holds is `status.md`.

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

<!-- rendered: data_model concepts -->

| Concept | Entity type | Key fields | Edges (type, direction, target) | Derived reads | Projections | Deliberately not a field |
|---|---|---|---|---|---|---|
| task | `task` | `status` (`open`, `blocked`, or a terminal value); `title`, `description`; `action_type[]` (declared classes); `assigned_to` (eligibility); `priority` | `ADDRESSED_BY` → batch; `PART_OF` → parent task; `PRODUCES` → action; `REFERS_TO` → artifact; `LEASE` ← principal; `CHECKPOINTS` ← checkpoint | claimable; active; chain; current batch; parent completion | `step_status` | claimant, `claimed_at`, any lease field; a liveness flag; the workflow it is in; the list of batches it has gone through; a parent's stored status |
| batch | `batch` | `project`, `workflow_type`; `status` (open or terminal); `opened_at`, `closed_at`; `successor` named by the closing sign-off, or none | `ADDRESSED_BY` ← task; `FOLLOWS` → batch (the one it follows); `CLOSES` ← sign-off; `PRODUCES` → artifact; `LEASE` ← principal (on a step, `step_name` on the edge) | step state per step (open, claimed, signed); current step; the successor's batch | — | the list of attached tasks (edges); a per-step status row; a sequence of workflows above it |
| lease | relationship `LEASE` | `claimed_at`, `expires_at`, `returned_at`; `runner_id`; `step_name` when the lease is on a step of a batch | principal → task, or principal → batch (step) | `held` (`expires_at` future, no `returned_at`); `lapsed` (`expires_at` past, no `returned_at`); `returned` | — | a stored state; a lock; anything on the task |
| sign-off | `sign_off` | `step_name`; `verdict`; `signed_at`; `agent`; `agent_definition_version`; `artifact_refs[]`; `successor` (closing sign-off only, one or none) | `CLOSES` → batch; `SIGNED_BY` → agent; `REFERS_TO` → artifact | the signed step state | contributes to `step_status` | a verdict on an issue or a PR (the subject is the batch's tasks) |
| action | `action` | `action_type`; `confidence`; `dedup_key` (idempotency of the effect); `taken_at`; `result_ref` | `PRODUCES` ← task; `REFERS_TO` → artifact it acts on; `CHECKPOINTS` ← checkpoint | blast radius under the policy; whether it may be taken | — | a stored gate decision; the artifact it leaves (an entity of its own) |
| checkpoint | `checkpoint` | `reason` (`gate_hold`, `repeated_lapse`, `unreadable_workflow`, `rounds_exhausted`, `unspawnable_assignee`, or a policy-declared class); `needed_input`; `options[]`; `status` (open, or a terminal approval: approved, denied, vetoed, timed out); `deferral_until`; `raised_at`, `resolved_at`; `resolution_note` | `CHECKPOINTS` → action or task (the subject, exactly one); `AWAITS` → principal (one or more); `RESOLVED_BY` → principal; `RAISED_BY` → principal or agent | the queue (open checkpoints whose `AWAITS` names the reader); quorum and separation of duties over its principals | — | the subject as free text; a resolver as a bare status write; a page or notification record |
| artifact | `artifact` | `kind` (`issue`, `pull_request`, `release`, `page`, `message`, …); `system`; `external_id`; `url` | `PRODUCES` ← batch; `REFERS_TO` ← task; `REFERS_TO` ← action; `REFERS_TO` ← sign-off | — | — | step state or verdicts (they belong to the batch and its sign-offs) |
| workflow | `workflow` | `project`, `workflow_type`; `steps[]` (`phase`, `step_name`, `owner_agent`, `parallel_group`, `join_step`, `required`, `on_fail`); `fast_paths[]`; `successors[]` | — | which batches are of it | — | a copy of the step list in code; a floor list |
| action policy | `action_policy` | `low_blast_action_types[]`, `high_blast_action_types[]`; `confidence_threshold`; `recurrence_count`; `always_checkpoint_boundaries[]`; `permission_scope` | — | blast radius for a class; whether a series has graduated | — | `operator_only` as a policy value (it is `NEVER` ahead of any policy) |
| agent session | `agent_session` | `runner_id`; `host`, `checkout`, `branch`, `head`; `started_at`, `last_seen_at` | `REFERS_TO` → task | active (with the lease) | — | a history of runners |
| agent | `agent_definition` | `name`, `prompt_markdown`, `context_entity_types[]`, version | `principal_binding` → principal; `LEASE` → task or batch | — | — | a claimant field on a task |
| principal | the principal entity (open, C9) | identity fields per the identity decision | credentials → principal (many-to-one); `ownership_grant` ← object; `delegation_edge` → principal | authority chain | — | a login string, an address, or a magic value standing in for the principal |
| grant | `agent_grant` | `sub`, `iss`; `capabilities[]` (operation × entity types × repositories); `param_constraints`; `expires_at` | — | permit, deny, or indeterminate for one request | — | a wildcard for a human |
| delegation | relationship `delegation_edge` | `scope`; `expires_at` | delegator → delegate | authority chain; attenuation | — | a prose note on a task |

<!-- /rendered -->

## Relationships

<!-- rendered: data_model relationships -->

| Edge type | Source → target | Meaning | What derives from it |
|---|---|---|---|
| `PART_OF` | child task → parent task | the child is part of the parent's work | parent completion (all children terminal); a task has at most one |
| `ADDRESSED_BY` | task → batch | the task is attached to the batch and goes through the workflow with it | a task's current batch (at most one non-terminal); a batch's task set; attach and detach are the writing and ending of this edge |
| `FOLLOWS` | batch → batch | the source batch opened for the tasks the target batch closed on, naming the source's workflow as successor | the chain, read from a live batch back to its intake batch |
| `LEASE` | principal → task; principal → batch (with `step_name`) | the principal holds the task, or the step of the batch | held / lapsed / returned; claimable; the claimed step state; active (with activity entities) |
| `CLOSES` | sign-off → batch (with `step_name`) | the step owner closed that step on the batch | the signed step state; the batch's closing sign-off and successor; `step_status` |
| `SIGNED_BY` | sign-off → agent | who signed | workflow-policy attribution |
| `PRODUCES` | task → action; batch → artifact | the task intends the effect; the batch left the record | an action's task for the gate and the dedup key; a batch's artifacts |
| `CHECKPOINTS` | checkpoint → action or task | the held subject | the decision queue; what resumes on resolution (the action is taken or refused; the task is re-claimed or closed) |
| `AWAITS` / `RESOLVED_BY` / `RAISED_BY` | checkpoint → principal | whose decision is needed; who gave it; who raised it | quorum, separation of duties, the queue's scoping |
| `REFERS_TO` | task → artifact; action → artifact; sign-off → artifact; agent session → task | the source concerns or acts on the target | intake's `link` step; an action's target; a sign-off's evidence |
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
  to the effect outside the system.
- **Read-back after every write that carries a decision** (principle 2): the retrieval that asserts the
  field holds the value written. A response code is not evidence.
- **Schema versions.** Every entity type is registered with a version, and a sign-off pins the
  `agent_definition` version it was made under; a write that names a field the registered version does not
  declare is not silently accepted as that field.
- **`raw_fragments` for undeclared fields.** A write carrying a field the schema does not declare lands
  in `raw_fragments`, not in the field, and the store reports success. A read-back therefore asserts the
  declared field, never the response; a field that is only ever found in `raw_fragments` is a schema that
  was never registered, and the linter for that shape is named in `principles.md` (invariant 2).
- **Edges carry their own timestamps and fields.** A relationship is written with `created_at` and,
  where it can end, an explicit end (`returned_at`, `ended_at`); a state derived from an edge is read from
  those, never from a status the edge would have to be transitioned to.

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
