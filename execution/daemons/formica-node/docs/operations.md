# Formica: operations

## Run

Configure the ateles repo **`.env`** (repo root, three directories above this package; e.g. `NEOTOMA_BEARER_TOKEN`, `NEOTOMA_HOST_URL` / `NEOTOMA_BASE_URL` for local dev). **`start.sh`** loads it automatically.

```bash
cd execution/daemons/formica
npm install
npm start
```

From **ateles** repo root:

```bash
npm run formica
```

Legacy alias: `npm run issue-processor`.

Graceful stop: **Ctrl+C**, or `./stop.sh` (reads `.run/daemon.pid`).

## macOS LaunchAgent (prod Neotoma, same host as tunnel)

The ateles repo ships a **LaunchAgent** that runs Formica against **`https://neotoma.markmhendrickson.com`** by default (the same Cloudflare tunnel target described for the prod Neotoma API in `execution/scripts/setup_neotoma_api_launchagent.sh`). Override with **`NEOTOMA_BASE_URL`** in your env file if your Cursor / CLI prod base URL differs.

**One command:** ensure **`NEOTOMA_BEARER_TOKEN`** (and any other keys) live in the ateles repo **`.env`**, then on your Mac:

```bash
bash execution/scripts/install_formica_launchd_from_ateles_env.sh
```

That only checks **`.env`** and runs **`setup_formica_launchagent.sh`**. At runtime, **`load_ateles_repo_env.sh`** sources **`$ATELES_ROOT/.env`** into the daemon (all variables — Neotoma, Cursor, Anthropic, OpenAI, Telegram, etc.).

### Optional override file

Set **`FORMICA_ENV_FILE`** to an extra file path if you must layer machine-only secrets **after** `.env` (unusual when the repo checkout is present).

### Manual steps (legacy)

1. ~~`~/.config/ateles/formica.env`~~ — **not required** when the LaunchAgent uses the ateles checkout; use repo **`.env`** only.

2. From repo root:

   ```bash
   bash execution/scripts/setup_formica_launchagent.sh
   ```

3. Logs: **`data/logs/formica_launchd.log`** and **`data/logs/formica_launchd.error.log`** under the ateles checkout used in the plist.

4. Unload: `launchctl unload "$HOME/Library/LaunchAgents/com.markmhendrickson.formica.plist"`

The wrapper **`execution/scripts/run_formica_launchd.sh`** sources **`execution/daemons/formica/load_ateles_repo_env.sh`** (repo **`.env`**), then an optional **`FORMICA_ENV_FILE`**, applies **`NEOTOMA_BASE_URL`** default, checks **`NEOTOMA_BEARER_TOKEN`**, then **`exec`** **`start.sh`** (which sources **`.env`** again so **`npm run formica`** behaves the same).

## Production notes

- Run under **process manager** (systemd, launchd, PM2) with `Restart=on-failure` and log capture on stderr.
- Set **`NEOTOMA_FORMICA_SUBSCRIPTION_ID`** (or legacy `NEOTOMA_ISSUE_PROCESSOR_SUBSCRIPTION_ID`) after first successful subscribe so restarts do not mint duplicate subscriptions.
- Persist **`FORMICA_SSE_CHECKPOINT`** (or legacy `ISSUE_PROCESSOR_SSE_CHECKPOINT`) on durable disk if you rely on `Last-Event-ID` replay across long outages.
- Ensure **`repos.*.worktree_base`** lives on a volume with enough free space; worktrees are not aggressively pruned automatically (cleanup helper logs stale dirs only).

## Telegram mirroring

When **`operator_transport.mirror_to_neotoma: true`**:

- Every **allowlisted** inbound Telegram message is written to Neotoma as a **`conversation_message`** with idempotency `telegram-inbound-{chat_id}-{message_id}`.
- If the text contains an **`ent_…`** entity id, a **`REFERS_TO`** edge is added from that message to the entity (typically an `issue`).

Slash commands such as **`/shipit`** are mirrored too, then handled by the daemon router.

## Troubleshooting

| Symptom | Likely cause | What to do |
|---------|--------------|------------|
| Exit on start: missing token | `NEOTOMA_BEARER_TOKEN` unset | Export token or use env file sourced by the service unit. |
| SSE 401 / repeated errors | Wrong token or clock skew | Regenerate token; verify `NEOTOMA_BASE_URL`. |
| `No repos.* entry matched` | `issue.repository` not in `repos` map | Add a key or adjust `issue.repository` at submission time. |
| `missing_sha` / `needs_repro` | `strict_reporter` without `reporter_git_sha` | Populate SHA at issue creation or relax `rebase_policy` for trusted flows. |
| `cursor_cli_missing` | `oneshot` + `cursor` runtime, no CLI | Install Cursor CLI / `cursor-agent`, or switch `agent_runtime` / `agent_mode`. |
| `CURSOR_API_KEY_or_CURSOR_CLOUD_API_KEY_required` | `conversational.sdk` without key | Export **`CURSOR_API_KEY`** or **`CURSOR_CLOUD_API_KEY`** (ateles `.env` often has the latter). |
| `ANTHROPIC_API_KEY_required` | `conversational.claude_api` without key | Export `ANTHROPIC_API_KEY`. |
| `gh pr create` fails | `gh` auth, fork permissions, or branch not pushed | Run `gh auth status`; ensure remote and permissions; check stderr in logs. |
| Telegram silent | Wrong `telegram_chat_id` or sender not allowlisted | Verify supergroup id format; add numeric user id to `telegram_allowed_user_ids`. |
| Mirror duplicates / 400 on store | Idempotency collision | Normal if Telegram retries the same `message_id`; Neotoma should dedupe by idempotency key. |

## Kill switch

Create (or update) a Neotoma entity with **`entity_type: daemon_config`** and **`active: false`**. The daemon continues to hold SSE open but **skips** new pipeline invocations until `active` is true again or the entity is removed.

## Tests

```bash
npm test
```

Useful before changing resolver, PR, or Anthropic mocking behavior.
