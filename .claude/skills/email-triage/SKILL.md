---
name: email-triage
description: "Step-by-step email inbox triage workflow with draft generation, data persistence, and archiving. Use when processing emails, triaging inbox, or when user mentions email triage, inbox processing, or email workflow."
triggers:
  - triage inbox
  - process emails
  - email workflow
  - go through inbox
  - handle emails
  - email triage
  - inbox processing
user_invocable: true
entity_id: ent_e3f5f239427961d2f4608208
---

# Triage Email

Full inbox triage: read all emails, categorize them, store every email as an entity in Neotoma, draft replies for actionable emails, confirm with the user, send approved replies, and archive no-action emails. Supports snoozing: snoozed emails are starred, archived, and stored in Neotoma with a `snoozed_until` date; they resurface automatically on the next triage run when the date is due.

## Prerequisites

- `gws` CLI must be available (`which gws`). This is the required Gmail interface — do NOT use the Gmail MCP server.
- Neotoma prod MCP must be connected (`mcp__mcpsrv_neotoma__*`). Verify with `get_session_identity`.

## Phase 0 — Check for snoozed emails due today

**Before pulling the inbox**, find email entities whose CURRENT snapshot still carries a live `snoozed_until` date on or before today. Query server-side on the snapshot field, not by lexical search:

`retrieve_entities(entity_type=\"email\", snapshot_filters={ \"snoozed_until\": { \"op\": \"lte\", \"value\": \"<today YYYY-MM-DD>\" } })`

`snapshot_filters` matches only entities whose computed snapshot currently contains a non-null `snoozed_until` ≤ today, so a cleared snooze (value set back to null/absent) is excluded at the source.

**Never surface a cleared snooze.** Do NOT use `search=\"snoozed_until\"` — lexical search matches any email whose *historical observations* ever mentioned snoozing, including ones already cleared, and re-surfaces them every run. If for any reason you fall back to a broader query, drop every result whose current snapshot `snoozed_until` is null, absent, or in the future BEFORE doing anything with it — a cleared or not-yet-due snooze is invisible to triage and is never mentioned to the user, not even to note that it was skipped.

For each genuine match (live snapshot `snoozed_until` ≤ today), fetch the Gmail message by its `message_id` field. Include these messages in the triage, clearly labeled **\"🔔 Snoozed (due today/overdue)\"**, and treat them as actionable. After the user acts on them, clear the `snoozed_until` by re-storing the entity with `snoozed_until: null`.

## Phase 1 — Pull the inbox

List all inbox messages (paginated): `gws gmail users messages list --params '{\"userId\":\"me\",\"labelIds\":[\"INBOX\"],\"maxResults\":500}'` (follow `nextPageToken`). Collect all message IDs, tell the user the count, then batch-fetch metadata (From, To, Subject, Date headers + labels + snippet) for all IDs with a Python script over `gws gmail users messages get` with `format: metadata`, saving to a temp file (e.g. `/tmp/inbox_meta.json`).

## Phase 2 — Categorize

