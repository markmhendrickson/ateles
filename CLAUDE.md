# Ateles — Claude Code Project Instructions

## Plan and task maintenance (automatic)

Each session maintains the Neotoma `plan` entity that matches **its own workstream** — never a fixed, hardcoded plan. The swarm-architecture plan `ent_99ace4dd6673aa36ed08b1fe` ("Ateles Agent Swarm Architecture") is the plan for swarm-architecture work **only**. Unrelated workstreams (tax prep, Neotoma release engineering, website, cloud hosting, etc.) each have their own plan and MUST NOT write into the swarm plan. Writing one workstream's `decisions`/`todos` into another's plan is the collision that corrupted this plan in June 2026.

**Select the bound plan once per session, as soon as the workstream is clear:**
1. Resolve the matching plan: `mcp__mcpsrv_neotoma__retrieve_entities` with `entity_type: plan` and a `search` for the workstream; pick the closest match.
2. If no existing plan fits, create one (`/update-plan` skill) and bind to it.
3. Maintain only that bound plan for the rest of the session.

**Apply these rules on every turn, without being asked — to the bound plan:**

- **Before correcting `decisions` or `todos`, RE-READ the current field and MERGE.** `correct` replaces the *entire* field, so add or update only the keys/items you authored and preserve every entry already present. NEVER rebuild a field from a stale in-memory copy — that silently deletes other sessions' entries. If the field changed since you last read it, re-read and re-merge before writing.
- **When a todo item is completed** — correct its `status` to `"done"` in `todos`, with relevant entity IDs, file paths, or PR numbers in a `notes` field.
- **Never mark a task or todo `done` while citing a commit, branch, file, or PR that does not resolve.** Verify the artifact exists first (git `cat-file`/`ls-remote`, GitHub). Unverifiable completion claims are the second failure mode that corrupted this plan in June 2026.
- **When a decision is settled** — add or correct one entry in the `decisions` map. One sentence, snake_case key.
- **When blockers change** (something unblocked, something newly blocked) — correct `next_steps` to reflect the current state.
- **When a new actionable task is identified** — create a `task` entity and link it `PART_OF` the bound plan. Use the `update-tasks` skill for field values and priority mapping.
- **When a daemon, entity, or file is renamed** — correct any stale references in `body`, `decisions`, and `todos` in the same turn the rename happens.

Do not wait until end of session. Apply corrections in the same turn as the work, after the work completes.

Use `mcp__mcpsrv_neotoma__correct` with idempotency keys in the form `update-plan-<field>-<YYYY-MM-DD>`. Use Neotoma prod (`mcp__mcpsrv_neotoma__*`) always.

For full step-by-step guidance: `/update-plan` and `/update-tasks` skills.

## Session-integrity hooks (mechanical enforcement)

`.claude/settings.json` wires three Claude Code lifecycle hooks (in `.claude/hooks/`) that mechanically enforce the plan-and-task contract above. They implement layer 1 of `docs/session_integrity.md`:

- **`session_start.py`** (SessionStart) — initializes per-session state, binds to the default plan (`ent_99ace4dd6673aa36ed08b1fe`), and injects a one-line reminder of the bind/turn/artifact contract. Always exits 0.
- **`user_prompt_submit.py`** (UserPromptSubmit) — lightweight per-turn counter. Exits 0.
- **`stop_finalizer.py`** (Stop) — the enforcement gate. Scans the transcript; classifies the session as **exempt** (no domain writes — grace path), **integral** (domain writes + a plan link + stored turns), or **violated** (domain writes but no plan link or zero turns). Emits a `harness_event` audit row each time.

All hooks are **fail-open** (stdlib-only Python; any error or missing `NEOTOMA_BEARER_TOKEN` → exit 0, never crash a session). Plan binding is judged from the **transcript** (an actual plan touch/link), not the SessionStart default intent.

