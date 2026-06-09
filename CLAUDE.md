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

---

## Standing constraints

- **Never hardcode secrets, IBANs, or contact details** — always read from env or parquet.
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
| agent_definition: Gryllus | `ent_900b8c9589145fde47787fe5` |
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
- ✅ neotoma docs/gryllus-174-175-176-instructions: branch pushed (PR pending)
- ✅ openclaw feat/neotoma-soul-override: committed (`c1e814610c`) + pushed (PR pending)
- ⚠️ openclaw main push rejected — local diverged from fork; needs `git pull --rebase origin main` or `--force-with-lease`

**Remaining manual operator steps:**
- Run `gh auth login` on Mac Studio, then open the 4 pending PRs (neotoma × 3, openclaw × 1)
- Create `ateles-agent` GitHub machine account + PAT in `ateles-private/.env` — unblocks Apus auto-mirror
- Create `neotoma-agent` GitHub machine account
- Add `ANTHROPIC_API_KEY` secret to ateles repo settings — activates Loxia GHA
- Add `NEOTOMA_PROBE_HOSTS` secret to neotoma repo settings — activates CI security gates
- Configure neotoma main branch protection after CI gates PR merges
- Deploy separate OpenClaw instance for Menura

**Requires manual operator action**:
- Create `ateles-agent` GitHub machine account + PAT in ateles-private/.env
- Create `neotoma-agent` GitHub account
- Add `ANTHROPIC_API_KEY` secret to ateles GitHub repo (for Loxia GHA)
- Deploy separate OpenClaw instance for Menura

## Recently resolved

- **Issues sync runaway** — root cause: `ops.correct()` passed `{corrections:map}` but server expects `{field,value,idempotency_key}`; Zod rejected silently causing repeated corrections. Fix: use `ops.executeTool("correct", {field, value, idempotency_key})` in sync_issues_from_github.ts. 35 orphaned Neotoma issue entities corrected; 520+ duplicate GitHub issues closed; 30 unique open issues remain (#368–#416). Push leg disabled pending GPG commit.
- **Post-checkout hook in worktrees** — added `[ -d ".git" ]` guard before `touch .git/hooks/.hooks-installed` in scripts/git-hooks/post-checkout and .git/hooks/post-checkout to prevent failure in git worktrees.

## Swarm governance layer

`execution_policy` + `checkpoint_brief` schemas define how any plan can be swarm-executed with permission scopes, quality criteria, blocking checkpoints, and fallback instructions. Replaces binary swarm/human split with per-plan autonomy calibration. 7 execution_policy entities created (see Key entity IDs above).
