# Ateles Implementation Phases

*Mirror of Neotoma plan `ent_99ace4dd6673aa36ed08b1fe`. Do not edit manually — update via Neotoma.*

## Purpose

Tracks the Ateles implementation roadmap across ten phases from initial scaffolding (Phase 0) to full swarm operational status (Phase 9). Serves as the public mirror of the Neotoma plan entity.

## Scope

Covers Phase 0–9 goals and task checklists. Each phase has a defined goal and a list of deliverables. Deferred items (not scheduled to any phase) are listed at the end. For agent details see `taxonomy.md`; for design rationale see `architecture.md`.

---

## Phase 0 — Foundations ✅

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

## Phase 1 — Schema + identity ✅

**Goal:** Neotoma has `agent_definition` entity type; first AAuth identities minted; lib/notify/ and lib/daemon_runtime/ scaffolded.

- [x] Neotoma: register `agent_definition` entity type with `prompt_markdown`, `tool_allowlist`, `agent_grant`, `override_policy` fields
- [x] Neotoma PR: `src/services/override_validation.ts` — enforce `override_policy` at write time (PR #398)
- [x] Create `agent_definition` entities for Onychomys, Monedula, neotoma-agent, Formica, Menura in Neotoma
- [x] Mint AAuth keypairs for Onychomys, Monedula, Formica, neotoma-agent (stored in ateles-private/keys/)
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
- [x] `agent_definition_override` entity type: registered schema in Neotoma (schema ID `308d67b1`); enforcement via PR #398
- [ ] Loxia evaluation: GHA + Claude API first (~30 lines); promote to named T3 only if Neotoma attribution of actions matters
- [ ] Temporal evaluation: adopt only if a daemon crash causes lost in-flight state that demonstrably hurts operations; Inngest as fallback
- [x] Lanius (GitHub workflow coordinator): GHA cron + workflow_definition schema landed; PR review chain + Struthio release trigger pending Phase 6
- [ ] neotoma-agent: due-date hygiene T4 skill — fires on `task` entity creation to add due dates and domain tags

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

---

## Phase 6 — Swarm coordinator + integrity monitor

**Goal:** Anthus and Otus operational; webhook secret rotation automated.

- [ ] Anthus daemon: swarm coordinator — global view of work-in-flight, surfaces conflicts to Onychomys
- [ ] Otus daemon: integrity monitor — weekly audit, due-date hygiene, system-issue self-healing, override conflict diff
- [ ] Implement webhook secret quarterly rotation via Otus with 7-day grace period

---

## Phase 7 — Ingestion agents

**Goal:** All external data sources flowing into Neotoma via dedicated ingestion agents.

- [ ] Cygnus: Google Calendar polling via `gws` CLI
- [ ] Aix: Asana webhook + bidirectional sync
- [ ] Mergus: Coinbase + Wise + Plaid + BTC + Stacks daily snapshots
- [ ] Geococcyx: Twilio SMS webhook
- [ ] Tinamus: HomeAssistant polling (5 min cadence)
- [ ] Turdus: email triage daemon — hourly Gmail poll → tasks for Apis
- [ ] Strigops: analytics gatherer — GA4, GSC, Umami, X, Typefully, Instagram backends

---

## Phase 8 — Product role panel

**Goal:** Product perspective agents operational for product development workflows.

- [ ] Wire Sturnus to `product_feedback.created` subscription
- [ ] Build product role panel: Pavo (PM), Paradisaea (designer), Bombycilla (architect), Phoenicurus (QA)

---

## Phase 9 — Meta-agent + remaining invocables

**Goal:** Cathartes can define new agents; all planned invocable agents operational.

- [ ] Cathartes meta-agent: writes new `agent_definition` entities from operator description
- [ ] Remaining invocable agents: Anas, Aquila, Falco, Tachornis, Procyon, Lutra, Lupinus, Salvia
- [ ] Expand Loxia/Gryllus to handle PRs across all `ateles-agents/<genus>` repos

---

## Pending setup (not phase-gated)

- **`ateles-agent` GitHub machine account**: create account; generate fine-grained PAT (Contents: write on `ateles` repo); store in `ateles-private/.env` as `ATELES_AGENT_PAT`; wire into Apus installed plist
- **`neotoma-agent` GitHub identity**: `castor-agent` account (Pull Shark x2) remains as-is; create new `neotoma-agent` GitHub account for future automation

## Completed setup (done)

- ~~**Apus env wiring**~~: all env vars wired into installed plist (`~/Library/LaunchAgents/com.ateles.apus.plist`) including `NEOTOMA_BEARER_TOKEN`
- ~~**Neotoma webhook subscription**~~: Apus endpoint (`https://apus.markmhendrickson.com/webhook`) registered as Neotoma webhook subscriber (subscription ID `7ce524e4`, entity `ent_6ba1914462908f682f206b56`)

## Deferred

- **Temporal orchestration**: evaluate at Phase 3 when 5+ daemons active and in-flight state loss has occurred; Inngest as fallback
