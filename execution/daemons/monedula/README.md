# Monedula

Recurring payments daemon named after *Corvus monedula* (jackdaw — *moneta* = money).

Runs on a ~15-minute poll via launchd (`StartInterval=900`). Each tick:

1. Fetches calendar events covering a rolling lookback window (default 168h)
   and selects sessions whose END time has already passed, that match an
   active payment profile, and that have no notified-marker yet
   ("event-end detection").
2. Evaluates one-off (due-date-triggered) invoices independently of the
   calendar fetch, every tick.
3. Sends ONE approval email per newly-triggered payment (attendance
   confirmation + preview for a recurring session; invoice preview for a
   one-off) and records a pending-approval marker.
4. Sweeps every marker still awaiting approval via
   `lib/approval/email_channel.py` (the swarm's standardized email-reply-
   to-approve module, ateles#276/#277): executes the payment on approval,
   rolls the linked task's `due_date` either way, and never re-notifies or
   double-pays a session already marked paid/skipped.

Email is the ONLY operator-facing channel for payment previews, approvals,
and confirmations. Telegram has been removed from this path entirely.

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

A payment profile with a `due_date` and no calendar trigger is a **one-off
invoice** (e.g. a rental payment) — it fires on the date alone, evaluated
every tick independent of the calendar leg, and is archived after a
successful send.

## Approval

Every payment — recurring or one-off — requires an explicit operator email
reply before it executes. The approval email carries a session-scoped token
(`[APPROVE-XXXXXXXX]`, from `lib/approval/tokens.token_for`) in its subject;
a reply only counts as a verdict for that specific session if the token is
echoed back (quoted subjects carry it automatically on a normal Reply).

- **Recurring sessions are attendance-gated**: a calendar event is not proof
  of attendance, so a bare "yes"/"approve" is NOT accepted. The operator
  must reply starting with **ATTENDED** or **PAID** to approve, or **SKIP**
  to decline (no payment, due date rolls forward). This preserves the gate
  main previously enforced over Telegram (`test_parse_reply.py`'s contract,
  now implemented by `monedula._parse_attendance_verdict`).
- **One-off invoices** have no session to attend, so a plain **APPROVE**
  reply is accepted.

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
| `ATELES_NOTIFY_EMAIL` | Must be `"1"` to arm the email approval channel — the daemon refuses to run a tick otherwise |
| `OPERATOR_EMAIL` / `MONEDULA_OPERATOR_EMAIL` | Operator's address for approval requests and confirmations |
| `ATELES_SWARM_EMAIL` | Optional `From:` address for outbound approval mail |
| `MONEDULA_LOOKBACK_HOURS` | Calendar lookback window for ended-session detection (default 168) |
| `MONEDULA_MAX_NOTIFY_PER_RUN` | Hard cap on notify emails sent in one tick (default 6) |
| `WISE_API_TOKEN` | Wise API bearer token |
| `NEOTOMA_BASE_URL` / `NEOTOMA_BEARER_TOKEN` | Neotoma API — primary source for Wise recipient contact (name/IBAN/wise_recipient_id) via profile.contact_id, and for payment_profile loading |
| `DATA_DIR` | Path to data directory (for legacy contacts.parquet fallback only — not required when Neotoma has the contact) |

## Logs

`~/Library/Logs/ateles/monedula.log` (rotating + repeat-suppressing —
see `lib/daemon_runtime/logging_setup.py`).

## Idempotency

Each triggered payment gets a marker (`.monedula_pending.json`, keyed by
event id + session date) that tracks `awaiting_approval -> approved -> paid`
(or `-> skipped`). A session is never re-notified once a marker exists, and
`_handle_approve` re-reads the marker immediately before executing to guard
against a race between ticks.

## Strandings

An ACTIVE payment profile the daemon cannot act on (missing trigger, bad
amount, unreachable Neotoma, …) is escalated as a Neotoma `escalation`
entity rather than silently warned-and-skipped — see `strandings.py`. A tick
with a stranded profile reports a non-zero exit even if there was otherwise
nothing else to do.

## Constraints

Standing rules enforced for every payment Monedula makes (see project `CLAUDE.md`):

- **Never hardcode payee data.** IBANs, wallet addresses, amounts, and contact
  details are read from Neotoma or env (contacts.parquet as legacy fallback
  only) — never inlined in code.
- **Yoga payments carry no memo / OP_RETURN.** Do not pass a `memo` on the yoga
  BTC path.
- **Yoga / therapy tasks are never marked completed.** Only the `due_date` is
  advanced — these are recurring obligations, not one-off tasks.
- **Payment paths are idempotent.** Guard against double-send (see
  Idempotency above) on every execution route.
- **Approval is per-payment and attendance-gated for recurring sessions.**
  See Approval above — this is a hard limit, not a UX preference.
