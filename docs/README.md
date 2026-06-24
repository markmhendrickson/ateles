# Ateles documentation

Documentation for **Ateles** — a single operator's personal + software agent swarm,
run by ~18 background daemons over a shared memory layer ([Neotoma](https://github.com/markmhendrickson/neotoma)),
with every agent action signed, scoped, and logged.

This index is organized by **who you are and what you're trying to do**, in priority order:
**decide → stand up → run → extend → maintain.** Status markers: ✅ exists · 🟡 partial / scattered · ⬜ planned.

> New here? Start with the **[Start here](#start-here--the-onboarding-ladder)** ladder below, then
> run `python3 execution/scripts/ateles_doctor.py` to see which rung your environment can reach.

---

## Start here — the onboarding ladder

Ateles is adopt-by-fork reference infrastructure, not a packaged install. Each rung below is an
independently runnable state that unlocks exactly one new dependency. `ateles_doctor.py` reports the
highest rung your environment currently reaches.

| Rung | You reach | New dependency | Works when… |
| ---- | --------- | -------------- | ----------- |
| **0 · Comprehend** | Clone, read the architecture, open one daemon + one SKILL.md | none | you can name the layers |
| **1 · First agent** | `claude --print --append-system-prompt SKILL.md "<task>"` | Claude CLI only | an agent answers in its persona (stub mode — no Neotoma, no keys) |
| **2 · Connect memory** | Point at Neotoma; supply minimal context entities | Neotoma + token | an agent reads & writes real state |
| **3 · First daemon** | Run one low-risk daemon (morning-brief / gorilla) foreground | one channel | a notification lands in Telegram |
| **4 · Attributed identity** | Mint one AAuth keypair + agent_grant | keys + grant | an observation is written with sub ≠ pat |
| **5 · Persist & schedule** | launchd / docker-compose; SOPS+age secrets; auto-deploy | host + secrets | survives reboot, redeploys from `origin/main` |
| **6 · Extend** | Add daemons; author your own agent / workflow definitions | — | your own agent runs end-to-end |

---

## P0 · Orient & decide
*Evaluator — the first 10 minutes.*

- ✅ [`../README.md`](../README.md) — what it does, who it's for, the layers, status
- ✅ [`architecture.md`](architecture.md) — the layered system design and Neotoma integration
- ✅ [`taxonomy.md`](taxonomy.md) · [`agents/`](agents/) — the agent roster by tier (T1–T4)
- ⬜ Concepts & glossary — Neotoma entity, AAuth, agent_grant, gate, daemon, tier, mirror, blast-radius (*to write*)
- 🟡 [`multi_tenant.md`](multi_tenant.md) — single-operator assumptions and the multi-operator path

## P1 · Stand it up
*Adopter — the fork-and-run path.*

- 🟡 [`setup.md`](setup.md) — setup walkthrough *(being realigned to the rungs above)*
- ⬜ Configuration reference — every `.env` var: meaning, default, consuming daemon (*highest-priority gap; see [`../.env.example`](../.env.example)*)
- ✅ [`aauth.md`](aauth.md) · [`aauth/`](aauth/) — AAuth keypairs, JWKS, identity provisioning
- ✅ [`secrets_management.md`](secrets_management.md) · [`credential_management.md`](credential_management.md) — SOPS+age, the `ateles-private` boundary
- 🟡 [`cloud_hosting.md`](cloud_hosting.md) · [`daemon_rc_autodeploy.md`](daemon_rc_autodeploy.md) — the 3-bucket deployment (Hetzner compose · macOS launchd · GitHub Actions)

## P2 · Run it day to day
*Operator.*

- ⬜ Daemon catalog — one entry per daemon: trigger, inputs, outputs, schedule, failure mode (*high-priority gap; per-daemon READMEs exist under [`../execution/daemons/`](../execution/daemons/)*)
- 🟡 [`email_loop_rollout_runbook.md`](email_loop_rollout_runbook.md) — the email execution loop (a primary operator interface)
- 🟡 [`skills-and-hooks-guide.md`](skills-and-hooks-guide.md) — the Claude Code skills & hooks mechanism
- ⬜ Notifications & paging — priority_rubric, silence windows, what pages you (*to write*)
- ⬜ Audit & observability — querying `agent_action_observation`, replaying an agent (*high-priority gap*)
- 🟡 [`agent_execution_runbook.md`](agent_execution_runbook.md) · [`smoke_test_runbook.md`](smoke_test_runbook.md) · [`tunnel_processes_cleanup.md`](tunnel_processes_cleanup.md) — operational runbooks

## P3 · Extend it
*Builder.*

- ✅ [`swarm_orchestration.md`](swarm_orchestration.md) — workflow_definition, gates, dispatch ordering, the artifact-header contract
- 🟡 Add an agent — agent_definition → keypair → grant → mirror (*covered partially in swarm_orchestration.md*)
- ⬜ Write a daemon — the `lib/daemon_runtime` contract: SSE vs poll, fail-open, lifecycle (*to write*)
- ⬜ Autonomy & gating model — confidence × blast-radius, readiness, checkpoints, drift→policy (*to write; design notes in [`swarm_hitl_checkpoints_design.md`](swarm_hitl_checkpoints_design.md)*)
- ✅ [`data_types.md`](data_types.md) — the entity / data-type catalog
- ✅ [`mcp_server_development_guide.md`](mcp_server_development_guide.md) · [`pr_review_routing.md`](pr_review_routing.md) — MCP servers, the grant proxy, PR review routing

## P4 · Govern & maintain
*Maintainer.*

- 🟡 Security model & trust boundaries — single-operator assumptions, sub ≠ pat (*see README "Security defaults"*)
- ✅ [`session_integrity.md`](session_integrity.md) — the plan-binding / turn-storage invariant
- ✅ [`swarm_smoke_test_plan.md`](swarm_smoke_test_plan.md) · [`swarm_qa_evals_design.md`](swarm_qa_evals_design.md) — testing tiers & eval design
- 🟡 [`data_publishing_privacy_guidelines.md`](data_publishing_privacy_guidelines.md) — PII scrubbing, RGPD legitimate-interest basis
- 🟡 [`linting-guide.md`](linting-guide.md) — linters, naming, the "entities not files" / mirror discipline
- ✅ [`phases.md`](phases.md) — roadmap / phases / status

---

## Reference & design notes

Deeper design docs and subsystem references not on the onboarding path: `a2a.md`, `swarm_orchestration.md`,
`swarm_github_interaction_design.md`, `swarm_hitl_checkpoints_design.md`, `swarm-trigger-layer.md`,
`task_execution_loop.md`, `durable_execution_substrate.md`, `agent_execution_architecture.md`,
`pr_review_routing.md`, and the [`mcp/`](mcp/), [`developer/`](developer/), [`runbooks/`](runbooks/),
[`testing/`](testing/), and [`shared/`](shared/) subdirectories.

## Operator notes (not repo functionality)

Some files under `docs/` are operator working notes rather than documentation of swarm functionality —
e.g. the HomeKit pairing notes, `why_mdns_reflector_may_not_work.md`, `docker_vs_native_homeassistant.md`,
the outreach/survey templates, and the saved transcript. These are candidates for an `archive/` subdir
and are intentionally omitted from the priority map above.
