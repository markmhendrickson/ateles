---
name: neotoma-learn
description: Update Neotoma MCP instructions from observed failures or scenarios; apply instruction changes directly in the Neotoma repo.
triggers:
  - neotoma_learn
  - /neotoma_learn
  - neotoma learn
  - update neotoma mcp instructions
  - strengthen neotoma instructions
user_invocable: true
entity_id: ent_77b71533e1dd275bec9f7112
---

# neotoma-learn

## Purpose

Define how to update the Neotoma MCP instructions file so agents both store and retrieve data correctly with Neotoma per observed failures or scenarios. Storage gaps (missed same-turn persistence, missing provenance, missed attachments) and retrieval gaps (skipped bounded retrieval, wrong query shape, ignored retrieved context, count/recency mis-queries) are equally in scope.

The skill also audits compliance with other Neotoma-related workspace rules in this repo (for example the consolidated always-on harness in `neotoma_harness.mdc`, the skills-source-of-truth and stub-fetch contract in `skills_neotoma_proactive_fetch`, the post-edit cache regeneration contract in `post_updates_neotoma_cache`, etc.). When a rule was not followed, apply the fix in the correct location per the durable-enhancement ladder — usually by strengthening the Neotoma MCP instructions, but sometimes by strengthening the workspace rule file itself.

## Scope

Applies when the user invokes `/neotoma_learn` in this repository. Target file is the sibling Neotoma repo instructions document.

When invoked (optionally with a scenario, report path, or failure description), follow the workflow below. When no scenario is provided, the default behavior is to interrogate the most recent conversational turn to determine whether the Neotoma MCP was used as expected, and apply a fix only if a gap is identified.

## When to use

Use when the user wants Neotoma MCP instructions strengthened so agents store and retrieve data correctly — for example same-turn storage when pulling data from other MCPs, a missed bounded retrieval at turn start, an answer that ignored relevant retrieved context, or a specific failure observed in chat.

## Target files

Primary target (Neotoma MCP instructions, most gaps land here):

- Path: `../neotoma/docs/developer/mcp/instructions.md` (relative to ateles repo root). Resolve absolute path if needed.
- Constraint: edit only within the first fenced code block in that file. Do not change markdown structure (headers, related docs, closing fence). Do not edit `neotoma/src/server.ts` fallback array.

Secondary targets (ateles workspace rules — only when the gap is about rule content, not MCP-agent behavior):

- Neotoma-related rule files under `.cursor/rules/` in this repo, including but not limited to:
  - `neotoma_harness.mdc`
  - `neotoma_evaluator_storage.mdc`
  - `post_updates_neotoma_cache.mdc`
  - `skills_neotoma_proactive_fetch.mdc`
  - `mcp_retry_usage_after_fix.mdc` (when the gap is Neotoma-specific)
- Constraint: only edit rule body content. Do not change the `---` frontmatter schema (`description`, `alwaysApply`, `globs`) except to add/strengthen an existing field's value when the rule's activation conditions are themselves the gap.
- Discovery: enumerate candidate rule files by listing `.cursor/rules/*.mdc` and matching filenames/bodies for "neotoma" or "mcp" (case-insensitive). Do not hardcode; pick up any new Neotoma-related rule that appears.

## Processing steps

### 1) Resolve target files

- Primary: from ateles repo root, `../neotoma/docs/developer/mcp/instructions.md`. Verify the file exists and includes section `## Instructions (content sent to clients)` with a single fenced code block for instruction lines.
- Secondary: enumerate Neotoma-related workspace rules. From ateles repo root, list `.cursor/rules/*.mdc` and keep files whose name or body mentions `neotoma` (case-insensitive) or that are Neotoma-specific MCP rules (e.g. `mcp_access_policy.mdc`, `mcp_retry_usage_after_fix.mdc`). Cache this list for steps 2 and 4. Do not hardcode a fixed list; always rediscover.

### 2) Interrogate the last conversational turn

