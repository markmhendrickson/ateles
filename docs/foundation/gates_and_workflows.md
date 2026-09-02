# Gates and workflows: declaration, instance, projection, and the gate that decides

**Vision phase:** P1 (governed execution for one principal). **Kind:** consolidation, not design.
**Derived from:** synthesis `ent_b0ce322f768e4fc676b73139` (PR-04 to PR-08, PR-20, PR-21, C3 to C6, C11),
prior art `ent_08460968e6f49dac21510f4a`, gate-state plan `ent_4222e5d52edd9bdba7b78cc1` (decisions cited
inline), architecture plan `ent_99ace4dd6673aa36ed08b1fe` decisions
`operator_only_is_never_auto_executable_not_merely_high_blast`, `unclassified_action_type_fails_closed_and_loudly`,
`gate_advisory_and_enforcing_paths_must_agree`, throughput plan `ent_18b902cf72822373f9da8ced` decision
`gate_machinery_is_already_pr_independent`. Code read on `origin/main` at `496bab3`, 2026-09-02. Supersedes
`swarm_orchestration.md` and `swarm_hitl_checkpoints_design.md` (archived).

## Purpose

State the gate model: which entity declares a workflow, which is its instance, which field is the read-path
projection; that the gate set is defined once; that the execution gate is independent of GitHub and decides
on three blast tiers; and what is true today about the approval substrate where stored policy and running
configuration disagree.

## Scope

`lib/daemon_runtime/gating.py` (enforcing), `execution/mcp/ateles/server.py` (advisory), the engines in
`swarm_dispatch.py` and `execution/daemons/anthus/`, and the entities `workflow_definition`,
`participation_record`, `checkpoint_brief`, `execution_policy`. Who may resolve a checkpoint is
`authority_model.md`.

## The invariants

### Declaration, instance, projection

`workflow_definition` declares: one entity per (project, workflow type), 8 on prod, each an ordered gate
list with `phase`, `gate_name`, `owner_agent`, `parallel_group`, `join_gate`, `required`, plus `fast_paths`.
`participation_record` is the instance: one per (work entity, gate), with status, timestamps, agent, artifact
refs, and the pinned `agent_definition` version. `gate_status` on the issue is a projection for the hot path:
`_gates_green()` must fail closed in one entity read. End state: `gate_status` is a projection of
`participation_record` with a reconciler proving they agree; neither is deleted, neither is a second source
of truth (`gate_status_map_should_remain`). No transition event type; history is the record's observations
(`no_gate_transition_event_type`). The name `workflow_definition` stays; `participation_record` is the weaker
name (`keep_the_name_workflow_definition`).

**On main:** the two engines are mutually blind (`real_defect_is_two_blind_engines`): Apis sequences from a
code literal and writes `gate_status`; Anthus sequences from the entities and writes `participation_record`;
neither reads the other. Zero records reach `satisfied`, because the terminal writes never supplied the
schema's required `agent` and the writer swallowed the rejection (ateles#736, open); history through
observations is trustworthy only from that fix forward, and the stranded rows were not backfilled.

### One gate set, defined once, tested for parity

