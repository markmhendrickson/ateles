# Entity schemas for process-meeting

> **Registration status (2026-08-21).** All six new types below are REGISTERED and ACTIVE in Neotoma prod:
> `transcription_profile` `ab48169d-f065-4271-ad12-d4e33bf9ac83` · `meeting_topic_read` `a61d4efc-5bf9-47a9-8c18-33e857666fb9` ·
> `participant_position` `b98bbd16-1a9a-407b-a7d5-37e39c55a7ce` · `meeting_value_assessment` `2a7deaef-349f-49ed-9a7c-096a385930f1` ·
> `recommended_message` `cd57f606-48d0-45af-b70b-36ab8f8e39eb` · `recommended_rendered_page` `c7ced7d0-c16e-4b40-bd13-841d0952d2ca`.
>
> The **extensions to `meeting_analysis` and `transcription` are NOT registered** and currently cannot be — see the two blockers noted in those sections. Their fields will land in `raw_fragments`: preserved, but not queryable. Query the child entities by `meeting_analysis_entity_id` instead.
>
> The type was named `meeting_topic_read` rather than `meeting_topic_analysis` because Neotoma's type-name linter rejects every `*_analysis` name as plural.

Field definitions for the six entity types this skill introduces, plus the extensions it needs on existing types. Register new fields with `update_schema_incremental` before the first store so data lands in declared fields rather than raw fragments.

Always `describe_entity_type` first — these definitions are the intent, the server is the authority.

---

## Existing types: extensions needed

### `transcription` (existing) — EXTENSIONS BLOCKED

> **Blocker:** `update_schema_incremental` returns `ERR_SCHEMA_MISSING_IDENTITY_CONFIG` — the existing schema declares neither `canonical_name_fields` nor `identity_opt_out`. Adding these fields requires re-registering the full schema for a type with 878 live entities, which changes its identity rule and must not be done casually. Until then these fields land in `raw_fragments`.


Add for Phase 1 preservation and Phase 2 fidelity:

| Field | Type | Notes |
|---|---|---|
| `audio_source_id` | string | Uploaded WAV artifact id |
| `mic_audio_source_id` | string | Companion mic file, when two-file capture |
| `video_source_id` | string | Screen capture, when present |
| `frame_source_ids` | array | Uploaded frame stills |
| `frame_count` | number | Total frames extracted (not necessarily uploaded) |
| `frame_interval_seconds` | number | From `RECORD_MEETING_FRAME_INTERVAL` |
| `frame_sampling` | string | Exactly what was uploaded vs extracted, e.g. `all` or `every_3rd_plus_endpoints` |
| `raw_transcript_source_ids` | array | Engine JSON, merged text, realtime chunks |
| `capture_started_at` | date | |
| `duration_seconds` | number | |
| `channel_layout` | string | `stereo_separated` \| `mono` \| `two_file_merge` |
| `device_notes` | string | Mic/system device substrings actually used |
| `speaker_label_map` | object | `{ "Speaker_1": "Alice Chen" }` — unresolved labels omitted, never guessed |
| `preservation_status` | string | `complete` \| `partial:<what failed>` |

`capture_method` already exists (see `transcribe_audio.py --capture-method`).

### `meeting_analysis` (existing, v1.0, 36 fields) — EXTENSIONS BLOCKED

> **Blocker:** Neotoma's type-name linter rejects `meeting_analysis` as "plural" (reading `-is` as a plural ending, suggesting `meeting_analysi`), which blocks `update_schema_incremental` on a type with 71 live entities. `force: true` is not exposed through the MCP tool. Until fixed, the added fields below land in `raw_fragments`.


Reuse declared fields: `title`, `meeting_date`, `meeting_type`, `source_type`, `source_reference`, `report_path`, `participant_names`, `participant_contact_entity_ids`, `calendar_event_entity_id`, `summary_bullets`, `decisions`, `action_items_mine`, `action_items_theirs`, `action_items_joint`, `open_questions`, `topics`, `risks_or_blockers`, `repo_signal`, `pii_inventory`, `artifact_recommendations`, `data_source`, `tags`.

