---
name: store-neotoma
description: "Review chat, preview exact Neotoma payloads, then store conversation, dual agent_message rows per turn (user + assistant, PART_OF conversation), mandatory derived transcript + analysis entities whenever the thread yields recoverable substance (verbatim messages, in-session summaries, tool traces, referenced files), and attachments after user confirmation."
triggers:
  - store_neotoma
  - /store_neotoma
  - store chat in neotoma
  - persist chat to neotoma
  - review and store conversation
entity_id: ent_ba1320078b84cc4c5b43c29e
---

# store-neotoma

## Purpose

Define the workflow for reviewing chat and storing it in Neotoma — conversation, dual `agent_message` rows per turn, derived transcript + analysis, and attachments — then reporting a succinct affected-records list. Neotoma is the only data store; there is no Parquet migration phase.

## Execution policy (no confirmation gate)

`/store_neotoma` stores directly, then reports — it does NOT preview-and-wait. (Updated 2026-06-05 per operator: the prior mandatory preview/confirm gate is removed, matching `/end`.) Build the plan, execute the store, then render the affected-records summary. The user may still steer: if the user volunteers scope or "how to store" input (in their invocation or a follow-up), apply it; if they explicitly ask to see a preview first, honor that one-off request. Absent such input, do not block on confirmation.

## Scope

Applies when the user asks to store the current chat in Neotoma. Covers extraction, storage, relationships, and reporting. Stores conversation, **two `agent_message` entities per logical turn** (`role: "user"` and `role: "assistant"`), each with `PART_OF` → conversation — the canonical shape from Neotoma MCP live chat instructions — plus **derived transcript and analysis** synthesized from whatever the conversation actually provides (see below), plus attachments.

## Prerequisites

- Chat transcript available in agent context (user and agent messages).
- Neotoma MCP available.
- Load `.cursor/rules/conversation_tracking.mdc` and `.cursor/rules/neotoma_harness.mdc` for entity schemas and access rules. `conversation_tracking` defines dual-message + `PART_OF`; do not use merged `role_user`/`role_agent` on a single `agent_message`.

## Phase 0: Extraction (build the store plan)

### Step 0.1 — Extract

Parse all user and agent messages in the conversation. For each **logical turn** (user message + following assistant reply):

- **User row:** `entity_type: agent_message`, `role: "user"`, `content` = exact user text, `turn_key` (stable per turn), optional `turn_number` (1-based), `timestamp`, `files_modified`, `tools_used`, `platform`, `model`, `neotoma_operations` (inferred from the turn).
- **Assistant row:** `entity_type: agent_message`, `role: "assistant"`, `content` = exact assistant text shown in chat, `turn_key` e.g. same base + `:assistant`, optional same `turn_number` / `timestamp` for sorting.
- Topics: extract from user questions, entity types discussed, file paths, domain keywords (finance, admin, health, work).
- Decisions: extract when agent states an approach, user confirms a choice, or implementation strategy is chosen.
- Action items: tasks to create, follow-ups mentioned.
- `files_modified`: file paths from tool calls, @-mentions, or explicit file references (typically attributed to the **user** message for that turn).
- `neotoma_operations`: inferred from agent tool-call patterns (`store`, `correct`, `create_relationship`).

Identify attachments. All of the following must be included:
- User-uploaded files (images, screenshots, files attached or pasted in any message, including turn 1). Use whatever path or content the platform provides.
- @-mentioned file paths.
- Attachment references and file URLs in message text.

### Derived transcript and analysis (mandatory default)

Unless the user explicitly excludes them, **always** store durable derived entities alongside dual messages. **`/store_neotoma` is not chat-only persistence:** relevant **transcript** and **analysis** must land in Neotoma whenever the thread offers material to derive from (see "available" below).

1. **Transcript digest** — At least one `note` (or `report` if the thread is long-form) that reconstructs **what happened in order**: turn-by-turn synopsis, short verbatim excerpts only when they fit naturally, paths and identifiers, and **explicit gaps** where exact `content` for a user or assistant row cannot be recovered from context. This is in addition to dual `agent_message` rows whenever those rows are available at full fidelity.
2. **Analysis digest** — At least one entity (`note`, `report`, `technical_research`, `competitive_analysis`, or another domain-appropriate type) capturing **substance distilled from the assistant side** across the thread: themes, decisions, risks, recommendations, follow-ups, and named people/companies/products when material. Merge into one note vs several entities based on clarity; do not skip analysis because "it was only in chat."

**What counts as "available":** Anything the agent can use to reconstruct the thread without inventing facts: full user/assistant message text in context, **conversation summaries** or handoff blocks injected in-session, tool-call narratives and quoted outputs, @-mentioned or opened file paths and their read contents, and user-pasted excerpts. **If material is partial, still write both digests** — the transcript note documents what is missing; the analysis note states what is inferred vs verbatim and marks uncertainty. **FORBIDDEN:** Omitting transcript and analysis digests just because some assistant rows lack exact `content` when summaries, themes, or tool evidence exist. **Rare exception:** the slice to store has literally no substantive narrative (e.g. only a bare ping with no prior context in window); then one short `note` may record that state, and analysis may be a single honest line.