Four copies at `496bab3`, two wrong: `swarm_dispatch.py:271` `PRE_IMPL_GATES = ("pm", "ux", "arch")`, the
executor; `lib/issue_labels.py:100` `PRE_IMPL_GATE_NAMES = ("pm", "arch")` under a comment claiming it
mirrors the former, so `blocked/gates` can read clear while `ux` is pending (the ateles#285 class, recurred);
`server.py:609` `_GATE_ORDER` omitting `qa`, `legal`, `release`; and the 8 `workflow_definition` entities,
advisory only, of which `ateles|bug` and `ateles|security` declare `pm` alone before `impl`. Consolidating to
one constant with a parity test is step zero (`four_divergent_copies_of_the_gate_set`,
`migration_is_incremental_no_flag_day`). A data-sourced list may add gates and never remove one, as a
correctness rule, not an availability fallback (C5). ateles#719 (drive sequences from the entities) is open.

### The gate is PR-independent

`evaluate_gate()` takes confidence, `action_type`, policy, and successful recurrences; no PR, issue, or
repository. `write_checkpoint_brief()` keys on `task_entity_id`. The default policy
(`ent_dfce6edecefe3eb7fc9e0337`) lists `send_external_comms` and `publish` as high blast; Apis maps Corvus
and Struthio to them, subscribes to `checkpoint_brief` on SSE, and re-dispatches on resolution; the operator
answers over Telegram. The consent gate for outbound non-code work exists and closes off GitHub; do not
build a second one (principle 6). What is PR-shaped is only the review machinery in `swarm_dispatch.py`
(issue `gate_status`, review verdicts, Vanellus merges), a separate mechanism layered on GitHub.

### Three blast tiers; `operator_only` is `NEVER`; unclassified fails closed and loudly

`blast_radius_for()` on main (ateles#724, merged): `NEVER_AUTO_EXECUTE_ACTION_TYPES = {"operator_only"}` wins
ahead of both policy sets, so a policy cannot demote it; a declared action type in neither set logs a
warning naming the value and resolves to `NEVER`, never to `blast_radius_default`; an absent action type
keeps the policy default, since "nothing declared" stays distinct from "declared and unclassified". `NEVER`
is a third tier: `HIGH` still auto-executes once a recurring series clears its count; `NEVER` short-circuits
ahead of the confidence axis and the recurrence path. The advisory path (`_route_task`) and the enforcing
path resolve identically, and a test asserts the duplicated never-set stays identical across the two
modules. `_FALLBACK_LOW_BLAST` exists because the fallback policy carried an empty low set; under
`failure_posture.md` an unreachable policy source is a halt, so that fallback is transitional.

### An unreadable workflow is `unknown`, and `unknown` holds

Never proceed on an empty sequence. An unreadable `workflow_definition` is a distinct state: dispatch held,
one aggregated escalation, never an exception swallowed into an empty tuple. Precedents on main: `_required_ci_state`
returns `"unknown"`; `_gates_green()` fails closed on an unreadable issue.

### Non-code deliverables pass through the same gate

A post, an outreach mail, a release, or a payment reaches approval through `action_type` and blast radius on
the task path, as code does. What non-code agents lack is delivery of the task (`work_model.md`), not a gate.

## Not enforced today, stated so nobody reads it as settled

- **The raiser cannot resolve.** `write_checkpoint_brief()` takes no principal; `read_checkpoint_resolution()`
  and the MCP `resolve_checkpoint` classify on `status` alone; nothing records who flipped it. An approval is
  whoever writes the status. Prior art names the smallest fix (GitHub prevent-self-review; NIST dynamic
  separation of duty); it is the P1 seed of P3 and is not built.
- **A waiting checkpoint has no timeout.** A brief sits at `awaiting_operator` indefinitely. Step Functions'
  heartbeat timeout on a waiting task is the analog; #378 requires explicit timeout states; none exists.

## Contradictions this document touches

**C3, four copies.** Resolved: one constant, above.

**C5, the floor.** The gate-state plan body argues for `PRE_IMPL_GATES` as a floor the data may add to; its
decisions map retracts that (`hardcoded_floor_proposal_is_retired`). Resolved for the retraction;
`failure_posture.md` states why.

**C6, merge authority, three states.** Stored policy says merge is operator-gated: `execution_policy`
`ent_e3ea122f7dc286fc0868e4a1` ("merge stays gated"), `ent_dfce6edecefe3eb7fc9e0337` with blocking merge
checkpoints, operator-ratified 2026-07-07. The code default agrees: `APIS_AUTONOMY_AUTO_MERGE` defaults to
`"0"` (`swarm_dispatch.py:1385`). The running configuration disagrees: the flag was set to `1`
(`analysis_finding` `ent_ca1a3f3c0dea2d9b583249fe`), Vanellus merged with auto-merge on twice on 2026-09-02
(throughput `abandon_merge_owner_hypothesis`), and 49 of 49 sampled merge briefs from June and July are still
`open`, so the merge queue was never drained through the entity. What is true today, as far as stored
evidence reaches: PRs merge without an operator checkpoint, and the stored policy is stale as a description
of the running system. Resolved by saying so. Open, and operator-only: whether the policy changes to match
the flag or the flag returns to the policy. The flag is per-checkout runtime state; its current value is
verified from the running process, not from this document.

**C11, confidence × blast is blast-only in practice.** The default policy is labelled calibrated at 0.85; 14
of 20 sampled PLAN briefs carry confidence 0 with empty `proposed_alternatives`, and blast radius is inferred
from the handling agent (ateles#682). Resolved by stating it: today the gate decides on blast radius alone,
until agents produce the score (#688) and blast comes from what the task does (#689), both open, in the
order `gating_vocabulary_order_is_load_bearing` fixes.

**C4, retired agent names in workflow data.** Three of eight `workflow_definition` entities name `gryllus`
(retired 2026-06-12), one `paradisaea`; `agent_policy` `ent_bf7732d200868ea64d5e8182` names Bombycilla for
the arch gate. The no-stale-mirror constraint is enforced for `docs/agents/`, not for design entities.
Stated; the fix is a data correction under the gate-state plan.

## Prior art

GitHub environment protection rules are the nearest declarative gate model; Ateles shares the declarative
definition and pre-step approval, not per-environment routing or the 1-of-n rule, since blast radius selects
the gate. Cedar's rule (zero permits is deny; any forbid wins) is the semantics the advisory and enforcing
paths share. Sources: `ent_08460968e6f49dac21510f4a`.
