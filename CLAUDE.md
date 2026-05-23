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

## Current phase blockers (Phase 2)

1. Configure 4 Neotoma mirror profiles — `ent_55649174d7dd2951fb90bf6d`
2. Create `ateles-agent` GitHub machine account — `ent_1765472ab3536042959f63df`
3. Create `neotoma-agent` GitHub account — `ent_1273cd3a4d06a34364fb517d`
4. OpenClaw `workspace-neotoma.ts` PR — `ent_5d6202c08d794ac1b7798b20`
5. Anthus daemon skeleton — `ent_8a18a26009bb932f0feb411e`
6. Tyto daemon skeleton — `ent_2482ad3ee9ec3c69d911c4fa`
