# Ateles Architecture

*Generated from Neotoma plan `ent_99ace4dd6673aa36ed08b1fe`. Do not edit manually.*

## Purpose

Documents the system design of the Ateles agent swarm: how agents are structured, how they integrate with Neotoma, and the key build-vs-adopt decisions that shape the architecture.

## Scope

Covers system layers, Neotoma integration (agent_definition entities, AAuth identity, webhook+mirror, notification routing), the daemon pattern, the payment profile pattern, and the full repo topology. Does not cover individual agent implementation details — see `taxonomy.md` for agent inventory and phase-specific docs for implementation plans.

---

## Overview

Ateles is a Neotoma-canonical personal agent swarm. "Neotoma-canonical" means:

1. **Agent definitions live in Neotoma**, not in code. Each agent is an `agent_definition` entity with `prompt_markdown`, `tool_allowlist`, and `agent_grant` fields. Updating an agent's prompt is a `correct()` call — no commit, no redeploy, full version history with author attribution.

2. **Every agent action is an attributed observation.** Neotoma's observation provenance model records which agent wrote which field, when, and from which source. "Why did this happen?" always has a traceable answer.

3. **Code is runtime mechanics.** The daemon code in this repo handles scheduling, event subscription, and API calls. The *intelligence* — what agents do, what they're allowed to do, how they behave — lives in Neotoma.

---

## System layers

```
┌─────────────────────────────────────────────────────┐
│  T1 Hosts: OpenClaw (resident agents), launchd (daemons) │
├─────────────────────────────────────────────────────┤
│  T2 Resident: Onychomys (operator), Menura (public)       │
├─────────────────────────────────────────────────────┤
│  T3 Daemons: Monedula / Formica / neotoma-agent / Apis / Apus │
│              Piculet / Strix / Anthus / Tyto / Turdus     │
├─────────────────────────────────────────────────────┤
│  T4 Invocable: Gryllus / Vanellus / Lanius / Cathartes / 20+ domain agents │
├─────────────────────────────────────────────────────┤
│  Shared libs: lib/notify/ · lib/daemon_runtime/           │
├─────────────────────────────────────────────────────┤
│  Neotoma (canonical store + event system + identity)      │
└─────────────────────────────────────────────────────┘
```

### T1 is a role, not a product

T1 describes any process that owns a channel message loop and spawns T2 residents per session. The implementation is deliberately open:

| Implementation | When to use |
|---|---|
| **OpenClaw** | Multi-channel setups (Telegram + WhatsApp + web); mature plugin/harness lifecycle; right choice when channel breadth matters |
| **Claude Agent SDK (custom)** | Single-channel or bespoke needs; full control with minimal surface area; T3 daemons like Monedula are already close to this pattern |
| **NanoClaw / equivalent** | Lightweight self-hosted variant; same role as OpenClaw with less overhead |
| **Raw webhook loop** | `aiohttp` + Bot API directly; valid when you don't need a framework; low ceremony, easy to audit |
| **Claude Code / Cursor** | IDE-driven T1 for development workflows; not suitable for always-on daemon swarms |

The current Ateles T1 for Onychomys is OpenClaw. For a single-channel operator setup, a thin custom loop using the Agent SDK is equally valid and has less surface area to maintain.

---

## Neotoma integration

### agent_definition entities
Each agent's configuration is a Neotoma `agent_definition` entity:
- `prompt_markdown` — the system prompt
- `tool_allowlist` — permitted tools
- `agent_grant` — capability tier (operator / service / public_read)
- `override_policy` — per-field rules for `agent_definition_override` entities

Daemons load their own `agent_definition` at spawn time via the Neotoma API. No config files.

### AAuth identity
Each daemon has a per-role AAuth keypair (`sub = <name>@ateles-swarm`). All Neotoma observations carry agent attribution. Capabilities are enforced at the data layer via `AgentGrant` — Menura cannot write private entities regardless of what code it runs.

### Webhook + mirror system
Neotoma sends HMAC-signed webhooks to Apus (`apus.markmhendrickson.com`) on entity changes. Apus triggers mirror profile rebuilds and commits to the public `ateles` repo via `ateles-agent` GitHub identity.

**Mirror is one-way: Neotoma → disk only.** Mirrored files are a read surface (IDE, git, Inspector). Human edits to mirrored files are overwritten on the next mirror run. To write back from disk: `neotoma edit <entity-id>` or `neotoma corrections create <entity-id> --field-name <field> --corrected-value "$(cat file.md)"`. The mirror profile has no automatic write-back option.

