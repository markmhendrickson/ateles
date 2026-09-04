# Gates and workflows: the workflow's steps, the passage, the sign-off, the projection, and the gate that decides

**Kernel document:** read on every review (`conformance.md`). **Kind:** foundation; states the design and
never the state of a checkout. **Derived from:** synthesis `ent_b0ce322f768e4fc676b73139` (PR-04 to PR-08,
PR-20, PR-21, C3 to C6, C11), prior art `ent_08460968e6f49dac21510f4a`, gate-state plan
`ent_4222e5d52edd9bdba7b78cc1`, architecture plan `ent_99ace4dd6673aa36ed08b1fe` decisions
`operator_only_is_never_auto_executable_not_merely_high_blast`,
`unclassified_action_type_fails_closed_and_loudly`, `gate_advisory_and_enforcing_paths_must_agree`,
`gating_vocabulary_order_is_load_bearing`, throughput plan `ent_18b902cf72822373f9da8ced` decision
`gate_machinery_is_already_pr_independent`, and PR #745 operator review (2026-09-04). Supersedes
`docs/archive/swarm_orchestration.md` and `docs/archive/swarm_hitl_checkpoints_design.md`. What is built
is `status.md`.

## Purpose

State the step and gate model: `workflow` declares; `passage` records one run of tasks through it; step
state is derived from edges and closed by a `sign-off`; `step_status` projects; one step set; sequencing
is data (`successors` + `FOLLOWS`); `gate` names the execution gate only; actions are entities; two
policies; the approval object.

## Scope

Workflow engines, the execution gate, and entities `workflow`, `passage`, `sign-off`, `action`,
`checkpoint_brief`, `execution_policy`. Checkpoint resolution and approval attribution:
`authority_model.md`. Unreadable workflow: `failure_posture.md`. Tasks on passages / artifacts:
`work_model.md`. Per-workflow step lists: `workflows.md` (authored companion; binds via `workflow`
entity + `render_workflow_docs.py --check`, not the review prompt).

## The invariants

### Declaration, passage, projection

`workflow` declares one entity per (project, workflow type): ordered `steps[]` (`phase`, `step_name`,
`owner_agent`, `parallel_group`, `join_step`, `required`, `on_fail`), plus `fast_paths` and `successors`.
Step names are data. A contiguous named group of steps is a stage.

A `passage` is one passage of tasks through a workflow (`work_model.md`). Its subject is tasks; issues/PRs
are artifacts by edge, never the thing a step is taken on.

A step has no entity of its own. Derived state: passage+step → **open**; lease from step owner →
**claimed**; `sign-off` → **signed**. Opening a step publishes claimable step work; the owner claims with
the same lease primitive as a task (`work_model.md`). A `sign-off` is the terminal write that closes a
step (verdict, timestamps, agent, artifact refs, pinned `agent_definition` version); a rejected write is
an error, never swallowed (principle 11).

`step_status` on the task is the hot-path projection of sign-offs so "all required steps signed?" fails
closed in one read. A reconciler proves it agrees with sign-offs; neither is a second source of truth.
No transition event type. One engine opens steps and reads sign-offs; a second blind engine is the defect
this model removes.

### One step set, defined once, tested for parity

The step sequence has one home. Unavoidable copies are derived at import or held equal by a parity test;
a comment is not parity (principle 9). A data-sourced list may add steps and never remove one (C5).
Migration is incremental, never a flag day.

### Sequencing is data: successors and the chain

`workflow.successors` names workflows a closing passage may hand to. The last step is singular (never a
parallel group); its sign-off selects exactly one successor from the list, or none. Opening the successor
carries a `FOLLOWS` edge from the new passage to the closed one. A task's chain is passages along
`FOLLOWS` back to intake — derived, never stored. No super-workflow entity. Parallel successors are
forbidden; split into child tasks instead. Intake is the universal entry
(`work_model.md#intake-is-every-tasks-first-passage`); naming intake as a successor is a declaration
error. Core designs: `workflows.md`.

### Two policies: workflow policy and execution policy

Workflow policy: which principals may claim which steps (step owners + `agent_grant`s;
`authority_model.md`). Execution policy: which actions may execute (`execution_policy` entity). Signing
off a step is workflow policy; whether a merge may execute is execution policy. Neither governs internal
Neotoma operational writes.

### Actions are entities; only actions execute

An `action` is one intended external effect (send, publish, merge, payment, release), related to its task.
Created when the effect becomes known — possibly mid-workflow. Tasks are worked; actions are executed;
`execute` is never said of a task. The gate evaluates per action at execution time. The dedup key lives
on the action (`work_model.md`).

### The execution gate is PR-independent

Inputs: action class, confidence, policy, successful recurrences — no PR, issue, or repository.
`write_checkpoint_brief()` keys on the action and its task. Outbound non-code consent is this gate, not a
second consent path (principle 6). PR-shaped review machinery (`step_status`, review verdicts, steward
merge) is a separate GitHub-layered mechanism; the PR is an artifact of the passage.

### Confidence and three blast tiers

`operator_only` is `NEVER`; unclassified fails closed and loudly. Order is load-bearing: a task declares
expected `action_type` classes at creation for eligibility; each `action` carries its class; blast
resolves from that class under `execution_policy` at execution; confidence is scored by the proposing
agent. `NEVER_AUTO_EXECUTE_ACTION_TYPES = {"operator_only"}` wins ahead of both policy sets. A declared
class in neither set logs a warning and resolves to `NEVER`; an absent class keeps the policy default
("nothing declared" ≠ "declared and unclassified"). `NEVER` short-circuits ahead of confidence and
recurrence; `HIGH` may still auto-execute once a recurring series clears. Advisory (`route_task`) and
enforcing paths resolve identically; a parity test holds the never-set equal. An unreachable policy
source is a halt (`failure_posture.md`), not a fallback with an empty low-blast set.

### An unreadable workflow is unknown, and unknown holds

Never proceed on an empty sequence. An unreadable `workflow` opens no steps, raises one aggregated
escalation, and never swallows into an empty tuple. Same for unreadable issue and CI state (principle 7).

### Non-code deliverables pass through the same gate

A post, outreach mail, release, or payment is an action with class and blast through the execution gate.
What non-code agents lack is task delivery (`work_model.md`), not a gate.

### The approval object

A `checkpoint_brief` is the execution gate's request for a decision on one action: interrupted, not
terminal (A2A `input-required`). It records who it awaits and who resolved it; deferral is bounded;
timeout is terminal and never continues. Whether raiser and resolver may be the same principal:
`authority_model.md`.

## Contradictions this document settles

**C3.** Four copies of the step set → one home, parity where a copy is unavoidable. **C5.** Hardcoded
floor retired; data may add, never remove as floor. **C6.** Merge is an action under `execution_policy`;
a runtime flag that disagrees is a configuration defect (resolution is the operator's); live values are
`status.md`. **C11.** Gate decides on confidence × blast; missing confidence input degrades to blast alone
— a gap in proposing agents, not a design change. **C4.** Retired agent names in workflow data: correction
is the gate-state plan's; whether done is `status.md`. **Names.** Reverses `keep_the_name_workflow_definition`:
`workflow` / `passage` / `sign-off` replace `workflow_definition` / `participation_record`; no entity
carries `run`; `gate` names only the execution gate. Plan correction is a request to its maintainer.
