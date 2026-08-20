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

- [**Swarm orchestration**](swarm_orchestration.md) — `workflow_definition`, gate semantics, and the
  artifact-header dispatch contract.
- [**Agent execution runbook**](agent_execution_runbook.md) ·
  [**Task execution loop**](task_execution_loop.md) ·
  [**Agent execution architecture**](agent_execution_architecture.md) — how a dispatch runs end-to-end.
- [**HITL checkpoints**](swarm_hitl_checkpoints_design.md) — confidence × blast-radius gating and approval.
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