**Provenance:** Set `data_source` (and `source_file` when a repo file is the subject) on each derived entity. Link with **`REFERS_TO`** from the **user** `agent_message` that prompted `/store_neotoma` (or the last user message in the stored slice) to each derived entity, and from the **closing assistant** `agent_message` to derived entities the assistant reply cites, per Neotoma MCP linkage rules.

**Optional extraction:** tasks, contacts, events, transactions, and other types the turns imply — **in addition to** the mandatory transcript + analysis digests above.

### Step 0.2 — Apply any user-volunteered input

If the user volunteered scope or "how to store" input (exclusions, title/topics changes, field-name or format preferences, or an explicit request to see a preview first), apply it now before storing. Otherwise proceed straight to Phase 1 — do not solicit confirmation.

## Phase 1: Storage

### Step 1.1 — Create conversation entity

Generate `conversation_id` if not yet final. Store via Neotoma MCP `store`:

- `entity_type: conversation`
- `conversation_id`, `title`, `turn_count`, `start_timestamp`, `last_updated`
- `topics`, `decisions`, `action_items`
- `platform`, `status: active`, `summary`

### Step 1.2 — Create agent_message entities per turn (dual rows)

For each turn, create **two** stored messages linked to the **same** conversation:

1. **User message:** `entity_type: agent_message`, `role: "user"`, `content` (full text), `turn_key`, optional `turn_number`, `timestamp`, `files_modified`, `tools_used`, `platform`, `model`, `neotoma_operations`.
2. **Assistant message:** `entity_type: agent_message`, `role: "assistant"`, `content` (full text), `turn_key` (distinct from user row), optional same `turn_number` / `timestamp`.

Store via Neotoma MCP `store`, batching multiple entities and `relationships` in one request. For each row, ensure **`PART_OF` from that `agent_message` → conversation**. Use **distinct** `idempotency_key` values per message (e.g. suffix `-user` / `-assistant` per turn).

### Step 1.25 — Store derived transcript and analysis

In the **same** batched `store` as the conversation and messages when practical, include the **transcript digest** and **analysis** entities. If batch size forces a second call, complete it in the same session before reporting success. Add **`REFERS_TO`** edges from the appropriate user/assistant `agent_message` rows to each derived entity. Do not end Phase 1 without persisting at least one transcript digest and one analysis entity unless the user removed them.

### Step 1.3 — Handle attachments

For each file in the attachments list with an accessible path: store via Neotoma using `file_path` (or `file_content` if required); create image/media entity if the file is an image; attempt `create_relationship(EMBEDS, user_agent_message_entity_id, file_entity_id)`. If EMBEDS fails (known issue), log and continue.

### Step 1.4 — Create relationships

- For **each** `agent_message` (user and assistant): `PART_OF` → conversation if not already created in the batched `store` call.
- For tasks, contacts, projects, plans, and other discussed entities: `conversation --[REFERS_TO]--> entity`, or `agent_message --[REFERS_TO]--> entity` for turn-level links.

### Step 1.5 — If user provided "how to store" input: invoke neotoma-learn skill

If the user provided input on how to store objects (field renames, exclusions, format preferences), after Phase 1 invoke the `neotoma-learn` skill with that input as the scenario. Do not use neotoma-learn to undo dual-message storage; that shape is canonical. If no such input, skip.

## Output and Reporting

After execution, render the **succinct affected-records list** in the Neotoma-MCP turn-report style — the same contract shared with `/end`:

- A `🧠 Neotoma` header linking the conversation.
- **Created (N) / Updated (N) / Retrieved (N)** groups. One bullet per record: emoji + label + linked `entity_type` text pointing to `<origin>/inspector/entities/<entity_id>`. No full snapshots — one labeled link per record.
- A one-line tally: `conversation_id`, agent_message count (expect ~2× turn count), transcript + analysis entity titles/ids, attachments stored, relationship count.

## Error Handling

- Neotoma MCP failures: retry once, then report and continue where possible.
- EMBEDS relationship failure: log and continue.

## References

- `execution/scripts/migrate_neotoma_chat_dual_messages.py` — One-off migration for legacy merged `role_user`/`role_agent` rows and inverted `PART_OF` edges.
- `.cursor/rules/conversation_tracking.mdc` — Entity model and fields.
- `.cursor/rules/neotoma_harness.mdc` — Canonical Neotoma harness (access, lifecycle pointer, turn report, invariants, QA).
- `.cursor/skills/neotoma-learn/SKILL.md` — Update Neotoma MCP instructions after user provides "how to store" input.
