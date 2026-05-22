# Ateles Implementation Phases

*Mirror of Neotoma plan `ent_99ace4dd6673aa36ed08b1fe`. Do not edit manually — update via Neotoma.*

## Purpose

Tracks the Ateles implementation roadmap across six phases from initial scaffolding (Phase 0) to full swarm operational status (Phase 5). Serves as the public mirror of the Neotoma plan entity.

## Scope

Covers Phase 0–5 goals and task checklists. Each phase has a defined goal and a list of deliverables. Deferred items (not scheduled to any phase) are listed at the end. For agent details see `taxonomy.md`; for design rationale see `architecture.md`.

---

## Phase 0 — Foundations (current)

**Goal:** Public repo exists, taxonomy documented, env vars renamed, daemon code clean.

- [x] Create `markmhendrickson/ateles` public repo
- [x] Write `docs/taxonomy.md` canonical agent table
- [x] Write `docs/architecture.md` system design
- [x] Refactor Monedula handlers: `therapy.py` + `yoga.py` → generic `WiseTransferHandler` + `BtcTransferHandler` + `PaymentProfile` (env-var driven, no PII in public code)
- [x] Rename `TELEGRAM_TOPIC_PAYMENTS` → `TELEGRAM_TOPIC_MONEDULA` (legacy alias kept)
- [x] Create `markmhendrickson/ateles-private` private repo
- [x] Write `.env.example` with all required env var names (no values)
- [x] Rename Strix daemon (mic recorder) in codebase

---

## Phase 1 — Schema + identity

**Goal:** Neotoma has `agent_definition` entity type; first AAuth identities minted; lib/notify/ and lib/daemon_runtime/ scaffolded.

- [x] Neotoma: register `agent_definition` entity type with `prompt_markdown`, `tool_allowlist`, `agent_grant`, `override_policy` fields
- [x] Neotoma PR: `src/services/override_validation.ts` — enforce `override_policy` at write time (PR #398)
- [x] Create `agent_definition` entities for Onychomys, Monedula, Castor, Formica, Menura in Neotoma
- [x] Mint AAuth keypairs for Onychomys, Monedula, Formica, Castor (stored in ateles-private/keys/)
- [x] `lib/notify/` — Apprise wrapper + priority_rubric loader (notifier.py ~130 LOC)
- [x] `lib/daemon_runtime/` — SSE subscription + agent_definition loader + AAuth signer (3 modules ~250 LOC)
- [x] Create `priority_rubric` entity in Neotoma (`ent_29ca079940c1e996a8c782f2`)
- [x] neotoma-agent daemon: Python, neotoma-repo automation skeleton

---

## Phase 2 — Mirror system + Onychomys migration

**Goal:** Neotoma → git mirror pipeline live; Onychomys config migrated to Neotoma.

- [ ] Neotoma: configure four mirror profiles (ateles-public-agents, ateles-public-skills, ateles-architecture-docs, ateles-private-agents)
- [x] Apus daemon: HTTPS webhook receiver (~200 LOC) + Cloudflare Tunnel at `apus.markmhendrickson.com`
- [ ] Apus: handle all four profiles; commit via `ateles-agent` GitHub identity; log delivery status to Neotoma
- [ ] OpenClaw PR: `workspace-neotoma.ts` (~300 LOC) with file→API fallback chain
- [x] Migrate Onychomys SOUL.md → `prompt_markdown` field in `agent_definition` entity
- [ ] Anthus daemon skeleton: metrics ingestion
- [ ] Tyto daemon skeleton: email triage

---

## Phase 3 — Public agents + Phase 1 hardening

**Goal:** Menura live at markmhendrickson.com/agent/; all Phase 1 daemons using lib/daemon_runtime/.

- [ ] Menura: separate OpenClaw instance, public-scoped AAuth identity, live at markmhendrickson.com/agent/
- [ ] Migrate Formica from JS to Python using lib/daemon_runtime/
- [ ] All daemons using lib/notify/ for notifications
- [ ] `agent_definition_override` entity type + Loxia evaluation (GHA vs. named agent)
- [ ] Temporal evaluation: assess for T3 daemons if in-flight state loss has occurred

---

## Phase 4 — Apis + task automation

**Goal:** Apis replaces Monedula's task scope; payment profiles migrate to Neotoma entities.

- [ ] Apis daemon: general task processor; yoga/therapy as Apis task templates
- [ ] `payment_profile` entity type in Neotoma (visibility=private); Monedula loads from Neotoma instead of env vars
- [ ] Turdus daemon skeleton: social/content scheduling

---

## Phase 5 — Formica Python + full swarm operational

**Goal:** All daemons in Python; full swarm running with Neotoma-canonical config.

- [ ] Formica: full Python rewrite using lib/daemon_runtime/ (lib/telegram/ JS stays for Telegram delivery)
- [ ] All agent_definition entities populated for active daemons
- [ ] Public "last 30 agent actions" feed via Menura (proof artifact for Neotoma Tier 1 ICPs)
- [ ] Quarterly AAuth keypair rotation via neotoma-agent

---

## Deferred

- **Temporal orchestration**: evaluate at Phase 3 when 5+ daemons active and in-flight state loss has occurred; Inngest as fallback
- **agent_definition_override**: Phase 1 Neotoma PR; operator customisation of agent prompts
