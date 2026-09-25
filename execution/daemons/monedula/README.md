# Monedula

Daily payments daemon named after *Corvus monedula* (jackdaw — *moneta* = money).

Runs once per day at 07:00 UTC (09:00 Madrid summer / 08:00 winter) via launchd.
Checks Google Calendar for yesterday's sessions that trigger payment obligations,
asks for **payment consent** (operator authorization UX — not GDPR Art. 7
consent), executes approved payments, and confirms.

**Email consent** is the default when Telegram credentials are absent.
**Telegram break-glass** is an explicit override only.

> **Email consent requires a separate swarm mailbox.** A consent reply is
> accepted only when it carries authentication evidence stamped by the
> receiving mail server. A reply sent from, and read in, the operator's own
> mailbox is self-sent and never carries it, so with one shared mailbox
> every genuine approval would be held. Until the swarm has its own mailbox
> (`ATELES_SWARM_EMAIL` + `ATELES_SWARM_GWS_CONFIG_DIR`, built in
> ateles#1221), Monedula sends **no** consent request, holds every payment,
> and raises one deduped blocker: `consent_email_needs_swarm_mailbox`.

This change does **not** enable global Notifier email for unrelated daemons
(Apis/Anthus flood — see ateles#1165). Monedula calls `lib.approval` directly.

## Consent channel resolution

| Condition | Channel | Source |
|-----------|---------|--------|
| `MONEDULA_CONSENT_CHANNEL=email` | Email consent | `env` |
| `MONEDULA_CONSENT_CHANNEL=telegram` | Telegram break-glass | `env` |
| Telegram creds absent + `ATELES_NOTIFY_EMAIL=1` + `OPERATOR_EMAIL` set | Email consent | `automatic` |
| Email selected but no separate swarm mailbox | Blocker (`consent_email_needs_swarm_mailbox`) — no request sent | — |
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
| `ATELES_SWARM_EMAIL` | **Required for email consent.** The swarm's own address; must differ from `OPERATOR_EMAIL` |
| `ATELES_SWARM_GWS_CONFIG_DIR` | **Required for email consent.** gws config directory for the swarm's own mailbox (ateles#1221); must exist |
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
A reply from the operator's own address that cannot be authenticated is also
not accepted, but it is not silent: the operator gets one notice per request
(`consent_reply_unauthenticated`) saying the reply arrived, was not accepted,
and no payment was made.
Stale-session tokens and ambiguous replies never execute payment.
Each marker is bound to the item's exact terms (payee, amount, currency,
session) and to the current request: if any term changes, or the request is
superseded, earlier approvals no longer match anything and a fresh request is
sent. Earlier requests for a different pending set are **superseded**.

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
| `consent_email_needs_swarm_mailbox` | Email consent selected but no separate swarm mailbox is configured. No request sent; every payment held. Set up the swarm mailbox (ateles#1221): `ATELES_SWARM_EMAIL` (distinct from `OPERATOR_EMAIL`) and `ATELES_SWARM_GWS_CONFIG_DIR`. Notifier key `monedula:consent_email_needs_swarm_mailbox` |
| `consent_reply_sender_rejected` | A reply carried the consent marker but its From: is not the operator's address. Ignored; payments held; nothing is sent back to that sender |
| `consent_reply_unauthenticated` | A reply came from the operator's address but could not be authenticated, so it was **not** accepted; payments held. One notice + escalation per request (Notifier key `monedula:consent_reply_unauthenticated:<fp>:g<gen>`). Usually means request and reply share one mailbox — check the swarm mailbox setup (ateles#1221), then reply again to the current request |
| `consent_reply_unrecognized` | Authenticated but unparseable line; hold item; correction mail explains form |
| `payment_journal_unreadable` | The payment journal could not be read or written; every payment held; inspect `.monedula_payment_journal.json` before anything is paid |
| `payment_outcome_unknown` | A transfer was attempted and its outcome never recorded (crash or rail error mid-transfer). Held and never retried automatically; check the rail, then resolve by hand |
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
`consent_reply_sender_rejected`, `consent_reply_unauthenticated`,
`consent_reply_unrecognized`, `consent_email_needs_swarm_mailbox`.
Logs never expose addresses, account identifiers, message bodies, or secrets.

## Idempotency

Local consent mark: `.monedula_consent_email.json` — fingerprint, request
generation and `sent_at` only; cleared when a pending set ends.

Payment journal: `.monedula_payment_journal.json` — durable and never cleared
by the consent flow. Keyed on a hash of the obligation (handler, session,
payee, amount, currency). Order per payment: record intent (fsynced) → execute
→ record outcome. An obligation with a recorded outcome is never paid again;
one with intent but no outcome is `payment_outcome_unknown` — held and
escalated, never retried. The journal also issues the monotonically
increasing request generation. Do not delete it: an unreadable journal holds
every payment, and a missing one while a consent request is outstanding is
treated the same way.
Stranded Notifier dedupe: `monedula:stranded:<sorted keys fp>`.
