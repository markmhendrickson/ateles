# Monedula

Daily payments daemon named after *Corvus monedula* (jackdaw — *moneta* = money).

Runs once per day at 07:00 UTC (09:00 Madrid summer / 08:00 winter) via launchd.
Checks Google Calendar for yesterday's sessions that trigger payment obligations,
previews them over Telegram, waits for operator approval, executes the payments,
and sends a confirmation.

## Handlers

| Handler  | Trigger (yesterday's event) | Payment           |
|----------|----------------------------|-------------------|
| yoga     | title contains "manel"     | €60 BTC via claude --print + btc-wallet MCP |
| therapy  | title contains "therapy" or "terapia" | €60 Wise transfer |

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
| `DATA_DIR` | Path to data directory (for contacts.parquet) |

## Logs

`~/Library/Logs/ateles/monedula.log`

## Idempotency

A `.monedula_last_run` file in the daemon directory records today's date on startup.
Subsequent launchd invocations within the same day exit immediately — preventing
double-payment if launchd retries or the machine wakes mid-day.

## Constraints

Standing rules enforced for every payment Monedula makes (see project `CLAUDE.md`):

- **Never hardcode payee data.** IBANs, wallet addresses, amounts, and contact
  details are read from env or parquet — never inlined in code.
- **Yoga payments carry no memo / OP_RETURN.** Do not pass a `memo` on the yoga
  BTC path.
- **Yoga / therapy tasks are never marked completed.** Only the `due_date` is
  advanced — these are recurring obligations, not one-off tasks.
- **Payment paths are idempotent.** Guard against double-send (see Idempotency
  above) on every execution route.

