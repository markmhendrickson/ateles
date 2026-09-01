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
| `MONEDULA_SETTLEMENT_ALERT_DAYS` | Days an unsettled transfer may sit before the digest reports it as suspect (default `5`) |

## Settlement lifecycle

Funding a Wise transfer and Wise delivering it are two different events, minutes
to days apart. `_fund_transfer()` reads `status` off the **payment** object, so
`COMPLETED` there means *the money left the balance* — not that the transfer
settled. Settlement is decided by the **transfer record's own** status, read back
from Wise.

A payment therefore moves through three states rather than one (ateles#575):

| Result status | Meaning | Task | `payment_profile.status` |
|---|---|---|---|
| `sent` | Wise reports `outgoing_payment_sent` — delivered | closed (`done`) | `archived` (one-off) |
| `awaiting_settlement` | submitted, not yet delivered | **stays open**, noted | `awaiting_settlement` |
| `manual_required` | the transfer failed or could not be made | stays open | unchanged |

`awaiting_settlement` is a hold, not a terminal state, and it exists only because
something drives its exit. `settlement.py` runs on every tick (before the
"nothing to do" early return), re-reads each parked transfer from Wise, and
resolves it:

- **settled** → note it, then close a one-off task / return a recurring profile
  to `active` and roll its `due_date`
- **failed** (`cancelled`, `funds_refunded`, `bounced_back`, `charged_back`) →
  note it, set the profile to `payment_failed`, and **leave the task open**.
  The profile is deliberately *not* returned to `active`: re-arming a failed
  payment automatically is a double-payment risk, so re-payment is an operator
  decision.
- **in flight / unreadable** → write nothing. Past
  `MONEDULA_SETTLEMENT_ALERT_DAYS` it is reported as *suspect* in the digest.

Exits are **observed, not asserted**: the sweep believes Wise's transfer record,
never the daemon's memory of what it submitted. An observed exit fails visibly —
a stuck transfer sits in the digest until someone looks — where a self-asserted
one fails invisibly, which is how ateles#552 and #575 stayed hidden.

Two `payment_profile` snapshot fields carry the in-flight state:
`pending_transfer_id` and `pending_transfer_at` (ISO date). They are written as
correction observations, because the CLI's `entities update` exposes only
`--status`, `--notes` and `--due-date`.

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
- **Never mark a payment done on an unsettled transfer.** Only an observed
  delivery closes a task. A profile with a transfer in flight is parked in
  `awaiting_settlement`, which the loader excludes from matching — the
  double-payment guard for the in-flight window.
- **The settlement sweep is GET-only.** A POST to Wise from the sweep is a
  second payment.

