# Documentation plan & reconciliation

> **What this is.** A first-principles plan for the documentation this repository *should* have, derived from
> an audit of what the code actually does (not from the existing docs), prioritized for the
> [ideal operator](icp.md) — followed by an audit of the *current* documentation and a concrete plan to
> reconcile the two. This document is the source of truth for the doc set; the
> [docs index](README.md) is the reader-facing navigation.

The bullseye reader is the **forking solo founder-operator** (see [icp.md](icp.md)). Their journey sets the
priority order: **decide → understand → stand up → operate → extend → maintain.** Documentation priority
follows that journey.

## Purpose

Specify the documentation set this repo should have (prioritized for the ideal operator), audit what exists
today, and lay out the actions to reconcile the two — so the docs match the audited functionality.

## Scope

Part 1 is the prioritized outline (what's needed). Part 2 audits the current `docs/` tree (current / stale /
misplaced / off-topic / operator-personal). Part 3 lists the reconciliation actions taken in this pass and
the follow-ups recommended to the operator. Excludes editing the design-note bodies themselves.

---

## Part 1 — The documentation the repo needs (prioritized outline)

Ordered by ICP priority. **P0** is what a prospective adopter needs in the first five minutes; **P5** is
deep reference consulted occasionally. "Status" = does an adequate doc exist today (see Part 2).

### P0 — Decide & orient (first 5 minutes: *what is this, is it for me?*)

| # | Doc | Purpose | Status |
| --- | --- | --- | --- |
| 1 | **README** | One-screen accurate picture: dual nature (substrate + ops suite), what it does, who it's for | ✅ rewritten |
| 2 | **ICP** (`icp.md`) | Whom the repo is primarily for, derived from functionality; explicit anti-profile | ✅ created |
| 3 | **Architecture overview** (`architecture.md`) | Entity model, four tiers, substrate, Neotoma integration | ✅ exists |
| 4 | **Documentation index** (`docs/README.md`) | Navigation map of the whole doc set, organized by the ICP journey | ⚠️ rewritten (was stale) |

### P1 — Understand the system (*is it worth adopting?*)

| # | Doc | Purpose | Status |
| --- | --- | --- | --- |
| 5 | **Capabilities / operational surface** (`capabilities.md`) | What the 18 daemons + 87 skills actually do, by life/work domain — the under-documented operational half | 🆕 created |
| 6 | **Agent taxonomy** (`taxonomy.md`) | The full roster: tiers, genera, status | ✅ exists |
| 7 | **Data types** (`data_types.md`) | The Neotoma entity-type catalog the swarm reads/writes | ✅ exists |
| 8 | **Substrate choice** (`neotoma_vs_alternatives.md`) | Why Neotoma as the canonical layer vs. alternatives | ✅ exists |

### P2 — Stand it up (*fork it and run one daemon*)

| # | Doc | Purpose | Status |
| --- | --- | --- | --- |
| 9 | **Forking & adoption guide** (`forking.md`) | What is operator-specific vs. portable; the context entities, secrets, identities, and grants a new operator must supply | 🆕 created |
| 10 | **Setup** (`setup.md`) | Neotoma + venv + AAuth keypairs + grants + first daemon under launchd | ✅ exists |
| 11 | **Secrets management** (`secrets_management.md`) | SOPS+age model, offline materialization | ✅ exists |
| 12 | **AAuth & keys** (`aauth.md`, `aauth/keys.md`) | Identity provisioning, keypair format, signing | ✅ exists |
| 13 | **Cloud hosting** (`cloud_hosting.md`) | docker-compose on a small ARM host over Tailscale | ✅ exists |

### P3 — Operate & extend (*run it daily, add agents/workflows*)

| # | Doc | Purpose | Status |
| --- | --- | --- | --- |
| 14 | **Swarm orchestration** (`swarm_orchestration.md`) | `workflow_definition` + gate semantics + the artifact-header dispatch contract | ✅ exists |
| 15 | **Agent execution** (`agent_execution_runbook.md`, `task_execution_loop.md`) | How a dispatch runs end-to-end; readiness + execution gates | ✅ exists |
| 16 | **HITL & gating** (`swarm_hitl_checkpoints_design.md`) | Confidence × blast-radius, checkpoint_brief approval | ✅ exists (design) |
| 17 | **GitHub interaction** (`pr_review_routing.md`, `swarm-trigger-layer.md`, `swarm_github_interaction_design.md`) | Issue/PR triage, PR-review routing, webhook→Apis | ✅ exists |
| 18 | **A2A gateway** (`a2a.md`) | Inbound agent-to-agent task receiver | ✅ exists |
| 19 | **Session integrity** (`session_integrity.md`) | Plan-link / turn-storage / artifact-linkage invariant + hooks | ✅ exists |

