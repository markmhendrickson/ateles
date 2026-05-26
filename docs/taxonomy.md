# Ateles Agent Taxonomy

*Mirror of Neotoma plan `ent_99ace4dd6673aa36ed08b1fe`. Do not edit manually — update via Neotoma.*

## Purpose

Canonical reference for every agent and daemon in the Ateles swarm. Defines tier assignments, genus names, AAuth identities, and implementation status for all T1–T4 components.

## Scope

Covers all Ateles agents: T1 hosts, T2 resident agents, T3 daemons, and T4 invocable agents. Naming conventions and AAuth identity table are also documented here. Implementation roadmap lives in `phases.md`.

---

## Tier structure

| Tier | Role | Description |
|---|---|---|
| **T1** | Hosts | A role, not a product. Any process that owns a channel message loop and spawns T2 residents. Valid implementations: OpenClaw (multi-channel), custom Agent SDK loop (single-channel), raw aiohttp+Bot API (low-ceremony), NanoClaw (lightweight self-hosted). |
| **T2** | Resident agents | Always-on agents with persistent identity, persona, and conversational state |
| **T3** | Daemons | Event-driven background processes; no persona; subscribe to Neotoma SSE or external webhooks |
| **T4** | Invocable agents | Stateless, spawned per task, stable AAuth identity, Neotoma-backed memory via query |

---

## T1 — Hosts

| Name | Description | Status |
|---|---|---|
| OpenClaw | Multi-channel Claude Code host; current implementation for Onychomys | active |
| Custom Agent SDK loop | Single-channel bespoke host; valid alternative for low-surface-area setups | available |
| Raw aiohttp + Bot API | Low-ceremony webhook loop; valid when no framework is needed | available |
| launchd | macOS system daemon host for all T3 daemons | active |

---

## T2 — Resident agents

| Name | Genus | Description | Status |
|---|---|---|---|
| **Onychomys** | *Onychomys* (grasshopper mouse) | Primary operator interface; runs in OpenClaw; OnychomysBot on Telegram; sole pager | active |
| **Menura** | *Menura* (lyrebird) | Public-facing personal representative at markmhendrickson.com/agent/; read-only public-scoped AAuth identity; routes inbound interest to Onychomys | planned (Phase 3) |

---

## T3 — Daemons

| Name | Genus | Language | Description | Status |
|---|---|---|---|---|
| **Monedula** | *Corvus monedula* (jackdaw) | Python | Recurring payment daemon; Wise IBAN + BTC transfers triggered by calendar events and Neotoma task due dates | active |
| **Formica** | *Formica* (wood ant) | JS → Python (Phase 5) | GitHub issue/PR automation; drives `process_issues` and `process_prs` skills | active |
| **neotoma-agent** | *neotoma-agent* | Python | Neotoma-repo automation; processes issues and PRs against the neotoma repo | active (Phase 1 skeleton) |
| **Apus** | *Apus* (swift) | Python | HTTPS webhook receiver; Neotoma→git mirror pipeline; commits via `ateles-agent` GitHub identity | active |
| **Piculet** | *Picumnus* (piculet woodpecker) | JS | Audio transcription daemon; monitors for new audio files, transcribes, stores in Neotoma | active |
| **Strix** | *Strix* (wood owl) | Python | Meeting/ambient audio recorder (menu bar app) | active |
| **Apis** | *Apis* (honeybee) | Python | Universal task dispatcher; subscribes to task events; routes to right agent + harness; absorbs Monedula's task scope | planned (Phase 4) |
| **Anthus** | *Anthus* (pipit) | Python | Swarm coordinator — global view of work-in-flight; surfaces conflicts to Onychomys | planned (Phase 2 skeleton, Phase 6 full) |
| **Tyto** | *Tyto* (barn owl) | Python | Screenshot watcher; visual counterpart to Strix | planned (Phase 2 skeleton) |
| **Turdus** | *Turdus* (thrush) | Python | Email triage daemon; hourly Gmail poll → tasks for Apis | active (Phase 2 skeleton) |

---

## T4 — Invocable agents (operations)