**Known schema pollution:** `to_name`, `to_email`, `format`, `subject`, `body`, `participant_contact_entity_id`, `delivery_channel`, `delivery_status`, `gmail_draft_id` leaked in from `recap_message` stores and do **not** belong to `meeting_analysis` — never populate them here. `source` is mistyped as `date` (it holds a string); prefer `data_source`. `description`, `status`, `due_date`, `priority` similarly leaked from `task`.

Add:

| Field | Type | Notes |
|---|---|---|
| `entity_mentions` | array | `{ mention, mention_type, verbatim }` — Phase 5 step 9, over-inclusive |
| `graph_reconciliation` | array | `{ mention, resolved_entity_id, classification, action_taken, evidence_quote }`; classification ∈ `confirms` \| `extends` \| `contradicts` \| `introduces` \| `stale_marks` \| `ambiguous` |
| `matter_entity_ids` | array | Substantive entities resolved in Phase 4 step 4 — the matter, not the people |
| `transcription_profile_entity_id` | string | |
| `topic_analysis_entity_ids` | array | |
| `participant_position_entity_ids` | array | |
| `research_inquiry_entity_ids` | array | |
| `value_assessment_entity_id` | string | |
| `recommended_message_entity_ids` | array | Phase 12 |
| `recommended_rendered_page_entity_id` | string | Phase 13 |
| `preservation_status` | string | Mirrors the transcription's, for query convenience |
| `depth` | string | `quick` \| `standard` \| `deep` |
| `skipped_for_depth` | array | `{ what, why }` — the no-silent-caps record |

### `recap_message` (existing — NO LONGER WRITTEN)

Prior runs of `analyze-meeting` created these. They stay valid and readable. **process-meeting creates none** — Phase 12 produces `recommended_message` instead, which carries no drafted body.

---

## New type: `transcription_profile`

The learning record. Written after every transcription, read before every one.

| Field | Type | Notes |
|---|---|---|
| `transcription_entity_id` | string | |
| `capture_method` | string | Signature field |
| `channel_layout` | string | Signature field |
| `device_notes` | string | Signature field |
| `participant_signature` | string | Sorted contact entity ids joined — signature field |
| `engine` | string | `elevenlabs` \| `openai_whisper` |
| `model_id` | string | e.g. `scribe_v2` |
| `mode` | string | `multichannel` \| `diarized` \| `two_file_merge` \| `whisper_plain` |
| `language_used` | string | What was passed |
| `language_detected` | string | What the engine reported — divergence is a lesson |
| `quality_grade` | string | `excellent` \| `good` \| `degraded` \| `poor` \| `failed` |
| `quality_metrics` | object | `{ word_count, speaker_count_detected, unlabeled_segment_ratio, mean_word_confidence, inaudible_segment_count }` |
| `duration_seconds` | number | |
| `wall_clock_seconds` | number | How long transcription took |
| `estimated_cost_usd` | number | When derivable |
| `problems` | array | `{ symptom, likely_cause, evidence }` |
| `lessons` | array | `{ lesson, applies_when, action }` — `action` must be executable: a flag, an env var, a step to skip |
| `operations_skipped` | array | `{ operation, reason, prior_profile_entity_id }` |
| `superseded_by` | string | Set when a later profile for the same signature replaces this one |
| `researched_date` | date | Profile creation date |

**Retrieval pattern:** `retrieve_entities` on `transcription_profile` filtered by `capture_method` + `channel_layout`, and separately by `participant_signature`. Apply every `lesson` whose `applies_when` matches.

---

## New type: `meeting_topic_read`

One per topic. Fans out as parallel subagents.