### P4 — Develop & maintain the substrate (*contribute hardening*)

| # | Doc | Purpose | Status |
| --- | --- | --- | --- |
| 20 | **MCP server development** (`mcp_server_development_guide.md`) | Building/extending the harness + grant proxy | ✅ exists |
| 21 | **Linting & tests** (`linting-guide.md`, `test-setup-guide.md`, `testing/`) | The 8 linters, git hooks, test patterns | ✅ exists |
| 22 | **RC autodeploy** (`daemon_rc_autodeploy.md`) | Rolling-main = RC deployment of daemons | ✅ exists |
| 23 | **Credential health** (`credential_health.md`, `credential_management.md`) | Proactive re-auth across the swarm | ✅ exists |

### P5 — Deep design & reference (*consult as needed*)

| # | Doc | Purpose | Status |
| --- | --- | --- | --- |
| 24 | **QA evals design** (`swarm_qa_evals_design.md`, `swarm_qa_evals_qe3_design.md`) | Agents-as-users eval framework | ✅ exists (design) |
| 25 | **Durable execution** (`durable_execution_substrate.md`) | Neotoma as a durable-execution substrate | ✅ exists |
| 26 | **Multi-tenant** (`multi_tenant.md`) | The (out-of-scope today) multi-operator path | ✅ exists |
| 27 | **Data publishing** (`data_publishing_transformation.md`, `data_publishing_privacy_guidelines.md`) | How operator data is transformed/published | ✅ exists |
| 28 | **Operator runbooks** (`runbooks/`, `developer/`) | Operator-specific operational notes | ✅ exists (see Part 2 caveats) |

---

## Part 2 — Audit of the current documentation

`docs/` today holds **294 files across 43 directories** — but a large share is **not documentation**, and a
meaningful slice is **stale** (describes the pre-swarm "three-layer" / Cursor-rules era) or **off-topic**
(home-automation troubleshooting, a conference transcript) or **operator-personal** (health plans, an
outreach contact list). The reconciliation opportunity is to separate the durable architecture/operating
docs from this sediment.

### A. Accurate & current — keep (the real doc set)

Architecture/operating docs that match the audited reality:

`architecture.md`, `taxonomy.md`, `phases.md`, `data_types.md`, `neotoma_vs_alternatives.md`,
`swarm_orchestration.md`, `swarm_smoke_test_plan.md`, `smoke_test_runbook.md`, `task_execution_loop.md`,
`agent_execution_runbook.md`, `agent_execution_architecture.md`, `a2a.md`, `pr_review_routing.md`,
`swarm-trigger-layer.md`, `swarm_github_interaction_design.md`, `swarm_hitl_checkpoints_design.md`,
`swarm_qa_evals_design.md`, `swarm_qa_evals_qe3_design.md`, `durable_execution_substrate.md`, `aauth.md`,
`aauth/keys.md`, `secrets_management.md`, `credential_management.md`, `credential_health.md`,
`session_integrity.md`, `setup.md`, `cloud_hosting.md`, `daemon_rc_autodeploy.md`,
`mcp_server_development_guide.md`, `linting-guide.md`, `test-setup-guide.md`, `testing/*`, `multi_tenant.md`,
`data_publishing_transformation.md`, `data_publishing_privacy_guidelines.md`, `agents/*` (generated),
`runbooks/agent-rename-and-isolation.md`.

### B. Stale or superseded — rewrite or archive

