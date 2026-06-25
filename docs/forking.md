# Forking & adoption guide

Ateles is a **reference architecture you fork**, not a package you install. There is intentionally no
operator-agnostic installer yet (that work is tracked on the issues board). This guide tells a new operator
what is **portable** versus **operator-specific**, and the minimum you must supply to stand up your own
swarm. It is the practical companion to [setup.md](setup.md); read [icp.md](icp.md) first to confirm the
pattern fits you.

> **The core idea.** Agent *prompts* and *daemon code* are generic and public. Everything specific to *you* —
> identity, jurisdiction, products, vendors, recipients, keys — lives in **Neotoma context entities** and
> **env/secrets**, resolved at runtime. Fork the code; supply your own context. Nothing operator-specific is
> baked into a prompt or a daemon.

## Purpose

Tell a new operator exactly what to keep, what to replace, and the minimum to supply to run their own swarm —
because Ateles is adopted by forking, not installing.

## Scope

Covers the portable vs. operator-specific split, the Neotoma context entities and env/secrets a fork must
supply, identity (AAuth) and grant provisioning, operator-personal content **not** to inherit, and a minimal
path to a first running daemon. Detailed install steps live in [setup.md](setup.md); secret mechanics in
[secrets_management.md](secrets_management.md).

---

## What's portable (keep as-is)

- **Daemon code** (`execution/daemons/`) — the orchestration logic is operator-agnostic; it reads identity,
  recipients, and config from env/Neotoma.
- **Runtime substrate** (`lib/daemon_runtime/`) — AAuth signing, SSE, grant checking, gating, readiness,
  task lifecycle, notification routing.
- **MCP servers** (`execution/mcp/`) — the harness and grant proxy.
- **Agent prompts** (`.claude/skills/`, `docs/agents/`) — generated from Neotoma `agent_definition`s and
  written to be generic. Per agent policy they are **public and PII-free**: a prompt states what an agent
  *does* (role, method, protocol), never who it does it for.
- **Operational tooling** (`scripts/`, `.github/`, `deploy/`) — linters, hooks, CI, cloud bootstrap.

## What's operator-specific (you must supply)

Operator specifics resolve at runtime from **context entities** in Neotoma and from **env/secrets** — never
from code. If an entity is missing, agents are designed to surface a blocker or degrade safely, not invent.

### 1. Context entities (in Neotoma)

These carry everything an agent needs to act *for you*. Create your own; reference them by type.

| Entity type | Supplies |
| --- | --- |
| `operator_profile` | Your identity (name, email, handles) |
| `locale_profile` | Jurisdiction, timezone, currency, language(s) |
| `product_profile` | Your products, taglines, positioning |
| `swarm_roster` | Which sibling agents exist (by role) + their AAuth subjects |
| `vendor_binding` | Third-party tools mapped to capability slots (payments, transcription, social, …) |
| `channel_config` | Your channels (Telegram chat/topic IDs, email addresses) |
| `payment_profile` | Recipients, rails, limits — never inlined in code |
| `tax_profile` / `tax_preparer` | Tax jurisdiction specifics |
| `priority_rubric` | When the swarm may page you vs. queue to a digest |
| `constitution` / `agent_policy` | Your founding principles and operating constraints |
| `brand_voice` / `calendar_routing_config` | Voice and calendar-routing rules |

An agent's `agent_definition.context_entity_types` lists which of these it pulls at spawn. To re-target an
agent to your world, you supply your own entities of those types — you do not edit the prompt.

### 2. Environment & secrets

Copy `.env.example` to `.env` and fill it in. Categories (see the file for the full list):

- **Neotoma**: `NEOTOMA_BEARER_TOKEN`, `NEOTOMA_BASE_URL`.
- **Operator identity**: `OPERATOR_NAME`, `OPERATOR_EMAIL`, your plan entity ID.
- **Channels**: `TELEGRAM_BOT_TOKEN`, chat/topic IDs, allowed user ID.
- **Vendors**: `WISE_API_TOKEN`, `MONEDULA_PROFILES`, calendar IDs, etc.
- **Agent definition IDs**: the Neotoma entity IDs of your daemons' definitions.
- **Mirror/cloud**: Apus port + webhook secret, repo paths, git author identity, `DATA_DIR`.

Secrets are sourced from 1Password and ride an **age-encrypted SOPS snapshot** in your *private* repo
(`ateles-private`), materialized offline by daemons/CI — never committed to the public repo. See
[secrets_management.md](secrets_management.md).

### 3. Identities (AAuth keypairs) & capability grants

- Mint an **AAuth keypair per agent** into `ateles-private/keys/<agent>.jwk.json`. Until a key is minted, the
  signer falls back to attributing actions to the operator token (documented Phase-1 behavior). See
  [aauth.md](aauth.md) and [aauth/keys.md](aauth/keys.md).
- File an **`agent_grant`** per agent declaring its capabilities — entity-op allowlist, MCP tool allowlist,
  repos, and parameter constraints (e.g. `max_amount_sats`). The grant proxy enforces these on every call.

### 4. Operator-personal content (do NOT inherit)

Some directories in this repo contain the *original operator's* personal or operator-specific material —
health plans, an outreach contact list, finance-dashboard diagnostics, home-automation notes. **Do not carry
these into your fork.** They are flagged for relocation in the
[documentation plan](documentation_plan.md#part-2--audit-of-the-current-documentation); your equivalents
belong in *your* `ateles-private`/Neotoma, never in a public repo. This also avoids inheriting third-party
personal data (an RGPD concern — see `CLAUDE.md`).

---

## Minimum path to a first running daemon

A deliberately small loop to prove the pattern before adopting the whole fleet:

1. **Stand up Neotoma** (see [neotoma quick start](https://github.com/markmhendrickson/neotoma#quick-start)).
2. **Create your context entities** — at minimum `operator_profile`, `locale_profile`, `channel_config`,
   `priority_rubric`.
3. **Fill `.env`** with Neotoma + Telegram credentials and your operator identity.
4. **Pick one self-contained daemon** with low blast radius — `gorilla` (fitness summaries, read-only) or
   `cotinga` (briefings) are good first choices; both mainly *read* and *notify*.
5. **Run it in the foreground**: `.venv/bin/python3 execution/daemons/gorilla/gorilla.py`. Confirm it reads
   Neotoma and routes a notification to your Telegram.
6. **Add an AAuth keypair + `agent_grant`** for that daemon and confirm the signed path and grant pre-check.
7. **Schedule it** under launchd (macOS) or `docker-compose` (cloud — see [cloud_hosting.md](cloud_hosting.md)).

From there, adopt additional daemons and agents one at a time, supplying the context entities each one's
`context_entity_types` requires.

---

## Guardrails that keep a fork portable

These are enforced so your fork (and the upstream) stay operator-agnostic:

- **`check_hardcoded_config.py`** (in `scripts/lint.sh`) fails the build if operator config (emails, calendar
  IDs, IBANs, BTC addresses) is hardcoded instead of env/Neotoma-sourced. Suppress a reviewed env-default
  with `# config-source-ok: <reason>`.
- **Structural PII gitleaks** (`.gitleaks.toml`) blocks accidental personal data in commits.
- **Agent prompts are public + PII-free** by policy; operator specifics live in context entities only.
- **`render_agent_docs.py --check`** prunes orphaned agent mirrors so renames leave no stale references.

The result: any operator can fork this repo, supply their own context entities and secrets, and run the same
swarm for their own company and life — which is the whole point.
