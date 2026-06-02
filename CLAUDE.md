# Ateles — Claude Code Project Instructions

## Plan and task maintenance (automatic)

The Ateles implementation plan lives in Neotoma as entity `ent_99ace4dd6673aa36ed08b1fe`.

**Apply these rules on every turn, without being asked:**

- **When a todo item is completed** — immediately correct its `status` to `"done"` in the plan's `todos` field via `mcp__mcpsrv_neotoma__correct`. Include relevant entity IDs, file paths, or PR numbers in a `"notes"` field.
- **When a decision is settled** — immediately add or correct the relevant entry in the plan's `decisions` map. One sentence, snake_case key.
- **When blockers change** (something unblocked, something newly blocked) — correct `next_steps` to reflect the current state.
- **When a new actionable task is identified** — create a `task` entity in Neotoma and link it `PART_OF` the plan. Use the `update-tasks` skill for guidance on field values and priority mapping.
- **When a daemon, entity, or file is renamed** — correct any stale references in `body`, `decisions`, and `todos` in the same turn the rename happens.

Do not wait until end of session. Apply corrections in the same turn as the work, after the work completes.

Use `mcp__mcpsrv_neotoma__correct` with idempotency keys in the form `update-plan-<field>-<YYYY-MM-DD>`. Use Neotoma prod (`mcp__mcpsrv_neotoma__*`) always.

For full step-by-step guidance: `/update-plan` and `/update-tasks` skills.

---

## Agent routing (automatic)

Before tasking any work to an agent, always look up the right agent for the domain:

1. **Retrieve all `agent_definition` entities** via `mcp__mcpsrv_neotoma__retrieve_entities` with `entity_type: agent_definition`.
2. **Match by `description` field** — descriptions are the authoritative statement of each agent's domain and job scope.
3. **Prefer the most specific match** — if one agent explicitly owns the domain (e.g. "owns platform-adapted social content"), that agent wins over a general-purpose one.
4. **Never assume from name alone** — agent names are bird genera; the description is ground truth.
5. **If no agent matches** — flag the gap, propose adding the domain to an existing agent's definition or creating a new one, and create the task anyway with `assigned_to` noting the gap.

**Quick reference — current agent roster** (descriptions abbreviated; always verify against live Neotoma snapshot):

| Agent | Tier | Domain |
|---|---|---|
| Apis | T3 daemon | Task dispatch / routing |
| Turdus | T3 daemon | Email triage |
| Anthus | T3 daemon | Swarm coordination |
| Tyto | T3 daemon | Screenshot watch |
| Apus | T3 daemon | Neotoma → git mirror |
| Cotinga | T3 daemon | Daily calendar briefing |
| Monedula | T3 daemon | Payment execution |
| Sylvia | T3 daemon | Recurring task lifecycle |
| Formica | T3 daemon | GitHub issue/PR automation (ateles) |
| Neotoma-agent | T3 daemon | GitHub issue/PR automation (neotoma) |
| Gryllus | T4 worker | GitHub issue implementation + PRs |
| Vanellus | T4 worker | PR review + merge |
| Lanius | T4 worker | GitHub workflow gating |
| Struthio | T4 worker | Release execution |
| Luscinia | T4 worker | Session compliance audit |
| Bombycilla | T4 worker | Technical architecture + schema design |
| Phoenicurus | T4 worker | QA + test coverage |
| Regulus | T4 worker | Developer relations + docs |
| Accipiter | T4 worker | UX + product design |
| Buteo | T4 worker | Legal + compliance |
| Pavo | T4 worker | Product management |
| Corvus | T4 worker | **Social content + long-form writing** |
| Paradisaea | T4 worker | Copy + positioning |
| Ciconia | T4 worker | Marketing + GTM strategy |
| Aythya | T4 worker | Visual + brand design |
| Hirundo | T4 worker | Customer intelligence |
| Fringilla | T4 worker | Financial analysis + quarterly review |
| Columba | T4 worker | Constitution / policy authority |
| Pavo | T4 worker | Product management |
| Menura | T2 public | Public-facing site agent (read-only) |
| Onychomys | T2 operator | Primary operator interface (OpenClaw) |

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

## Key entity IDs

| Entity | ID |
|---|---|
| Ateles plan | `ent_99ace4dd6673aa36ed08b1fe` |
| priority_rubric | `ent_29ca079940c1e996a8c782f2` |
| Apus webhook subscription | `ent_6ba1914462908f682f206b56` |
| update-plan skill | `ent_5d7f84290f290383e53d1a42` |
| update-tasks skill | `ent_c21f9fb84691f43f45e6cd55` |
| agent_definition: Apis | `ent_acdb65a8c5dccc1c5f6c7171` |
| agent_definition: Turdus | `ent_138a463654de2b1d46cec0db` |
| agent_definition: Anthus | `ent_887e8fd74d79eb63344df63e` |
| agent_definition: Tyto | `ent_affecbbecf52edb633c534f8` |
| agent_definition: Apus | `ent_692e8533840be7195240a1e4` |
| agent_definition: Cotinga | `ent_6c85e2a550580c88024da8f4` |
| agent_definition: Monedula | `ent_26e45f38f53798eb42961a69` |
| agent_definition: Sylvia (fka Strix) | `ent_1faed5788fcc0e5200bb0120` |
| agent_definition: Fringilla | `ent_a6e9d4d4d684a7f3603b1fe3` |
| agent_definition: Formica | `ent_d62f1df8784b7f4fcadc7d74` |
| agent_definition: Neotoma-agent | `ent_c5c8d28bd420ca094f9d5a48` |
| agent_definition: Gryllus | `ent_900b8c9589145fde47787fe5` |
| agent_definition: Vanellus | `ent_fedc0fbabef6ef203f8029c9` |
| agent_definition: Lanius | `ent_f9c2c573e7fba5bc8c3e58c3` |
| agent_definition: Struthio | `ent_7df43f2bd35df575abfaa920` |
| agent_definition: Luscinia | `ent_56c7f1f528c2d34a47862362` |
| agent_definition: Bombycilla | `ent_3425a79b4c39f08cdb0c62f8` |
| agent_definition: Phoenicurus | `ent_42843b65dd18fc39294e94a1` |
| agent_definition: Regulus | `ent_46f3385204e51cd91efd1ab3` |
| agent_definition: Accipiter | `ent_7079893d01e208cde15a4f52` |
| agent_definition: Buteo | `ent_6f90952eaf5d1eed51b9621c` |
| agent_definition: Pavo | `ent_bf712273fe3ea48a505c6e81` |
| agent_definition: Corvus | `ent_b95bf915804ac40bba674529` |
| agent_definition: Paradisaea | `ent_c842afe3e816aa2d762a6221` |
| agent_definition: Ciconia | `ent_f2f10ae2c6e4869327831d78` |
| agent_definition: Aythya | `ent_fe71134e46209c21cf413b9b` |
| agent_definition: Hirundo | `ent_ba1ea2886064d5365c1bc7bb` |
| agent_definition: Columba | `ent_949454e143e72df5bf833dfd` |
| agent_definition: Menura | `ent_78fa62a9d99fac9ba11fc687` |
| agent_definition: Onychomys | `ent_706f1432822b4a9d9d71c127` |
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
