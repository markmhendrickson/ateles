# Ideal Customer Profile (ICP)

> **How this was derived.** This profile is reverse-engineered from a first-principles audit of what the
> code in this repository actually does — the 18 daemons under `execution/daemons/`, the 87 skills under
> `.claude/skills/`, the runtime substrate under `lib/daemon_runtime/`, the MCP servers under
> `execution/mcp/`, and the operational tooling under `scripts/`, `.github/`, and `deploy/`. It is **not**
> derived from the README, marketing copy, or any prior positioning doc. The question it answers is narrow:
> *given only what this software does, who is the one person it is built for?*

## Purpose

Define whom this repository is primarily for — derived solely from a functionality audit, not from prior
positioning — so the README, documentation, and roadmap can serve that operator deliberately.

## Scope

Covers the primary ideal operator, the codebase evidence that points to them, their jobs-to-be-done,
adjacent users who get value, and the explicit anti-profile. Out of scope: go-to-market tactics and pricing
(Ateles is a fork-and-adapt reference architecture, not a product).

---

## The one-line answer

**Ateles is for the solo technical founder-operator who wants to run their entire company *and* their
personal life through a single, governed, audited fleet of AI agents that they own and can fork.**

One person who is simultaneously the engineer, the CEO, the marketer, the finance department, and the
chief-of-staff of a small software business — and who has decided the way to scale a company of one is to
delegate both the company's operational work and their own life-admin to a swarm of attributed,
capability-scoped agents backed by a single source of truth.

This person is not buying a product. They are adopting a blueprint. The bullseye user must be willing and
able to read Python, provision their own identities and secrets, and adapt daemons to their own
infrastructure.

---

## Why the functionality points here

The capabilities in this repo only make sense as a coherent whole for *one specific kind of user*. The
breadth is the tell: no team builds this for a single shared workload, and no consumer ships this much
self-hosted machinery. Each functional cluster narrows the profile.

### 1. They run a software product business — and want agents to do real engineering

The repo contains a full software-delivery org expressed as agents and daemons:

| Function | Where it lives |
| --- | --- |
| Code implementation from issues | `cicada` skill; dispatched by `formica`/`neotoma-agent` daemons |
| PR review & stewardship | `vanellus`, `lanius` skills; `loxia_review.py` + `loxia-pr-review.yml` GHA |
| QA / release-readiness | `phoenicurus` skill; `phoenicurus-release` daemon |
| Autonomous releases | `struthio` skill; `create-release` / `publish` skills |
| Product management & architecture | `pavo`, `waxwing` skills |
| Issue/PR triage off webhooks | `formica`, `neotoma-agent` daemons; `apis` GitHub gateway |
| Dev-rel & docs quality | `regulus` skill |

→ The operator ships at least one real software product (this repo targets two: **Neotoma** and **Ateles**
itself) and wants the agent fleet to do attributable engineering work, not just chat.

### 2. They do their own go-to-market, content, and customer development

`corvus` (content/social voice), `manucode` (copy/positioning), `ciconia` (marketing strategy), `aythya`
+ `accipiter` (brand/UX design), `hirundo` + `analyze-neotoma-feedback` + `process-feedback` +
`interview-admin` (customer development), and `write` / `write-blog-post` / `social` / `deploy-website` /
`create-website` (publishing). Social scheduling runs through a Typefully MCP.

→ A solo founder doing build-in-public, developer relations, and GTM with no marketing hire.

### 3. They manage their own money — including crypto — and cross-border tax

`monedula` executes recurring payments in **both fiat (Wise) and Bitcoin**; `fringilla` and `run-scorecard`
do portfolio/liquidity analysis; `extract-amazon-order` and `quarterly-portfolio-review` handle expense and
review hygiene; `picus` prepares **multi-jurisdiction taxes** and `buteo` covers legal/GDPR.

→ A financially self-directed individual with crypto holdings and cross-border (US ↔ EU) tax exposure.

### 4. They live a quantified, calendar-driven, multilingual life

`gorilla` + `scrape-chatgpt-workout` (workout logging, progression analysis, inactivity nudges);
`cotinga` (05:00 meeting-prep briefings), `sylvia` + `remember-calendar` (recurring-task ↔ calendar sync),
`find-technician-slot` (scheduling around workouts); `strix` + `record_meeting` + `tyto` + `piculet`
(meeting recording and transcription with consent tracking); `turdus` + `email-triage*` + `riparia`
(email triage, drafting, reply routing); `language` (English / Spanish / Catalan, Barcelona usage).

→ Someone with a packed calendar of investor/partner/customer calls, high inbound email, a gym routine
worth tracking, and a life conducted in three languages from Barcelona.

### 5. They want everything to share one memory and one audit trail