| Field | Type | Notes |
|---|---|---|
| `topic` | string | |
| `meeting_analysis_entity_id` | string | |
| `synthesis` | string | What was said, with quotes |
| `affected_entities` | array | `{ entity_id, entity_type, how_affected }` |
| `strategic_read` | string | Grounded in plan `ent_99ace4dd6673aa36ed08b1fe` — retrieve, don't assume |
| `implications` | array | `{ implication, confidence, horizon }`; horizon ∈ `immediate` \| `this_quarter` \| `long_term` |
| `changes_our_position` | boolean | Most topics confirm rather than change — say so |
| `changes_our_position_explanation` | string | |
| `contradicts` | array | `{ entity_id, stored_assumption, meeting_evidence }` |
| `open_threads` | array | Feeds Phase 9 |
| `recommended_actions` | array | Feeds Phase 10 |
| `strategic_weight` | number | Ranking basis; recorded so skipped topics are visible |

---

## New type: `participant_position`

One per participant per meeting. Evaluates arguments, never people. **Never leaves the graph** — excluded from every Phase 12/13 recommendation and from all outbound text.

| Field | Type | Notes |
|---|---|---|
| `contact_entity_id` | string | |
| `participant_name` | string | |
| `meeting_analysis_entity_id` | string | |
| `positions` | array | See structure below |
| `stance_summary` | string | One paragraph: where they stand, on what basis |
| `changes_from_prior` | string | vs. prior `participant_position`; cite the entity id. `_No prior positions recorded._` on first meeting |
| `prior_position_entity_id` | string | |
| `credibility_notes` | string | Grounded in stored entities only — never impression |
| `attribution_confidence` | string | `high` \| `medium` \| `low` — set `low` when the transcription grade is `degraded` or worse |

Each entry in `positions`:

```json
{
  "claim": "…",
  "verbatim_quote": "…",
  "topic": "…",
  "position_type": "assertion|argument|preference|commitment|objection|question|concession",
  "support": "first_hand_experience|data_cited|third_party_report|inference|assertion_only",
  "contextual_relevance": "…",
  "related_entity_ids": ["ent_…"],
  "force": "high|medium|low",
  "force_justification": "…",
  "falsifiable_by": "…",
  "agreement_state": "agreed|disagreed|unaddressed|deferred"
}
```

`support` is the highest-value field — record it honestly. A forceful assertion with `support: assertion_only` from someone outside their domain is `force: low` regardless of delivery.

---

## New type: `meeting_value_assessment`

One per meeting. Runs after the analytical phases; Phases 12–13 read it. Inbound-only — **never quoted into outbound artifacts or recommendation bodies**.

| Field | Type | Notes |
|---|---|---|
| `meeting_analysis_entity_id` | string | |
| `value_grade` | string | `high` \| `moderate` \| `low` \| `negative` — `negative` is a real option |
| `value_rationale` | string | Decisions reached, blockers cleared, information gained the graph lacked, commitments secured |
| `what_changed` | array | Concrete deltas |
| `what_did_not_change` | array | Equally informative, usually omitted |
| `counterfactual` | string | Would an email have done this? |
| `cost` | string | Duration + prep + follow-through, against the value |
| `follow_through_risk` | string | What quietly fails absent action, and which task guards it |
| `relationship_significance` | array | One entry per non-Mark participant, structure below |

Each entry in `relationship_significance`:

```json
{
  "contact_entity_id": "ent_…",
  "participant_name": "…",
  "relationship_state_before": "…",
  "relationship_state_after": "…",
  "trajectory": "strengthened|maintained|strained|new|dormant_reactivated",
  "trajectory_evidence": "…",
  "significance": "high|moderate|low",
  "reciprocity_state": { "mark_owes": ["…"], "they_owe": ["…"] },
  "trust_signals": [{ "signal": "…", "quote": "…", "direction": "toward_mark|toward_them" }],
  "next_touch": { "action": "…", "timing": "…", "reason": "…" },
  "strategic_role": "customer|evaluator|partner|advisor|referrer|peer|other"
}
```

`next_touch` is an *input* to Phase 12, which turns it into a `recommended_message` and files the task. It carries no task id itself.

---

## New type: `recommended_message`

Phase 12. **Carries no drafted prose.** `key_points` are bullets a writer expands, never sentences a recipient could receive verbatim.

