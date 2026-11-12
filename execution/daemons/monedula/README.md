# Monedula

Daily payments daemon named after *Corvus monedula* (jackdaw — *moneta* = money).

Runs once per day at 07:00 UTC (09:00 Madrid summer / 08:00 winter) via launchd.
Checks Google Calendar for yesterday's sessions that trigger payment obligations,
asks for **payment consent** (operator authorization UX — not GDPR Art. 7
consent), executes approved payments, and confirms.

**The per-payment approval record** (`payment_approval`, below) is the default
and only automatic consent channel: one Neotoma record per payment, resolved by
the coordinator session with the operator's literal answer. It needs no Telegram
or email credentials. **Email** and **Telegram** are legacy break-glass channels,
selected only by an explicit `MONEDULA_CONSENT_CHANNEL=email|telegram`; the
sections about the swarm mailbox, reply examples and Telegram below apply only
to those. To enable the default channel, follow "Enabling checklist".

> **Legacy (email break-glass only): consent runs through the swarm's own mailbox.** It works once
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

## Payment consent: per-payment approval record (default)

Payment consent no longer travels over Telegram or email replies (operator
ruling 2026-11-12). The default and only automatic channel is `payment_approval`
(`MONEDULA_CONSENT_CHANNEL=checkpoint` is still accepted as an alias):

1. For each session, Monedula writes one Neotoma `payment_approval` entity
   (handler, session, amount, currency, payee display name, rail, reference;
   never an IBAN, BTC address or recipient id; no memo for BTC) and reads it
   back. It is raised once per payment.
2. The coordinator session lists `payment_approval` entities with
   `status == "awaiting_operator"`, puts each to the operator, and corrects
   two fields: `operator_answer` (the literal answer, written FIRST) and then
   `status` (`approved` / `rejected`).
3. Monedula pays only for `approved` with an answer that passes the
   attendance parser (`attended <handler>` / `yes <handler>`; a bare `yes`
   does not approve) and whose stored fields match the payment about to be
   made. Anything else, or Neotoma being unreachable, pays nothing.

### Coordinator resolver contract

- **Entity and filter:** `entity_type: payment_approval`, `status == "awaiting_operator"`.
- **Order of corrections:** `operator_answer` (the operator's literal text) first,
  then `status` (`approved` or `rejected`). Writing `status` first leaves an
  approval with no authenticated answer, which is held.
- **Identity:** both corrections must be made by the resolver principal whose AAuth
  sub and key thumbprint are configured for Monedula as
  `MONEDULA_PAYMENT_APPROVER_SUB` and `MONEDULA_PAYMENT_APPROVER_JKT` (both
  required; the thumbprint is the 43-character RFC 7638 value). Monedula does not
  trust the snapshot: it reads `status` and `operator_answer` through the
  observations their field provenance names, with the same verifier the
  checkpoint release path uses
  (`gating.read_authenticated_field_value`, a field-name-parameterised form of
  `read_authenticated_checkpoint_resolution`). A write by any other principal,
  an untrusted attribution tier, a different tenant, or an unset approver
  configuration holds the payment. Any later write to either field by someone
  else withdraws the approval.
- **Recovery:** a record with `status` approved but no (or a wrong) answer is held and
  alerted per payment; correct `operator_answer` on the SAME record (as the
  configured resolver), then it is re-read on the next tick.
- **Operator alerts (all deduped per payment key):** a blocker when a record is raised
  (handler, session date, amount, short key), a blocker when it has been
  `awaiting_operator` longer than `MONEDULA_APPROVAL_STALE_HOURS` (default 24), a
  blocker per held payment naming the reason, and a warning-level notice when a rejection skips a
  session. A coordinator that is not watching therefore shows up as an alert, not
  silence.
- **A changed record stays blocked.** If the amount, payee, reference, session
  or rail of the payment changes after the record was raised, the record no
  longer matches and the payment is held permanently; reject the stale record
  and a fresh one is raised under the new obligation key (a changed amount or
  payee changes the key). Bound fields are never re-bound in place.
- **One bad record holds only its own payment**; other payees proceed. Only when
  no record can be read at all (Neotoma down) is the tick left unclaimed.
- **Approver identity (this deployment):** the resolver principal is
  `payment-approver@ateles-swarm`, key thumbprint
  `LmdMZ-VRVsRCt-daCQTWfMBUx92kfMZ3L32tsYxYfPU` (a public thumbprint), with the key
  minted at `ateles-private/keys/payment-approver.jwk.json`. Set
  `MONEDULA_PAYMENT_APPROVER_SUB` / `MONEDULA_PAYMENT_APPROVER_JKT` on the Monedula
  daemon to those two values. The resolver writes its corrections with
  `neotoma request --aauth`, with `NEOTOMA_AAUTH_PRIVATE_JWK_PATH`,
  `NEOTOMA_AAUTH_SUB` and `NEOTOMA_AAUTH_ISS` set for that key. These values are
  configuration only; nothing in the code carries them.
- **Grants outside the code:** the `payment-approver` key needs its Neotoma grant
  (Mark's), and the Wise token must be present for the therapy profile (yoga pays by
  BTC and needs none). Without either, nothing is approved or paid.
- **Schema first:** run
  `python3 execution/scripts/register_payment_approval_schema.py` (dry run) then
  `--apply` before Monedula first runs with this channel; without the schema every
  raise fails its read-back and all payments are held.
- **Surfacing check (before enabling):** with the coordinator session open, let
  Monedula raise one record (or create a synthetic `awaiting_operator` record) and
  confirm both that the coordinator lists it and puts it to the operator in the
  session the operator uses, AND that the "raised" blocker actually arrived on the
  operator's notification channel (the alert path depends on the notifier having a
  working transport; an undeliverable alert is logged at error level). Record the
  result. Until that is shown, sessions can sit in `awaiting` unnoticed.
