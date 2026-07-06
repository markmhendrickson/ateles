# Monedula

Daily payments daemon named after *Corvus monedula* (jackdaw — *moneta* = money).

Runs once per day at 07:00 UTC (09:00 Madrid summer / 08:00 winter) via launchd.
Checks Google Calendar for yesterday's sessions that trigger payment obligations,
previews them over Telegram, waits for operator approval, executes the payments,
and sends a confirmation.

## Handlers

| Handler  | Trigger (ended calendar event)                          | Payment           |
|----------|----------------------------------------------------------|-------------------|
| yoga     | recurringEventId match (falls back to title contains "manel" if unconfigured) | €60 BTC via claude --print + btc-wallet MCP |
| therapy  | title contains "therapy" or "terapia" (no stable recurring/event id available yet — see note below) | €60 Wise transfer |

Each profile may set `calendar_recurring_event_id` (for a true recurring
series) or an explicit `calendar_event_ids` allowlist (for non-recurring
events with a stable id) to match ONLY that calendar event, ignoring title
keywords entirely — this avoids one keyword accidentally matching an
unrelated event (e.g. "Therapy in-person" vs. "Walk to therapy"). Keyword
matching remains the fallback when neither is configured. The therapy
calendar event is currently recreated weekly with a new id and has no
recurringEventId, so it cannot use id-based matching yet; see the follow-up
task for making it a true recurring calendar series.

## Setup

```bash
cd execution/daemons/monedula
chmod +x install.sh
./install.sh
```

## Environment variables

Loaded automatically from `~/.config/neotoma/.env` at startup.

| Variable | Purpose |
|----------|---------|
| `TELEGRAM_BOT_TOKEN` | Bot token |
| `TELEGRAM_CHAT_ID` | Target chat/group ID |
| `TELEGRAM_ALLOWED_USER_ID` | Operator's Telegram user ID |
| `TELEGRAM_TOPIC_PAYMENTS` | Thread ID for payments topic |
| `WISE_API_TOKEN` | Wise API bearer token |
| `NEOTOMA_BASE_URL` / `NEOTOMA_BEARER_TOKEN` | Neotoma API — primary source for Wise recipient contact (name/IBAN/wise_recipient_id) via profile.contact_id |
| `DATA_DIR` | Path to data directory (for legacy contacts.parquet fallback only — not required when Neotoma has the contact) |

## Logs

`~/Library/Logs/ateles/monedula.log`

## Idempotency

A `.monedula_last_run` file in the daemon directory records today's date on startup.
Subsequent launchd invocations within the same day exit immediately — preventing
double-payment if launchd retries or the machine wakes mid-day.

## Constraints

Standing rules enforced for every payment Monedula makes (see project `CLAUDE.md`):

- **Never hardcode payee data.** IBANs, wallet addresses, amounts, and contact
  details are read from Neotoma or env (contacts.parquet as legacy fallback
  only) — never inlined in code.
- **Yoga payments carry no memo / OP_RETURN.** Do not pass a `memo` on the yoga
  BTC path.
- **Yoga / therapy tasks are never marked completed.** Only the `due_date` is
  advanced — these are recurring obligations, not one-off tasks.
- **Payment paths are idempotent.** Guard against double-send (see Idempotency
  above) on every execution route.