| Name | Genus | Description | Status |
|---|---|---|---|
| **Gryllus** | *Gryllus* (field cricket) | Issue worker — fixes issues, opens PRs across repos via passed-in identity | planned (Phase 3) |
| **Vanellus** | *Vanellus* (lapwing) | PR steward — triages and merges eligible PRs | planned (Phase 5) |
| **Sturnus** | *Sturnus* (starling) | Feedback digester — extracts entities from `product_feedback` | planned (Phase 6) |
| **Strigops** | *Strigops* (kakapo) | Analytics gatherer — pluggable backends: Umami, GA4, GSC, X, Typefully, LinkedIn, Instagram | planned (Phase 7) |
| **Corvus** | *Corvus* (crow) | Outbound poster — sends drafts to social platforms after operator approval | planned |
| **Lanius** | *Lanius* (shrike) | GitHub workflow coordinator — GHA cron; full lifecycle: issues → workflow gates → PR creation → review chain → Struthio release trigger | active (GHA + schema; Phase 6 full) |
| **Anas** | *Anas* (duck) | Receipt extractor — Amazon, restaurant, hotel | planned |
| **Aquila** | *Aquila* (eagle) | Quarterly portfolio review | planned |
| **Falco** | *Falco* (falcon) | Deployment monitor — GitHub Actions polling | planned |
| **Tachornis** | *Tachornis* (swift) | Deploy watchdog — post-merge sanity, distinct from Falco | planned |
| **Pica** | *Pica* (magpie) | Disk cleanup | active (skill) |
| **Otus** | *Otus* (scops owl) | Integrity monitor + system-issue self-healer; weekly audit, override conflict diff | planned (Phase 6) |
| **Procyon** | *Procyon* (raccoon) | PR hygiene — stale branches, dependency drift, lint debt | planned |
| **Lutra** | *Lutra* (otter) | Claims, disputes, and external order tracking (Minted, etc.) | planned |
| **Lupinus** | *Lupinus* (lupine) | Tax + regulatory filings; Finance Google Sheet reconciliation | planned |
| **Salvia** | *Salvia* (sage) | Health-data agent — workout scraping, meal logs, biomarkers | planned |
| **Cathartes** | *Cathartes* (turkey vulture) | Meta-agent — writes new `agent_definition` entities in Neotoma from operator description | planned (Phase 9) |

---

## T4 — Invocable agents (ingestion)

| Name | Genus | Source | Method | Status |
|---|---|---|---|---|
| **Cygnus** | *Cygnus* (swan) | Google Calendar | `gws` CLI polling | planned (Phase 7) |
| **Aix** | *Aix* (wood duck) | Asana | Webhook + bidirectional sync | planned (Phase 7) |
| **Mergus** | *Mergus* (merganser) | Coinbase, Wise, Plaid, BTC, Stacks | API polling | planned (Phase 7) |
| **Geococcyx** | *Geococcyx* (roadrunner) | Twilio SMS | Webhook (existing) | planned (Phase 7) |
| **Tinamus** | *Tinamus* (tinamou) | HomeAssistant | Polling every 5 min | planned (Phase 7) |

---

## T4 — Product role panel

