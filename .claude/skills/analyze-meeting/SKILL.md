---
name: analyze-meeting
description: "General-purpose meeting analysis. Reads a transcript (file path, transcription entity, or pasted text), extracts a structured analysis (summary, decisions, action items, open questions), persists records to Neotoma (meeting_analysis, task, recap_message, proposed_github_issue), drafts recap messages per participant (email via Gmail when address is known, otherwise generic message text), looks up the corresponding Google Calendar event by recording time to resolve participant emails and store a linked calendar_event entity, and (opt-in) opens public follow-up issues in relevant repos with PII scrubbed. Designed to be auto-invoked by /record_meeting on stop, but also runs standalone."
triggers:
  - analyze meeting
  - analyze this meeting
  - meeting analysis
  - meeting recap
  - /analyze-meeting
user_invocable: true
entity_id: ent_c6077840664ee87ae30b7922
---

# Analyze Meeting

Produce a structured, actionable analysis of a meeting transcript and stage all follow-ups (Neotoma tasks, drafted recap emails, proposed public issues). Complement to [`analyze-neotoma-feedback`](../analyze-neotoma-feedback/SKILL.md): that skill is Neotoma-customer-development-specific; this one handles any meeting type and focuses on follow-through rather than positioning analysis.

When both skills fire on the same transcript (typical for a Neotoma evaluator call), this skill produces the operational follow-up; `analyze-neotoma-feedback` produces the customer-development analysis. They do not duplicate each other — they are linked via shared `transcription` and `contact` entities.

## When to use

- Auto-invoked by [`record-meeting`](../record_meeting/SKILL.md) after a successful stop+transcribe.
- Invoked manually with `/analyze-meeting <source>` for an existing transcript on disk or a Neotoma `transcription` entity id.
- Skip silently when the transcript is empty, was clearly not a meeting (e.g. solo voice memo without action content), or when `RECORD_MEETING_AUTO_ANALYZE_MEETING=0` is set in env.

## Invocation

```
/analyze-meeting <source>
/analyze-meeting <source> --open-issues          # open real GH issues instead of staging drafts
/analyze-meeting <source> --no-email             # skip Gmail draft staging
/analyze-meeting <source> --participants "Alice <alice@x>, Bob <bob@y>"
```

`<source>` is auto-detected:
- **Absolute file path** (exists on disk) — read the file. Handles transcripts produced by `transcribe_audio.py`, including `last_meeting_transcription.txt`.
- **Neotoma entity reference** — a `transcription` entity id, or a name / canonical identifier resolvable to a transcription via `retrieve_entity_by_identifier`.
- **Raw pasted text** — anything that doesn't match the above.

Flags:
- `--open-issues` — actually open public issues in relevant repos via the GitHub MCP. Default is **off**: issues are staged as `proposed_github_issue` entities in Neotoma and written to the local report only. Same effect as setting `MEETING_ANALYSIS_OPEN_GH_ISSUES=1` in env.
- `--no-recap` — skip all recap drafting (neither email nor generic message). `recap_message` entities still go to Neotoma.
- `--participants` — comma-separated `Name <email>` overrides when speaker labels in the transcript are unreliable (diarization missed names, etc.).

## Step 1: Resolve source + identify participants

1. Resolve the source per the input modes above. Capture raw transcript text.
2. Resolve associated `transcription` entity (when the source is a file produced by `transcribe_audio.py`, search Neotoma by `audio_file_path` per `is_already_transcribed` in `execution/scripts/transcribe_audio.py`). Reuse if present; otherwise note that no `transcription` entity is linked (skill still proceeds — meeting_analysis stands alone).
3. **Identify participants:**
   - Speaker labels in diarized transcripts (`[Speaker 1]`, `[Mark]`, `[System]`/`[Mic]`).
   - Name mentions in transcript ("Thanks, Alice"), salutations, sign-offs.
   - Honor `--participants` flag overrides.
   - For each non-`Mark` participant, resolve via `retrieve_entity_by_identifier` against `contact` / `person`. Capture `entity_id` and `email` when present.
