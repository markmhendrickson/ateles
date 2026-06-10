---
name: store-neotoma
description: "Review chat and automatically store conversation, dual agent_message rows per turn (user + assistant, PART_OF conversation), mandatory derived transcript + analysis entities whenever the thread yields recoverable substance (verbatim messages, in-session summaries, tool traces, referenced files), and attachments — executing immediately without a confirmation gate, then reporting exactly what was stored."
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

Define the workflow for reviewing chat, building the Neotoma store payload, and executing storage automatically — without a confirmation gate — then reporting what was stored. Neotoma is the only data store; there is no Parquet migration phase.

## Scope

Applies when the user asks to store the current chat in Neotoma. Covers payload construction, storage, relationships, and a post-store report.

Reviews the current chat conversation, builds the records to store in Neotoma, and **stores them immediately**: conversation, **two `agent_message` entities per logical turn** (`role: "user"` and `role: "assistant"`), each with `PART_OF` → conversation — the canonical shape from Neotoma MCP live chat instructions — plus **derived transcript and analysis** synthesized from whatever the conversation actually provides (see below), plus attachments.

Auto-execute (no confirmation gate): This workflow stores immediately; it MUST NOT pause to ask the user to confirm or approve before writing. It MUST still build the full, correct payload (dual messages + mandatory derived digests), then **report exactly what was stored** afterward. If the user supplies corrections after the fact, apply them via a follow-up `correct`/`store`. The only thing that suppresses storage is an explicit user instruction not to store (or to exclude specific turns/entities).

## Prerequisites

- Chat transcript available in agent context (user and agent messages).
- Neotoma MCP available.
- Load `.cursor/rules/conversation_tracking.mdc` and `.cursor/rules/neotoma_harness.mdc` for entity schemas and access rules. `conversation_tracking` defines dual-message + `PART_OF`; do not use merged `role_user`/`role_agent` on a single `agent_message`.

## Phase 0: Build the payload (then auto-store)

### Step 0.1 — Extract and build preview

Parse all user and agent messages in the conversation. For each **logical turn** (user message + following assistant reply):

- **User row (preview + store):** `entity_type: agent_message`, `role: "user"`, `content` = exact user text, `turn_key` (stable per turn), optional `turn_number` (1-based), `timestamp`, `files_modified`, `tools_used`, `platform`, `model`, `neotoma_operations` (inferred from the turn).
- **Assistant row (preview + store):** `entity_type: agent_message`, `role: "assistant"`, `content` = exact assistant text shown in chat, `turn_key` e.g. same base + `:assistant`, optional same `turn_number` / `timestamp` for sorting.
- Topics: extract from user questions, entity types discussed, file paths, domain keywords (finance, admin, health, work).
- Decisions: extract when agent states an approach, user confirms a choice, or implementation strategy is chosen.
- Action items: tasks to create, follow-ups mentioned.
- `files_modified`: file paths from tool calls, @-mentions, or explicit file references (typically attributed to the **user** message for that turn).
- `neotoma_operations`: inferred from agent tool-call patterns (`store_structured`, `correct`, `create_relationship`).

Identify attachments. All of the following must be included:
- User-uploaded files (images, screenshots, files attached or pasted in any message, including turn 1). Use whatever path or content the platform provides.
- @-mentioned file paths.
- Attachment references and file URLs in message text.

### Derived transcript and analysis (mandatory default)

Unless the user explicitly excludes them in Phase 0 revision, **always** plan and store durable derived entities alongside dual messages. **`/store_neotoma` is not chat-only persistence:** relevant **transcript** and **analysis** must land in Neotoma whenever the thread offers material to derive from (see “available” below).