Default behavior when the user has not provided an explicit scenario. The goal is to determine whether the Neotoma MCP was used as expected during the most recent turn (and the immediately preceding assistant turn, since many Neotoma obligations span the user-phase and closing-phase stores).

Reconstruct what happened:

- Identify the last user message and the assistant's response to it.
- Enumerate which tools/MCPs were invoked (Gmail, Calendar, web scraper, file reads, shell output, other MCPs, host IDE tools).
- Enumerate which Neotoma calls occurred and in what order relative to other tools, separated by kind:
  - Retrieval calls: `retrieve_entity_by_identifier`, `retrieve_entities`, `list_entity_types`, `list_observations`, `list_timeline_events`, `getStats`.
  - Storage calls: `store_structured`, `create_relationship`, `parse_file`, CLI `neotoma store` (and whether it ran with `--api-only`/`--base-url` when MCP was available).
- Note whether user-supplied attachments, screenshots, pasted content, or file paths appeared in the user message.
- Note whether the turn created or materially edited a local file that is itself the substantive deliverable (especially `.md`, `.txt`, `.json`, `.csv`, or similar report/plan/analysis artifacts), and whether that file was also stored in Neotoma or left repo-only.
- Note whether the user's request implied lookup/recall/counting/listing (identity, "what do you know about X", "how many", "recent", "latest"), and whether any retrieval was performed before the reply.
- Note whether the assistant produced a user-visible reply, and whether that reply rendered the required `🧠 Neotoma` disclosure section when applicable.

Additionally, read each Neotoma-related workspace rule file discovered in step 1. For each rule, identify the concrete obligations it places on the agent for this turn (the "contract" the rule encodes). Build a third checklist of **workspace-rule checks** to evaluate alongside retrieval-side and storage-side checks below. Examples (non-exhaustive, always re-read live rule text):

- `neotoma_harness`: Neotoma-only access; turn lifecycle pointer + MCP alignment; mandatory `🧠 Neotoma` turn report; invariants and auto-repair; condensed QA (load `neotoma_qa_reflection_deep.mdc` for Tier 2–4 and feedback-tool detail).
- `skills_neotoma_proactive_fetch`: Neotoma as source of truth for skills; when a stub-style `SKILL.md` contains `entity_id`, fetch full content via `retrieve_entity_snapshot` before executing; sync stubs when `synced_at` is older than the refresh interval; never write full skill content into the repo.
- `post_updates_neotoma_cache`: when editing post content, update Neotoma **and** regenerate the website cache in the same turn; do not auto-deploy without explicit request.
- `neotoma_evaluator_storage`, `mcp_retry_usage_after_fix`: read live and include any turn-relevant obligations.

For each applicable rule, record whether the obligation was triggered by the last turn, whether it was honored, and (if not) the specific clause that was missed.

Compare against the expected Neotoma contract. Retrieval-side checks:

- Bounded retrieval at turn start: for entities implied by the user message (concrete identifiers, categories, recency windows), `retrieve_entity_by_identifier` and/or `retrieve_entities` should have been called before the user-phase store and before responding.
- Query shape: concrete identifiers (names, emails, ids, exact titles) should use `retrieve_entity_by_identifier`; plural/category/list-intent queries ("last N transactions", "recent tasks", "latest events") should use `retrieve_entities` scoped by `entity_type` with an explicit limit or time window, not identifier lookup.
- Retrieved context grounding: when retrieval returned relevant results, the assistant's answer should have used them (or explicitly reconciled them) rather than inventing memory-backed claims or ignoring known matches; when retrieval returned nothing relevant, the answer should proceed normally without fabricating memory citations.
- Duplicate avoidance via retrieval: before storing entities that could already exist (people, companies, places, contacts, recurring tasks), matching rows should have been retrieved first and reused by `entity_id` for relationships instead of creating duplicates.
- Publication-recency vs observation-recency: "recently published" / publication-ordered queries should sort by `published_date`/`published_at` descending with dedup by `entity_id`, not by `last_observation_at`/`updated_at`.
- Entity-type cardinality: "how many entities per type" / histogram / sorted type totals should be answered from `getStats` (HTTP `GET /stats` → `entities_by_type`) first, not from `list_entity_types` `field_count` and not from a per-type retrieve_entities scan; when `getStats` is unreachable, the reply should say so explicitly instead of substituting.
- Bounded completeness: list/count answers derived from entity graphs should include a bounded completeness pass across likely equivalent containers/identifiers and common relationship variants, with dedup by `entity_id` and reconciled totals (or clearly noted remaining ambiguity).