**Rollout posture:** defaults to **WARN** (logs the violation, exits 0). Set `ATELES_SESSION_INTEGRITY_ENFORCE=1` to switch the Stop hook to **BLOCK** (exit 2 + `{"decision":"block"}`), preventing a clean stop until the session binds a plan and stores its turns. Per-session state lives in `.claude/.session_state/` (gitignored).

---

## Standing constraints

- **Plan-mirrored docs are render targets, not source files.** `docs/taxonomy.md`, `docs/phases.md`, and `docs/architecture.md` mirror plan `ent_99ace4dd6673aa36ed08b1fe` fields (`taxonomy_markdown`, `phases_markdown`, `architecture_markdown`). Never edit these files directly: correct the plan field via `mcp__mcpsrv_neotoma__correct`, then run `python3 execution/scripts/render_plan_docs.py`; run `--check` before committing them. For an operator-approved local edit, `--push` writes the files back as plan corrections.
- **Agent prompts are always public and PII-free** (agent_policy `ent_c3c5e4a9350250cbf69e08bf`). `agent_definition.prompt_markdown` and its `.claude/skills/<name>/SKILL.md` mirror describe how an agent reasons and acts — never operator data. No payee names, IBANs, BTC addresses, contact names/emails, phone numbers, addresses, health facts, or financial figures in a prompt. Operator-specifics live in Neotoma entities (`payment_profile`, `contact`, `workout_session`, …) retrieved at runtime via the agent's `context_entity_types`. If a prompt can't be made public without leaking, move the data into an entity and reference it by type. There is one mirror flow (Neotoma → public ateles); no per-agent prompt `visibility` gating.
- **Agent prompts describe a role generically; specifics come from context entities** (agent_policy `ent_f2e21d651669c24183b2b4eb`). A prompt states what the agent *does* (role, method, protocol), not who it does it for. Operator/locale/vendor/swarm/tax peculiars are resolved at runtime from context entities, not inlined: identity from `operator_profile`, jurisdiction/timezone/currency/language from `locale_profile`, products/taglines from `product_profile`, sibling agents + AAuth subs from `swarm_roster` (by role, not hardcoded name), third-party tools from `vendor_binding` (capability slots), channels from `channel_config`, plus `tax_profile`/`tax_preparer`, `task_policy`, `constitution`, `payment_profile`, `brand_voice`, `calendar_routing_config`. Always give a missing-entity fallback (surface a blocker or degrade safely; never invent). Goal: any operator can fork and supply their own context entities. Renamed/removed agents leave no stale mirror — `render_agent_docs.py` prunes orphans and `--check` flags them.
- **Never hardcode secrets, IBANs, or contact details** — always read from env or parquet.
- **Operator-specific config is env/Neotoma-sourced, never baked into code.** Operator identity (name, email), calendar IDs, recipients, and entity IDs that vary per operator must be read from env (or parquet / Neotoma) at runtime so the swarm stays portable and operator-agnostic — not literals in daemon code. This is a *sourcing* rule distinct from the public-repo PII scan (`.gitleaks.toml` deliberately allowlists the operator's own identity). Enforced by `scripts/linters/check_hardcoded_config.py` (runs in `scripts/lint.sh`); suppress a reviewed env-default with `# config-source-ok: <reason>`.
- **Yoga payments: never include memo/OP_RETURN** — do not pass `memo` parameter.
- **Yoga/therapy tasks: never mark as completed** — only update `due_date`.
- **Always use Neotoma prod** (`mcp__mcpsrv_neotoma__*`), never the dev instance.
- **Google Calendar**: always use `gws` CLI with `Europe/Madrid` timezone.
- **Gmail**: always use `gws gmail ...` commands, not the Gmail MCP server.
- **Strip PII before filing issues** — scrub usernames, worktree names, platform names; use `visibility: private` for session-derived issues.

---

## People-data processing (RGPD legitimate-interest basis)

Neotoma's storage of third-party personal data (contacts, meeting transcripts, enrichment) for relationship management runs under **RGPD Art. 6(1)(f) legitimate interest**, NOT the household exemption — because the data drives professional action toward those people (CJEU *Lindqvist* / *Ryneš*: locally-held, unshared data still falls under the RGPD once it's used to act on people outside the household sphere). Apply these as standing discipline:

- **Minimize at capture.** When storing a person from a transcript or meeting, retain what serves the relationship (role, context, commitments, follow-ups). Do NOT persist incidental sensitive disclosures — health, finances, family situations, political/religious views (RGPD Art. 9 categories) — into durable contact profiles unless directly relevant to a stored task. Summarize, don't transcribe verbatim, when the detail is sensitive and incidental.
- **Purpose-bind.** Enrichment is for managing the operator's actual relationships. Do not build profiles on people with no relationship to the operator.
- **Honor objection.** If a person asks not to be tracked, or asks what's held, treat it as an Art. 21 objection / Art. 15 access request: stop enrichment on that entity and surface it to the operator. Never argue the person down.
- **No external publication of person-data** without the operator's explicit per-case approval (overlaps the PII-scrubbing rule for issues above).

This is the EU counterpart to the recording-disclosure guardrail in the `record_meeting` skill (US all-party-consent + Spain Art. 197). Recording calls the operator is **not** a party to is a hard refusal — it loses both the US one-party basis and the Art. 197 safe harbor.

---

## Key entity IDs

| Entity | ID |
|---|---|
| Ateles Agent Swarm Architecture plan (swarm work only — not a catch-all) | `ent_99ace4dd6673aa36ed08b1fe` |
| priority_rubric | `ent_29ca079940c1e996a8c782f2` |
| Apus webhook subscription | `ent_6ba1914462908f682f206b56` |
| update-plan skill | `ent_5d7f84290f290383e53d1a42` |
| update-tasks skill | `ent_c21f9fb84691f43f45e6cd55` |
| agent_definition: Apis | `ent_acdb65a8c5dccc1c5f6c7171` |
| agent_definition: Turdus | `ent_138a463654de2b1d46cec0db` |
| agent_definition: Anthus | `ent_887e8fd74d79eb63344df63e` |
| agent_definition: Tyto | `ent_affecbbecf52edb633c534f8` |
| agent_definition: Cicada | `ent_900b8c9589145fde47787fe5` |
| agent_definition: Vanellus | `ent_fedc0fbabef6ef203f8029c9` |
| agent_definition: Formica | `ent_d62f1df8784b7f4fcadc7d74` |
| Neotoma schema: payment_profile | `8f10fe72-2924-422c-b2ee-d537d9952576` |
| Neotoma schema: escalation | `c005dcb3-d9fb-4791-a154-fdb09ab9da12` |
| Neotoma schema: daemon_report | `a9ea8131-502f-44e7-87a6-8149bab7d55c` |
| Neotoma schema: harness_event | `689230f4-cd83-49b6-baa7-a752cf70629d` |
| Neotoma schema: execution_policy | `0e61f23f-b1bd-46a3-8824-9dde710db9e6` |
| Neotoma schema: checkpoint_brief | `b0bfcfab-1f07-4526-8fa5-d5ace343b004` |
| exec policy: Resolve #262 mirror bug | `ent_8b5f56d611bfa01b7efae973` |
| exec policy: Resolve #158 pull_request schema | `ent_76e195b7dc9b5f22432fd12c` |
| exec policy: #174/#175/#176 instructions batch | `ent_47061cdf3bf4609db806e495` |
| exec policy: FU-2026-05-004 Turn Summary widget | `ent_dd00928c59a2a73bff756325` |
| exec policy: CI security gates GHA | `ent_5002905df344d74b01de30a0` |
| exec policy: Influencer Research | `ent_3a4bbff3f1a0f17558756ec6` |
| exec policy: SEO/SERP Copy | `ent_7e32fd9ebec7907673363737` |

## Current phase blockers (Phase 5–6)

