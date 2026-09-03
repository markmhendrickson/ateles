# Gates and workflows: declaration, instance, projection, and the gate that decides

**Kernel document:** read on every review (`conformance.md`). **Kind:** foundation; states the design and
never the state of a checkout. **Derived from:** synthesis `ent_b0ce322f768e4fc676b73139` (PR-04 to PR-08,
PR-20, PR-21, C3 to C6, C11), prior art `ent_08460968e6f49dac21510f4a`, gate-state plan
`ent_4222e5d52edd9bdba7b78cc1` (decisions cited inline), architecture plan `ent_99ace4dd6673aa36ed08b1fe`
decisions `operator_only_is_never_auto_executable_not_merely_high_blast`,
`unclassified_action_type_fails_closed_and_loudly`, `gate_advisory_and_enforcing_paths_must_agree`,
`gating_vocabulary_order_is_load_bearing`, throughput plan `ent_18b902cf72822373f9da8ced` decision
`gate_machinery_is_already_pr_independent`. Supersedes `docs/archive/swarm_orchestration.md` and
`docs/archive/swarm_hitl_checkpoints_design.md`. What is built is `status.md`.

## Purpose

State the gate model: which entity declares a workflow, which is its instance, which field is the read-path
projection; that the gate set is defined once; that the execution gate is independent of GitHub and decides
on confidence and three blast tiers; and how an approval object is shaped.

## Scope

The workflow engines, the execution gate, and the entities `workflow_definition`, `participation_record`,
`checkpoint_brief`, and `execution_policy`. Who may resolve a checkpoint, and how an approval is attributed,
is `authority_model.md`; what happens when a workflow cannot be read is `failure_posture.md`.

## The invariants

### Declaration, instance, projection

`workflow_definition` declares: one entity per (project, workflow type), an ordered gate list with `phase`,
`gate_name`, `owner_agent`, `parallel_group`, `join_gate`, `required`, plus `fast_paths`. Gate names are
data: a workflow may declare gates beyond the review sequence (a draft gate, a deterministic lint, an
operator preview). `participation_record` is the instance: one per (work entity, gate), with status,
timestamps, agent, artifact refs, and the pinned `agent_definition` version; a terminal write supplies every
field the schema requires, and a rejected write is an error, never swallowed. `gate_status` on the issue is
a projection for the hot path: `_gates_green()` must fail closed in one entity read. `gate_status` is a
projection of `participation_record` with a reconciler proving they agree; neither is deleted, neither is a
second source of truth (`gate_status_map_should_remain`). No transition event type; history is the record's
observations (`no_gate_transition_event_type`). The name `workflow_definition` stays; `participation_record`
is the weaker name (`keep_the_name_workflow_definition`). One engine sequences gates from the entities and
writes the instance; a second engine that sequences from a code literal and cannot see the first is the
defect the model exists to remove (`real_defect_is_two_blind_engines`).

### One gate set, defined once, tested for parity

The gate sequence has one home. Where a copy is unavoidable (a module that must not import the executor), it
is derived at import time or held equal by a parity test; a comment claiming it mirrors another constant is
not parity (principle 9). A data-sourced list may add gates and never remove one, as a correctness rule,
not an availability fallback (C5). Migration is incremental, never a flag day
(`migration_is_incremental_no_flag_day`).

### The execution gate is PR-independent

`evaluate_gate()` takes confidence, `action_type`, policy, and successful recurrences; no PR, issue, or
repository. `write_checkpoint_brief()` keys on the task. The consent gate for outbound non-code work is this
gate: a policy lists `send_external_comms` and `publish` as high blast, the dispatcher maps the content
agents to them, subscribes to `checkpoint_brief`, and re-dispatches on resolution. Do not build a second
consent gate (principle 6). What is PR-shaped is only the review machinery (issue `gate_status`, review
verdicts, the steward's merge), a separate mechanism layered on GitHub.

### Confidence and three blast tiers; `operator_only` is `NEVER`; unclassified fails closed and loudly

The order is load-bearing (`gating_vocabulary_order_is_load_bearing`): `action_type` is declared when the
task is created, from what the task does, never inferred from which agent would handle it; blast radius is
resolved from `action_type` under the `execution_policy`; confidence is scored by the proposing agent. The
gate decides on confidence and blast together. `NEVER_AUTO_EXECUTE_ACTION_TYPES = {"operator_only"}` wins
ahead of both policy sets, so a policy cannot demote it; a declared action type in neither set logs a
warning naming the value and resolves to `NEVER`, never to the policy default; an absent action type keeps
the policy default, since "nothing declared" stays distinct from "declared and unclassified". `NEVER` is a
third tier: `HIGH` still auto-executes once a recurring series clears its count; `NEVER` short-circuits
ahead of the confidence axis and the recurrence path. The advisory path (`route_task`) and the enforcing
path resolve identically, and a test holds the duplicated never-set equal across the two modules. A
fallback policy with an empty low-blast set is transitional: under `failure_posture.md` an unreachable
policy source is a halt, not a fallback.

### An unreadable `workflow_definition` is `unknown`, and `unknown` holds

Never proceed on an empty sequence. An unreadable `workflow_definition` is a distinct state: dispatch held,
one aggregated escalation, never an exception swallowed into an empty tuple. The same holds for an
unreadable issue and an unreadable CI state (principle 7).

### Non-code deliverables pass through the same gate

A post, an outreach mail, a release, or a payment reaches approval through `action_type` and blast radius
on the task path, as code does. What non-code agents lack is delivery of the task (`work_model.md`), not a
gate.

### The approval object

A `checkpoint_brief` is the execution gate's request for a decision: interrupted, not terminal (A2A's
`input-required`). It records who it awaits, who resolved it, and ends in a terminal state; a deferral is
bounded and a timeout is a terminal state that never continues
(`deferral_must_be_bounded_and_escalate_off_neotoma`). The raiser and the resolver are distinct roles on the
object; whether the same principal may hold both is `authority_model.md`.

## Contradictions this document settles

**C3, four copies of the gate set.** Resolved: one home, parity where a copy is unavoidable, above.

**C5, the floor.** The gate-state plan body argues for a hardcoded gate list as a floor the data may add
to; its decisions map retracts that (`hardcoded_floor_proposal_is_retired`). Resolved for the retraction;
`failure_posture.md` states why.

**C6, merge authority.** Merge is a boundary the `execution_policy` governs, and the stored policy is the
source of truth for whether it is operator-gated. A runtime flag that disagrees with the stored policy is a
configuration defect, and its resolution is the operator's: change the policy to match the flag, or return
the flag to the policy. Neither the flag's value nor the queue's state is stated here; both are `status.md`.

**C11, confidence × blast.** The gate decides on both axes by design; a confidence input that is not
produced degrades the gate to blast radius alone, which is a gap in the proposing agents, not a change to
the design. Whether the input is produced is `status.md`.

**C4, retired agent names in workflow data.** A renamed or retired agent leaves no stale mirror, in code
and in design entities alike. The data correction is the gate-state plan's; whether it has been made is
`status.md`.

## Prior art

GitHub environment protection rules are the nearest declarative gate model; Ateles shares the declarative
definition and pre-step approval, not per-environment routing or the 1-of-n rule, since blast radius
selects the gate. Cedar's rule (zero permits is deny; any forbid wins) is the semantics the advisory and
enforcing paths share. A2A's `input-required` and `auth-required` are the interrupted states a checkpoint
is; Ateles does not share A2A's agent-asserted `working`, which has no owner and no expiry. Sources:
`ent_08460968e6f49dac21510f4a`.