| Name | Genus | Role | Status |
|---|---|---|---|
| **Pavo** | *Pavo* (peacock) | Product manager — prioritisation synthesiser, tagging governance | planned (Phase 8) |
| **Paradisaea** | *Paradisaea* (bird-of-paradise) | Designer — UI/copy review, visual consistency | planned (Phase 8) |
| **Bombycilla** | *Bombycilla* (waxwing) | Technical architect — layered architecture, schema review | planned (Phase 8) |
| **Phoenicurus** | *Phoenicurus* (redstart) | QA — test coverage, regression, scorecard | planned (Phase 8) |
| **Accipiter** | *Accipiter* (hawk) | UX & product design — user flows, information architecture, interaction specs | planned (Phase 8) |
| **Mimus** | *Mimus* (mockingbird) | Growth / go-to-market — ICP research, launch announcements, distribution channels | planned (Phase 8) |
| **Hirundo** | *Hirundo* (swallow) | Customer intelligence — ICP synthesis, contact-level market signal aggregation, competitive analysis, inbound channel consolidation | planned (Phase 8) |
| **Aythya** | *Aythya* (diving duck) | Data analyst — metrics synthesis, cohort analysis, KPI tracking | planned (Phase 8) |
| **Buteo** | *Buteo* (buzzard) | Legal / compliance — risk assessment, T&C review | planned (Phase 8) |
| **Ciconia** | *Ciconia* (stork) | Finance advisor — budget, forecasting, financial modelling | planned (Phase 8) |
| **Columba** | *Columba* (dove) | Constitution keeper — cross-agent principles, escalation codification | planned (Phase 6) |
| **Luscinia** | *Luscinia* (nightingale) | Compliance supervisor — legal + regulatory, privacy, data governance | planned (Phase 8) |
| **Regulus** | *Regulus* (kinglet) | Developer relations — SDK docs, community, developer feedback (not end-user support) | planned (Phase 8) |
| **Struthio** | *Struthio* (ostrich) | Release manager — changelog, version tagging, release notes, Vanellus trigger | planned (Phase 6) |

---

## Naming convention

All persistent agents and daemons are named after bird (or plant) genera. Names are chosen for:
- Mnemonic fit to the agent's function (e.g. Monedula = jackdaw = *moneta* = money; Gryllus = cricket = small, fast, noisy)
- Distinctiveness within the swarm
- Public shareability (no private associations)

Plant genera (Lupinus, Salvia) are used for domain agents where no suitable bird fits. T4 invocable agents may also remain as unnamed skill invocations if reuse frequency doesn't warrant a stable identity.

---

## AAuth identities

Each T2 and T3 agent has a distinct AAuth keypair with `sub = <name>@ateles-swarm`. Capabilities are enforced at the Neotoma data layer via `AgentGrant`. `sub` is per-role, not per-repo — GitHub automation uses the separate `ateles-agent` and `neotoma-agent` GitHub identities.

| Agent | AAuth sub | Scope |
|---|---|---|
| Onychomys | `onychomys@ateles-swarm` | full operator scope |
| Menura | `menura@ateles-swarm` | read-only, `visibility=public` entities only |
| Monedula | `monedula@ateles-swarm` | task + transaction write |
| Formica | `formica@ateles-swarm` | issue + PR write |
| neotoma-agent | `neotoma-agent@ateles-swarm` | neotoma-repo issue + PR write |
| Apus | `apus@ateles-swarm` | mirror trigger + event write |
| Piculet | `piculet@ateles-swarm` | audio entity write |
| Strix | `strix@ateles-swarm` | audio capture write |
| Apis | `apis@ateles-swarm` | task read + payment_event write (planned) |
| Anthus | `anthus@ateles-swarm` | swarm metrics read (planned) |

---

## GitHub machine accounts

Ateles uses dedicated GitHub machine accounts for all automated pushes and PR authorship. These are separate from AAuth identities — they are GitHub OAuth identities used to write commits and interact with GitHub APIs, not Neotoma credentials.

| Account | Used by | Purpose | Status |
|---|---|---|---|
| `ateles-agent` | Apus, Formica, Gryllus | Commits mirrored from Neotoma→git; automated PRs and issue comments against the ateles repo | pending creation |
| `neotoma-agent` | neotoma-agent daemon | Automated PRs and issue comments against the neotoma repo | pending creation |

### Setup checklist

For each account:
1. Create a GitHub account with the username above
2. Generate a personal access token (PAT) with `repo` and `workflow` scopes
3. Store the PAT in `ateles-private/.env` as `ATELES_AGENT_PAT` / `NEOTOMA_AGENT_PAT`
4. Add the account as a collaborator with write access to the relevant repo
5. Set the git identity in the daemon's launchagent env (`ATELES_AGENT_GIT_NAME`, `ATELES_AGENT_GIT_EMAIL`)

Both accounts use `noreply@users.noreply.github.com`-style emails (e.g. `ateles-agent@users.noreply.github.com`) to keep commits attributable without exposing personal email addresses.