**GPG-blocked items resolved 2026-05-24** (operator pushed from Mac Studio with GPG key loaded):
- ✅ ateles: committed + pushed to origin/main
- ✅ neotoma feat/seed-pull-request-schema: branch pushed (PR pending `gh auth login`)
- ✅ neotoma fix/262-content-field-heading-entity-mode: branch pushed (PR pending)
- ✅ neotoma docs/cicada-174-175-176-instructions: branch pushed (PR pending)
- ✅ openclaw feat/neotoma-soul-override: committed (`c1e814610c`) + pushed (PR pending)
- ⚠️ openclaw main push rejected — local diverged from fork; needs `git pull --rebase origin main` or `--force-with-lease`

**Remaining manual operator steps:**
- Run `gh auth login` on Mac Studio, then open the 4 pending PRs (neotoma × 3, openclaw × 1)
- ✅ `ateles-agent` + `neotoma-agent` GitHub machine accounts created; PATs provisioned in the private env (see private notes) — unblocks Apus auto-mirror + Cicada PRs (verified 2026-06-11)
- Add `ANTHROPIC_API_KEY` secret to ateles repo settings — activates Loxia GHA
- Add `NEOTOMA_PROBE_HOSTS` secret to neotoma repo settings — activates CI security gates
- Configure neotoma main branch protection after CI gates PR merges
- Deploy separate OpenClaw instance for Menura

**Requires manual operator action**:
- Add `ANTHROPIC_API_KEY` secret to ateles GitHub repo (for Loxia GHA)
- Deploy separate OpenClaw instance for Menura

(✅ `ateles-agent` / `neotoma-agent` accounts + PATs done — see "Remaining manual operator steps" above; PAT→private-env wiring captured as `env_var_mapping` entities for `/sync-env-from-1password` (entity IDs in private notes).)

## Recently resolved

- **Secrets management — SOPS+age, snapshots in the PRIVATE `ateles-private` repo (Design B)** — 1Password Family stays canonical; values ride an age-encrypted snapshot (`ateles-private/secrets/*.sops.enc`) that daemons/CI/other machines decrypt **offline**, fixing the daemon `op read`-needs-live-session token-refresh bug. 1Password service accounts rejected (Business/Teams-only; Family suffices by storing just the age private key). **`ateles` is PUBLIC** — encrypted snapshots must NOT live here; this repo keeps only the no-secret tooling (`execution/scripts/secrets_{lib,publish,materialize}.py`, default `ATELES_SECRETS_DIR=~/repos/ateles-private`). Public repo originally held the snapshot (PR #142) → relocated to `ateles-private` (encryption held; age private key was never committed; `NEOTOMA_BEARER_TOKEN` rotation pending as precaution). age key at `~/.config/sops/age/keys.txt` + `op://Private/ateles-sops-age/key`; CI secret `SOPS_AGE_KEY` set; runbook `docs/secrets_management.md`. Same private-repo rule applies to neotoma (public product) + openclaw (public fork): their operator secrets materialize from `ateles-private`, never committed to those repos. Follow-up tasks PART_OF the plan: shared-checkout isolation (`ent_52d2317ce6ad181a8676c004`, high) and daemon redeploy label gap (`ent_22ed087bb7ef1b906cd4ad64`, med).
- **Issues sync runaway** — root cause: `ops.correct()` passed `{corrections:map}` but server expects `{field,value,idempotency_key}`; Zod rejected silently causing repeated corrections. Fix: use `ops.executeTool("correct", {field, value, idempotency_key})` in sync_issues_from_github.ts. 35 orphaned Neotoma issue entities corrected; 520+ duplicate GitHub issues closed; 30 unique open issues remain (#368–#416). Push leg disabled pending GPG commit.
- **Post-checkout hook in worktrees** — added `[ -d ".git" ]` guard before `touch .git/hooks/.hooks-installed` in scripts/git-hooks/post-checkout and .git/hooks/post-checkout to prevent failure in git worktrees.

## Swarm governance layer

`execution_policy` + `checkpoint_brief` schemas define how any plan can be swarm-executed with permission scopes, quality criteria, blocking checkpoints, and fallback instructions. Replaces binary swarm/human split with per-plan autonomy calibration. 7 execution_policy entities created (see Key entity IDs above).