4. **Resolve missing emails via Google Calendar** (run after step 3, before moving on):
   - Determine the recording timestamp from the transcript source file's mtime or filename (format `YYYY-MM-DD-HHMMSS`).
   - Use `gws calendar events list --timezone Europe/Madrid` to fetch events within a ±90-minute window around that timestamp. Pick the best-matching event (title overlap with transcript topics, attendee names matching identified participants, or timing overlap).
   - If a match is found:
     - Extract attendee names and emails from the calendar event.
     - Cross-reference with participants identified in step 3: update any participant whose email was previously unknown.
     - Store a `calendar_event` entity in Neotoma (fields: `title`, `start_time`, `end_time`, `attendees` as array of `{ name, email }`; `calendar_event_id` from the API). Link it to the `meeting_analysis` via `REFERS_TO` in the Step 7 store.
     - Record `calendar_event_entity_id` on the `meeting_analysis`.
   - If no calendar match is found: note `_Calendar: no matching event found._` in the report and proceed without it.
5. **Classify meeting type** based on participants + content. Pick one:
   - `customer_call` — external customer/evaluator/prospect call (Neotoma or other product feedback).
   - `partner_call` — external partner or vendor.
   - `1_on_1` — peer / advisor / mentor / friend conversation.
   - `internal` — multi-party Mark-internal meeting (rare; flag explicitly).
   - `interview` — Mark interviewing or being interviewed.
   - `other` — anything else; explain.

If classification is ambiguous, pick the most defensible and explain in the report. Do not refuse.

## Step 2: Extract structured analysis

Read the full transcript. Extract the following with verbatim quotes from the transcript where possible (never invent quotes — paraphrase is labeled `paraphrase: …`):

1. **Summary** — 3–5 bullets capturing why the meeting happened, what was discussed, and the outcome.
2. **Decisions made** — short list. Each decision: what was decided + who decided + verbatim quote when present.
3. **Action items**, split three ways:
   - **Mine** — things Mark committed to. Each item: description, target date if mentioned, related repo / project if relevant.
   - **Theirs** — things a participant committed to. Each item: who, description, target date if mentioned.
   - **Joint / TBD** — items raised but unowned.
4. **Open questions** — questions left unresolved that need a future answer (mine, theirs, or shared).
5. **Topics + key threads** — short list of topics with one-line synthesis each. Useful for searching and clustering across meetings.
6. **Risks / blockers raised** — anything either party flagged as a concern or blocker.
7. **Repo / project signal** — extracted mentions of repos, products, or projects (e.g. `neotoma`, `ateles`, `markmhendrickson.com`, named features). For each: which action items or open questions relate to it; whether the related work belongs in a public issue (see Step 4).
8. **PII inventory** — names, emails, phone numbers, employer references, internal project names, customer names, or anything else that should be scrubbed before going public. Build this list explicitly — it drives Step 3.

If a section has no signal, render it as an explicit `_None._` marker rather than omitting it, so the report shape stays stable across meetings.

### Data minimization (RGPD legitimate-interest discipline)

See CLAUDE.md "People-data processing". Neotoma's storage of meeting participants and transcripts runs under RGPD Art. 6(1)(f) legitimate interest, **not** the household exemption, because the data drives professional action. When extracting people into durable `contact`/`person` profiles:

- Keep relationship-relevant facts (role, context, commitments, follow-ups).
- Do NOT persist incidental Art. 9 sensitive disclosures (health, finances, family situations, political/religious views) into contact profiles unless directly relevant to a stored task — summarize rather than store verbatim when a sensitive detail is incidental.

This governs what **enters** the graph (inbound). It is in addition to Step 3's PII scrubbing, which governs what **leaves** for public issues (outbound). The two are complementary, not duplicative.

## Step 3: PII scrubbing rules (for public issue drafts only)

Any text destined for a public GitHub issue MUST be scrubbed:

- **Names → roles.** Replace participant names with role labels (`an evaluator`, `a partner`, `a customer at a Series-B fintech`). Never use a real name in a public issue body unless the participant is a public-facing collaborator on that repo AND has clearly consented in the conversation (e.g. an OSS contributor offering to file the issue themselves).
- **Emails / phone numbers → removed.** Strip entirely; do not replace with `[redacted]` mid-sentence — rewrite the sentence.
- **Employer / customer names → generalized.** "Acme Corp asked …" → "An evaluator asked …".
- **Internal product / project names → check carefully.** If a name appears in the public site (`../neotoma/frontend/src/site/site_data.ts`, `../personal/websites/*`) or has been published in a blog post / tweet, it's public; otherwise generalize.
- **Verbatim quotes → reframed.** Quotes from non-public participants are reframed as observations ("Users on the evaluator track report friction around X") rather than quoted with attribution.
- **Internal URLs / paths → removed.** Strip absolute paths, internal dashboard links, prod URLs that aren't public.