Storage-side checks:

- Store-first ordering: the user-phase `store_structured` call should have happened before any other MCP/host tool use or the user-visible reply (after bounded retrieval).
- Closing assistant store: if a user-visible reply was produced, a closing `agent_message` (role `assistant`) `store_structured` plus `PART_OF` relationship to the same conversation used in the user-phase store should have occurred.
- Entity extraction: any entities implied by the user message (tasks, people, companies, events, transactions, places, receipts, notes, research, etc.) should have been stored with appropriate `entity_type` and `REFERS_TO` links from the user message.
- Attachments and unstructured payloads: user-supplied files/screenshots/pasted content should have been persisted via the combined store path (`file_path` or `file_content` + `mime_type`) with an `EMBEDS` relationship from the user message to the resulting file asset, in the same turn.
- External tool provenance: when data came from another MCP/tool (email, calendar, web, shell output, parsed file), the derived entities should include `data_source` and/or `api_response_data` (or be linked to a stored source artifact), and should have been stored before the assistant responded with that data.
- Tasks/commitments: explicit user intent or outbound commitments with named counterparties should have produced a `task` entity with `due_date` and a `REFERS_TO` link to the counterparty contact (reused via retrieval when possible, created if missing).
- Synthesized deliverables: reviews, reports, research, analyses, or briefings should have been stored as a structured note/report/research entity, not left only in chat.
- Agent-authored deliverable files: when the turn created or materially edited a markdown/text/json/csv or similar deliverable file, the file itself should have been persisted via the combined store path together with the structured note/report/research entity it supports, rather than being left only in the repo or working tree.
- Conversation identity: `turn_key` should be scoped to a distinct `conversation_id` (no generic `cursor:<n>` prefix) and idempotency keys should be turn-unique.
- Display rule: if non-bookkeeping entities were created/updated/retrieved this turn, the reply should have shown the `🧠 Neotoma` section with appropriate Created/Updated/Retrieved groups; otherwise the empty-state Suggestions sub-section. Entities read from existing state without a new observation this turn must appear under `Retrieved`, not under `Created` or `Updated`.

Classify the outcome:

- Compliant: Neotoma was used as expected and all applicable workspace-rule obligations were honored. Record which contract points and rule clauses were satisfied and stop before editing instructions or rules. Proceed to step 5 with an explicit "no fix needed" result.
- Non-compliant: at least one contract point or rule clause was skipped, reordered, or silently omitted. For each failure, record the specific check that failed and classify where the fix should land:
  - **MCP-agent-behavior gap** (retrieval or storage contract, or an obligation clearly expressed as agent MCP behavior): fix belongs in `../neotoma/docs/developer/mcp/instructions.md`.
  - **Workspace-rule-content gap** (rule wording is unclear, missing the clause that was violated, missing an activation trigger, or out of date relative to current Neotoma behavior): fix belongs in the offending `.cursor/rules/*.mdc` file in this repo.
  - **Both**: apply both fixes in the same turn.
  Proceed to step 3.
- Ambiguous: the turn did not clearly imply a Neotoma obligation (e.g. trivial acknowledgement with no entities and no external data) and no active rule's activation condition applied. Treat as compliant and report accordingly.