1. **Transcript digest** — At least one `note` (or `report` if the thread is long-form) that reconstructs **what happened in order**: turn-by-turn synopsis, short verbatim excerpts only when they fit naturally, paths and identifiers, and **explicit gaps** where exact `content` for a user or assistant row cannot be recovered from context (so Neotoma still carries navigable structure). This is in addition to dual `agent_message` rows whenever those rows are available at full fidelity.
2. **Analysis digest** — At least one entity (`note`, `report`, `technical_research`, `competitive_analysis`, or another domain-appropriate type) capturing **substance distilled from the assistant side** across the thread: themes, decisions, risks, recommendations, follow-ups, and named people/companies/products when material. Merge into one note vs several entities based on clarity; do not skip analysis because “it was only in chat.”

**What counts as “available”:** Anything the agent can use to reconstruct the thread without inventing facts: full user/assistant message text in context, **conversation summaries** or handoff blocks injected in-session, tool-call narratives and quoted outputs, @-mentioned or opened file paths and their read contents, and user-pasted excerpts. **If material is partial, still write both digests** — the transcript note documents what is missing; the analysis note states what is inferred vs verbatim and marks uncertainty. **FORBIDDEN:** Omitting transcript and analysis digests just because some assistant rows lack exact `content` when summaries, themes, or tool evidence exist. **Rare exception:** the slice to store has literally no substantive narrative (e.g. only a bare ping with no prior context in window); then one short `note` may record that state, and analysis may be a single honest line — still preview this explicitly.

**Provenance:** Set `data_source` (and `source_file` when a repo file is the subject) on each derived entity. Link with **`REFERS_TO`** from the **user** `agent_message` that prompted `/store_neotoma` (or the last user message in the stored slice) to each derived entity, and from the **closing assistant** `agent_message` to derived entities the assistant reply cites, per Neotoma MCP linkage rules.

**Optional extraction** (still previewed): tasks, contacts, events, transactions, and other types the turns imply — **in addition to** the mandatory transcript + analysis digests above.

Build payload structure:

- Conversation: full payload with every property and exact value to be stored (see Step 0.2).
- Messages: **two** exact `agent_message` payloads per turn (user and assistant), each with every property and exact value to be stored, plus distinct `idempotency_key` / `turn_key` per row. Include `PART_OF` targets (conversation `entity_id` after conversation is created).
- Attachments: list of files to store with path, type, and any store payload (`file_path` or `file_content`).
- **Derived transcript + analysis:** exact payloads (mandatory unless the user explicitly excludes them).
- Other derived entities: exact payloads for any additional non-chat entities implied by the conversation.

### Step 0.2 — Assemble the payload

Assemble the full set of records as structured objects with exact properties and values. These same sections are what the post-store report (see Output and Reporting) will enumerate, so build them completely.

Sections:
1. Conversation entity (exact payload).
2. Agent_message entities (exact payloads — **pairs** per turn: user then assistant).
3. Attachments (all user-uploaded files, @-mentioned paths, file URLs).
4. Relationships to create.
5. **Derived transcript entity** (exact payload) and **analysis entity** (exact payload), plus any other derived entities (tasks, contacts, etc.).

### Step 0.3 — Proceed directly to storage

Do **not** pause for confirmation. Once the payload is assembled, proceed immediately to Phase 1. Honor only pre-stated user constraints already present in the conversation (e.g. "skip turn 3", "don't store X", "use conversation_id without date prefix") — apply them while assembling, then store. Any corrections the user raises *after* storage are handled with a follow-up `correct`/`store`, not by withholding the write.

## Phase 1: Storage (automatic — no confirmation required)

### Step 1.1 — Create conversation entity

Generate `conversation_id` if not yet final. Store via Neotoma MCP:

- `entity_type: conversation`
- `conversation_id`, `title`, `turn_count`, `start_timestamp`, `last_updated`
- `topics`, `decisions`, `action_items`
- `platform: cursor`, `status: active`, `summary`

Use `store_structured` (or equivalent MCP action).

### Step 1.2 — Create agent_message entities per turn (dual rows)

For each turn (respecting exclusions from preview), create **two** stored messages linked to the **same** conversation:

1. **User message:** `entity_type: agent_message`, `role: "user"`, `content` (full text), `turn_key`, optional `turn_number`, `timestamp`, `files_modified`, `tools_used`, `platform`, `model`, `neotoma_operations` as previewed.
2. **Assistant message:** `entity_type: agent_message`, `role: "assistant"`, `content` (full text), `turn_key` (distinct from user row), optional same `turn_number` / `timestamp`.

Store via Neotoma MCP (`store` / `store_structured`), batching multiple entities and `relationships` in one request when supported. For each row, ensure **`PART_OF` from that `agent_message` → conversation** (conversation `entity_id` from Step 1.1 response). Use **distinct** `idempotency_key` values per message (e.g. suffix `-user` / `-assistant` per turn).

### Step 1.25 — Store derived transcript and analysis

In the **same** batched `store` as the conversation and messages when practical, include the previewed **transcript digest** and **analysis** entities. If batch size forces a second call, complete it in the same session before reporting success. Add **`REFERS_TO`** edges from the appropriate user/assistant `agent_message` rows to each derived entity (and `PART_OF` only for messages, not for these digests unless schema calls for it). Do not end Phase 1 without persisting at least one transcript digest and one analysis entity unless the user removed them in Phase 0.

### Step 1.3 — Handle attachments

For each file in the attachments list with an accessible path:

- Store file via Neotoma using `file_path` (or `file_content` if required).
- Create image/media entity if file is an image.
- Attempt `create_relationship(EMBEDS, user_agent_message_entity_id, file_entity_id)` for attachments on the user turn (or conversation-level link if turn-level linking is unavailable).
- If EMBEDS fails (known issue), log and continue.

### Step 1.4 — Create relationships

- For **each** `agent_message` (user and assistant): `create_relationship(PART_OF, agent_message_entity_id, conversation_entity_id)` if not already created in the same batched `store_structured` call.
- For tasks, contacts, projects, execution plans, and other discussed entities: `conversation --[REFERS_TO]--> entity`, or `agent_message --[REFERS_TO]--> entity` for turn-level links.

### Step 1.5 — If user provided "how to store" input: invoke neotoma-learn skill

- If during Phase 0 the user provided input on how to store objects (field renames, exclusions, format preferences), after Phase 1 invoke the `neotoma-learn` skill with that input as the scenario/description (for example "When storing conversation from /store_neotoma: use conversation_id without date prefix"). Do not use neotoma-learn to undo dual-message storage; that shape is canonical.
- If the user gave no "how to store" input, skip this step.

## Output and Reporting

After execution, report:
- Storage summary: `conversation_id`, **agent_message count** (expect ~2× turn count when both sides exist), **transcript + analysis derived entity titles/ids**, attachments stored, relationship count, other derived entity count.
- Per `.cursor/rules/persistence.mdc`, show stored entities via retrieve/list observation actions where relevant.
- Render the mandatory `🧠 Neotoma` turn report per `.cursor/rules/neotoma_harness.mdc`.

## Error Handling

- Neotoma MCP failures: retry once, then report and continue where possible.
- EMBEDS relationship failure: log and continue.

## References

- `execution/scripts/migrate_neotoma_chat_dual_messages.py` — One-off migration for legacy merged `role_user`/`role_agent` rows and inverted `PART_OF` edges; use `--cli` with the `neotoma` binary when HTTP bearer is rejected (dry-run by default, `--execute` to apply).
- `.cursor/rules/conversation_tracking.mdc` — Entity model and fields.
- `.cursor/rules/neotoma_harness.mdc` — Canonical Neotoma harness (access, lifecycle pointer, turn report, invariants, QA).
- `.cursor/rules/confirmation_requirements.mdc` — Preview/confirm pattern.
- `.cursor/skills/neotoma-learn/SKILL.md` — Update Neotoma MCP instructions after user provides "how to store" input.