If a proposed issue cannot be cleanly scrubbed (e.g. the bug is fundamentally a story about one named user's setup), demote it from `proposed_github_issue` to a Mark-internal `task` and note why in the report.

## Step 4: Derive proposed public issues

Public issues are not generated by default for every action item — they are generated only when:
1. The action item maps to a specific repo (from Step 2 #7).
2. The work makes sense to track in public (bug, feature request, doc gap, comparison page idea, etc.).
3. After PII scrubbing, the issue body still conveys the actual problem.

For each proposed issue capture:
- **Repo**: `<owner>/<repo>` — must match the allowlist below. Skip if no confident repo match.
- **Title**: short, problem-statement-first ("FAQ entry: difference between Neotoma and Mem0", not "Alice asked about Mem0").
- **Labels**: best-guess based on issue kind (`bug`, `enhancement`, `docs`, `discussion`).
- **Body**: scrubbed per Step 3. Must include:
   - One-paragraph context (why this matters).
   - Specific ask or expected behavior.
   - Acceptance criteria when concrete.
   - Source attribution: `Surfaced in a meeting on YYYY-MM-DD; participants and direct quotes recorded privately.` — never link the recording or transcript publicly.
- **Backed by**: which action item / open question / decision; verbatim transcript quote (kept in Neotoma entity, NOT in the issue body).
- **Confidence**: `high` (concrete bug or explicit feature ask), `medium` (implied), `low` (speculative — usually demote to task instead).

**Repo routing** (default — override via `MEETING_ANALYSIS_ALLOWED_REPOS` env, comma-separated `owner/repo`):
- **`markmhendrickson/ateles`** — default repo for all issues unless a more specific match applies. Use for swarm tooling, skills, daemon work, and anything that doesn't clearly belong elsewhere.
- **`markmhendrickson/neotoma`** — use when the issue is specifically about Neotoma: its MCP, schema, API, product behaviour, data model, or SDK.

When the signal from Step 2 #7 points clearly to Neotoma, route there; otherwise default to `ateles`. Do not create issues for repos outside this list — note them in the report as `_Out-of-scope repo mentioned: <name> — no issue staged._`.

## Step 5: Draft recap messages

For each external participant, produce one recap. The format depends on whether their email address is known:

**When email is known** (resolved from Neotoma `contact`, transcript, or Google Calendar in Step 1):
- Draft a **recap email** with:
  - **Subject**: `Recap: <short meeting topic>` or `Following up on <topic>`.
  - **Greeting**: first name.
  - **Recap paragraph**: 2–4 sentences capturing the gist of the conversation.
  - **Decisions / next steps** (when any): short bulleted list.
  - **My action items**: short bulleted list, with target dates when committed.
  - **Their action items**: short bulleted list, framed as `Just to capture what you mentioned: …`. Soft — never demanding.
  - **Issue links**: only when issues were actually opened (`--open-issues` mode). Omit when still drafts.
  - **Sign-off**: `Mark` (or `Best, Mark`).
- Stage in Gmail: use `gws gmail draft create` (per the `Always use GWS CLI for Gmail` rule). On failure, record the reason in `recap_message.delivery_status` and continue.

**When email is not known**:
- Draft a **generic recap message** — same content structure as above, but without subject line or email greeting formality. Format it as plain conversational text suitable for sending via Messages, WhatsApp, or any other channel.
- Do **not** attempt Gmail staging. Set `recap_message.delivery_channel` to `message` and `delivery_status` to `pending_manual_send`.

In both cases, group participants from the same organization into a single recap when natural; otherwise one per participant.

Tone matches the meeting tone: warm and direct, no corporate filler. Never invent commitments. Frame tentative items as `Happy to look at X next week if useful`.

Each recap is stored in Neotoma as a `recap_message` entity (not `email_draft`) with fields: `to_name`, `to_email` (or null), `format` (`email` | `message`), `subject` (email only), `body`, `participant_contact_entity_id`, `delivery_channel` (`gmail` | `message`), `delivery_status` (`staged` | `failed:<reason>` | `pending_manual_send`), `gmail_draft_id` (when staged).

Skip all recap drafting when `--no-recap` is passed or `MEETING_ANALYSIS_RECAP=0` is set.

## Step 6: Write the report

Output location: alongside the transcript when the source is a file, otherwise under `data/imports/audio/`:

- File source: `<transcript_dir>/<transcript_stem>_meeting_analysis.md`
- Entity / paste source: `data/imports/audio/<YYYY-MM-DD-HHMMSS>_meeting_analysis.md`

Template:

```markdown
# Meeting Analysis: <short title>

**Date:** YYYY-MM-DD
**Meeting type:** customer_call | partner_call | 1_on_1 | internal | interview | other
**Source:** <transcript file path or transcription entity_id>
**Transcription entity:** ent_… (or `_not linked_`)
**Calendar event:** <title> @ <start_time> (ent_… or `_not found_`)
**Participants:** Name <email or _unknown_>, …
**Analyst turn:** <conversation_id>:<turn_id>

## Summary

- bullet
- bullet

## Decisions

- <what was decided> — <who> — "<verbatim quote when present>"
- _None._  (when no decisions made)

## Action items

### Mine
- <description> — due: YYYY-MM-DD (or `unspecified`) — repo: <owner/repo> (or `none`)

### Theirs
- <person>: <description> — due: YYYY-MM-DD (or `unspecified`)

### Joint / TBD
- <description>

## Open questions

- <question> — owner: mine | theirs | shared

## Topics

- <topic> — <one-line synthesis>

## Risks / blockers

- <risk> — raised by: <person> — "<quote>"

## Repo / project signal

- <repo or project> — related items: <bullet refs above> — public-issue candidate: yes | no | demoted (<reason>)

## Proposed public issues

One entry per proposed issue. If none warranted, render `_None._`.

- **<owner/repo>** — `<labels>` — confidence: <high|medium|low>
  - Title: <title>
  - Body:
    > <scrubbed body, multi-line>
  - Backed by: action item / open question reference + verbatim quote (private, kept in Neotoma entity not in this rendered body)

## Recap messages

One block per participant (or participant group).

- **To:** Name <email or _no email — send via message_> — format: email | message
  - Subject: <subject> (email only)
  - Body:
    > <full recap body>
  - Delivery: <gmail:<draft_id> | pending_manual_send | failed:<reason>>

## PII inventory (private)

- Names: <list>
- Emails: <list>
- Other sensitive: <list>

## Follow-up tasks (Mark-internal)

- <task> — due: YYYY-MM-DD (or `unspecified`) — links to: <repo, contact, etc.>
```

Sections with no content render as `_None._` rather than being omitted.

## Step 7: Persist to Neotoma (same turn)

Follow Neotoma MCP turn lifecycle and `[PROVENANCE]` rules.

### Entity-type reuse check (before first store in a fresh session)

Before storing, call `list_entity_types` with keywords `meeting`, `task`, `recap`, `issue`, `calendar` to check whether the following types already exist. Reuse exact strings when present; introduce only when missing:

- `meeting_analysis` (this skill's primary output)
- `task` (Mark-internal action items)
- `recap_message` (drafted recap messages — email or generic)
- `proposed_github_issue` (drafted public issues)
- `calendar_event` (matched Google Calendar event)
- `contact` (participants — reuse existing only; create stubs only when no match)

### Retrieve first

- `transcription` by audio_file_path (when source is a file produced by `transcribe_audio.py`).
- `contact` / `person` for each participant by name and email.
- `calendar_event` by `calendar_event_id` (when a match was found in Step 1) — reuse if already stored.
- Existing open `task` entities for the same repo/topic — to avoid duplicates.

### Store

Single `store` (combined entities + relationships) call when batchable. Entities (typical order):

1. `conversation` — current chat conversation (turn lifecycle).
2. `agent_message` — current user message, `turn_key: "{conversation_id}:{turn_id}"`.
3. `meeting_analysis` with fields:
   - `title`, `meeting_date`, `meeting_type`
   - `source_type` (file | entity | text)
   - `source_reference` (path or entity_id)
   - `report_path` (absolute path to written markdown)
   - `participant_names` (array), `participant_contact_entity_ids` (array)
   - `calendar_event_entity_id` (when matched; otherwise null)
   - `summary_bullets` (array)
   - `decisions` (array of `{ what, who, quote? }`)
   - `action_items_mine` (array of `{ description, due_date?, repo? }`)
   - `action_items_theirs` (array of `{ person, description, due_date? }`)
   - `action_items_joint` (array of strings)
   - `open_questions` (array of `{ question, owner }`)
   - `topics` (array of `{ topic, synthesis }`)
   - `risks_or_blockers` (array of `{ risk, raised_by, quote? }`)
   - `repo_signal` (array of `{ repo_or_project, related_item_refs, public_issue_candidate }`)
   - `pii_inventory` (object with `names`, `emails`, `other`)
   - `data_source` (e.g. `analyze-meeting.skill {timestamp} source=<path>`)
4. One `task` per Mark action item, with `description`, `due_date`, `status: open`, `source: meeting_analysis`, and a `REFERS_TO` edge to the `meeting_analysis`.
5. One `recap_message` per participant (or group), with `to_name`, `to_email` (or null), `format` (`email` | `message`), `subject` (email only), `body`, `participant_contact_entity_id`, `delivery_channel` (`gmail` | `message`), `delivery_status` (`staged` | `failed:<reason>` | `pending_manual_send`), `gmail_draft_id` (when staged).
6. One `proposed_github_issue` per drafted issue, with `repo`, `title`, `labels` (array), `body_scrubbed`, `confidence`, `backed_by_quote` (verbatim — private), `opened_url` (null until Step 9 runs).
7. `calendar_event` — store when a match was found (fields: `title`, `start_time`, `end_time`, `attendees`, `calendar_event_id`). Skip if already retrieved.
8. `contact` — create stubs only for participants with no existing match; reuse retrieved entity_ids otherwise.

Attach the transcript as a `file_path` on the combined store (if the source is a file) so the raw artifact is preserved per `[PROVENANCE]`.

### Relationships

Batch via `relationships` in the same `store` call where possible:

- `PART_OF`: `agent_message` → `conversation`.
- `REFERS_TO`: `agent_message` → `meeting_analysis`.
- `REFERS_TO`: `meeting_analysis` → `transcription` (when linked).
- `REFERS_TO`: `meeting_analysis` → `calendar_event` (when matched).
- `REFERS_TO`: `meeting_analysis` → each participant `contact`.
- `REFERS_TO`: each `task` → `meeting_analysis` (and to the relevant `contact` when the task is owed to a specific person).
- `REFERS_TO`: each `recap_message` → `meeting_analysis` + the recipient `contact` + the `transcription`.
- `REFERS_TO`: each `proposed_github_issue` → `meeting_analysis` (NOT to `contact` directly, to keep public-issue → person attribution one hop away).

### Idempotency

- Turn idempotency key: `conversation-{conversation_id}-{turn_id}-analyze-meeting-{timestamp_ms}`.
- Per-meeting idempotency key for `meeting_analysis`: `meeting-<sha256(transcript_path or transcription_entity_id)[:12]>` — prevents duplicate analyses when re-run on the same transcript.

## Step 8: Deliver recap messages

For each `recap_message` not suppressed by `--no-recap`:

**Email format** (`delivery_channel: gmail` — participant email is known):
- Run `gws gmail draft create --to "<name> <email>" --subject "<subject>" --body "<body>"` (per the `Always use GWS CLI for Gmail` rule — never use the Gmail MCP directly).
- On success: set `delivery_status: staged`, `gmail_draft_id` to the returned id. Update the stored entity via a narrow `store` call.
- On failure: set `delivery_status: failed:<reason>`. Do not retry within the same turn. Continue with remaining recaps.
- Do not auto-send. Drafts wait in Gmail for explicit user review.

**Message format** (`delivery_channel: message` — no email address):
- No automated delivery. Set `delivery_status: pending_manual_send`.
- Surface the full message body in the report under `## Recap messages` so it can be copied and sent manually (via Messages, WhatsApp, etc.).

## Step 9: Open GitHub issues (opt-in)

Default is **off**. Issues are opened only when:
- `--open-issues` flag is passed, OR
- `MEETING_ANALYSIS_OPEN_GH_ISSUES=1` is set in env.

When opt-in is active, for each `proposed_github_issue`:

- Verify the target repo is in the allowlist (Step 4). Skip silently otherwise.
- Re-verify the body has been scrubbed (no participant names, no emails, no internal URLs). If the body fails the scrub check, demote to task and log a warning.
- Call the GitHub MCP `issue_write` (create) with `owner`, `repo`, `title`, `body`, `labels`. Capture the issue URL.
- Update the `proposed_github_issue` entity's `opened_url` field and append the URL to the recap email body's `Issue links` section when the recipient is connected to that work item.
- On failure: leave `opened_url` null and record the error in `open_error`.

When opt-in is **off**: issues stay as drafts. The recap email omits issue links (do not promise links that don't exist).

## Step 10: Close the turn

After producing the user-visible reply, store the assistant `agent_message` per the closing-store step of the Neotoma MCP turn lifecycle and link `PART_OF` to the same conversation entity.

## Step 11: Surface to the user

Reply with:
- One-line headline (e.g. `Meeting with Alice and Bob — 3 decisions, 4 of my action items, 2 issue drafts.`).
- **My action items** as a short numbered list (max 5) with due dates.
- **Recap messages** — one line per message: `→ <Name> — email: <staged|failed> | message: pending_manual_send`.
- **Proposed issues** — one line per issue: `<owner/repo>: <title> — <opened-URL | draft>`.
- Absolute report path.
- `meeting_analysis` entity id and a list of created `task` / `email_draft` / `proposed_github_issue` entity ids.

Render the `Neotoma` section per the `[COMMUNICATION & DISPLAY]` display rule, listing created and retrieved entities.

## Behavior rules

- **No invented commitments.** Every action item, decision, and quote MUST trace to specific transcript text. Paraphrase is labeled `paraphrase: …`. Never put words in a participant's mouth in the recap email or in a quoted block in the report.
- **PII scrubbing is non-negotiable for public issues.** A `proposed_github_issue` body that still contains a participant name, email, internal URL, or unverbalized internal project name is a bug — demote to task. Better to demote than to leak.
- **Recap messages are never auto-sent.** Email recaps wait in Gmail Drafts for explicit user review. Generic message recaps are surfaced as copy-paste text only.
- **Email only when address is known.** If a participant's email cannot be resolved from Neotoma, the transcript, or Google Calendar, always fall back to generic message format — never guess an address.
- **Public issues opt-in.** Default is staging, not opening. The skill writes drafts and waits.
- **Reuse contacts.** Never create duplicate `contact` entities for participants who already exist in Neotoma. Use `retrieve_entity_by_identifier` first.
- **Stable shape.** Empty sections render as `_None._` in the report AND as empty arrays in the entity, so cross-meeting queries are reliable.
- **Skip silently on empty / non-meeting transcripts.** Short solo voice memos and accidental recordings do not get analysis treatment. The auto-invoke from `/record_meeting` checks for at least one of: ≥2 distinct speakers (when diarization present), ≥200 words, or at least one second-person pronoun followed by a verb of commitment. Otherwise: silent skip.
- **No claims about side effects you didn't perform.** If Gmail staging failed, say so. If issues were not opened (default), say `drafted` not `opened`. The summary line matches reality.

## Out of scope

- Sending the recap emails — handled by the user via Gmail review.
- Closing the loop on action items — handled by other workflows (task completion, follow-up meetings).
- Producing the customer-development analysis when the meeting is Neotoma feedback — handled by [`analyze-neotoma-feedback`](../analyze-neotoma-feedback/SKILL.md). Both skills run in parallel when the Neotoma heuristic in [`record_meeting`](../record_meeting/SKILL.md) fires; the two analyses cross-link via the shared `transcription` and `contact` entities.
- Speaker name correction when diarization mislabels — the user can pass `--participants` to override; deeper diarization-repair lives in `transcribe_audio.py`.
