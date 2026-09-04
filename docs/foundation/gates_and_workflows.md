# Gates and workflows: the workflow's steps, their runs, the projection, and the gate that decides

**Kernel document:** read on every review (`conformance.md`). **Kind:** foundation; states the design and
never the state of a checkout. **Derived from:** synthesis `ent_b0ce322f768e4fc676b73139` (PR-04 to PR-08,
PR-20, PR-21, C3 to C6, C11), prior art `ent_08460968e6f49dac21510f4a`, gate-state plan
`ent_4222e5d52edd9bdba7b78cc1` (decisions cited inline), architecture plan `ent_99ace4dd6673aa36ed08b1fe`
decisions `operator_only_is_never_auto_executable_not_merely_high_blast`,
`unclassified_action_type_fails_closed_and_loudly`, `gate_advisory_and_enforcing_paths_must_agree`,
`gating_vocabulary_order_is_load_bearing`, throughput plan `ent_18b902cf72822373f9da8ced` decision
`gate_machinery_is_already_pr_independent`, and PR #745 operator review (2026-09-04). Supersedes
`docs/archive/swarm_orchestration.md` and `docs/archive/swarm_hitl_checkpoints_design.md`. What is built
is `status.md`.

## Purpose

State the step and gate model: which entity declares a workflow, which records a run of it, which field is
the read-path projection; that the step set is defined once; that the word `gate` names one decision, the
execution gate, which is independent of GitHub and decides per action on confidence and three blast
tiers; that actions are entities and only actions execute; which two policies govern claiming and
executing; and how an approval object is shaped.

## Scope

The workflow engines, the execution gate, and the entities `workflow`, `workflow_run`, `step_run`,
`action`, `checkpoint_brief`, and `execution_policy`. Who may resolve a checkpoint, and how an approval is
attributed, is `authority_model.md`; what happens when a workflow cannot be read is `failure_posture.md`;
how tasks attach to a run is `work_model.md`.

## The invariants

### Declaration, run, projection

`workflow` declares: one entity per (project, workflow type), an ordered list of steps (`steps[]`) with
`phase`, `step_name`, `owner_agent`, `parallel_group`, `join_step`, `required`, plus `fast_paths`. Step
names are data: a workflow may declare steps beyond the review sequence (a draft step, a deterministic
lint, an operator preview), and a named group of contiguous steps is a stage (the review stage, the
release stage). A `workflow_run` is one passage of a work item through a workflow. A `step_run` is the
instance of one step within one run, with status, timestamps, agent, artifact refs, and the pinned
`agent_definition` version; a step is closed by its owner's sign-off, a terminal write that supplies every
field the schema requires, and a rejected write is an error, never swallowed. `step_status` on the issue
is a projection for the hot path: the question "are all required steps signed off" must fail closed in
one entity read. `step_status` is a projection of the step runs with a reconciler proving they agree;
neither is deleted, neither is a second source of truth (`gate_status_map_should_remain`, under its former
name). No transition event type; history is the record's observations (`no_gate_transition_event_type`).
One engine sequences steps from the entities and writes the runs; a second engine that sequences from a
code literal and cannot see the first is the defect the model exists to remove
(`real_defect_is_two_blind_engines`).

### One step set, defined once, tested for parity

The step sequence has one home. Where a copy is unavoidable (a module that must not import the executor),
it is derived at import time or held equal by a parity test; a comment claiming it mirrors another
constant is not parity (principle 9). A data-sourced list may add steps and never remove one, as a
correctness rule, not an availability fallback (C5). Migration is incremental, never a flag day
(`migration_is_incremental_no_flag_day`).

### Two policies: workflow policy and execution policy

Two questions, two policies. Workflow policy answers which principals may claim which steps of which
workflows: it is the workflow's declared step owners together with the `agent_grant`s in force
(`authority_model.md`). Execution policy answers which actions may execute and under what gate: it is the
`execution_policy` entity, the policy a principal evaluates the execution gate against. A step owner's
right to sign off a step is workflow policy; whether the merge that follows may execute is execution
policy. Neither policy governs internal operational writes to Neotoma, which are not actions.

### Actions are entities; only actions execute

An `action` is an entity representing one intended effect outside the Ateles system (a send, a publish, a
merge, a payment, a release), related to the task it serves (`PRODUCES` from the task; `REFERS_TO` where
the action cites the artifact it acts on). It is created when the effect becomes known, which may be
mid-workflow: a task may produce many actions, most unknown at creation. Tasks are worked (claimed,
progressed, completed); actions are executed; `execute` is never said of a task. The execution gate is
evaluated per action, at the moment of execution, so an effect discovered late is gated no differently
from one declared at creation. The action's dedup key (`work_model.md`) lives on the action.

### The execution gate is PR-independent

