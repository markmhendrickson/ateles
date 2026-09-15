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
| `TELEGRAM_BOT_TOKEN` | Bot token used to send previews and long-poll for the approval reply |
| `TELEGRAM_CHAT_ID` | Target chat/group ID |
| `TELEGRAM_ALLOWED_USER_ID` | Operator's Telegram user ID |
| `TELEGRAM_TOPIC_PAYMENTS` | Thread ID for payments topic |
| `MONEDULA_DEAD_GATE_THRESHOLD` | Consecutive channel failures before the dead-gate alarm fires (default `3`) |
| `WISE_API_TOKEN` | Wise API bearer token |
| `DATA_DIR` | Path to data directory (for contacts.parquet) |

**Telegram getUpdates is single-consumer per bot token.** If another process
polls `getUpdates` on the *same* `TELEGRAM_BOT_TOKEN`, Monedula's poll loses
the race and gets HTTP 409 Conflict (ateles#554). As of this writing,
Cyphorhinus (`execution/daemons/cyphorhinus/`, itself deprecated in favor of
the email transport — see its module docstring) uses a *different* env var,
`CYPHORHINUS_TELEGRAM_BOT_TOKEN`, and on the current deployment machine that
resolves to a genuinely different bot token than `TELEGRAM_BOT_TOKEN` — so it
is not the source of a live 409. If Monedula's log shows repeated 409s,
confirm what else on the host is polling `getUpdates` for the same token
before assuming it is Cyphorhinus; the two were never designed to share one.
Host-side identification and elimination of the second consumer is tracked in
ateles#890 (not closed by dead-gate alarming alone).

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

## Consent gate (ateles#554)

The **only** mechanism that may authorize a payment is an explicit Telegram
attendance reply, parsed by `_parse_reply` (see `test_parse_reply.py` for the
accepted forms — a bare "yes" is intentionally rejected).

`telegram_poll_approval` returns one of three outcomes:

- **`reply`** — a real message arrived. `_parse_reply` decides approval as
  always. An explicit decline (`"no"`, unrecognized text, a bare "yes") is a
  clean run: exit 0, no escalation, same as it has always been.
- **`timeout`** — the poll ran to its deadline with no message. Treated as a
  **channel failure**, not a decline — the operator may never have seen the
  prompt. Payment is blocked, an `escalation` entity is written to Neotoma,
  and the process exits 1.
- **`channel_error`** — missing credentials, an HTTP error (409 Conflict
  chief among them), or a malformed Telegram response. Same handling as
  `timeout`.

A channel failure and a decline are deliberately never allowed to look the
same in the log or in the exit code again — that indistinguishability is
what let a 0/480 gate run silently for ten weeks (ateles#554).

**Dead-gate alarm.** `MONEDULA_DEAD_GATE_THRESHOLD` (default 3) consecutive
channel failures — with no reply landing in between — additionally fire a
`monedula_dead_consent_gate` escalation at `severity=critical`. Any reply,
approved or declined, resets the streak: it proves the channel works.

**`payment_approved` is vestigial.** An earlier, unmerged branch
(`feat/monedula-task-autoexecute`, PR #249) read a `payment_approved` boolean
off the linked Neotoma task and executed payment directly from it, with no
interactive confirmation. That branch never merged to `main` and the field is
not wired to anything — `monedula.py` does not read it, and must not. If
`payment_approved` is ever intended as a real authorization path it needs a
deliberate design decision, code, and tests of its own; until then it may
persist as a dead field on task entities, and no code path may use it to gate
`handler.execute`.

