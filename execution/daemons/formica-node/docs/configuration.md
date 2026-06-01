# Formica: configuration reference

All paths are relative to the daemon package root unless noted. Override the config file with **`FORMICA_CONFIG`** (absolute path). Legacy: **`ISSUE_PROCESSOR_CONFIG`** is still read if `FORMICA_CONFIG` is unset.

**Shell environment:** `start.sh` and the launchd wrapper load **`$ATELES_ROOT/.env`** (ateles repo root) via **`load_ateles_repo_env.sh`**. Put API keys and URLs there so Formica shares the same variables as the rest of the repo.

String values may embed **`${ENV_VAR}`** placeholders (expanded at load time from `process.env`).

## Top-level blocks

### `subscription`

| Field | Type | Description |
|-------|------|-------------|
| `entity_types` | string[] | Neotoma entity types to watch (e.g. `issue`, `product_feedback`). |
| `event_types` | string[] | Substrate event names (e.g. `entity.created`, `entity.updated`). |
| `delivery_method` | `sse` \| `webhook` | How Neotoma delivers events. Local dev typically uses **`sse`**. |
| `webhook_url` | string | Required when `delivery_method` is `webhook`. |

### `processing`

| Field | Type | Default / notes |
|-------|------|-----------------|
| `max_concurrent` | number | Reserved for future parallelism; pipeline is effectively serial today. |
| `dry_run` | boolean | **`true`**: no git mutations, no Neotoma writes from pipeline (still connects SSE). |
| `max_prs_per_hour` | number | Rate limiter for successful PR opens (default **5**). |
| `agent_runtime` | `cursor` \| `claude_code` | CLI used for **`oneshot`** (not used for `conversational.sdk`). |
| `agent_mode` | string | `oneshot`, `conversational.sdk`, `conversational.claude_api`, `human_handoff`. |
| `conversational_max_turns` | number | Caps SDK / Anthropic conversation rounds. |
| `cursor_sdk_model` | string | Passed to `@cursor/sdk` (e.g. `composer-2`). |
| `anthropic_model` | string | Anthropic Messages `model` id. |
| `auto_classify` | boolean | If **`true`**, uses OpenAI when `OPENAI_API_KEY` is set; else heuristic. |
| `auto_fix` | boolean | If **`false`**, stops before push/PR when there are local commits; use Telegram **`/shipit`** to resume. |
| `rebase_policy` | string | `strict_reporter`, `mainline`, `min_release_ge_reporter`. Per-issue `rebase_policy` overrides. |
| `default_branch` | string | Integration branch for `gh pr create --base` and optional rebase target. |
| `align_pr_to_main` | boolean | If **`true`**, runs `git rebase origin/<default_branch>` in the worktree before push (conflicts surface as `human_needed`). |
| `dirty_tree_policy` | `abort` \| `auto_commit_bot` | Preflight when index is dirty before push. |

### `repos`

Map logical **`issue.repository`** values (or substring matches) to host paths.

| Field | Description |
|-------|-------------|
| `path` | Absolute path to the **main** clone (`git` remote operations run here). |
| `worktree_base` | Directory under which per-issue worktrees are created (must be writable). |

If no key matches `issue.repository`, the first repo entry is used (fallback).

### `neotoma`

| Field | Description |
|-------|-------------|
| `base_url` | Neotoma HTTP API origin (no trailing slash). Overridden by **`NEOTOMA_BASE_URL`**. |

### `operator_transport`

| Field | Description |
|-------|-------------|
| `backend` | `none` or **`telegram`**. |
| `telegram_bot_token` | Bot token; use **`${TELEGRAM_BOT_TOKEN}`** in YAML. |
| `telegram_chat_id` | Target chat (often negative for supergroups). |
| `telegram_allowed_user_ids` | Numeric Telegram user IDs allowed to drive `/shipit` and operator queue. |
| `use_message_threads` | Prefer forum topics when the chat supports them. |
| `telegram_message_thread_id` | Optional default thread id for outbound handoff messages. |
| `mirror_to_neotoma` | If **`true`**, each allowlisted inbound Telegram message is stored in Neotoma (see [operations](operations.md#telegram-mirroring)). |

## Environment variables

| Variable | Purpose |
|----------|---------|
| `NEOTOMA_BASE_URL` | API origin (overrides `neotoma.base_url`). |
| `NEOTOMA_BEARER_TOKEN` | **Required** for Neotoma HTTP auth. |
| `NEOTOMA_FORMICA_SUBSCRIPTION_ID` | If set, skips `POST /subscribe` and reuses this SSE subscription id. Legacy: `NEOTOMA_ISSUE_PROCESSOR_SUBSCRIPTION_ID`. |
| `FORMICA_CONFIG` | Path to YAML config file. Legacy: `ISSUE_PROCESSOR_CONFIG`. |
| `FORMICA_SSE_CHECKPOINT` | Path to persist `Last-Event-ID` for SSE replay across restarts (default: `.run/last_event_id.txt`). Legacy: `ISSUE_PROCESSOR_SSE_CHECKPOINT`. |
| `DEBUG_FORMICA` | Set to `1` for extra stderr logging. Legacy: `DEBUG_ISSUE_PROCESSOR`. |
| `CURSOR_API_KEY` | For **`conversational.sdk`**: bearer for `@cursor/sdk` (optional if `CURSOR_CLOUD_API_KEY` is set). |
| `CURSOR_CLOUD_API_KEY` | For **`conversational.sdk`**: same as many ateles `.env` layouts; used when `CURSOR_API_KEY` is unset. |
| `FORMICA_CURSOR_MODEL` | Overrides `processing.cursor_sdk_model`. Legacy: `ISSUE_PROCESSOR_CURSOR_MODEL`. |
| `ANTHROPIC_API_KEY` | Required for **`conversational.claude_api`**. |
| `FORMICA_ANTHROPIC_MODEL` | Overrides `processing.anthropic_model`. Legacy: `ISSUE_PROCESSOR_ANTHROPIC_MODEL`. |
| `OPENAI_API_KEY` | Optional; enables LLM classification when `auto_classify` is true. |
| `TELEGRAM_BOT_TOKEN` | Injected when YAML uses `${TELEGRAM_BOT_TOKEN}`. |

## Neotoma entities used by the daemon

| Entity / query | Role |
|----------------|------|
| `issue` | Primary workload; snapshot fields drive repo, base SHA, classification updates. |
| `daemon_session` | One row per processing attempt; audit fields (`resolved_base_commit`, `worktree_path`, etc.). |
| `daemon_config` | Optional kill switch: `active: false` disables new pipeline work. |
| `conversation_message` | Issue thread updates; Telegram mirror rows when enabled. |

Relationships: `daemon_session` **REFERS_TO** `issue`; mirrored Telegram messages **REFERS_TO** `ent_*` when parsed from text.