A principal evaluating the execution gate supplies the action's class, confidence, the policy, and
successful recurrences; no PR, issue, or repository. `write_checkpoint_brief()` keys on the action and its
task. The consent gate for outbound non-code work is this gate: a policy lists `send_external_comms` and
`publish` as high blast, the content agents' actions carry those classes, the runner subscribes to
`checkpoint_brief`, and the task is re-claimed on resolution. Do not build a second consent gate
(principle 6). What is PR-shaped is only the review machinery (issue `step_status`, review verdicts, the
steward's merge action), a separate mechanism layered on GitHub.

### Confidence and three blast tiers

`operator_only` is `NEVER`; unclassified fails closed and loudly. The order is load-bearing
(`gating_vocabulary_order_is_load_bearing`): a task declares at creation, as its `action_type`, the
classes of action it expects to produce, from what the task does and never from which agent would handle
it; that declaration serves early eligibility and claim decisions. Each `action` carries its own class,
and blast radius is resolved from that class under the `execution_policy` at the moment of execution;
confidence is scored by the proposing agent. The gate decides on confidence and blast together.
`NEVER_AUTO_EXECUTE_ACTION_TYPES = {"operator_only"}` wins ahead of both policy sets, so a policy cannot
demote it; a declared class in neither set logs a warning naming the value and resolves to `NEVER`, never
to the policy default; an absent class keeps the policy default, since "nothing declared" stays distinct
from "declared and unclassified". `NEVER` is a third tier: `HIGH` still auto-executes once a recurring
series clears its count; `NEVER` short-circuits ahead of the confidence axis and the recurrence path. The
advisory path (`route_task`) and the enforcing path resolve identically, and a test holds the duplicated
never-set equal across the two modules. A fallback policy with an empty low-blast set is transitional:
under `failure_posture.md` an unreachable policy source is a halt, not a fallback.

### An unreadable workflow is unknown, and unknown holds

Never proceed on an empty sequence. An unreadable `workflow` is a distinct state: no step of it is
assigned or claimed, one aggregated escalation is raised, and no exception is swallowed into an empty
tuple. The same holds for an unreadable issue and an unreadable CI state (principle 7).

### Non-code deliverables pass through the same gate

A post, an outreach mail, a release, or a payment is an action with a class and a blast radius, and it
reaches approval through the execution gate on the task path, as a merge does. What non-code agents lack
is delivery of the task (`work_model.md`), not a gate.

### The approval object

A `checkpoint_brief` is the execution gate's request for a decision on one action: interrupted, not
terminal (A2A's `input-required`). It records who it awaits, who resolved it, and ends in a terminal
state; a deferral is bounded and a timeout is a terminal state that never continues
(`deferral_must_be_bounded_and_escalate_off_neotoma`). The raiser and the resolver are distinct roles on the
object; whether the same principal may hold both is `authority_model.md`.

## Contradictions this document settles

**C3, four copies of the step set.** Resolved: one home, parity where a copy is unavoidable, above.

**C5, the floor.** The gate-state plan body argues for a hardcoded step list as a floor the data may add
to; its decisions map retracts that (`hardcoded_floor_proposal_is_retired`). Resolved for the retraction;
`failure_posture.md` states why.

**C6, merge authority.** Merge is an action whose class the `execution_policy` governs, and the stored
policy is the source of truth for whether it is operator-gated. A runtime flag that disagrees with the
stored policy is a configuration defect, and its resolution is the operator's: change the policy to match
the flag, or return the flag to the policy. Neither the flag's value nor the queue's state is stated here;
both are `status.md`.

**C11, confidence × blast.** The gate decides on both axes by design; a confidence input that is not
produced degrades the gate to blast radius alone, which is a gap in the proposing agents, not a change to
the design. Whether the input is produced is `status.md`.

**C4, retired agent names in workflow data.** A renamed or retired agent leaves no stale mirror, in code
and in design entities alike. The data correction is the gate-state plan's; whether it has been made is
`status.md`.

**The names `workflow`, `step_run`, `workflow_run`.** This reverses the recorded decision
`keep_the_name_workflow_definition` in the gate-state plan `ent_4222e5d52edd9bdba7b78cc1`. Reason:
"definition" and "record" are redundant qualifiers when every entity in the store is a definition or a
record of something; `workflow` declares, `workflow_run` is one passage, `step_run` is one step's instance
within it, and `participation_record` named the weakest of the three. `gate` is withdrawn from the step
vocabulary so that the word names exactly one thing, the execution gate. The correction to that plan is a
request to its maintainer; the decision keys cited above keep their recorded names.

## Prior art

GitHub environment protection rules are the nearest declarative model of a step with a required sign-off;
Ateles shares the declarative definition and pre-step approval, not per-environment routing or the 1-of-n
rule, since blast radius selects the gate. Cedar's rule (zero permits is deny; any forbid wins) is the
semantics the advisory and enforcing paths share. A2A's `input-required` and `auth-required` are the
interrupted states a checkpoint is; Ateles does not share A2A's agent-asserted `working`, which has no
owner and no expiry. Sources: `ent_08460968e6f49dac21510f4a`.