### Notification routing
All agent notifications flow through `lib/notify/` which reads a `priority_rubric` entity from Neotoma at startup. Routing rules (silence windows, digest collapse, escalation ladder) are Neotoma config — not hardcoded. Delivery via [Apprise](https://github.com/caronc/apprise) (Telegram-primary).

---

## Daemon pattern

Each T3 daemon follows the same pattern:

```
startup:
  1. load env from ~/.config/neotoma/.env
  2. load agent_definition from Neotoma API
  3. load priority_rubric from Neotoma API (via lib/notify/)
  4. subscribe to relevant Neotoma entity types via SSE (lib/daemon_runtime/)

event loop:
  on event → spawn T4 invocable agent or execute skill
  on schedule → run periodic task (digest, cleanup, etc.)
  on result → store observation in Neotoma with AAuth attribution
  on error → route via lib/notify/ per priority_rubric
```

---

## Payment profile pattern (Monedula)

Monedula is a recurring payment daemon. All payment-specific configuration — recipient, amount, reference, calendar keywords — is loaded from env vars via `PaymentProfile` objects. No payment details are hardcoded.

Adding a new recurring payment requires zero code changes: add a new profile prefix to `MONEDULA_PROFILES` and set the corresponding env vars (or a Neotoma `payment_profile` entity in Phase 1+).

In Phase 4, payment profiles will migrate to Neotoma `payment_profile` entities (visibility=private), making them queryable and versioned alongside all other agent configuration.

---

## Swarm governance

`execution_policy` and `checkpoint_brief` entity types define how any plan can be executed with calibrated autonomy:

- **execution_policy**: per-plan permission scope, quality criteria, blocking checkpoints, fallback instructions, and autonomy level (full-auto → checkpoint → human-approval → operator-only)
- **checkpoint_brief**: structured artifact an agent submits to pause at a defined gate before proceeding

This replaces the binary swarm/human split. Each plan carries its own autonomy calibration. Seven policies are seeded for active work streams (see `CLAUDE.md` key entity IDs).

The **escalation chain**: agent → domain-expert agent → Columba (constitution keeper) → operator via Onychomys. Each escalation resolution is codified as a Neotoma entity for future instances.

The **three-layer constitution**: (1) `core_principles` entity (project-wide); (2) per-repo constitution owned by Columba; (3) per-agent operating principles in `agent_definition`.

---

## MCP harness model

T4 invocable agents connect to capabilities via MCP servers, not local filesystem worktrees:

- **`github_harness`** MCP server: issue/PR read-write, branch management (planned Phase 5)
- **`code_harness`** MCP server: file read, apply patches, run tests (planned Phase 5)

Neotoma is the config plane (agent_definition, execution_policy, checkpoint_brief). MCP is the capability plane. Worktrees are temporary scaffolding until the harness servers are live.

---

## Build vs. adopt decisions

| Layer | Decision |
|---|---|
| T1 host | OpenClaw (current, multi-channel); custom Agent SDK loop valid for single-channel |
| Agent orchestration | Claude Agent SDK (native); Temporal evaluated at Phase 3 |
| Notification delivery | Apprise Python lib — Telegram-native, zero infra |
| Daemon scheduling | launchd (macOS); Temporal eval at Phase 3 |
| Prompt versioning | Neotoma agent_definition (provenance-native) |
| Identity + capabilities | AAuth + AgentGrant (Neotoma-native) |
| Repo sync | Apus webhook daemon; GHA fallback |
| LangChain / LangGraph | Not used — abstractions lose Neotoma attribution |
| Knock / Courier | Rejected — SaaS, no Telegram, wrong scale |

---

## Repo topology

| Repo | Visibility | Contents |
|---|---|---|
| `markmhendrickson/ateles` | public | Agent code, architecture docs, shared libs |
| `markmhendrickson/ateles-private` | private | Keys, secrets, personal config |
| `markmhendrickson/ateles-agents/<genus>` | public | Graduated per-agent repos with independent lifecycle |
| `markmhendrickson/neotoma` | public | Neotoma server (canonical store) |

---

## Why Neotoma instead of {LangSmith, PromptLayer, custom DB}?

Neotoma collapses 7 systems into 1:

| Without Neotoma | With Neotoma |
|---|---|
| Humanloop/PromptLayer for prompt versioning | `agent_definition` entity with correction history |
| Custom event log for agent actions | Observation provenance on every field write |
| Auth0 + custom IAM | AAuth keypairs + AgentGrant at data layer |
| Neo4j or custom graph | Cross-entity relationships native |
| LangSmith for tracing | Durable observation graph (not ephemeral traces) |
| Separate chat archive | Conversation + message entity types |
| Custom webhook infra | Native Neotoma webhook subscriptions |

The cost: data model discipline and API-write friction.
The benefit: a single unified provenance graph across prompts, actions, events, conversations, and identity.