| Item | Problem | Disposition |
| --- | --- | --- |
| `docs/README.md` | Index describes an **old Asana/Cursor-rules repo** (`data_rules.md`, `/reports/asana-api-coverage-analysis.md`, `/plans/neotoma-architecture-integration.md`) that no longer exists | **Rewrite** as a real index (done — see [README.md](README.md)) |
| `docs/shared/**` (31 files) | Legacy "three-layer Strategy/Execution/Truth" foundation + 9 cursor agent-policy files; superseded by the entity/agent_policy model. Doubled path `docs/shared/docs/...` | **Archive** under `docs/archive/` or delete after confirming nothing references it |
| `cursor_directory.md`, `foundation-vs-ateles-rules.md`, `skills-and-hooks-before-after.md`, `skills-and-hooks-guide.md`, `cursor_rules/*` | Cursor/foundation-era guidance, largely pre-swarm | **Archive**; fold any still-true bits into `linting-guide.md`/`setup.md` |
| `legacy_data_types_inventory.md` | Self-described legacy; superseded by `data_types.md` | **Archive** (keep as historical inventory) |

### C. Misplaced — relocate (not documentation)

| Item | Problem | Disposition |
| --- | --- | --- |
| `docs/mcp/**` (**145 files**: 56 `.py`, shell, CI, `requirements.txt`) | These are **full MCP server projects/code** (asana, onepassword, homekit, parquet, google-search-console, whatsapp, x_api, typefully, interviews_admin, google-maps), not docs. They dominate the `docs/` file count and pollute every docs metric. | **Relocate** to a top-level `mcp-servers/` (or `execution/mcp/vendor/`); keep only true per-server `README.md` docs under `docs/mcp/` if desired |
| `docs/developer/*` | Operator-specific operational rules (chatgpt-share, formica issue processing, finances-dashboard diagnostics, cloudflare token directive) | **Relocate** to `docs/runbooks/` (operator runbooks) and de-PII |

### D. Off-topic for a reference architecture — archive or move out

| Item | Problem | Disposition |
| --- | --- | --- |
| `transcript_peter_thiel_sxsw_2013_*.md` | A 2013 conference transcript — not repo documentation | **Remove** from `docs/` (belongs in Neotoma as a `transcription`/source entity, not the public doc set) |
| `homekit_*.md` (4), `docker_vs_native_homeassistant.md`, `why_mdns_reflector_may_not_work.md`, `tunnel_processes_cleanup.md` | Home-automation troubleshooting notes — operator's smart-home, unrelated to the swarm | **Move** to `docs/runbooks/home-automation/` or out of the repo |
| `generic_pdf_form_filler_guide.md` | A standalone how-to unrelated to the swarm core | **Move** to `docs/runbooks/` |
| `neotoma_developer_release_tester_*` (2), `neotoma_io_redirect.md`, `neotoma_post_lookup.md`, `agent_dependency_discovery_signal_loss.md` | Neotoma-product GTM / essay artifacts, not Ateles architecture | **Move** to the neotoma repo |

### E. Operator-personal / PII in a PUBLIC repo — REMOVED, history scrub pending ✅⚠️

A first-principles PII audit of the working tree and all 51 commits found the third-party exposure was
**bounded and shallow**: one file, in one commit (`94aa438`), plus operator-own health notes. No secrets,
IBANs, national IDs, or real third-party emails/phones anywhere; the 2 Bitcoin matches are a public
"onboarding buddy" tip address in `loop-start`, not operator PII.

| Item | Concern | Action taken |
| --- | --- | --- |
| `docs/outreach/LINKEDIN_ICP_PRIORITY_LIST.md` (814 people) | **Third-party names + LinkedIn profiles** in a public repo → RGPD Art. 6(1)(f) | **Removed** from the working tree; `docs/outreach/` gitignored. History scrub pending — see [runbooks/pii-history-scrub.md](runbooks/pii-history-scrub.md) |
| `docs/health/lean_bulk_8_week_plan_2026.md`, `docs/health/facial_puffiness_mitigation_checklist.md` | Operator's personal health data | **Removed** from the working tree; `docs/health/` gitignored. History scrub pending (same runbook) |
| `docs/developer/neotoma_finances_dashboard_neotoma_side_diagnostics.md` | Suspected financial specifics | **Re-assessed: not PII.** It is a technical API-diagnostics runbook (HTTP checks, endpoint contracts) — kept |

> **Why the working-tree removal isn't enough.** On a public repo the file remains fully browsable in the
> old commit until `main`'s history is rewritten and force-pushed, and GitHub purges its cached views. That
> irreversible step is the operator's to run — the [history-scrub runbook](runbooks/pii-history-scrub.md) has
> the exact commands, a GitHub Support request template, and a PR/fork checklist.