Based on sender, subject, snippet, and labels, sort each email into: **Actionable** (personal email needing a reply or careful read; pending decisions; meeting requests), **Awaiting reply** (you sent something and haven't heard back), **Informational** (newsletters, digests, release notes), **No-action** (automated notifications, receipts, marketing, CI/CD alerts, OTP codes, GitHub noise).

**Storage floor — there is no purely \"no action\" outcome.** Every thread, including marketing, CI noise, and receipts, is stored in Neotoma as an `email` entity at minimum (plus any derivable transaction/event/contact entities). \"No-action\" means only: no reply drafted; store + archive.

Heuristics: GitHub CI/bot notifications → no-action. Receipts/paid confirmations → no-action (but extract transaction entity). **Invoices with outstanding payment due → Actionable**: reply to acknowledge receipt + intent to pay, keep in inbox, create a payment `task` + `transaction`; do not archive until paid. Emails from real people → actionable or awaiting-reply. Promotional review-request emails → no-action.

Present a category-count summary table before proceeding.

## Phase 3 — Read and display actionable emails

**Check sent history before drafting**: fetch the full thread (`gws gmail users threads get`) and inspect every message's `labelIds`. **CRITICAL — a `DRAFT` is NOT a `SENT` reply:** only a message bearing the `SENT` label and newer than the actionable inbound counts as \"already replied\"; a message bearing the `DRAFT` label is an *unsent* draft and must NOT be treated as a reply. Never collapse `DRAFT` into \"sent\"/\"replied\", and when printing a thread summary never bucket messages as just \"sent\" vs \"received\" — surface the `DRAFT` label distinctly, or you risk misreading an unsent draft as a completed reply. If a newer `SENT` reply exists, skip drafting and downgrade (\"✅ Already replied — skipping\"). If an unsent `DRAFT` already exists in the thread (DRAFT label, no newer SENT), do NOT create a competing draft — surface the existing draft for review/send and link its `draft_message` entity. For each actionable email not yet replied to (no SENT, no DRAFT), fetch the full body (`format: full`, base64url-decode), display it (sender, subject, date, body), and draft a reply.

Drafting rules:
- Match the register and language of the original email.
- Be concise; no filler phrases.
- Preserve commitments, dates, and requests from prior messages; don't contradict or repeat them.
- **Use retrieved context**: before drafting, consult the Neotoma records retrieved for the thread (contact notes, prior emails, plans, payment profiles) and external tools where relevant — for any scheduling thread, check Google Calendar via `gws calendar events list` (Europe/Madrid) for the relevant date range so the draft reflects real availability and existing invites.
- **Spanish sign-offs**: \"Un saludo\" or \"Saludos\" for professional contacts; \"Un abrazo\" only for close personal contacts; never \"Saludos cordiales\".
- **Store drafts in Neotoma, don't paste them in chat**: store every drafted reply as a `draft_message` entity (fields: `title`, `to_name`, `to_email`, `subject`, `body`, `status: \"pending_approval\"`, `recipient_contact_id` when known, `notes` with the in-reply-to Gmail message ID and thread ID). In chat, show only the recipient, a one-line gist, and the Neotoma inspector link (`<origin>/inspector/entities/<entity_id>`) so the user opens the full draft in the Neotoma app. After sending, correct the draft entity's `status` to `\"sent\"`; if the user discards it, correct to `\"discarded\"`.

## Phase 4 — Store entities in Neotoma

**Every email processed during triage** must be stored as an `email` entity (check `retrieve_entity_by_identifier` on the message ID first). Declared schema fields: `message_id` (\"gmail:<id>\"), `subject`, `from_address`, `from_name`, `to` (string), `received_at` (ISO 8601), `summary` (1–2 sentences), `thread_id`. Non-schema fields (`data_source`, `snoozed_until`) are preserved in raw_fragments.

Also extract and store derived entities:
- **Contacts**: sender + named recipients (check existing first).
- **Transactions**: invoices, receipts, payment confirmations.
- **Events**: meetings/visits/bookings — always include full email-body context in `description` (attendees + roles, purpose/agenda, docs/links shared, thread background, source message ID).
- **Tasks**: action items, commitments, promises. Invoices with outstanding payment must generate a `task` (status open, priority medium, due ~1 week) plus the `transaction`.
- **Payment profiles**: for invoices from known vendors, create/update `payment_profile` (label, payment_type \"wise\", amount_eur, wise_reference, contact_id, calendar_keywords, neotoma_task_id, status \"active\"). If vendor IBAN unknown, note in task and ask before creating.
- **Organizations/Companies**: companies mentioned as senders or subjects of substantive threads.
- **Drafts**: every drafted reply as a `draft_message` entity (`status: \"pending_approval\"`) per the drafting rules above — the per-thread table links to these.

Provenance: every entity gets `data_source: \"Gmail message id=<message_id> <ISO-date>\"` with the specific message ID. Use Neotoma prod exclusively. Batch entities into store calls; repair any `unknown_fields_count > 0` immediately by re-storing with declared schema fields.

## Phase 4b — Per-thread triage table

After storing, present **one table listing every inbox thread** — group messages by Gmail `threadId`, one row per thread. This is the central triage artifact; the user approves all actions from it. Columns:

| Column | Content |
|---|---|
| **Category** | Actionable / Awaiting reply / Informational / No-action (see Phase 2) |
| **Thread** | Subject (truncated) + sender |
| **New?** | Whether the thread had no prior Neotoma `email` entity before this run (the store call's `action: created` vs `matched_existing`/`extended` tells you) |
| **Retrieved** | Neotoma records retrieved for context on this thread (contacts, prior emails, plans, payment profiles, …) |
| **Created/Updated** | Neotoma records created or updated from this thread (email, transaction, event, contact, organization, …) |
| **Draft?** | ✏️ + a markdown link to the stored `draft_message` entity in the Neotoma inspector (`<origin>/inspector/entities/<id>`) if a reply is drafted; — if none warranted. Do not paste full draft bodies in chat — the link is how the user reads the draft. Drafts must be informed by the retrieved Neotoma records AND external context where relevant (e.g. Google Calendar via `gws` for scheduling threads) |
| **Other ops** | Non-Neotoma operations staged or warranted: calendar event move/create, archive, payment execution, follow-up nudge, … (staged only — never executed before confirmation) |
| **Tasks** | `task` entities created or updated specifically as a result of this thread |
| **People/Orgs** | People and organizations involved in the thread, each annotated with how it was touched in Neotoma: (c)reated, (u)pdated, or (r)etrieved |

Rules:
- **Order rows by Category** — group the table in this order: Actionable, Awaiting reply, Informational, No-action. Keep the Category column so the grouping is explicit.
- **Link every named Neotoma entity in every cell** — every entity referenced in the Retrieved, Created/Updated, Draft?, Tasks, and People/Orgs columns must be a markdown link to its Neotoma inspector page (`<origin>/inspector/entities/<entity_id>`), not bare text. The store-call results give you each `entity_id`. Keep the (c)/(u)/(r) annotation alongside the link. Only senders deliberately not stored as entities (bulk CI bots, marketing platforms) may appear as plain text.
- **Every inbox thread appears in the table** — including CI noise, marketing, and receipts. Homogeneous noise threads (e.g. GitHub CI notifications from the same repo) may be grouped into one summary row with a count, but counts must reconcile with the inbox total.
- For scheduling threads, check Google Calendar (`gws calendar events list` with `Europe/Madrid`) for the relevant date range before drafting, and stage any calendar move/create in **Other ops**.
- Substantive threads should have their people/orgs stored as contact/organization entities; bulk noise senders (CI bots, marketing platforms) may be named in the table without dedicated organization entities unless they matter to the user's graph.

## Phase 5 — Confirm before acting

Before sending any reply or archiving any email, show the user the drafted replies (recipient + subject + Neotoma inspector link) and the archive list (count + categories). Ask for explicit confirmation.

**Verify recipient before sending**: read the actual From/To headers of the message being replied to (`format: metadata`) and confirm the expected person. Send with `gws gmail +reply --message-id <id> --body \"<text>\"`, then verify the sent message's `threadId` matches the expected thread via the SENT label listing. After sending, correct the `draft_message` entity's `status` to `\"sent\"`.

**Clear snooze after acting**: if an actioned email has a live `snoozed_until` set in Neotoma, clear it immediately by re-storing with `snoozed_until: null`. Check on every actioned email, not just Phase 0 ones. Once cleared, the email is an ordinary stored record — it must not be surfaced as snoozed on any later run.

## Phase 6 — Archive no-action emails

Archive by removing the INBOX label: `gws gmail users messages modify --params '{\"userId\":\"me\",\"id\":\"<id>\"}' --json '{\"removeLabelIds\":[\"INBOX\"]}'` (script for bulk). Report \"Archived N emails.\"

## Phase 6b — Snooze an email

1. Star it (`addLabelIds: [\"STARRED\"]`), 2. archive it (`removeLabelIds: [\"INBOX\"]`), 3. store/update the email entity with `snoozed_until: \"YYYY-MM-DD\"`. Confirm: \"Snoozed until <date> — starred and archived.\"

## Do not

- **Send replies or archive without explicit user confirmation** — always ask first.
- **Treat any thread as purely \"no action\"** — every thread is stored in Neotoma as an `email` entity at minimum; \"no-action\" only ever means no reply + archive after storage.
- **Skip the per-thread triage table** — every inbox thread must appear in the Phase 4b table with its retrieved/created records, draft status, staged ops, tasks, and people/orgs.
- **Leave entity mentions in the table unlinked** — every Neotoma entity named in a table cell must link to its inspector page; rows must be ordered by Category with a Category column.
- **Skip recipient verification** — wrong-thread replies are silent errors that are hard to undo.
- **Use the Gmail MCP server** — always use `gws gmail` CLI.
- **Use the Neotoma dev instance** — always use prod.
- **Create duplicate contacts** — check for existing entities before storing new ones.
- **Skip provenance** — every entity needs a `data_source` tied to a specific message ID.
- **Echo full email bodies in the reply** — summarize; store the full content in Neotoma.
- **Paste full draft bodies in chat** — store each draft as a `draft_message` entity and link to its Neotoma inspector page from the table's Draft column.
- **Archive emails that may need action** — when in doubt, surface to the user.
- **Archive invoices with outstanding payment** — keep in inbox until payment confirmed.
- **Treat invoices as no-action** — always draft a receipt-acknowledgement reply.
- **Use \"un abrazo\" in professional emails** — business contacts get \"Un saludo\"/\"Saludos\".
- **Store meeting events with only calendar metadata** — include email-body context in `description`.
- **Draft a reply without checking sent history first.**
- **Treat a `DRAFT` in the thread as a `SENT` reply** — read `labelIds` explicitly; a draft never satisfies the reply gate, and an existing draft must be surfaced for review/send, not duplicated.
- **Surface a snoozed email whose snooze has been cleared** — Phase 0 must filter on the live snapshot `snoozed_until` (non-null, ≤ today); never re-surface an email whose snooze is null/absent/future, and never mention it (not even to say it was skipped). Do not use lexical `search=\"snoozed_until\"`, which re-matches cleared snoozes from history.
- **Leave snoozed_until set after acting.**
- **Use wrong archive syntax** — `--params` for id, `--json` for `removeLabelIds`.

## Edge cases

- Emails in other languages: draft replies in the same language.
- Very old emails: flag — the action window may have passed.
- Anthropic billing/API warnings: flag for direct action at console.anthropic.com.
- Meeting confirmations awaiting reply: actionable even if automated-looking.
- CC'd (not To): lower priority; surface but don't draft unless clearly needed.
