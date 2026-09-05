# Ateles documentation

Documentation for the Ateles agent swarm — a single-operator AI fleet that runs a founder's company and
personal life, built on [Neotoma](https://github.com/markmhendrickson/neotoma) as the canonical memory and
state layer. Start with the [README](../README.md) for the one-screen picture.

This index is organized by the path a new adopter actually takes: **decide → understand → stand up →
operate → extend → maintain.** For the full reasoning behind this structure — and an audit of which docs
are current, stale, misplaced, or operator-personal — see the
[documentation plan & reconciliation](documentation_plan.md).

## Purpose

Navigate the full Ateles documentation set, ordered by the path a new adopter takes.

## Scope

Indexes every current doc by priority tier (P0–P5). For the reasoning behind the structure and the audit of
stale, misplaced, or operator-personal files, see the [documentation plan](documentation_plan.md).

---

## P0 · Foundation

*What the swarm's work conforms to.* Foundation documents in [`docs/foundation/`](foundation/), each
phase-agnostic and evergreen: they define the design whole, mark undecided questions open, and carry no
implementation state. The arch gate and the review lenses read the **kernel** (Principles, Work model,
Gates and workflows) on every review and the **keyed** documents by changed path (`conformance.md`).
What is built, and where the code still contradicts the design, is measured in
[`foundation/status.md`](foundation/status.md), dated and regenerated rather than maintained.

Kernel (always read):

- [**Principles**](foundation/principles.md) — the eleven invariants, each with the class of mechanism that
  makes it bind.
- [**Work model**](foundation/work_model.md) — pull as the only delivery; assignment constrains
  eligibility; claim and lease as one primitive (lease as relationship); liveness derived at read time;
  no assignment log; task status + lease `held` / `lapsed` / `returned`; tasks go through workflows in
  batches; artifacts are records a batch leaves.
- [**Gates and workflows**](foundation/gates_and_workflows.md) — `workflow` declares steps; a batch is
  the tasks going through them and the record of that; step state from batch + lease + `sign-off`;
  `step_status` projects; one step set; actions are entities and are taken through the PR-independent
  action gate on confidence and three blast tiers; the checkpoint.

Keyed (read when matching paths change — see `conformance.md`):

- [**Failure posture**](foundation/failure_posture.md) — Neotoma as a hard dependency: halt work, never stop
  observing, announce off-Neotoma, read back every write, refuse resume-by-replay.
- [**Vocabulary**](foundation/vocabulary.md) — canonical terms, each with a definition, its related
  terms, the words it bans outright (**Never**) and the senses it bans (**Not for**), grouped by the
  document that owns each; linted by `execution/scripts/check_foundation_vocabulary.py`.
- [**Data model**](foundation/data_model.md) — how each concept is recorded: entity type, key fields,
  edges, derived reads, projections, and what is deliberately not a field; the relationships table; the
  record conventions (observations, corrections, idempotency keys, schema versions, `raw_fragments`).
- [**Adapters**](foundation/adapters.md) — how external systems reach the work model and how it reaches
  them: inbound events are signals about artifacts (a sign-off by a named principal, an observation, an
  action confirmation, or a task for intake, never a workflow instruction); outbound operations are
  actions through the action gate; the two invariants, the four outcomes, and the five rules every
  system applies, and what a new adapter must satisfy before the record trusts it. Each system's full
  surface is in its own keyed document below.
- [**GitHub**](foundation/github.md) — the code host's full event surface: every event GitHub can deliver
  across issues, pull requests, reviews, releases, security advisories, checks, and repository-level
  operations, each marked handled, deliberately ignored, or unhandled, and each resolving to one of the
  four adapter outcomes or to a counted drop; the outbound operation, action class, and confirmation for
  every step that reaches the host; and what the adapter withholds from a security advisory.
- [**Gmail**](foundation/gmail.md) — the mail system's full surface: every inbound signal and what it
  becomes, the conditions the system never delivers, a thread and its messages each artifacts related by
  `PART_OF`, the outbound operations the workflows take on mail, and what the adapter refuses.
- [**Calendar**](foundation/calendar.md) — the calendar's full surface: a series and its occurrences each
  artifacts related by `PART_OF`, every inbound signal and what it becomes, the one event-write path, and
  what the design uses of the API against what it offers.
- [**Telegram**](foundation/telegram.md) — the operator's chat channel, and why a message in it is not an
  instruction: the callback payload is the swarm's own text and free text is not; a reaction never carries
  a decision; a read during a halt is answered with the halt and never with data.
- [**Payments**](foundation/payments.md) — the least reversible boundary: the dedup key, the transfer whose
  confirmation never returned, the approver shown exactly what the verifier signed, tolerance as an
  `action_policy` value defaulting to zero, and terminal declared per rail and bound per instance.
- [**Conformance**](foundation/conformance.md) — how issue-based work binds to the foundation: the
  always-read kernel, the path-keyed reading list, the design-basis statement, the direction of truth per
  record class, and the rule that keeps state out of the foundation.
- [**Conformance suite**](foundation/conformance_suite.md) — the acceptance suite the foundation is judged by,
  designed and not built: one row per rule with the from-zero setup, the action, the observable that goes red,
  and whether it is mechanical or review-only; the rules with no failing artefact and what each would need to
  say; the pairs of rules whose observables contradict; the bootstrap sequence a from-zero run needs; the
  permutation axes and which cross-products are load-bearing; and the disposable-instance isolation that
  makes touching the production record impossible rather than unlikely.
- [**Authority model**](foundation/authority_model.md) — the tuple `principal + domain + scope + action +
  conditions + time` defined whole: principals, tenancy, ownership, grants, attribution, delegation,
  approval, quorum and separation of duties, and the initiative objects, with the identity decision
  (C9) settled on an `operator` entity and the P4 brief's questions marked open.

Authored companions (design prose; **not** inlined into review prompts):

- [**Scenarios**](foundation/scenarios.md) — ten walkthroughs of the work model and gate model in motion:
  claim/lease/lapse, assignment, several tasks going through a workflow as one batch, a task detached
  from a batch, a parent with children in independent batches, an operator-only task, an action
  discovered mid-workflow at each blast tier, the halt, and intake into a successor.
- [**Workflows**](foundation/workflows.md) — designs of the core workflows (intake and successors);
  purpose, steps, fast paths — binds via `workflow` entities and `render_workflow_docs.py --check`.
- [**Migration**](foundation/migration.md) — the population plan's second leg: how the record an instance
  already holds is carried into the design's types — each type's disposition (keep, re-type, derive,
  retire, introduce), the record primitive that carries it, the order and its dependencies, what is
  reversible, how the carrying is itself governed, and the gaps the mapping exposed in the foundation.

Companion report (not a foundation design document; not in the review reading list):

- [**Status**](foundation/status.md) — dated, perishable measurement of the gap between the foundation and
  a checkout; regenerated, never maintained. Foundation docs may name it as the state home only; they must
  not embed its figures as design evidence.

## P0 · Decide & orient

*What is this, and is it for me?*

- [**Who it's for (ICP)**](icp.md) — the ideal operator this is built for, and the explicit anti-profile.
- [**Architecture**](architecture.md) — the entity model, four agent tiers, and Neotoma integration.
- [**Agent taxonomy**](taxonomy.md) — the full roster of agents by tier and status ([per-agent docs](agents/)).
- [**Phases**](phases.md) — the implementation roadmap.

## P1 · Understand the system

*What does it actually do?*

- [**Capabilities**](capabilities.md) — the operational surface: all 18 daemons and the skill catalog, by
  life/work domain. This is the concrete half of the repo.
- [**Data types**](data_types.md) — the Neotoma entity-type catalog the swarm reads and writes.
- [**Neotoma vs. alternatives**](neotoma_vs_alternatives.md) — why Neotoma as the substrate.
- [**Durable execution substrate**](durable_execution_substrate.md) — Neotoma as a durable-execution layer.

## P2 · Stand it up

*Fork it and run one daemon.*

- [**Forking & adoption**](forking.md) — what's operator-specific vs. portable; the context entities,
  secrets, identities, and grants a new operator must supply.
- [**Setup**](setup.md) — Neotoma, venv, AAuth keypairs, grants, first daemon under launchd.
- [**Secrets management**](secrets_management.md) — the SOPS + age model, offline materialization.
- [**AAuth**](aauth.md) · [**Keys**](aauth/keys.md) — agent identity, keypair format, signing.
- [**Cloud hosting**](cloud_hosting.md) — running daemons under docker-compose on a small ARM host.

## P3 · Operate & extend

*Run it daily; add agents and workflows.*

- [**Gates and workflows**](foundation/gates_and_workflows.md) — `workflow` / batch / `sign-off` /
  `step_status`, and the PR-independent action gate (the former
  [`swarm_orchestration.md`](archive/swarm_orchestration.md) and
  [`swarm_hitl_checkpoints_design.md`](archive/swarm_hitl_checkpoints_design.md) are archived with pointers).
- [**Agent execution runbook**](agent_execution_runbook.md) ·
  [**Agent execution architecture**](agent_execution_architecture.md) — how a dispatch runs end-to-end; the
  work model itself is [`foundation/work_model.md`](foundation/work_model.md) (the former
  [`task_execution_loop.md`](archive/task_execution_loop.md) described the retired push model and is archived).
- [**PR review routing**](pr_review_routing.md) · [**Swarm trigger layer**](swarm-trigger-layer.md) ·
  [**GitHub interaction design**](swarm_github_interaction_design.md) — issue/PR triage and webhook flow.
- [**A2A gateway**](a2a.md) — the inbound agent-to-agent task receiver.
- [**Session integrity**](session_integrity.md) — plan-link / turn-storage / artifact-linkage invariant.
- [**Smoke-test plan**](swarm_smoke_test_plan.md) · [**Smoke-test runbook**](smoke_test_runbook.md) — the
  phased rollout cadence.

## P4 · Develop & maintain the substrate

*Contribute hardening.*

- [**MCP server development**](mcp_server_development_guide.md) — building/extending the harness + grant proxy.
- [**Linting guide**](linting-guide.md) · [**Test setup**](test-setup-guide.md) ·
  [**Testing patterns**](testing/) — the 8 linters, git hooks, and test conventions.
- [**Daemon RC autodeploy**](daemon_rc_autodeploy.md) — rolling-main = release-candidate deployment.
- [**Credential health**](credential_health.md) · [**Credential management**](credential_management.md) —
  proactive re-auth across the swarm.

## P5 · Deep design & reference

*Consult as needed.*

- [**QA evals design**](swarm_qa_evals_design.md) · [**QE3 eval authoring**](swarm_qa_evals_qe3_design.md)
- [**Multi-tenancy**](multi_tenant.md) — the out-of-scope-today multi-operator path.
- [**Data publishing transformation**](data_publishing_transformation.md) ·
  [**Privacy guidelines**](data_publishing_privacy_guidelines.md)
- [**Operator runbooks**](runbooks/) — operator-specific operational notes.

---

## Planning

- [**Documentation plan & reconciliation**](documentation_plan.md) — the prioritized outline this index
  follows, plus an audit of the current docs (current / stale / misplaced / off-topic / operator-personal)
  and the reconciliation actions. **Note:** the PII audit found and **removed** a third-party LinkedIn list
  (`docs/outreach/`) and personal health notes (`docs/health/`); the history scrub on `main` has been
  **executed** (see the [PII history-scrub runbook](runbooks/pii-history-scrub.md)). The large tree of MCP
  server *code* formerly under `docs/mcp/` has been **relocated** to top-level `mcp-servers/`.

> **A note on what lives here.** Not everything under `docs/` is documentation. The vendored MCP server *code*
> was relocated to top-level `mcp-servers/`; the live agent-policy docs now live under `docs/policies/`; and
> off-topic, superseded, and legacy-foundation material (the former `docs/shared/`) is under `docs/archive/`.
> See the [documentation plan](documentation_plan.md) for the disposition of each. New documentation should
> follow the P0–P5 structure above.