- **After payment:** the operator is told the outcome per payment: a warning-level notice when sent (the Notifier drops INFO, so no INFO alert is promised anywhere);
  a blocker when manual action is needed or the transfer did not complete (it is
  never retried automatically). Payee account details are never put in an alert.
- **Calendars:** every calendar the account can read is scanned, including shared
  calendars others can write to. Events on them can raise approval requests;
  nothing is paid without the operator's per-payment answer.
- **Cutover:** a session already paid under the pre-window obligation key is
  recognised by that legacy key and marked settled, never raised again.

`payment_approval` is deliberately not a `checkpoint_brief`: approving a brief
releases its task to the Apis dispatcher. Apis subscribes only to `task` and
`checkpoint_brief` and the record carries no task reference, so only Monedula
consumes the approval. `MONEDULA_CONSENT_CHANNEL=email|telegram` remains as an
explicit legacy break-glass override and is never chosen automatically.

### Enabling checklist (in order)

1. **Register the schema.** `python3 execution/scripts/register_payment_approval_schema.py`
   (dry run), then `... --apply`. Verify: the script prints
   `registered and verified: payment_approval`.
2. **Set the approver identity on the Monedula daemon** (its launchd environment or
   the private env it loads): `MONEDULA_PAYMENT_APPROVER_SUB` and
   `MONEDULA_PAYMENT_APPROVER_JKT`, to the values in "Approver identity" above.
   Verify: restart the daemon and read the variables back from the running process
   (never print the key file).