| Field | Type | Notes |
|---|---|---|
| `recipient_name` | string | |
| `recipient_contact_entity_id` | string | |
| `recipient_email` | string | Recorded so a later drafting run need not re-resolve. Never used to send from here |
| `not_in_meeting` | boolean | True when the recipient was not a participant |
| `channel` | string | `email` \| `message` \| `call` \| `in_person` \| `none` |
| `channel_rationale` | string | Grounded in how they actually correspond, not a default |
| `intent` | string | One sentence: the message's actual job, not "recap the meeting" |
| `why_worth_sending` | string | What happens if it is not sent. "Nothing much" → `channel: none` |
| `key_points` | array | `{ point, basis }` — basis traces to a decision, action item, or transcript moment |
| `must_not_include` | array | Unconfirmed commitments, Phase 8 positions, Phase 11 reads, Art. 9 disclosures, internal refs, other participants' private context |
| `tone` | string | Formality, warmth, directness, and criticism volume — named explicitly |
| `timing` | string | `same_day` \| `within_48h` \| `after:<event>` \| `before:<date>`, tied to something real |
| `open_loops_it_closes` | array | Entity ids of action items / open questions / reciprocity gaps |
| `references_recap_page` | boolean | True → `DEPENDS_ON` the `recommended_rendered_page` |
| `priority` | string | Per `priority_rubric` `ent_29ca079940c1e996a8c782f2` |
| `drafting_notes` | string | Voice guide to read, prior thread to match, language choice, cc list |
| `task_entity_id` | string | The companion task. Required unless `channel: none` |

**Validation before storing:** no field may contain a sentence addressed to the recipient in second person. If `key_points` reads as copy rather than a brief, rewrite it as bullets.

---

## New type: `recommended_rendered_page`

Phase 13. **Carries no `html_body` and no guest token.** Section briefs only; [`draft-rendered-page`](../draft-rendered-page/SKILL.md) builds the page when the operator runs the task.

| Field | Type | Notes |
|---|---|---|
| `recommended` | boolean | False is a first-class result |
| `recommendation_rationale` | string | Against the two-or-more criteria in Phase 13 |
| `meeting_analysis_entity_id` | string | |
| `audience` | array | `{ name, contact_entity_id, organization }` |
| `audience_register` | string | The page is written for its widest reader — name the constraint |
| `purpose` | string | Beyond recording: alignment, internal circulation, shared record |
| `proposed_title` | string | |
| `proposed_sections` | array | `{ section, what_it_covers, source }` — briefs, not copy |
| `visual_elements` | array | `{ element, what_it_shows, data_source }` — `draft-rendered-page` needs concrete SVG content |
| `must_not_include` | array | Always: Phase 8 positions, Phase 11 reads, unvetted Phase 9 research, other participants' private context, internal entity ids and repo paths, Art. 9 disclosures |
| `sharing_posture` | string | `guest_link_to_named_participants` (default) \| `internal_only` \| `public` |
| `neotoma_instance` | string | Target instance — client instance for client matters, personal prod otherwise |
| `instance_rationale` | string | Which participants, which matter entity, which instance already holds it |
| `instance_decision` | string | `resolved` \| `operator_required` — ambiguity is a disclosure risk, never a guess |
| `linked_from_messages` | array | `recommended_message` entity ids that should link this page |
| `task_entity_id` | string | The companion task. Required when `recommended: true` |

---

## Reused types

- **`research_inquiry`** (v1.0, 7 fields) — `question`, `origin` (`explicit`/`implicit`), `raised_by`, `why_it_matters`, `blocking`, `status` (`open`/`answered`/`partially_answered`/`unresolved`).
- **`research_finding`** (v1.1, 19 fields) — use declared fields: `title`, `subject`, `method`, `summary`, `conclusion`, `confidence`, `sources`, `options`, `researched_date`. Ignore the legacy `q1_git_granularity` / `q2_concurrency` / `q3_regex_edges` / `positioning_impact` / `risk_rating` fields — retained for old entities only.
- **`task`** — per `update-tasks` skill. `priority` maps through `priority_rubric` `ent_29ca079940c1e996a8c782f2`. Every Phase 12/13 recommendation gets one.
- **`proposed_github_issue`**, **`calendar_event`**, **`contact`** — unchanged from the prior skill.
