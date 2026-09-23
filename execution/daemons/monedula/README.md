# Monedula

Daily payments daemon named after *Corvus monedula* (jackdaw — *moneta* = money).

Runs once per day at 07:00 UTC (09:00 Madrid summer / 08:00 winter) via launchd.
Checks Google Calendar for yesterday's sessions that trigger payment obligations,
asks for **payment consent** (operator authorization UX — not GDPR Art. 7
consent), executes approved payments, and confirms.

**Email consent** is the default when Telegram credentials are absent.
**Telegram break-glass** is an explicit override only.

This change does **not** enable global Notifier email for unrelated daemons
(Apis/Anthus flood — see ateles#1165). Monedula calls `lib.approval` directly.

## Consent channel resolution

| Condition | Channel | Source |
|-----------|---------|--------|
| `MONEDULA_CONSENT_CHANNEL=email` | Email consent | `env` |
| `MONEDULA_CONSENT_CHANNEL=telegram` | Telegram break-glass | `env` |
| Telegram creds absent + `ATELES_NOTIFY_EMAIL=1` + `OPERATOR_EMAIL` set | Email consent | `automatic` |
| Both Telegram creds + operator principal resolve | Telegram break-glass | `automatic` |
| Nothing configured | Blocker (`consent_channel_unconfigured`) | — |

Never a silent no-op when payments are pending.

## Consent states

| State | Meaning |
|-------|---------|
| `awaiting_approval` | Request sent or held; no reply yet (silence ≠ decline) |
| `approved` | Explicit operator approve / ATTENDED |
| `skipped` | Explicit `SKIP` only |
| `blocked` | Channel/config/send/read failure — no payment moved |

Missing config, transport failure, unread inbox, and no reply never become
`skipped`.

## Environment variables

Loaded automatically from `~/.config/neotoma/.env` at startup.

| Variable | Purpose |
|----------|---------|
| `ATELES_NOTIFY_EMAIL` | `"1"` arms `lib.approval` email send/read for Monedula consent |
| `OPERATOR_EMAIL` | Consent request recipient; exact-match From: for replies |
| `ATELES_SWARM_EMAIL` | Optional From: for outbound consent mail |
| `MONEDULA_CONSENT_CHANNEL` | Optional override: `email` \| `telegram` |
| `TELEGRAM_BOT_TOKEN` | Telegram break-glass only |
| `TELEGRAM_CHAT_ID` | Telegram break-glass only |
| `TELEGRAM_ALLOWED_USER_ID` | Operator Telegram user ID (required when Telegram selected) |
| `TELEGRAM_TOPIC_PAYMENTS` | Thread ID for payments topic |
| `MONEDULA_DEAD_GATE_THRESHOLD` | Consecutive channel failures before dead-gate alarm (default `3`) |
| `WISE_API_TOKEN` | Wise API bearer token |
| `DATA_DIR` | Path to data directory (for contacts.parquet) |

## Email consent — request / reply examples (synthetic)

**Single-item request** (calendar): subject carries the marker; body allows bare
`ATTENDED` / `SKIP`.

```
Subject: [Ateles] Monedula consent 2026-09-22 [APPROVE-1A2B3C4D]

- therapy | 2026-09-22 | Studio Example | EUR 60 | [APPROVE-1A2B3C4D]

Reply ATTENDED or SKIP (subject carries the marker).
Do not use PAID as a pre-execution approval verb.
```

Operator reply:

```
ATTENDED
```

**Multi-item:** every reply line MUST include its marker.

```
ATTENDED [APPROVE-1A2B3C4D]
SKIP [APPROVE-9F8E7D6C]
```

Each marked line authorizes or skips only that match; other matches for the
same handler stay blocked until their own line.

Bare `ATTENDED` without a marker on a multi-item set is unrecognized — no
payment executes; one deduped in-thread correction explains the accepted form.

Non-operator senders (`attacker@evil.example`) are ignored; payments stay held.
Stale-session tokens and ambiguous replies never execute payment.
Earlier requests for a different pending set are **superseded**.

Pending-set dedupe: one consent email per fingerprint; unchanged set logs
`consent_request_suppressed reason=unchanged_pending_set` and still sweeps replies.

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

## Troubleshooting — reason codes

| Reason code | Check / remediation |
|-------------|---------------------|
| `consent_channel_unconfigured` | Set `MONEDULA_CONSENT_CHANNEL`, or arm `ATELES_NOTIFY_EMAIL=1` + `OPERATOR_EMAIL`, or Telegram break-glass vars |
| `consent_request_send_failed` | Request not delivered; payments blocked; next tick retries; check `gws gmail +send` |
| `consent_reply_read_failed` | Replies could not be checked; no payment executed; check `gws`/mailbox |
| `consent_reply_sender_rejected` | Non-operator From:; payments held; do not expect a reply to the rejected sender |
| `consent_reply_unrecognized` | Authenticated but unparseable line; hold item; correction mail explains form |
| `payment_profiles_stranded` | Fix the named payment_profile config; Notifier key `monedula:stranded:<fp>` |

Consent-channel-failure alert key: `monedula:consent_channel_failed`
(`email_eligible=True`, actionable body).

## Telegram break-glass notes

**Telegram getUpdates is single-consumer per bot token.** If another process
polls `getUpdates` on the same `TELEGRAM_BOT_TOKEN`, Monedula's poll loses
the race and gets HTTP 409 Conflict (ateles#554). Host-side identification is
tracked in ateles#890.

### Who may approve (Telegram)

`TELEGRAM_ALLOWED_USER_ID` names the single principal permitted to authorize a
payment when Telegram is selected. Unset or non-numeric ⇒ `channel_error`.

## Logs

`~/Library/Logs/ateles/monedula.log`

Log events include: `consent_channel_selected`, `consent_request_suppressed`,
`consent_request_send_failed`, `consent_reply_read_failed`,
`consent_reply_sender_rejected`, `consent_reply_unrecognized`.
Logs never expose addresses, account identifiers, message bodies, or secrets.

## Idempotency

Local consent mark: `.monedula_consent_email.json` — fingerprint + `sent_at` only.
Stranded Notifier dedupe: `monedula:stranded:<sorted keys fp>`.