3. **Confirm the grants** (Mark's): the `payment-approver` key's Neotoma grant, and
   the Wise token for therapy. Verify: the resolver's `neotoma request --aauth`
   correction of a test record succeeds.
4. **Run the surfacing check** above. Verify: you received the raised blocker.
5. **Enable** by leaving `MONEDULA_CONSENT_CHANNEL` unset.

**Answering without a coordinator session.** If no coordinator is watching, the
operator can resolve the record by hand as the resolver principal: correct
`operator_answer` to `attended <handler>` (or `yes <handler>`), then correct
`status` to `approved` (or `rejected` to skip), in that order, using
`neotoma request --aauth` with the resolver key. Verify: the next tick sends the
"payment SENT" or outcome alert.

## Session timing

Every tick reads all calendars (`calendarList list`, then `events list` per
calendar, Europe/Madrid) and evaluates sessions that ended within
`MONEDULA_SESSION_WINDOW_HOURS` (default 24). A session is raised once,
recorded as pending in the payment journal keyed on (handler, event id), and
stays pending until approved, skipped or paid, even across restarts, midnight
and a failed profile load. A failed read of any one calendar fails the whole
fetch (never "no sessions").

## Swarm mailbox (legacy email break-glass)

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
| `MONEDULA_CONSENT_CHANNEL` unset | Per-payment approval record (`payment_approval`) | `automatic` |
| `MONEDULA_CONSENT_CHANNEL=payment_approval` (or the alias `checkpoint`) | Per-payment approval record | `env` |
| `MONEDULA_CONSENT_CHANNEL=email` | Email consent (legacy break-glass) | `env` |
| `MONEDULA_CONSENT_CHANNEL=telegram` | Telegram break-glass (legacy) | `env` |
| Email selected but swarm mailbox not configured | Blocker (`consent_email_needs_swarm_mailbox`) — no request sent, no mail call made | — |

Email and Telegram are never chosen automatically. The approval record needs no
Telegram or email credentials, only the approver identity below.

Never a silent no-op when payments are pending.

## Consent states

| State | Meaning |
|-------|---------|
| `awaiting_approval` | Record raised (or request sent) and held; no answer yet (silence ≠ decline) |
| `approved` | Record `approved` with an authenticated answer naming the session (`attended <handler>` / `yes <handler>`); legacy email: explicit ATTENDED |
| `skipped` | Record `rejected` by the approver; legacy email: explicit `SKIP` |
| `blocked` | Unauthenticated, mismatched, unreadable or unraisable record, or a channel/config/send/read failure — no payment moved |

Missing config, transport failure, unread inbox, and no reply never become
`skipped`.

## Environment variables

Loaded automatically from `~/.config/neotoma/.env` at startup.

Default channel (`payment_approval`):

| Variable | Purpose |
|----------|---------|
| `MONEDULA_PAYMENT_APPROVER_SUB` / `MONEDULA_PAYMENT_APPROVER_JKT` | **Required.** The resolver principal and its 43-character key thumbprint; unset means nothing can be approved |
| `MONEDULA_APPROVAL_STALE_HOURS` | Hours an unanswered record waits before a stale blocker, repeated every further interval (default `24`) |
| `MONEDULA_SESSION_WINDOW_HOURS` | Rolling session window (default `24`) |
| `MONEDULA_CONSENT_CHANNEL` | Optional: `payment_approval` (alias `checkpoint`) \| `email` \| `telegram`; unset means `payment_approval` |
| `WISE_API_TOKEN` | Wise API bearer token (therapy; yoga pays by BTC) |
| `DATA_DIR` | Path to data directory (for contacts.parquet) |

Legacy break-glass channels (email, Telegram) only:

| Variable | Purpose |
|----------|---------|
| `ATELES_NOTIFY_EMAIL` | `"1"` arms `lib.approval` email send/read for Monedula consent |
| `OPERATOR_EMAIL` | Consent request recipient (To:); exact-match From: for replies |
| `ATELES_SWARM_EMAIL` | **Required for email consent.** The swarm mailbox's address; must differ from `OPERATOR_EMAIL`. See "Swarm mailbox" |
| `ATELES_SWARM_GWS_CONFIG_DIR` | **Required for email consent.** gws config directory signed in as the swarm mailbox; must exist. See "Swarm mailbox" |
| `ATELES_MAIL_AUTHSERV_ID` | Optional trusted receiving-server id (default `mx.google.com`). See "Swarm mailbox" |
| `TELEGRAM_BOT_TOKEN` | Telegram break-glass only |
| `TELEGRAM_CHAT_ID` | Telegram break-glass only |
| `TELEGRAM_ALLOWED_USER_ID` | Operator's Telegram user ID. **Required when Telegram is selected.** Unset or non-numeric ⇒ the gate refuses every reply (`channel_error`) and escalates — it is never a wildcard. See "Who may approve (Telegram)" below. |
| `TELEGRAM_TOPIC_PAYMENTS` | Thread ID for payments topic |
| `MONEDULA_DEAD_GATE_THRESHOLD` | Consecutive channel failures before dead-gate alarm (default `3`) |

## Email consent (legacy break-glass) — request / reply examples (synthetic)

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
| `payment_approval_unavailable` | Neotoma could not be read or written for a payment approval (or the `payment_approval` schema is not registered); that payment is held and the next tick retries |
| `consent_request_send_failed` | Request not delivered; payments blocked; next tick retries; check `gws gmail +send` |
| `consent_reply_read_failed` | Replies could not be checked; no payment executed; check `gws`/mailbox |
| `consent_email_needs_swarm_mailbox` | Email consent selected but the swarm mailbox is not configured. No request sent, no mail call made; every payment held. The alert names the variable to fix: `ATELES_SWARM_EMAIL` (distinct from `OPERATOR_EMAIL`) and/or `ATELES_SWARM_GWS_CONFIG_DIR` (an existing gws config dir signed in as the swarm account). See "Swarm mailbox". Notifier key `monedula:consent_email_needs_swarm_mailbox` |
| `consent_reply_sender_rejected` | A reply carried the consent marker but its From: is not the operator's address. Ignored; payments held; nothing is sent back to that sender |
| `consent_reply_unauthenticated` | A reply came from the operator's address but could not be authenticated, so it was **not** accepted; payments held. One notice + escalation per request (Notifier key `monedula:consent_reply_unauthenticated:<fp>:g<gen>`). Check that the reply was sent from the operator's own mailbox, that `ATELES_SWARM_GWS_CONFIG_DIR` is signed in as the swarm account (not the operator's), and that `ATELES_MAIL_AUTHSERV_ID` matches the swarm mailbox's provider; then reply again to the current request |
| `consent_reply_unrecognized` | Authenticated but unparseable line; hold item; correction mail explains form |
| `payment_journal_unreadable` | The payment journal could not be read or written; every payment held; inspect `.monedula_payment_journal.json` before anything is paid |
| `payment_outcome_unknown` | A transfer was attempted and its outcome never recorded (crash or rail error mid-transfer). Held and never retried automatically; check the rail, then resolve by hand |
| `payment_approval_not_accepted` | A record exists but does not permit payment: not written by the configured approver, approved with no answer, answer does not name the session (a bare yes does not approve), or it does not match the payment. The alert names the reason and the record's short key; fix the SAME record (correct `operator_answer` as the resolver) |
| `invalid_wise_recipient_id` | A profile's `wise_recipient_id` is not a positive integer (Neotoma profile or env `<PREFIX>_WISE_RECIPIENT_ID`). Stranded alert; payments for it are held until it is fixed |
| `payment_outcome` (alert key `monedula:payment_outcome:<obligation>`) | After an approved payment ran: a warning-level notice for `sent` (the lowest priority the Notifier delivers); blocker for `manual_required`, failed or unreported, saying it was NOT paid and will not be retried. Pay by hand from the payment profile, then reconcile |
| `payment_terms_changed` | The payee or amount changed after approval, so the approval no longer applies; nothing paid; a fresh request is raised |
| `payment_profiles_stranded` | Fix the named payment_profile config; Notifier key `monedula:stranded:<fp>` |

Consent-channel-failure alert key: `monedula:consent_channel_failed`
(`email_eligible=True`, actionable body).

## Telegram break-glass notes (legacy)

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
consent answer, and which one applies depends on the channel:

- **Default (`payment_approval`):** a `payment_approval` record whose `status` is
  `approved` and whose `operator_answer` names the attended session
  (`attended <handler>` / `yes <handler>`), both written by the configured
  approver (`MONEDULA_PAYMENT_APPROVER_SUB` / `_JKT`) and read through their field
  provenance, and whose bound fields match the payment about to be made. A bare
  "yes" does not approve. See "Payment consent: per-payment approval record".
- **Legacy break-glass, only when explicitly selected** with
  `MONEDULA_CONSENT_CHANNEL=email|telegram`: an email reply as described above, or
  a Telegram attendance reply, parsed by `_parse_reply` (see `test_parse_reply.py`
  for the accepted forms; a bare "yes" is intentionally rejected here too).

### Telegram break-glass only

Everything in the rest of this section describes the legacy Telegram and email
channels. It does not apply to the default `payment_approval` channel, whose
failures show up as the alerts in the troubleshooting table.

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

**Dead-gate alarm (Telegram break-glass only).** `MONEDULA_DEAD_GATE_THRESHOLD` (default 3) consecutive
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

Log events include, for the default channel: `consent_channel_selected`,
`payment_approval_raised`, `payment_approval_unavailable`,
`payment_approval_not_accepted`, `payment_not_completed`,
`payment_preflight_failed`; and for the legacy email channel:
`consent_request_suppressed`,
`consent_request_send_failed`, `consent_reply_read_failed`,
`consent_reply_sender_rejected`, `consent_reply_unauthenticated`,
`consent_reply_unrecognized`, `consent_email_needs_swarm_mailbox`.
Logs never expose addresses, account identifiers, message bodies, or secrets.

## Idempotency

A `.monedula_last_run` file in the daemon directory records the day the
calendar leg was claimed. Later launchd invocations the same day skip the
calendar fetch (one-off profiles are still evaluated every tick) — preventing
double-payment if launchd retries or the machine wakes mid-day.

Local consent mark (legacy email channel only): `.monedula_consent_email.json` —
fingerprint, request generation and `sent_at` only; cleared when a pending set
ends. The default channel keeps no such file; its state is the Neotoma
`payment_approval` record plus the payment journal below.

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
  above) on every execution route. On every consent channel, the default
  `payment_approval` included, that guard is the durable payment journal (`.monedula_payment_journal.json`, see
  Idempotency above): an obligation with a recorded outcome is never paid
  again, and one with intent but no outcome is held, never retried.