Apply the durable-enhancement ladder from `neotoma_harness` / `neotoma_qa_reflection_deep`: prefer instruction tightening first; only edit rule files when the gap is genuinely about rule content (missing/unclear/outdated wording) rather than MCP behavior.

When the user did provide an explicit scenario, report path, or failure description, use it directly instead of (or in addition to) the last-turn interrogation.

### 3) Read current instruction and rule sources

- Skip this step if step 2 classified the turn as Compliant or Ambiguous.
- For MCP-agent-behavior gaps: open `../neotoma/docs/developer/mcp/instructions.md` and locate the first fenced code block (between first ``` and next ```). Read current lines for comparison and minimal editing.
- For workspace-rule-content gaps: open each offending `.cursor/rules/*.mdc` file. Read the frontmatter and body so edits preserve the activation contract (`description`, `alwaysApply`, `globs`) and overall section structure.
- In both cases, check whether an existing instruction or clause already covers the identified gap. If so, prefer strengthening its wording (e.g. adding "do not respond until storage is complete", "MUST", or an explicit FORBIDDEN clause) over appending a new line or a new section.

### 4) Draft and apply proposed instructions and/or rule updates

- Skip this step if step 2 classified the turn as Compliant or Ambiguous.
- Route each failure to its target based on the classification from step 2:
  - **MCP-agent-behavior gaps** → edit `../neotoma/docs/developer/mcp/instructions.md` (first fenced code block only).
  - **Workspace-rule-content gaps** → edit the specific `.cursor/rules/*.mdc` file(s) in this repo, only within the rule body (preserve frontmatter unless the rule's activation conditions are themselves the gap, in which case tighten `description` / `globs` / `alwaysApply` minimally).
  - **Both** → apply both in the same turn.
- For MCP-agent-behavior edits: add or edit only lines inside the first fenced block to produce the updated block. Add explicit mandatory instruction(s) tailored to the specific contract point that failed in step 2.
- For storage-side failures:
  - Generic cross-MCP: "When you have pulled data from another MCP (email/calendar/search), do not respond with that data until all relevant entities are extracted and stored in Neotoma in the same turn."
  - Attachments: strengthen EMBEDS / combined-store-path wording and the prohibition against host-only copies.
  - Provenance: strengthen `data_source` / `api_response_data` / source-artifact retention wording.
  - Closing store: strengthen the "MUST NOT end the turn without the closing assistant store when you produced a user-visible reply" wording.
  - Agent-authored deliverable files: strengthen the instruction that markdown/text/json/csv deliverables authored or materially edited by the agent must themselves be stored in Neotoma in the same turn via the combined store path, alongside a structured note/report/research entity; repo-only copies are insufficient.
- For retrieval-side failures:
  - Missing bounded retrieval: "Before responding or storing, perform bounded retrieval (`retrieve_entity_by_identifier` for concrete identifiers, `retrieve_entities` for category/list/recency queries) for entities implied by the user message."
  - Wrong query shape: reinforce the identifier-vs-category distinction and the `entity_type` + limit/time-window requirement for list-intent queries.
  - Ignored retrieved context: "When retrieval returns relevant results, ground the reply in those results (or reconcile them explicitly); do not invent memory-backed claims, and do not silently ignore matches."
  - Duplicate creation: "Check for existing records via bounded retrieval before storing entities that could already exist; reuse the existing `entity_id` for relationships when a match is found."
  - Recency queries: reinforce `published_date`/`published_at` DESC + dedup by `entity_id` for "recently published" queries vs `last_observation_at` for observation recency.
  - Cardinality queries: reinforce `getStats` / `GET /stats` as the first-choice source for per-type counts, with an explicit prohibition against using `list_entity_types` `field_count` as cardinality.
  - Completeness: reinforce the bounded completeness pass for list/count answers with dedup by `entity_id`.
- For workspace-rule-content failures (edit the relevant `.cursor/rules/*.mdc` file):
  - QA audit / feedback reporting (`neotoma_harness` + `neotoma_qa_reflection_deep`): strengthen the Tier 1–4 audit clauses, the durable-enhancement ladder, a specific severity-classification entry that was silently skipped, or the `Feedback-tool reporting` section's trigger, bullet shape, 🟢/🟡/🔴 mapping, or polling/privacy rules for `submit_feedback` / `get_feedback_status`. Do not reintroduce the retired `Neotoma QA:` footer format — the `🧠 Neotoma` turn report is the only sanctioned disclosure surface.
  - Skills fetch-from-Neotoma (`skills_neotoma_proactive_fetch`): strengthen the "MUST fetch full content via `retrieve_entity_snapshot` before executing" wording, the stale-stub threshold, or the prohibition against writing full skill content into the repo.
  - Post updates / cache (`post_updates_neotoma_cache`): strengthen the same-turn Neotoma-update + cache-regen requirement, or the explicit-deploy boundary.
  - Transactions scope (`transactions_neotoma_only`): strengthen the prohibition against duplicating transaction data in side files.
  - Activation-condition gaps: if the rule simply did not load when it should have, tighten `description` (more specific "load when..." language) and/or `globs` so the rule activates for the scenario that failed.
- Optionally strengthen existing lines with "do not respond until storage is complete", "MUST", or an explicit FORBIDDEN clause rather than appending a new line when the gap is already partially covered. The same preference applies to rule-file edits: tighten existing clauses before adding new sections.
- If the explicit scenario includes a local report/draft/analysis file path and the file is available, persist that artifact in Neotoma in the same turn using the combined store path and link it to the prompting conversation/user message; do not limit the fix to instruction text when the missing durable artifact can still be repaired immediately.
- Keep one instruction per line in the MCP instructions fenced block. No nested code blocks there. Deduplicate when new lines overlap existing lines.
- In `.cursor/rules/*.mdc` bodies, match the surrounding markdown style (headers, bullets, fenced examples) and keep edits minimal and localized.
- Apply directly: write the updated files in the same turn. For the MCP instructions file, write the updated first fenced code block and preserve everything else. For rule files, preserve frontmatter and unrelated sections.

### 5) Report what changed

Always report, regardless of whether a fix was applied:

- Whether the last turn was classified Compliant, Non-compliant, or Ambiguous (or whether an explicit user-provided scenario was used instead).
- Which Neotoma contract points were checked, grouped as retrieval-side, storage-side, and workspace-rule-side. Name the specific rule files that were evaluated in the last group. Cite any failing check by name (contract point or rule clause).
- If Non-compliant: the failure case used, whether it was a retrieval gap, a storage gap, a workspace-rule gap, or a combination; which files were edited (the Neotoma MCP instructions file, one or more `.cursor/rules/*.mdc` files, or both); the specific lines added or changed; and a reminder that MCP instructions are picked up on next load (no code deploy required) while workspace rules take effect immediately in-repo.
- If Compliant or Ambiguous: state explicitly that no instruction or rule change was applied and summarize the evidence on retrieval, storage, and workspace-rule sides that supported that classification.

## Constraints

- Always apply Neotoma MCP instruction and workspace-rule changes directly in the same turn; do not require preview/confirmation before writing.
- Prefer strengthening the Neotoma MCP instructions over editing workspace rules when the gap is about MCP agent behavior. Only edit `.cursor/rules/*.mdc` when the gap is clearly about the rule's own content (missing clause, unclear wording, outdated obligation, or activation conditions that failed to load the rule when they should have).
- Do not edit `neotoma/src/server.ts` or fallback array.
- Do not modify the Neotoma MCP instructions file outside the first fenced code block.
- Do not change rule frontmatter schema fields other than `description`, `alwaysApply`, and `globs`; and only adjust those three when the rule's activation conditions are themselves the gap.
- Do not create new rule files from this skill. If a missing rule is genuinely required, report that as a recommendation in step 5 rather than inventing one.
- If a primary target file is missing or the fenced block cannot be found, report error and do not create/overwrite file structure.
