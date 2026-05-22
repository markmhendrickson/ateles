# Ateles Agent Taxonomy

*Generated from Neotoma plan `ent_99ace4dd6673aa36ed08b1fe`. Do not edit manually.*

## Purpose

Canonical reference for every agent and daemon in the Ateles swarm. Defines tier assignments, genus names, AAuth identities, and implementation status for all T1–T4 components.

## Scope

Covers all Ateles agents: T1 hosts, T2 resident agents, T3 daemons, and T4 invocable agents. Naming conventions and AAuth identity table are also documented here. Implementation roadmap lives in `phases.md`.

---

## Tier structure

| Tier | Role | Description |
|---|---|---|
| **T1** | Hosts | Long-running processes that host or spawn agents |
| **T2** | Resident agents | Always-on agents with persistent identity and state |
| **T3** | Daemons | Event-driven background processes, one concern each |
| **T4** | Invocable agents | Stateless, spawned per task, return structured output |

---

## T1 — Hosts

| Name | Genus | Description | Status |
|---|---|---|---|
| OpenClaw | — | Claude Code host for Onychomys and Menura resident agents | active |
| launchd | — | macOS system daemon host for all T3 daemons | active |

---

## T2 — Resident agents

| Name | Genus | Description | Status |
|---|---|---|---|
| **Onychomys** | *Onychomys* (grasshopper mouse) | Primary operator interface; runs in OpenClaw; OnychomysBot on Telegram | active |
| **Menura** | *Menura* (lyrebird) | Public-facing personal representative at markmhendrickson.com/agent/; read-only public-scoped AAuth identity | planned (Phase 3) |

---

## T3 — Daemons

| Name | Genus | Language | Description | Status |
|---|---|---|---|---|
| **Monedula** | *Corvus monedula* (jackdaw) | Python | Recurring payment daemon; Wise IBAN + BTC transfers triggered by calendar events and Neotoma task due dates | active |
| **Formica** | *Formica* (wood ant) | JS → Python (Phase 5) | GitHub issue/PR automation; drives `process_issues` and `process_prs` skills | active |
| **neotoma-agent** | *Castor* (beaver) | Python | Neotoma-repo automation; processes issues and PRs against the neotoma repo | active (Phase 1 skeleton) |
| **Anthus** | *Anthus* (pipit) | Python | Metrics and analytics ingestion (Umami, etc.) | planned (Phase 2) |
| **Apis** | *Apis* (honeybee) | Python | General task processor; absorbs Monedula's task scope; yoga/therapy tasks become Apis templates | planned (Phase 1) |
| **Apus** | *Apus* (swift) | Python | Lightweight HTTPS webhook receiver; listens for Neotoma webhooks; triggers mirror rebuilds and git commits | planned (Phase 2) |
| **Piculet** | *Picumnus* (piculet woodpecker) | JS | Audio transcription daemon; monitors for new audio files, transcribes, stores in Neotoma | active |
| **Strix** | *Strix* (wood owl) | Python | Meeting/ambient audio recorder | active |
| **Tyto** | *Tyto* (barn owl) | Python | Email triage daemon | planned (Phase 2) |
| **Turdus** | *Turdus* (thrush) | Python | Social media / content scheduling daemon | planned (Phase 3) |

---

## T4 — Invocable agents

| Name | Genus | Description | Status |
|---|---|---|---|
| **Loxia** | *Loxia* (crossbill) | Reviews external PRs against ateles mirror files; auto-approves trivial PRs | planned (Phase 3 eval — may be replaced by GHA + Claude API) |
| Skill-based agents | — | Stateless agents invoked via skills (e.g. `process_issues`, `process_prs`, `import_audio`) | active |

---

## Naming convention

All persistent agents and daemons are named after bird genera. Names are chosen for:
- Mnemonic fit to the agent's function (e.g. Monedula = jackdaw = *moneta* = money)
- Distinctiveness within the swarm
- Public shareability (no private associations)

T4 invocable agents may use genus names or remain as unnamed skill invocations depending on reuse frequency.

---

## AAuth identities

Each T2 and T3 agent has a distinct AAuth keypair with `sub = <name>@ateles-swarm`. Capabilities are enforced at the Neotoma data layer via `AgentGrant`. GitHub automation uses the `ateles-agent` and `neotoma-agent` identities.

| Agent | AAuth sub | Scope |
|---|---|---|
| Onychomys | `onychomys@ateles-swarm` | full operator scope |
| Menura | `menura@ateles-swarm` | read-only, `visibility=public` entities only |
| Monedula | `monedula@ateles-swarm` | task + transaction write |
| Formica | `formica@ateles-swarm` | issue + PR write |
| neotoma-agent | `neotoma-agent@ateles-swarm` | neotoma-repo issue + PR write |
| Apus | `apus@ateles-swarm` | mirror trigger + event write |
