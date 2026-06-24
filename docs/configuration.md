# Configuration reference

Every Ateles environment variable, what reads it, and its default. Source of truth for
the values is [`.env.example`](../.env.example); this page explains them.

**Where it loads from.** Daemons read a single env file — copy `.env.example` to
`~/.config/ateles/.env` (or wherever a daemon's launchd `EnvironmentFile` / systemd
`EnvFile` points). launchd does not source your shell profile, so the file must be
self-contained. Never commit a file with real values.

**Sourcing rule.** Operator-specific values (identity, calendar IDs, recipients, entity
IDs that vary per operator) are read from env (or parquet / Neotoma) at runtime — never
hardcoded in daemon code. This keeps the swarm portable and is enforced by
`scripts/linters/check_hardcoded_config.py`. Secrets are materialized from the private
`ateles-private` repo via SOPS+age (see [secrets_management.md](secrets_management.md)),
not stored here in plaintext.

Legend: **Required** = a daemon fails or no-ops without it · **Default** = used when unset.

---

## Neotoma — the memory layer (all daemons)

| Variable | Required | Default | Read by | Purpose |
| -------- | -------- | ------- | ------- | ------- |
| `NEOTOMA_BEARER_TOKEN` | ✅ | — | every daemon | API auth. `NEOTOMA_BEARER_TOKEN_PROD` is promoted to this at startup when a remote base URL is detected. |
| `NEOTOMA_BASE_URL` | — | `https://neotoma.markmhendrickson.com` | every daemon | Neotoma API endpoint. Point at your own instance for a fork. |

## Operator identity

Read by daemons that brief, notify, or research on the operator's behalf.

| Variable | Required | Default | Read by | Purpose |
| -------- | -------- | ------- | ------- | ------- |
| `OPERATOR_NAME` | — | — | Cotinga, briefing prompts | Operator full name used in prompts. |
| `OPERATOR_EMAIL` | — | — | Cotinga, Sylvia | Primary email / Google Calendar primary calendar ID. |
| `ATELES_PLAN_ENTITY_ID` | — | `ent_99ace4dd6673aa36ed08b1fe` | live-convergence lookups | Neotoma entity ID of the canonical Ateles plan. Set your own for a fork. |

## Telegram — notifications & commands

| Variable | Required | Default | Read by | Purpose |
| -------- | -------- | ------- | ------- | ------- |
| `TELEGRAM_BOT_TOKEN` | ✅ for notify | — | `lib/notify`, Monedula | Bot token from @BotFather. |
| `TELEGRAM_CHAT_ID` | ✅ for notify | — | `lib/notify` | Destination chat the bot posts to. |
| `TELEGRAM_ALLOWED_USER_ID` | — | — | Monedula | Only this user's commands are honored. |
| `TELEGRAM_TOPIC_MONEDULA` | — | — | Monedula | Topic (thread) ID for payment notifications. Legacy name `TELEGRAM_TOPIC_PAYMENTS` still accepted. |
| `TELEGRAM_TOPIC_COTINGA` | — | — | Cotinga | Topic for daily briefings. |
| `TELEGRAM_TOPIC_APUS` | — | — | Apus | Topic for mirror activity. |
| `TELEGRAM_TOPIC_NEOTOMA_AGENT` | — | — | neotoma-agent | Topic for agent notifications. |

## Payments — Wise & Monedula profiles

| Variable | Required | Default | Read by | Purpose |
| -------- | -------- | ------- | ------- | ------- |
| `WISE_API_TOKEN` | ✅ for Wise transfers | — | Monedula (WiseTransferHandler) | Wise API token; required for any profile with `payment_type=wise`. |
| `MONEDULA_PROFILES` | ✅ for Monedula | — | Monedula | Comma-separated list of profile **prefixes** (e.g. `THERAPY,YOGA`). Each drives a `<PREFIX>_*` set below. |

**Per-profile variables.** For each prefix in `MONEDULA_PROFILES`, define:

| Variable (per `<PREFIX>`) | Applies to | Purpose |
| ------------------------- | ---------- | ------- |
| `<PREFIX>_LABEL` | all | Human-readable label for Telegram messages. |
| `<PREFIX>_CALENDAR_KEYWORDS` | all | Comma-separated keywords matched against calendar event titles. |
| `<PREFIX>_PAYMENT_TYPE` | all | `wise` or `btc`. |
| `<PREFIX>_AMOUNT_EUR` | all | Transfer amount in EUR (integer). |
| `<PREFIX>_CONTACT_ID` | wise | Neotoma `contact_id` prefix for IBAN lookup. |
| `<PREFIX>_CONTACT_CATEGORY` | wise | Fallback: `contacts.parquet` category. |
| `<PREFIX>_CONTACT_PLATFORM` | wise | Fallback: `contacts.parquet` platform. |
| `<PREFIX>_WISE_REFERENCE` | wise | Wise transfer reference string. |
| `<PREFIX>_BTC_ADDRESS` | btc | Destination BTC address. |
| `<PREFIX>_NEOTOMA_TASK_ID` | optional | Task entity ID whose `due_date` is rolled after payment. |
| `<PREFIX>_TASK_KEYWORDS` | optional | Comma-separated keywords for task-search fallback. |

