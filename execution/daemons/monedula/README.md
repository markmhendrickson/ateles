# Monedula

Daily payments daemon named after *Corvus monedula* (jackdaw — *moneta* = money).

Runs once per day at 07:00 UTC (09:00 Madrid summer / 08:00 winter) via launchd.
Checks Google Calendar for yesterday's sessions that trigger payment obligations,
asks for **payment consent** (operator authorization UX — not GDPR Art. 7
consent), executes approved payments, and confirms.

**Email consent** is the default when Telegram credentials are absent.
**Telegram break-glass** is an explicit override only.

> **Email consent runs through the swarm's own mailbox.** It works once
> `ATELES_SWARM_EMAIL` and `ATELES_SWARM_GWS_CONFIG_DIR` are set (see
> "Swarm mailbox" below). The request goes **from** the swarm mailbox **to**
> `OPERATOR_EMAIL`; the operator's reply arrives in the swarm mailbox as
> genuinely inbound mail, which the receiving server authenticates. A reply
> is accepted only with that authentication evidence — which is why the
> operator's own mailbox can never be used: a reply sent from, and read in,
> the same mailbox is self-sent and carries none. If either variable is
> missing or wrong, Monedula sends **no** consent request, makes no mail call
> at all, holds every payment, and raises one deduped blocker,
> `consent_email_needs_swarm_mailbox`, naming the variable to fix.

## Swarm mailbox

| Variable | What it is |
|----------|------------|
| `ATELES_SWARM_EMAIL` | The swarm mailbox's own email address — a separate account, never the operator's. Every consent request is sent From it. Lives in the private env, never in this repo. |
| `ATELES_SWARM_GWS_CONFIG_DIR` | Path to a `gws` config directory that is signed in as that swarm account (for example `~/.config/gws-swarm`). Every consent mail call — send the request, search and read replies, reply in thread — runs with `GOOGLE_WORKSPACE_CLI_CONFIG_DIR` set to it, in that subprocess only. Must exist. |
| `ATELES_MAIL_AUTHSERV_ID` | Optional. The receiving server whose `Authentication-Results` are trusted; default `mx.google.com` (Gmail). Change only when the swarm mailbox moves off Gmail. |

Everything else Monedula does with `gws` — the operator's calendar read —
keeps the operator's default config. The general Notifier's email delivery is
not moved to the swarm mailbox by this change; that remains ateles#1221.

