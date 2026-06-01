# Formica — Neotoma issue-processing daemon (Phase 4)

**Formica** is the execution-layer codename (genus *Formica*, ants) for this package. It is a strategy-layer daemon in **ateles** that subscribes to Neotoma substrate events (`issue`, optional `product_feedback`), resolves a git base from reporter provenance, creates worktrees, runs an agent mode (`oneshot` / `conversational.sdk` / `conversational.claude_api` / `human_handoff`), optionally opens PRs via `gh`, and writes `daemon_session` + issue observations back to Neotoma. Optional **Telegram** operator transport handles `human_needed` handoffs and `/shipit` to resume when `auto_fix` is false.

Canonical spec: Neotoma repo `docs/private/strategy/nervous_system_plans/04_issue_processing_daemon.md`.

## Further documentation

| Doc | Contents |
|-----|----------|
| [docs/configuration.md](docs/configuration.md) | Full YAML blocks, env vars, Neotoma entity touchpoints. |
| [docs/architecture.md](docs/architecture.md) | Data flow, module map, concurrency, spec pointer. |
| [docs/operations.md](docs/operations.md) | Runbook, Telegram mirror, troubleshooting, kill switch. |
| [../../../docs/developer/formica_issue_processing_rules.md](../../../docs/developer/formica_issue_processing_rules.md) | Repo rule: when the target worktree exposes Neotoma's `/process-issues` skill, Formica should direct the spawned agent to use it for the current issue. |

**macOS prod (launchd):** `bash execution/scripts/install_formica_launchd_from_ateles_env.sh` (verifies repo **`.env`**, installs agent). Runtime loads **all** vars from **`$ATELES_ROOT/.env`** via **`load_ateles_repo_env.sh`** — see [docs/operations.md](docs/operations.md#macos-launchagent-prod-neotoma-same-host-as-tunnel).

## Prerequisites

- Neotoma HTTP API with **subscriptions** and **SSE** (`GET /events/stream`).
- Bearer token (`neotoma auth mcp-token` or server `NEOTOMA_BEARER_TOKEN`).
- **git** and **gh** on `PATH` for PR automation; optional **cursor-agent** / **claude** for agent runs.
- `config.yaml` **`repos`** entries with real `path` + `worktree_base` for each `issue.repository` value you expect.

## Quick start

```bash
cd execution/daemons/formica
# Put NEOTOMA_BEARER_TOKEN, NEOTOMA_HOST_URL / NEOTOMA_BASE_URL, CURSOR_CLOUD_API_KEY, etc.
# in the ateles repo root .env — start.sh loads that file automatically.
npm install
npm start
```

From repo root: `npm run formica` (alias: `npm run issue-processor`).

Stop: `Ctrl+C`, or `./stop.sh` (uses `.run/daemon.pid`).

### Environment

**`start.sh`** sources **`load_ateles_repo_env.sh`**, which loads **`$ATELES_ROOT/.env`** (repo root, three levels above this package). Define Neotoma, Cursor, Anthropic, OpenAI, Telegram, and any other keys there; Formica sees the same exports as other ateles tooling.

### Legacy names

The package previously lived at `execution/daemons/issue_processor` with `ISSUE_PROCESSOR_*` / `NEOTOMA_ISSUE_PROCESSOR_SUBSCRIPTION_ID` env vars and `[issue-processor]` log lines. Those paths and variables still work where documented as **legacy**; prefer **`FORMICA_*`** and **`NEOTOMA_FORMICA_SUBSCRIPTION_ID`**.

## Configuration highlights

| YAML block | Purpose |
|------------|---------|
| `subscription` | `entity_types`, `event_types`, `delivery_method` (`sse` or `webhook`) |
| `processing` | `dry_run`, `auto_classify`, `auto_fix`, `rebase_policy`, `agent_mode`, `agent_runtime`, `dirty_tree_policy`, `align_pr_to_main`, `max_prs_per_hour` |
| `repos.<key>` | `path` (main clone), `worktree_base` (per-issue worktrees) |
| `operator_transport` | `backend: none \| telegram`, `${TELEGRAM_BOT_TOKEN}`, `telegram_chat_id`, `telegram_allowed_user_ids`, `use_message_threads`, `telegram_message_thread_id` |

Kill switch: create a Neotoma entity typed `daemon_config` with `active: false` to pause processing while keeping SSE connected.

## Tests

```bash
npm test
```

## Implementation map (this package)

| Spec module | File |
|--------------|------|
| SSE + subscription | `src/daemon.mjs`, `src/neotoma.mjs` |
| Classifier | `src/classifier.mjs` |
| Base resolver | `src/base_resolver.mjs` |
| Worktrees + patch | `src/worktree_manager.mjs` |
| Agent runner | `src/agent_runner.mjs` |
| PR preflight / push / `gh` | `src/pr_manager.mjs` |
| Operator queue | `src/operator_queue.mjs` |
| Telegram transport | `src/operator_transport.mjs` |
| Neotoma writes | `src/state_updater.mjs` |
| Orchestration | `src/pipeline.mjs` |
| Kill switch + rate limit | `src/kill_switch.mjs`, `src/rate_limit.mjs` |
| Cursor SDK conversational | `src/cursor_sdk_runner.mjs` |
| Anthropic Messages API | `src/anthropic_runner.mjs` |
| Telegram → Neotoma mirror | `src/telegram_mirror.mjs` |

## Conversational modes

| `processing.agent_mode` | Requirements |
|-------------------------|----------------|
| `conversational.sdk` | **`CURSOR_API_KEY`** if set, else **`CURSOR_CLOUD_API_KEY`** (matches typical ateles `.env`); `@cursor/sdk` is a normal dependency. Uses `Agent.create` with `local.cwd = worktree`, streams assistant output, waits on `operator_queue` between turns, `/shipit` or `DONE` ends the loop. Model: `processing.cursor_sdk_model` or `FORMICA_CURSOR_MODEL` (legacy `ISSUE_PROCESSOR_CURSOR_MODEL`, default `composer-2`). |
| `conversational.claude_api` | `ANTHROPIC_API_KEY`. Calls Anthropic Messages API (`https://api.anthropic.com/v1/messages`). Model: `processing.anthropic_model` or `FORMICA_ANTHROPIC_MODEL` (legacy `ISSUE_PROCESSOR_ANTHROPIC_MODEL`). |

Optional: `processing.conversational_max_turns` caps rounds (default 24 SDK / 16 Anthropic).

When the target worktree exposes Neotoma's `/process-issues` skill, Formica now prompts the spawned agent to use that workflow for the current issue instead of relying on the generic ad hoc issue-fix prompt.

## Telegram → Neotoma mirroring

When `operator_transport.backend: telegram` and **`mirror_to_neotoma: true`**, every allowlisted inbound Telegram text is stored as a `conversation_message` with idempotency `telegram-inbound-{chat_id}-{message_id}` and `data_source` citing Telegram. If the message body contains an `ent_…` entity id, a **`REFERS_TO`** edge is created from that message to the entity.

## Safety defaults

- `processing.dry_run: true` — logs pipeline intent without git writes or Neotoma mutations.
- `processing.auto_fix: false` — no push/PR until you enable it or confirm via Telegram `/shipit` after local work.
- Hourly PR cap via `max_prs_per_hour` (default 5).