> Guardrails (see project rules): Yoga payments never include a memo/OP_RETURN; yoga/therapy
> tasks are never marked complete — only their `due_date` is updated.

## Calendar — Cotinga

| Variable | Required | Default | Read by | Purpose |
| -------- | -------- | ------- | ------- | ------- |
| `COTINGA_CALENDAR_IDS` | — | `OPERATOR_EMAIL` alone | Cotinga | Comma-separated Google Calendar IDs for the daily brief (primary + shared/family). |

## Notification routing — `lib/notify`

| Variable | Required | Default | Read by | Purpose |
| -------- | -------- | ------- | ------- | ------- |
| `PRIORITY_RUBRIC_ENTITY_ID` | — | `ent_29ca079940c1e996a8c782f2` | `lib/notify` (Apprise) | priority_rubric entity governing silence windows, digest times, and per-priority routing. |

## Agent definitions — `lib/daemon_runtime`

Optional explicit entity IDs for each daemon's `agent_definition`. If unset, the runtime
searches by name. Set once after Phase 1 to skip the lookup.

| Variable | Default |
| -------- | ------- |
| `MONEDULA_AGENT_DEFINITION_ID` | `ent_26e45f38f53798eb42961a69` |
| `NEOTOMA_AGENT_DEFINITION_ID` | `ent_c5c8d28bd420ca094f9d5a48` |
| `APUS_AGENT_DEFINITION_ID` | `ent_692e8533840be7195240a1e4` |

## Transcription extraction — Cyphorhinus / Piculet

| Variable | Required | Default | Read by | Purpose |
| -------- | -------- | ------- | ------- | ------- |
| `NEOTOMA_COMPANY_ENTITY_ID` | — | `ent_44835c5b0047ce26ffbe40bc` | Cyphorhinus, Piculet | Canonical company entity that product mentions relate to during extraction. Override for a fork. |

## Mirror webhook — Apus

| Variable | Required | Default | Read by | Purpose |
| -------- | -------- | ------- | ------- | ------- |
| `APUS_WEBHOOK_SECRET` | recommended | — | Apus | HMAC-SHA256 secret for verifying Neotoma webhook signatures. |
| `APUS_PORT` | — | `8741` | Apus | Local listen port (Cloudflare Tunnel proxies the public hostname → this). |
| `ATELES_REPO_PATH` | ✅ for Apus | — | Apus | Local checkout of the public ateles repo (mirror commit target). |
| `ATELES_PRIVATE_REPO_PATH` | — | — | Apus | Local checkout of `ateles-private`. |
| `ATELES_AGENT_GIT_NAME` | — | `ateles-agent` | Apus | Git author name for mirror commits. |
| `ATELES_AGENT_GIT_EMAIL` | — | — | Apus | Git author email for mirror commits. |

## Identity — AAuth keypairs

| Variable | Required | Default | Read by | Purpose |
| -------- | -------- | ------- | ------- | ------- |
| `ATELES_PRIVATE_KEYS_DIR` | — | `../ateles-private/keys/` (relative to repo root) | `lib/daemon_runtime` (AAuthSigner) | Directory of per-agent AAuth keypairs. `ateles doctor` checks this for rung 4. |

## Data

| Variable | Required | Default | Read by | Purpose |
| -------- | -------- | ------- | ------- | ------- |
| `DATA_DIR` | ✅ for parquet lookups | — | shared libs | Root directory for parquet files (e.g. `contacts.parquet`). |

---

## Minimum sets by onboarding rung

Mapped to the [onboarding ladder](README.md#start-here--the-onboarding-ladder); run
`python3 execution/scripts/ateles_doctor.py` to confirm what your environment satisfies.

- **Rung 1 (first agent, stub mode):** none — the `claude` CLI only.
- **Rung 2 (connect memory):** `NEOTOMA_BEARER_TOKEN` (+ `NEOTOMA_BASE_URL` for a fork).
- **Rung 3 (first daemon):** add `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`; plus the daemon's own vars (e.g. `OPERATOR_EMAIL` + `COTINGA_CALENDAR_IDS` for Cotinga, or `MONEDULA_PROFILES` + `WISE_API_TOKEN` for Monedula).
- **Rung 4 (attributed identity):** `ATELES_PRIVATE_KEYS_DIR` populated with a minted keypair.
- **Rung 5 (persist & schedule):** the SOPS age key for secret materialization (see [secrets_management.md](secrets_management.md)); Apus adds `ATELES_REPO_PATH` and `APUS_WEBHOOK_SECRET`.