### F. Command docs that duplicate skills — consider folding

`language_command.md`, `learn_neotoma_command.md`, `neotoma_learn_command.md`, `store_neotoma_command.md`,
`agent_auto_invocation.md` overlap with their `.claude/skills/<name>/SKILL.md`. Keep only if they add
operator-facing context the SKILL.md omits; otherwise link to the skill from a single "skills" reference.

---

## Part 3 — Reconciliation actions

### Done in this pass

1. **Rewrote `README.md`** from the functionality audit — dual framing, accurate daemon/agent counts, honest
   maturity, corrected the launchd claims, fixed the "MIT" claim by adding a real `LICENSE`.
2. **Created `docs/icp.md`** — the ideal-operator profile from first principles.
3. **Rewrote `docs/README.md`** — a true documentation index organized by the ICP journey (replacing the
   stale Asana/Cursor-era index).
4. **Created `docs/capabilities.md`** — the operational surface (18 daemons + skill categories by domain),
   documenting the previously under-represented half of the repo.
5. **Created `docs/forking.md`** — the adopt-by-forking guide: what's operator-specific vs. portable.
6. **Created this plan** (`docs/documentation_plan.md`).
7. **Added `LICENSE`** (MIT) to match the README's repeated license claim.
8. **Removed the PII files** (Part 2.E) from the working tree, gitignored `docs/outreach/` and `docs/health/`
   to prevent recurrence, and wrote the [history-scrub runbook](runbooks/pii-history-scrub.md) for the
   operator to finish the job on `main`.

### Reconciliation follow-ups

**Done (2026-06-25):**
- ✅ **Ran the history scrub** — `main` + 45 branches rewritten with `git filter-repo` and force-pushed; the
  PII is unreachable in a fresh clone, and the PR-ref + cache purge is requested from GitHub Support. See
  [runbooks/pii-history-scrub.md](runbooks/pii-history-scrub.md).
- ✅ **Moved off-topic docs** out of the doc set — HomeKit/home-automation (7) + the PDF-form-filler guide
  relocated under [runbooks/](runbooks/) (`runbooks/home-automation/`); the SXSW transcript, the
  agent-discovery essay, and the 2 Neotoma developer-release GTM docs archived under [archive/](archive/) (to
  be re-homed to the neotoma repo / Neotoma). The 2 Neotoma *ops* runbooks (`neotoma_io_redirect`,
  `neotoma_post_lookup`) were kept as operator runbooks under [runbooks/](runbooks/) — `io_redirect` drives a
  live script in this repo.
- ✅ **Folded the command-docs (Part 2.F)** — the 5 SKILL.md-duplicating command docs archived under
  [archive/](archive/); the `.claude/skills/<name>/SKILL.md` mirror is the source of truth.
- ✅ **Archived the dead Cursor-era docs (Part 2.B, partial)** — `foundation-vs-ateles-rules`,
  `skills-and-hooks-{before-after,guide}`, `legacy_data_types_inventory`, `cursor_directory`, `cursor_rules/`
  → [archive/](archive/) (gitleaks allowlist paths updated). `docs/shared/**` was **left in place**: 3 of its
  files (`agent-workflow-requirements`, `agent-mcp-access-policy`, `agent-data-rules`) are cited as live
  policy by the linters + `.gitleaks.toml`.

**Still pending (larger / operator-judgment moves):**
- **Relocate `docs/mcp/**` (145 code files)** out of `docs/` to a top-level `mcp-servers/`.
- **`docs/shared/**` (the legacy three-layer foundation)** — relocate the 3 live policy docs
  (`agent-workflow-requirements`, `agent-mcp-access-policy`, `agent-data-rules`) to an active home and update
  their ~7 linter / `.gitleaks.toml` / `linting-guide.md` citations, then archive the rest. Deferred because
  it edits linter code (option 2 from the B discussion).
- *(nicety)* Build a single consolidated skills reference if the archived command-docs' context is still
  wanted.

The remaining relocations touch a large code tree (`docs/mcp/`) or need a reference sweep (`docs/shared/`)
where a wrong move is hard to reverse — sequence them with a grep-for-references check before each move.