Every daemon and skill reads and writes **Neotoma** as the canonical entity store; `tyto`/`piculet` capture
screenshots and audio into it; `sturnus` + `intake-relationship` maintain a CRM inside it. The runtime
substrate adds **AAuth-signed identity** (RFC 9421 HTTP signatures, `lib/daemon_runtime/aauth_httpsig.py`),
**capability grants** (`grant_checker.py` + `mcp_tool_grant_proxy`), an **observation log** of every tool
call, **session-integrity enforcement** (`.claude/hooks/`), and **execution gating** by confidence ×
blast-radius (`gating.py`).

→ This operator is not satisfied with convenience alone. They want to *trust* the fleet: to know which agent
did what, on whose authority, with what scope, built on which evidence — and to keep a human approval gate
on anything high-blast (a push, a merge, a payment, an outbound message).

### 6. They are local-first, own their keys, and will fork rather than install

Scheduling is `launchd` on macOS plus a `docker-compose` cloud option on a €4/mo ARM VPS over Tailscale;
secrets are SOPS+age snapshots decrypted offline; GitHub PATs and AAuth keypairs are the operator's own;
the config-sourcing linter (`check_hardcoded_config.py`) and the public/PII discipline exist precisely so
**any operator can fork and supply their own context entities.** There is no installer and no hosted tier.

→ Privacy- and sovereignty-minded, technically self-sufficient, and explicitly expected to adapt the code.

---

## Defining characteristics of the bullseye user

- **Scope:** A company of one (or near-one). One operator is the source of authority across every agent.
- **Technical depth:** Comfortable reading and modifying Python; can provision keypairs, PATs, env/SOPS
  secrets, launchd/Docker units, and MCP servers.
- **AI-native workflow:** Already lives in `claude` CLI / Claude Code + MCP; thinks in agents and prompts.
- **Governance-minded:** Wants attribution, capability-scoping, audit, and human checkpoints — not a
  free-running autopilot.
- **Memory-centric:** Wants a single canonical truth layer (Neotoma) instead of context scattered across
  a dozen SaaS tools.
- **Breadth of surface:** Runs code, content, finance, health, household, meetings, email, CRM, residency/
  tax, and customer-dev — and wants them coordinated, not siloed.
- **Build-in-public posture:** Public repo, PII-scrubbed prompts, open-source default.

## Jobs they are hiring Ateles to do

1. **Operate** — dispatch agents across products and life domains from one surface, without manual
   sequencing or re-explaining context each time.
2. **Govern** — keep every agent identifiable, in-scope, and auditable; get paged only on real escalations.
3. **Remember** — make one durable, queryable record of everything the swarm and the operator know.
4. **Extend** — add a new agent or workflow declaratively (an entity + a grant), not as bespoke glue code.
5. **Trust the dangerous parts** — let low-blast work run unattended while high-blast actions checkpoint
   for explicit approval.

---

## Adjacent users (real value, not the bullseye)

- **Agent-infrastructure engineers** evaluating *patterns* — AAuth attribution, capability grants, the
  observation log, workflow gating, session-integrity invariants — to borrow into their own systems. They
  read this repo as a worked reference, not as something they run wholesale.
- **AI/agent-ops researchers** studying long-running multi-agent orchestration (background daemons spawning
  `claude --print` subprocesses with distinct identities, coordinated over an SSE event stream) — a
  different regime from in-process LLM chains.
- **Aspiring company-of-one operators** who want this but need packaging first. Their path is gated on
  installability work (provisioning UX, config schema, multi-operator boundary) tracked in `ateles#18`.

## Who this is explicitly NOT for (anti-ICP)

- **Teams and multi-operator organizations.** The trust model assumes one operator behind every agent;
  multi-tenant separation is out of scope and would require multi-tenant Neotoma.
- **Anyone wanting zero-install / SaaS onboarding.** Ateles is a reference architecture you fork and wire
  up, not a product you sign up for.
- **Single-agent users.** If one assistant chat meets your needs, the entire governance and orchestration
  layer is pure overhead.
- **Non-technical users.** Forking daemons, minting keypairs, and managing SOPS/launchd are table stakes.
- **Hosted-agent providers** looking for a turnkey platform to resell.

---

## Implications for documentation and positioning

The functionality audit shows Ateles is **two systems in one**: a swarm *governance/runtime substrate* and a
working *personal-and-business operations suite*. The bullseye user needs both stories told in proportion —
the governance guarantees that make the fleet trustworthy, **and** the concrete day-to-day automation that
makes it worth running at all. Documentation should let a forking operator (a) evaluate the pattern quickly,
(b) stand up the substrate (Neotoma + identities + grants + one daemon), and (c) adopt or replace individual
agents and skills for their own life and company — in that order of priority.