This change does **not** enable global Notifier email for unrelated daemons
(Apis/Anthus flood — see ateles#1165). Monedula calls `lib.approval` directly.

## Consent channel resolution

| Condition | Channel | Source |
|-----------|---------|--------|
| `MONEDULA_CONSENT_CHANNEL=email` | Email consent | `env` |
| `MONEDULA_CONSENT_CHANNEL=telegram` | Telegram break-glass | `env` |
| Telegram creds absent + `ATELES_NOTIFY_EMAIL=1` + `OPERATOR_EMAIL` set | Email consent | `automatic` |
| Email selected but swarm mailbox not configured | Blocker (`consent_email_needs_swarm_mailbox`) — no request sent, no mail call made | — |
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
| `OPERATOR_EMAIL` | Consent request recipient (To:); exact-match From: for replies |
| `ATELES_SWARM_EMAIL` | **Required for email consent.** The swarm mailbox's address; must differ from `OPERATOR_EMAIL`. See "Swarm mailbox" |
| `ATELES_SWARM_GWS_CONFIG_DIR` | **Required for email consent.** gws config directory signed in as the swarm mailbox; must exist. See "Swarm mailbox" |
| `ATELES_MAIL_AUTHSERV_ID` | Optional trusted receiving-server id (default `mx.google.com`). See "Swarm mailbox" |
| `MONEDULA_CONSENT_CHANNEL` | Optional override: `email` \| `telegram` |
| `TELEGRAM_BOT_TOKEN` | Telegram break-glass only |
| `TELEGRAM_CHAT_ID` | Telegram break-glass only |
| `TELEGRAM_ALLOWED_USER_ID` | Operator's Telegram user ID. **Required when Telegram is selected.** Unset or non-numeric ⇒ the gate refuses every reply (`channel_error`) and escalates — it is never a wildcard. See "Who may approve (Telegram)" below. |
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
| `consent_email_needs_swarm_mailbox` | Email consent selected but the swarm mailbox is not configured. No request sent, no mail call made; every payment held. The alert names the variable to fix: `ATELES_SWARM_EMAIL` (distinct from `OPERATOR_EMAIL`) and/or `ATELES_SWARM_GWS_CONFIG_DIR` (an existing gws config dir signed in as the swarm account). See "Swarm mailbox". Notifier key `monedula:consent_email_needs_swarm_mailbox` |
| `consent_reply_sender_rejected` | A reply carried the consent marker but its From: is not the operator's address. Ignored; payments held; nothing is sent back to that sender |
| `consent_reply_unauthenticated` | A reply came from the operator's address but could not be authenticated, so it was **not** accepted; payments held. One notice + escalation per request (Notifier key `monedula:consent_reply_unauthenticated:<fp>:g<gen>`). Check that the reply was sent from the operator's own mailbox, that `ATELES_SWARM_GWS_CONFIG_DIR` is signed in as the swarm account (not the operator's), and that `ATELES_MAIL_AUTHSERV_ID` matches the swarm mailbox's provider; then reply again to the current request |
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
payment when Telegram is selected. It is resolved **before** polling, and the
gate fails closed on every uncertain case:

| Condition | Outcome |
|---|---|
| Message from the configured operator | approval accepted |
| Message from anyone else in the chat | ignored, logged |
| `TELEGRAM_ALLOWED_USER_ID` unset | `channel_error` + escalation, no reply accepted |
| `TELEGRAM_ALLOWED_USER_ID` non-numeric | `channel_error` + escalation |
| Message with no sender id (channel post) | ignored |

Absence of a configured principal is the **absence of authority, never a
wildcard**. Before this was enforced, an unset value skipped the identity check
altogether (`if allowed_user_id and ...` short-circuits on `None`), so any member
of the group topic could authorize an irreversible transfer.

A misconfiguration surfaces as `channel_error` rather than `timeout` on purpose:
#554 exists because a broken gate was indistinguishable from an operator
declining, and a silently-degraded principal check would rebuild that same
failure one layer down.

## Consent gate (ateles#554)

The **only** mechanisms that may authorize a payment are an explicit operator
consent reply: an email reply as described above, or — when Telegram
break-glass is selected — a Telegram attendance reply, parsed by `_parse_reply`
(see `test_parse_reply.py` for the accepted forms — a bare "yes" is
intentionally rejected).

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

On the email path the same separation holds: send, read and configuration
failures are `blocked`, never `skipped` (see "Consent states" above).

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

## Logs

`~/Library/Logs/ateles/monedula.log`

Log events include: `consent_channel_selected`, `consent_request_suppressed`,
`consent_request_send_failed`, `consent_reply_read_failed`,
`consent_reply_sender_rejected`, `consent_reply_unauthenticated`,
`consent_reply_unrecognized`, `consent_email_needs_swarm_mailbox`.
Logs never expose addresses, account identifiers, message bodies, or secrets.

## Idempotency

A `.monedula_last_run` file in the daemon directory records the day the
calendar leg was claimed. Later launchd invocations the same day skip the
calendar fetch (one-off profiles are still evaluated every tick) — preventing
double-payment if launchd retries or the machine wakes mid-day.

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

## Constraints

Standing rules enforced for every payment Monedula makes (see project `CLAUDE.md`):

- **Never hardcode payee data.** IBANs, wallet addresses, amounts, and contact
  details are read from env or parquet — never inlined in code.
- **Yoga payments carry no memo / OP_RETURN.** Do not pass a `memo` on the yoga
  BTC path.
- **Yoga / therapy tasks are never marked completed.** Only the `due_date` is
  advanced — these are recurring obligations, not one-off tasks.
- **Payment paths are idempotent.** Guard against double-send (see Idempotency
  above) on every execution route. On the email consent path that guard is
  the durable payment journal (`.monedula_payment_journal.json`, see
  Idempotency above): an obligation with a recorded outcome is never paid
  again, and one with intent but no outcome is held, never retried.
