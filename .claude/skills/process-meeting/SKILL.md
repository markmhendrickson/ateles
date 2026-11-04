---
name: process-meeting
description: "Full meeting processing. Imports raw recording materials into Neotoma intact (audio, video, frames, every transcript variant), transcribes at the highest fidelity and diarization quality the available engines support, and records a reusable transcription_profile so future runs improve instead of repeating known-bad or needlessly expensive work. Then extracts every relatable fact into Neotoma against existing entities (updating them, not duplicating), files and updates Ateles swarm tasks, runs per-topic sub-analyses with strategic framing, evaluates each participant's positions and arguments for contextual force, conducts proactive external research to close open questions, and assesses the meeting's overall value and its significance to each relationship. Drafts NO outbound messages: it recommends the follow-up messages worth sending as tasks carrying intent, key points, and tone, and recommends a participant-facing recap rendered_page routed to the appropriate Neotoma instance. Supersedes analyze-meeting. Auto-invoked by /record_meeting on stop; also runs standalone on any transcript, audio file, or transcription entity."
triggers:
  - process meeting
  - process this meeting
  - ingest meeting
  - analyze meeting
  - analyze this meeting
  - meeting analysis
  - meeting recap
  - process meeting recording
  - /process-meeting
  - /ingest-meeting
  - /analyze-meeting
user_invocable: true
entity_id: ent_c6077840664ee87ae30b7922
supersedes: analyze-meeting
---

# Process Meeting

Take a meeting from raw capture to fully-metabolized memory in one pass: preserved source materials, best-available transcription, a graph updated against what Neotoma already knows, swarm tasks that reflect the new state of the world, per-topic strategic sub-analyses, a participant-by-participant read of who argued what and how much it should count, external research that closes the questions the meeting opened, and a judgment about what the meeting was actually worth and what it did to each relationship.

**This skill drafts nothing outbound.** No recap emails, no messages, no Gmail drafts, no published pages. Where a message or a page is worth producing, it says so as a task carrying the intent, the points to cover, and the register — and leaves the writing to the operator or a later drafting run. See [Phase 12](#phase-12-recommend-follow-up-communications) and [Phase 13](#phase-13-recommend-the-recap-rendered-page).

This skill **supersedes** `analyze-meeting`, which now forwards here. Complement to [`analyze-neotoma-feedback`](../analyze-neotoma-feedback/SKILL.md): that skill is Neotoma-customer-development-specific; this one handles any meeting and owns processing plus follow-through. When both fire on the same transcript, they cross-link via the shared `transcription` and `contact` entities and do not duplicate each other.

## When to use

- Auto-invoked by [`record-meeting`](../record_meeting/SKILL.md) after a successful stop.
- Manually: `/process-meeting <source>` for a transcript on disk, a raw audio/video file, or a Neotoma `transcription` entity id.
- Re-run on an already-processed meeting to deepen it — the skill is idempotent per source and will reuse rather than re-derive expensive artifacts (see [Phase 1](#phase-1-preserve-raw-materials) and [Phase 3](#phase-3-learn--record-the-transcription-profile)).

## Invocation

```
/process-meeting <source>
/process-meeting <source> --open-issues       # open real GH issues instead of staging drafts
/process-meeting <source> --no-research       # skip Phase 9 external research
/process-meeting <source> --retranscribe      # force re-transcription even if a good transcript exists
/process-meeting <source> --participants "Alice <alice@x>, Bob <bob@y>"
/process-meeting <source> --depth quick|standard|deep    # default: standard
```

`<source>` is auto-detected:
- **Audio/video file** (`.wav`, `.m4a`, `.mp3`, `.mp4`, `.mov`) — run the full pipeline from Phase 1.
- **Transcript file path** — skip capture; still preserve the transcript itself per Phase 1.
- **Neotoma entity reference** — a `transcription` entity id, or a name resolvable via `retrieve_entity_by_identifier`.
- **Raw pasted text** — anything that matches none of the above.

Flags:
- `--open-issues` — actually open public issues. Default **off** (staged as `proposed_github_issue`). Same as `MEETING_ANALYSIS_OPEN_GH_ISSUES=1`.
- `--no-research` — skip Phase 9. Researchable questions are still recorded as `research_inquiry` entities for later.
- `--retranscribe` — ignore an existing good transcript and re-run transcription. Costs money; only use when the profile says quality was poor.
- `--participants` — comma-separated `Name <email>` overrides when diarization labels are unreliable.
- `--depth` — controls sub-analysis and research breadth:
  - `quick` — Phases 1–6 + 11–14. No sub-analyses, no argument evaluation, no research.
  - `standard` (default) — all phases; sub-analyses on the top 3 topics by strategic weight; research on up to 5 questions.
  - `deep` — all phases; sub-analysis on every topic; research on every researchable question; per-participant argument evaluation at full granularity.

**There is no `--recap` flag and no drafting flag.** Recap content is never drafted by this skill under any flag combination.

## Phase map

| Phase | Requirement | Output |
|---|---|---|
| 1. Preserve raw materials | Fidelity | `transcription` + uploaded source files |
| 2. Transcribe at best fidelity | Fidelity | Transcript variants, all stored |
| 3. Record the transcription profile | Learning | `transcription_profile` |
| 4. Resolve participants, matter, calendar | Grounding | `contact`, `calendar_event`, matter entities |
| 5. Extract the core analysis | Extraction | `meeting_analysis` core fields |
| 6. Reconcile against the graph | Extraction | Updated existing entities |
| 7. Per-topic sub-analyses | Strategy | `meeting_topic_read` |
| 8. Participant argument evaluation | Positions | `participant_position` |
| 9. Proactive external research | Research | `research_inquiry`, `research_finding` |
| 10. Swarm task creation + update | Tasks | `task` created and corrected |
| 11. Meeting value + relationship significance | Value | `meeting_value_assessment` |
| 12. Recommend follow-up communications | Recommend | `recommended_message` + `task` per message |
| 13. Recommend the recap rendered page | Recommend | `recommended_rendered_page` + `task` |
| 14. Report, issues, persist, surface | Delivery | Report, Neotoma stores |

Phases 5–11 may run **concurrently as subagents** once Phase 4 completes — see [Concurrency](#concurrency). Phases 1–4 are strictly sequential. Phases 12–13 run after 11, because a recommendation depends on knowing the meeting's value and each relationship's state.

---

## Phase 1: Preserve raw materials

Nothing is analyzed before the source is safe. The graph must contain the artifact, not a path to it — a path field is not storage.

1. **Enumerate the capture set.** For a recording produced by `/record_meeting`, the set is larger than the WAV. Look next to the audio file for all of:
   - the audio WAV (and any `--mic-file` companion; stereo separation means two logical channels)
   - the screen-capture video (`.mp4`) when `RECORD_MEETING_VIDEO` was on
   - the `frames/` directory of extracted stills
   - the `<stem>_neotoma_relations.json` sidecar
   - any existing transcript text files (`<stem>.txt`, `last_meeting_transcription.txt`)
2. **Resolve or create the `transcription` entity.** Search Neotoma by `audio_file_path` (per `is_already_transcribed` in `execution/scripts/transcribe_audio.py`) and by `original_source_file`. Reuse when found — never create a second `transcription` for the same recording.
3. **Upload every artifact.** For each file in the capture set, call `store` with `file_path` so the bytes land in Neotoma with content-addressed dedup. Capture each returned `source_id`. Record them on the `transcription` entity as:
   - `audio_source_id`, `mic_audio_source_id`, `video_source_id`
   - `frame_source_ids` (array), `frame_count`, `frame_interval_seconds`
   - `raw_transcript_source_ids` (array — one per transcript variant, see Phase 2)
   Frames: upload all of them at `deep`, and at `standard` upload all when `frame_count <= 60`, otherwise upload every Nth to stay under 60 while always keeping the first and last. Record `frame_sampling` describing exactly what was uploaded so the omission is visible rather than silent.
4. **Never delete or move the local originals.** This skill only reads them.
5. **Record capture provenance** on the `transcription`: `capture_method` (see the `--capture-method` values in `transcribe_audio.py`), `capture_started_at`, `duration_seconds`, `channel_layout` (`stereo_separated` | `mono` | `two_file_merge`), `device_notes`.

If an upload fails, record `preservation_status: partial:<what failed>` on the `transcription` and continue — but say so explicitly in the final report. Never report full-fidelity preservation you did not achieve.

## Phase 2: Transcribe at best fidelity

Goal: the best transcript the available engines can produce for *this* audio's shape, with every intermediate kept.

1. **Check for an existing acceptable transcript first.** If the `transcription` entity already has text and its linked `transcription_profile` (Phase 3) reports `quality_grade` of `good` or better, reuse it. Re-transcribe only under `--retranscribe`, or when the profile grades it `poor`/`failed` **and** names a remedy that is now available (e.g. an API key that was missing then is present now). This is the primary defense against repeating expensive operations.
2. **Select the engine and mode by channel layout.** Follow what `transcribe_audio.py` already implements rather than inventing a parallel path — invoke the script, do not reimplement it:
   - **Stereo separated** (`ch1=system`, `ch2=mic`) → ElevenLabs **multichannel**, which merges `[System]` / `[Mic]` in time order when word timestamps exist. Highest-fidelity attribution available; prefer this whenever the recording was made with `--separate-sources`.
   - **Two files** (`--mic-file` present) → two-file merge: mic as single speaker `[You]`, remote diarized as `[Speaker_N]`.
   - **Mono** → ElevenLabs diarization (`--diarize`).
   - **No `ELEVENLABS_API_KEY`** → OpenAI Whisper, no diarization. Grade the result `degraded` in Phase 3 and record the missing key as the remedy.
   - Model id comes from `ELEVENLABS_STT_MODEL_ID` (default `scribe_v2`). Do not downgrade it.
3. **Pass known context to improve accuracy.** Before transcribing, gather participant names (Phase 4 does this properly, but a cheap pre-pass helps): calendar attendees for the recording window, and `contact` names already in Neotoma for those attendees. Supply `--language` when the meeting language is known from the calendar event or prior meetings with the same participants — auto-detect is a common source of Spanish/Catalan/English mislabeling in Barcelona meetings.
4. **Store every transcript variant, not just the final one.** Each of these is a separate uploaded artifact linked to the `transcription`:
   - the raw engine response (JSON with word timestamps and speaker turns) — this is the highest-fidelity object and must be preserved even though it is not human-readable
   - the merged, speaker-labeled plain-text transcript
   - any realtime chunk transcripts produced during recording
   Put the human-readable merged transcript in the `transcription` entity's text field; put the rest in `raw_transcript_source_ids`.
5. **Assess quality** and carry the numbers into Phase 3: `word_count`, `speaker_count_detected`, `unlabeled_segment_ratio`, `mean_word_confidence` when the engine reports it, and a count of `[inaudible]`/empty segments.

**Speaker naming.** Diarization gives `[Speaker_1]`, not names. Map labels to real names using: direct address in the transcript ("Thanks, Alice"), self-introduction, the channel convention (`[Mic]`/`[You]` is always Mark), and calendar attendees. Store the mapping as `speaker_label_map` on the `transcription` (`{ "Speaker_1": "Alice Chen" }`). When a label cannot be resolved confidently, leave it unmapped rather than guessing — a wrong attribution poisons every downstream phase, especially Phase 8.

## Phase 3: Learn — record the transcription profile

This is the mechanism that makes future runs better and cheaper. It is not optional bookkeeping; Phase 2 step 1 reads it.

**Before transcribing**, retrieve prior `transcription_profile` entities matching this recording's signature and apply their lessons:

- Signature fields: `capture_method`, `channel_layout`, `device_notes`, `engine`, `model_id`, `language`, and `participant_signature` (sorted contact entity ids, when known).
- Retrieve via `retrieve_entities` on `transcription_profile` filtered by `capture_method` + `channel_layout`, and separately by `participant_signature`.
- Apply every `lesson` whose `applies_when` matches the current recording — e.g. "this participant's audio is low-gain; multichannel mislabels their turns as system audio; force `--mic-file` merge instead", or "Whisper auto-detect labels this recurring bilingual meeting as Spanish; pass `--language en`".
- If a prior profile records that a specific expensive operation was **useless** for this signature (e.g. frame extraction on an audio-only call, diarization on a solo memo), skip it and note that you did.

**After transcribing**, store a `transcription_profile` entity with:

- `transcription_entity_id`, `signature` fields as above
- `engine`, `model_id`, `mode` (`multichannel` | `diarized` | `two_file_merge` | `whisper_plain`), `language_used`, `language_detected`
- `quality_grade`: `excellent` | `good` | `degraded` | `poor` | `failed`
- `quality_metrics`: the numbers from Phase 2 step 5
- `duration_seconds`, `wall_clock_seconds`, `estimated_cost_usd` when derivable
- `problems` (array of `{ symptom, likely_cause, evidence }`) — what actually went wrong
- `lessons` (array of `{ lesson, applies_when, action }`) — the durable, reusable instruction for next time. `action` must be concrete and executable (a flag, an env var, a step to skip), not an aspiration.
- `operations_skipped` (array of `{ operation, reason, prior_profile_entity_id }`) — the audit trail of what this run avoided and why.
- `superseded_by` — null; set when a later profile for the same signature supersedes this one.

Link `PART_OF` from the profile to the `transcription`, and `SUPERSEDES` to any prior profile with the same signature that this run's lessons replace.

**Grading rubric** (apply honestly — an inflated grade means Phase 2 reuses a bad transcript forever):
- `excellent` — every speaker labeled and named, no unlabeled segments, no inaudible runs.
- `good` — all speakers labeled, ≤5% unlabeled segments, names resolvable.
- `degraded` — transcription usable but attribution partly lost (e.g. Whisper fallback, no diarization). Downstream Phase 8 must be marked lower-confidence.
- `poor` — significant unlabeled or inaudible content; conclusions unsafe.
- `failed` — no usable transcript.

When a profile is graded `poor` or `failed`, also create a `task` for fixing the underlying capture problem (Phase 10), because the fix is usually a device or env change, not a re-run.

## Phase 4: Resolve participants, matter, and calendar

1. **Identify participants** from: the `speaker_label_map`, name mentions, salutations and sign-offs, `--participants` overrides, and the calendar event.
2. **Match the calendar event.** Determine the recording timestamp from the source file mtime or the `YYYY-MM-DD-HHMMSS` filename. Query `gws calendar events list --timezone Europe/Madrid` for a ±90-minute window. Pick the best match by title overlap, attendee-name overlap, and timing. On a match, extract attendee names and emails, store/reuse a `calendar_event` entity (`title`, `start_time`, `end_time`, `attendees` as `{ name, email }`, `calendar_event_id`), and record `calendar_event_entity_id` on the analysis. On no match, note `_Calendar: no matching event found._` and proceed.
3. **Resolve each participant to an existing entity** via `retrieve_entity_by_identifier` against `contact` and `person`, by name **and** by email. Reuse aggressively — a duplicate contact fragments the relationship history that Phase 11 depends on. Create a stub only when there is genuinely no match, and mark it `identity_confidence: stub_from_meeting`.
4. **Resolve the meeting's SUBSTANTIVE entities against Neotoma (required — run before any Phase 5 composition).** A transcript's real subject is rarely in the invoking text ("transcribe and analyze"); it is in the *content*. Before composing the analysis, extract the entities the meeting is *about* — not just its participants — and look each up so the analysis is written against existing memory rather than from scratch. For each that appears in the transcript, run `retrieve_entity_by_identifier` (concrete names) or `retrieve_entities` (topical search):
   - **Organizations / vendors / builders / counterparties** named or alluded to.
   - **Ongoing disputes, claims, projects, or matters** the meeting advances (search by topic plus any proper noun: property address, product, matter name).
   - **Properties, assets, or locations** central to the discussion.
   - **Prior meetings / transcriptions / dispute_notes / plans** on the same matter, so this analysis links into an existing thread instead of forking a parallel one.

   Use the matches to (a) reuse existing entity_ids and link into them via `REFERS_TO`, (b) name entities correctly instead of as generic stand-ins ("the builder" → the actual org), and (c) reconcile findings against what memory already records. **The source-file dedup lookup (Phase 1 step 2) and participant resolution (step 3 above) do NOT satisfy this step** — those find the recording and the people, not the matter. If nothing matches, note it and proceed; the lookup is not optional.
5. **Load relationship context** for each resolved participant, because Phases 8, 11, 12, and 13 need it: prior `meeting_analysis` entities referring to them, open `task` entities owed to or by them, prior `participant_position` entities, recent `email_message` threads, and their `contact` snapshot. Summarize into a working `relationship_prior` for each — last interaction date, interaction count, outstanding commitments in both directions, and the trajectory as previously recorded.
6. **Classify the meeting type**: `customer_call`, `partner_call`, `1_on_1`, `internal`, `interview`, `other`. Ambiguous cases get the most defensible label plus an explanation. Do not refuse to classify.

### Data minimization (RGPD legitimate-interest discipline)

Per CLAUDE.md "People-data processing": storage of participants and transcripts runs under RGPD Art. 6(1)(f) legitimate interest, **not** the household exemption, because the data drives professional action toward those people.

- Keep relationship-relevant facts: role, context, commitments, follow-ups, stated positions.
- Do **not** persist incidental Art. 9 sensitive disclosures — health, finances, family situations, political or religious views — into durable `contact` profiles or into `participant_position` entities unless directly relevant to a stored task. Summarize rather than transcribe verbatim when a sensitive detail is incidental.
- This constraint binds Phases 5–13 as much as Phase 4. The raw transcript preserved in Phase 1 is the record; the derived profiles are deliberately narrower than it.
- If a participant asks not to be tracked, treat it as an Art. 21 objection: stop enrichment on that entity, mark it, and surface it to the operator. Never argue them down.

---

## Phase 5: Extract the core analysis

Read the full transcript. Extract with verbatim quotes wherever possible; paraphrase is explicitly labeled `paraphrase: …`. Never invent a quote.

1. **Summary** — 3–5 bullets: why the meeting happened, what was discussed, the outcome.
2. **Decisions** — each with what was decided, who decided, and a verbatim quote when present.
3. **Action items**, split: **mine** (`{ description, due_date?, repo? }`), **theirs** (`{ person, description, due_date? }`), **joint/TBD**.
4. **Open questions** — unresolved, with owner. Mark each `researchable: true|false` — this feeds Phase 9.
5. **Topics + key threads** — `{ topic, synthesis }`. These become the units of Phase 7.
6. **Risks / blockers** — `{ risk, raised_by, quote? }`.
7. **Repo / project signal** — `{ repo_or_project, related_item_refs, public_issue_candidate }`.
8. **PII inventory** — names, emails, phone numbers, employer references, internal project names. Drives the scrubbing rules in Phase 14.
9. **Entity mentions** — every person, company, product, place, date, amount, and commitment named in the meeting, with the verbatim mention. This is the raw input to Phase 6 and should be over-inclusive; Phase 6 decides what is worth storing.

Sections with no signal render as `_None._` rather than being omitted, so cross-meeting queries stay reliable.

## Phase 6: Reconcile against the graph

The meeting is new information about a world Neotoma already models. Extraction that only creates is extraction done wrong.

For each entity mention from Phase 5 step 9:

1. **Resolve before creating.** `retrieve_entity_by_identifier` on the mention; for ambiguous mentions use `identify_entity_by_signals`. Check `list_potential_duplicates` before introducing a near-name.
2. **Classify the mention** as one of:
   - **Confirms** — restates what the graph already holds. Store nothing; optionally note the corroboration.
   - **Extends** — new fact about a known entity. Add via `store` as a new observation on that entity.
   - **Contradicts** — conflicts with a stored value. Use `correct` with idempotency key `process-meeting-<meeting_slug>-<field>` and cite the transcript quote as the basis. Never silently overwrite; a correction is a claim and needs its evidence.
   - **Introduces** — genuinely new entity. Create it with full provenance back to the `meeting_analysis`.
   - **Stale-marks** — reveals stored information is now out of date without stating the new value (e.g. "she's not at that company anymore"). Correct the field to reflect the uncertainty and file a Phase 10 task to establish the current value.
3. **Update the relationship graph, not just fields.** New `works_at`, `partner_of`, `invested_in`, `manages` edges surfaced in conversation are exactly what this pass is for. Create them via `create_relationships`.
4. **Contact enrichment** on each participant: role, current employer, active projects, stated preferences, communication cadence — subject to the Art. 9 constraint above.
5. **Record what you did.** Every reconciliation lands in `graph_reconciliation` on the `meeting_analysis`: an array of `{ mention, resolved_entity_id, classification, action_taken, evidence_quote }`. This is what makes the pass auditable and what stops the next run from redoing it.

**Never create a duplicate.** When resolution is ambiguous between two existing entities, do not pick one and do not create a third: record the ambiguity in `graph_reconciliation` with `classification: ambiguous` and file a Phase 10 task to disambiguate.

## Phase 7: Per-topic sub-analyses

For each topic from Phase 5 step 5 (top 3 by strategic weight at `standard`, all at `deep`), produce a `meeting_topic_read` entity. Run these as parallel subagents — they are independent.

Each sub-analysis contains:

- `topic`, `meeting_analysis_entity_id`
- `synthesis` — what was actually said on this topic, with quotes.
- `affected_entities` — `{ entity_id, entity_type, how_affected }` for every Neotoma entity this topic bears on: plans, tasks, contacts, companies, repos, execution policies.
- `strategic_read` — what this topic means for the operator's active strategy. Ground it in the plan (`ent_99ace4dd6673aa36ed08b1fe`) and in current phase blockers; retrieve them rather than assuming.
- `implications` — array of `{ implication, confidence, horizon }` where horizon is `immediate` | `this_quarter` | `long_term`.
- `changes_our_position` — boolean plus explanation. Does this topic change what the operator should do, or merely confirm it? Most topics confirm. Say so when they do rather than manufacturing significance.
- `contradicts` — anything in this topic that cuts against a stored assumption, decision, or plan field. Link `REFERS_TO` the contradicted entity.
- `open_threads` — what remains unresolved on this topic specifically.
- `recommended_actions` — feeds Phase 10.

**Strategic weight** for ranking topics at `standard` depth: how many existing entities the topic touches, whether it contradicts a stored assumption, whether it affects an active plan blocker, and whether it carries a commitment by either party. Rank on those, and state the ranking in the report so a skipped topic is visible.

## Phase 8: Participant argument evaluation

For each participant (including Mark), evaluate the positions they took. One `participant_position` entity per participant per meeting.

Fields:

- `contact_entity_id`, `participant_name`, `meeting_analysis_entity_id`
- `positions` — array of `{ claim, verbatim_quote, topic, position_type }` where `position_type` is `assertion` | `argument` | `preference` | `commitment` | `objection` | `question` | `concession`.
- For each position also record:
  - `support` — what backing they offered: `first_hand_experience` | `data_cited` | `third_party_report` | `inference` | `assertion_only`. This is the single most useful field; record it honestly.
  - `contextual_relevance` — how much this bears on the entities in play, and which ones. Link `REFERS_TO` those entities.
  - `force` — `high` | `medium` | `low`, plus a one-sentence justification. Force is a function of support quality, the speaker's standing on this specific question, and how directly it bears on a live decision. A confident assertion with no support from someone outside their domain is `low` force no matter how forcefully delivered.
  - `falsifiable_by` — what evidence would settle it. When this is nameable and the answer is external, it becomes a Phase 9 `research_inquiry`.
  - `agreement_state` — `agreed` | `disagreed` | `unaddressed` | `deferred` between this participant and Mark.
- `stance_summary` — one paragraph: where this person stands overall, on what basis.
- `changes_from_prior` — compared to their prior `participant_position` entities: what moved, what hardened, what they dropped. Cite the prior entity id. `_No prior positions recorded._` on first meeting.
- `credibility_notes` — calibration on this participant's track record where the graph supports it: have their prior predictions or commitments held up? Ground every note in a stored entity; never editorialize about a person from impression alone.

**Discipline for this phase:**

- Evaluate arguments, not people. `force: low` is a judgment about a claim's support, never about the person's worth.
- Do not fabricate operator internal state. If Mark did not voice agreement, the state is `unaddressed`, not inferred assent.
- When the Phase 3 grade is `degraded` or worse, attribution is unreliable — set `attribution_confidence: low` on the entity and say so in the report. Do not build a confident position profile on a transcript that could not tell speakers apart.
- Sensitive disclosures stay out per the Art. 9 rule, even when they were argumentatively relevant. Summarize the argument's shape without the sensitive particulars.

## Phase 9: Proactive external research

Close the questions the meeting opened, rather than filing them away. Runs by default; `--no-research` records inquiries without executing them.

1. **Assemble the inquiry set** from: Phase 5 open questions marked `researchable`, Phase 7 `open_threads`, Phase 8 `falsifiable_by` items, and **implicit** questions — factual claims a participant made that the meeting simply accepted, unfamiliar company/product/person/term references, and comparisons asserted without evidence. Implicit questions matter as much as explicit ones; a meeting rarely stops to ask what it should have.
2. **Store each as a `research_inquiry`** with `question`, `origin` (`explicit` | `implicit`), `raised_by`, `why_it_matters`, `blocking` (does an action item depend on the answer?), and `status: open`.
3. **Prioritize.** Research those that block an action item or bear on a live decision first. At `standard` depth research up to 5; at `deep` research all; at `quick` research none. When the cap truncates the set, say which inquiries went unresearched in the report — a silent cap reads as "we looked into everything."
4. **Research each** with `WebSearch` and `WebFetch`, plus the Neotoma graph itself when the answer might already be stored. Prefer primary sources: company site, filings, docs, the person's own writing. Cross-check any load-bearing claim against a second source.
5. **Store a `research_finding` per inquiry** using its declared fields: `title`, `subject`, `method` (searches run, sources consulted), `summary`, `conclusion`, `confidence` (state the basis, e.g. "confirmed from primary source" vs "absence of evidence in secondary coverage, not proven"), `sources` (URLs), `options` when it informs a decision, `researched_date`. Link `REFERS_TO` the `research_inquiry`, the `meeting_analysis`, and any entity the finding bears on. Mark the inquiry `status: answered | partially_answered | unresolved`.
6. **Feed the results back.** A finding that contradicts a participant's assertion updates that position's `force` in Phase 8. A finding that resolves a blocker updates the corresponding task in Phase 10. A finding that changes an entity's stored facts goes through Phase 6's reconciliation rules — including `correct` with the finding cited as evidence.

**Research honesty.** Never state a conclusion the sources do not support. "Could not determine" is a valid, useful finding — record it with `confidence` explaining what was searched and why it came up empty. Verify what a citation actually measures, not merely that it exists.

## Phase 10: Swarm task creation and update

The meeting changes what the swarm should be doing. Reflect that in Neotoma tasks in the same turn, per the standing CLAUDE.md plan-and-task contract.

**Retrieve the existing task inventory first** — open `task` entities related to the participants, the topics, the named repos, and the plan. Search task **bodies**, not just titles. Nothing below fires before this retrieval.

Then, for each candidate:

1. **Existing task confirmed** — the meeting says it is still needed. Correct `notes` with the new evidence; refresh `due_date` when a date was discussed.
2. **Existing task completed** — the meeting reveals it is done. Correct `status: done` with a `notes` field citing the transcript quote. (Never for yoga/therapy tasks — those only ever get `due_date` updated.)
3. **Existing task superseded or obsolete** — correct `status` and explain, citing the meeting.
4. **New task** — create a `task` entity: `description`, `due_date` (or `unspecified`), `status: open`, `priority` per the `priority_rubric` (`ent_29ca079940c1e996a8c782f2`), `source: process-meeting`, and `notes` carrying the evidence quote. Link `PART_OF` the plan (`ent_99ace4dd6673aa36ed08b1fe`) and `REFERS_TO` the `meeting_analysis` plus the relevant `contact` when it is owed to or by someone.

**Task sources — sweep all of them.** Action items (mine, theirs, joint) are the obvious source and the smallest one. Also generate tasks from: Phase 3 capture problems graded `poor`/`failed`; Phase 6 ambiguous or stale-marked entities needing disambiguation; Phase 7 `recommended_actions`; Phase 8 positions whose `falsifiable_by` needs internal work rather than external research; Phase 9 unresolved inquiries and findings that open new work; Phase 11 relationship actions; and Phases 12–13, whose entire output is task-shaped.

**Update the plan** in the same turn per CLAUDE.md: settled decisions into `decisions`, changed blockers into `next_steps`, completed todos to `done`. Use `correct` with idempotency key `update-plan-<field>-<YYYY-MM-DD>`.

**Deduplicate before filing.** A task that restates an open task is a correction to that task, not a new entity. When in doubt about whether two tasks are the same, link them `REFERS_TO` and note the overlap rather than creating a near-duplicate.

Use the [`update-tasks`](../update-tasks/SKILL.md) and [`update-plan`](../update-plan/SKILL.md) skills for field-value and priority-mapping guidance.

## Phase 11: Meeting value and relationship significance

One `meeting_value_assessment` entity per meeting. This is the synthesis phase — it runs last among the analytical phases because it reads the others' outputs, and Phases 12–13 read it.

**Overall value:**

- `value_grade` — `high` | `moderate` | `low` | `negative`. Negative is a real option (a meeting that cost time and set a relationship back), and using it when warranted is the point of having the field.
- `value_rationale` — what specifically made it valuable or not. Ground it: decisions reached, blockers cleared, information gained that the graph did not hold, commitments secured. A meeting where nothing was decided and nothing was learned is `low`, however pleasant it was.
- `what_changed` — the concrete deltas: entities updated, positions moved, tasks created or closed, questions answered.
- `what_did_not_change` — questions still open, decisions deferred, positions unmoved. Equally informative and usually omitted.
- `counterfactual` — would an email have done this? Say so when the answer is yes; it is a standing input to how the operator spends time.
- `cost` — duration plus preparation and follow-through, measured against the value.
- `follow_through_risk` — what will quietly fail if nothing else happens, and which task guards against it.

**Per-participant relationship significance** — an array, one entry per non-Mark participant:

- `contact_entity_id`, `participant_name`
- `relationship_state_before` / `relationship_state_after` — drawn from the Phase 4 `relationship_prior`, not from impression.
- `trajectory` — `strengthened` | `maintained` | `strained` | `new` | `dormant_reactivated`, with the evidence.
- `significance` — `high` | `moderate` | `low`: how much this meeting mattered *to this relationship specifically*, which is often different from the meeting's overall value.
- `reciprocity_state` — outstanding commitments in each direction. Asymmetry here is the most actionable thing in the whole entity: note when Mark owes more than he is owed, or the reverse.
- `trust_signals` — concrete things said or done that bear on trust in either direction, quoted. No speculation about feelings.
- `next_touch` — the recommended next contact and its timing, plus the reason. This is an input to Phase 12, which turns it into a concrete recommendation; the corresponding task is created there.
- `strategic_role` — what this person is to the operator's active work, grounded in the plan and stored entities: customer, evaluator, partner, advisor, referrer, peer. State it plainly; do not inflate a single call into a partnership.

**Discipline for this phase:**

- **No fabricated emotion or internal state**, for the operator or the participant. Every claim traces to something said, done, or previously stored.
- **Never predict a third party's reaction.**
- **Do not invent praise.** A meeting assessment is not a morale exercise; `low` value honestly assessed is more useful than a generous read.
- **The relationship read is inbound-only.** This entity informs the operator. It is never quoted into any outbound artifact, and never into a Phase 12 or 13 recommendation body that a recipient could see.

---

## Phase 12: Recommend follow-up communications

**This skill drafts no messages.** It identifies which follow-up communications are worth sending and specifies them well enough that writing one is a small, well-briefed job — then stops.

Explicitly prohibited in this phase and everywhere else in the skill:
- No `recap_message` entities carrying drafted bodies.
- No Gmail drafts. Do not call `gws gmail draft create`, and do not call the Gmail MCP.
- No drafted sentences intended for a recipient's eyes — not in the task, not in the report, not in the reply.
- No sending, under any flag.

### What to produce

For each communication worth sending, create one `recommended_message` entity **and** one linked `task`. The recommendation names the job; the task is how it reaches the operator's queue.

`recommended_message` fields:

- `recipient_name`, `recipient_contact_entity_id`, `recipient_email` (when known — recorded so the drafting run does not have to re-resolve it; never used to send anything here)
- `channel` — `email` | `message` | `call` | `in_person` | `none`. `none` is a legitimate output: see "When to recommend nothing".
- `channel_rationale` — why that channel fits this recipient and this content, grounded in how they have actually corresponded (Phase 4 `relationship_prior`), not a default.
- `intent` — one sentence: what this message is *for*. Not "recap the meeting" but the actual job, e.g. "confirm the two dates they committed to before their team's planning cycle closes" or "give them the comparison they asked for so the evaluation can proceed".
- `why_worth_sending` — what happens if it is not sent. When the honest answer is "nothing much", recommend `channel: none` instead.
- `key_points` — array of `{ point, basis }`. The points the message must cover, each traced to a decision, action item, or verbatim transcript moment. **Points, not prose** — a bullet a writer expands, never a sentence the recipient could receive verbatim.
- `must_not_include` — array. Things that would be wrong to put in this message: unconfirmed commitments, positions from Phase 8, anything from Phase 11, sensitive disclosures per the Art. 9 rule, internal repo or entity references, another participant's private context.
- `tone` — the register: how formal, how warm, how direct, and how much correction or pushback belongs in it. Ground it in the meeting's own tone and in prior correspondence with this person. Criticism volume is a tone variable; name it explicitly when the message carries any.
- `timing` — when to send and why: `same_day`, `within_48h`, `after:<event>`, `before:<date>`. Tie it to something real — their planning cycle, a stated deadline, a commitment date.
- `open_loops_it_closes` — which action items, open questions, or reciprocity gaps this message resolves. Link `REFERS_TO` them.
- `references_recap_page` — boolean: should this message link the Phase 13 recap page? When true, the drafting run must wait for the page to exist.
- `priority` — per the `priority_rubric`.
- `drafting_notes` — anything a writer needs that is not content: a voice guide to read first, a prior thread to match, a name spelling, a language choice (Spanish/Catalan/English), a person to cc.

### Companion task

One `task` per recommendation: `description` naming the recipient, channel, and intent; `status: open`; `due_date` from `timing`; `priority`; `source: process-meeting`. Link `PART_OF` the plan, `REFERS_TO` the `recommended_message`, the `meeting_analysis`, and the recipient `contact`.

The task is the deliverable. A recommendation with no task is a recommendation that will not happen.

### Grouping and coverage

- One recommendation per recipient, or one per organization when the same message serves several people and nothing in it is person-specific. Never bundle recipients whose `must_not_include` differ.
- **Not every participant warrants a message.** Recommending a message to everyone is the failure mode this phase exists to avoid.
- Sweep beyond participants: a follow-up may be owed to someone who was *not* in the meeting (an introduction promised, a colleague who needs the outcome, a third party whose input an open question needs). Recommend those too, marked `not_in_meeting: true`.

### When to recommend nothing

Record `channel: none` with `why_worth_sending` explaining the reasoning when: the meeting closed cleanly with no open loops, the next touch is a scheduled meeting rather than a message, the other party owes the next move, or a message would manufacture obligation. Recommending silence is a real recommendation — store it so the next run does not re-derive it, but file no task.

## Phase 13: Recommend the recap rendered page

A rendered page is the right recap medium for a meeting whose outcome is worth giving every participant in one shareable, durable artifact — better than a per-person email chain, because everyone sees the same record.

**Recommend it; do not build it.** This phase produces a `recommended_rendered_page` entity plus a `task`. It writes no `html_body`, stores no `rendered_page`, and mints no guest token. The page is built when the operator runs the task, following [`draft-rendered-page`](../draft-rendered-page/SKILL.md).

### Should there be one?

Recommend a recap page when **two or more** hold:
- Three or more participants, or two or more organizations.
- Decisions or commitments that several people need to see identically.
- Content with structure a message renders badly: a timeline, a comparison, a decision table, a set of options, a scope breakdown.
- A matter with continuity — prior meetings on the same thread, where a durable URL accumulates value.
- Participants who will forward it to people who were not in the room.

Recommend **against** one (`recommended: false`, with the reason recorded) for a 1:1 whose outcome is two sentences, a meeting whose content is too sensitive for a shareable link, or a first contact where a page reads as heavier than the relationship supports. Store the negative recommendation and file no task.

### `recommended_rendered_page` fields

- `recommended` — boolean; `recommendation_rationale` — why, against the criteria above.
- `audience` — array of `{ name, contact_entity_id, organization }`: exactly who this page is for. Drives what may appear on it.
- `audience_register` — one shared page must be written for its widest reader. Name the register and the constraint it imposes.
- `purpose` — what the page is for beyond recording: securing alignment, giving the other side something to circulate internally, establishing a shared record before work starts.
- `proposed_title`, `proposed_sections` — array of `{ section, what_it_covers, source }`, each traced to a decision, action item, or topic from Phases 5 and 7. **Section briefs, not copy.** No drafted sentences.
- `visual_elements` — where structure earns a diagram, table, or timeline, and what it would show. `draft-rendered-page` requires concrete SVG content, so name what the chart plots.
- `must_not_include` — the hard boundary for a multi-party artifact. Always: Phase 8 positions, Phase 11 relationship reads, Phase 9 research the operator has not vetted, another participant's private context, internal entity ids and repo paths, sensitive disclosures per the Art. 9 rule. Add meeting-specific exclusions.
- `sharing_posture` — `guest_link_to_named_participants` (default) | `internal_only` | `public`. Anything beyond the default is a decision the operator makes, not this skill.
- `neotoma_instance` + `instance_rationale` — see routing below.
- `linked_from_messages` — which Phase 12 recommendations should link this page, so ordering is explicit.

### Instance routing

Route by the meeting's context, defaulting to the operator's personal prod instance:

1. **A client or partner instance** when the meeting is client work and that client has its own Neotoma instance — the recap belongs in the graph its audience and its matter already live in. Name the instance explicitly.
2. **Personal prod instance** (`mcp__mcpsrv_neotoma__*`, per the always-use-prod rule) for everything else: personal, internal, evaluator, and one-off external meetings.
3. **When ambiguous** — the meeting spans a client matter and personal work, or the client instance's existence is unverified — record both candidates in `instance_rationale`, mark `instance_decision: operator_required`, and say so in the task. Do not guess: a client recap on the wrong instance is a disclosure problem, not a filing error.

Record the basis, not just the answer: which participants, which matter entity, and which instance already holds that matter.

### Companion task

One `task`: `description` naming the audience, purpose, and target instance, and pointing at `draft-rendered-page`; `due_date` from the earliest dependent message's timing; `priority`; `source: process-meeting`. Link `PART_OF` the plan and `REFERS_TO` the `recommended_rendered_page`, the `meeting_analysis`, and each audience `contact`.

Where a Phase 12 recommendation has `references_recap_page: true`, note the dependency on both tasks so the message is not written before the page exists.

---

## Phase 14: Report, issues, persist, surface

### 14a. PII scrubbing rules (public-facing text only)

Any text destined for a public GitHub issue MUST be scrubbed:

- **Names → roles.** "an evaluator", "a partner", "a customer at a Series-B fintech". Never a real name unless the participant is a public-facing collaborator on that repo AND clearly consented in conversation.
- **Emails / phone numbers → removed.** Rewrite the sentence; do not leave `[redacted]` mid-sentence.
- **Employer / customer names → generalized.**
- **Internal product / project names → checked.** Public if it appears on the public site (`../neotoma/frontend/src/site/site_data.ts`, `../personal/websites/*`) or in a published post; otherwise generalize.
- **Verbatim quotes → reframed** as observations rather than attributed quotes.
- **Internal URLs / paths → removed.**

Scrub **before** filing, never after — an edit cannot remove what GitHub keeps in edit history. If an issue cannot be cleanly scrubbed, demote it to a Mark-internal `task` and say why.

### 14b. Derive proposed public issues

Only when: the item maps to a specific repo, the work makes sense to track in public, and the scrubbed body still conveys the problem.

Capture per issue: `repo` (allowlist below), `title` (problem-statement-first), `labels`, `body_scrubbed` (context paragraph, specific ask, acceptance criteria when concrete, and `Surfaced in a meeting on YYYY-MM-DD; participants and direct quotes recorded privately.`), `backed_by_quote` (verbatim — private, never in the body), `confidence` (`high`/`medium`/`low`; demote `low` to task).

**Repo routing** (override via `MEETING_ANALYSIS_ALLOWED_REPOS`):
- `markmhendrickson/ateles` — default: swarm tooling, skills, daemons, anything not clearly elsewhere.
- `markmhendrickson/neotoma` — Neotoma MCP, schema, API, product behaviour, data model, SDK.

Repos outside the list: `_Out-of-scope repo mentioned: <name> — no issue staged._`

### 14c. Write the report

Output: `<transcript_dir>/<transcript_stem>_meeting_processed.md` for file sources, else `data/imports/audio/<YYYY-MM-DD-HHMMSS>_meeting_processed.md`.

Structure (sections with no content render `_None._`):

```markdown
# Meeting: <short title>

**Date:** YYYY-MM-DD · **Type:** <meeting_type> · **Depth:** <quick|standard|deep>
**Source:** <path or entity_id>
**Preservation:** <complete | partial:...> — audio, video, N frames, M transcript variants
**Transcription:** <engine> <model> <mode> — quality <grade> (<key metric>)

## Value and relationships          <!-- Phase 11 — leads, because it answers "was this worth it" -->
## Recommended follow-ups           <!-- Phase 12 — intent/points/tone per recipient, NO drafted prose -->
## Recommended recap page           <!-- Phase 13 — audience, sections, instance + rationale -->
## Summary                          <!-- Phase 5 -->
## Decisions
## Action items                     <!-- Mine / Theirs / Joint -->
## Open questions
## Topics                           <!-- with per-topic sub-analysis links, Phase 7 -->
## Participant positions            <!-- Phase 8 -->
## Research findings                <!-- Phase 9, incl. what went unresearched -->
## Graph reconciliation             <!-- Phase 6: confirmed / extended / corrected / introduced / ambiguous -->
## Tasks                            <!-- Phase 10 + 12 + 13: created and updated, with entity ids -->
## Risks / blockers
## Repo / project signal
## Proposed public issues
## Transcription profile            <!-- Phase 3: grade, problems, lessons, operations skipped -->
## PII inventory (private)
```

`## Recommended follow-ups` renders intent, key points, tone, and timing per recipient — **never a drafted message body**. If the report contains a sentence a recipient could be sent verbatim, the skill has violated its own contract.

### 14d. Persist to Neotoma

Follow the Neotoma MCP turn lifecycle and `[PROVENANCE]` rules.

**Entity-type reuse check** before the first store in a fresh session — call `list_entity_types` for `meeting`, `transcription`, `task`, `research`, `position`, `value`, `recommended`, `rendered`, and reuse exact strings. Existing types this skill uses: `transcription`, `meeting_analysis`, `task`, `proposed_github_issue`, `calendar_event`, `contact`, `research_inquiry`, `research_finding`. Types this skill introduces: `transcription_profile`, `meeting_topic_read`, `participant_position`, `meeting_value_assessment`, `recommended_message`, `recommended_rendered_page`.

**`recap_message` is no longer written by this skill.** Existing `recap_message` entities from prior runs stay valid and readable; this skill creates none.

**Schema status (as of 2026-08-21).** The six types this skill introduces are REGISTERED and active: `transcription_profile` (identity: `transcription_entity_id`), `meeting_topic_read` (identity: `meeting_analysis_entity_id` + `topic`), `participant_position` (identity: `meeting_analysis_entity_id` + `participant_name`), `meeting_value_assessment` (identity: `meeting_analysis_entity_id`), `recommended_message` (identity: `meeting_analysis_entity_id` + `recipient_name`), `recommended_rendered_page` (identity: `meeting_analysis_entity_id`). Array fields use `merge_array` reducers so a re-run accumulates rather than overwrites.

**Two schema constraints you will hit — plan around them, do not fight them:**

1. **The new `meeting_analysis` fields could NOT be registered.** A Neotoma type-name linter rejects any `*_analysis` type as "plural" (it reads the `-is` ending as a plural suffix and suggests `meeting_analysi`). This blocks `update_schema_incremental` on `meeting_analysis` entirely, even though the type has 71 live entities. Until it is fixed, the fields listed below (`graph_reconciliation`, `entity_mentions`, the child entity-id arrays, `depth`, …) will land in `raw_fragments` and will NOT be queryable. Store them anyway — the data is preserved and a later schema fix plus `migrate_existing: true` will promote them — but do NOT rely on querying `meeting_analysis` by those fields. Query the child entities directly instead; each carries `meeting_analysis_entity_id`, which is the reliable path.
2. **`transcription` has no identity config**, so `update_schema_incremental` refuses to add the Phase 1 preservation fields (`audio_source_id`, `frame_source_ids`, `speaker_label_map`, …). Fixing it requires re-registering the full schema for a type with 878 live entities — an identity-rule change that must not be made casually. Same handling: store the fields, expect `raw_fragments`, and treat `transcription_profile` (which IS registered and IS queryable) as the durable home for anything Phase 2 or 3 needs to look up later.

The practical consequence: **the Phase 3 learning loop works** (it queries `transcription_profile`, which is registered), and **the Phase 8 prior-position lookup works** (it queries `participant_position`, which is registered). The degraded path is only querying `meeting_analysis` by its new fields.

**Schema check before storing.** `meeting_analysis` v1.0 declares 36 fields, several of which (`to_name`, `subject`, `delivery_status`, `gmail_draft_id`, …) leaked in from `recap_message` stores and do not belong to it. Call `describe_entity_type` first and use declared fields where they fit. This skill's new fields — `graph_reconciliation`, `entity_mentions`, `topic_analysis_entity_ids`, `participant_position_entity_ids`, `research_inquiry_entity_ids`, `value_assessment_entity_id`, `recommended_message_entity_ids`, `recommended_rendered_page_entity_id`, `transcription_profile_entity_id`, `preservation_status` — need registering via `update_schema_incremental` on first use rather than landing as raw fragments.

**Full data fidelity.** Every field of extracted data lands in a stored field. If any store returns `unknown_fields_count > 0`, repair immediately with declared field names before closing the turn.

**Relationships** (batch in the same `store` where possible):

- `PART_OF`: `agent_message` → `conversation`; `transcription_profile` → `transcription`; `meeting_topic_read` → `meeting_analysis`; `task` → plan.
- `REFERS_TO`: `meeting_analysis` → `transcription`, `calendar_event`, each participant `contact`, each matter entity resolved in Phase 4 step 4; each `task` → `meeting_analysis` (+ `contact` when owed); each `proposed_github_issue` → `meeting_analysis` only (keeps public-issue → person attribution one hop away); each `participant_position` → `contact` + `meeting_analysis` + affected entities; each `research_finding` → its `research_inquiry` + `meeting_analysis` + affected entities; `meeting_value_assessment` → `meeting_analysis` + each participant `contact`; each `recommended_message` → `meeting_analysis` + recipient `contact` + the open loops it closes; `recommended_rendered_page` → `meeting_analysis` + each audience `contact`.
- `DEPENDS_ON`: each `recommended_message` with `references_recap_page: true` → `recommended_rendered_page`.
- `SUPERSEDES`: new `transcription_profile` → prior profile for the same signature; `participant_position` → that participant's prior position entity.
- `EMBEDS`: `transcription` → each uploaded source artifact.

**Idempotency:**
- Turn: `conversation-{conversation_id}-{turn_id}-process-meeting-{timestamp_ms}`.
- Per-meeting: `meeting-<sha256(transcript_path or transcription_entity_id)[:12]>` for the `meeting_analysis`, so a re-run deepens rather than duplicates.
- Per-phase children: `<meeting_idempotency_key>-<phase>-<slug>` (e.g. `meeting-a1b2c3d4e5f6-topic-pricing`, `meeting-a1b2c3d4e5f6-recmsg-alice`).

### 14e. Open GitHub issues (opt-in)

Default **off**. Only with `--open-issues` or `MEETING_ANALYSIS_OPEN_GH_ISSUES=1`. Verify the repo is allowlisted, re-verify the scrub, then create via the GitHub MCP and record `opened_url`. On a failed scrub check, demote to task. On API failure, leave `opened_url` null and record `open_error`. When off, say "drafted" — never "opened".

### 14f. Close the turn and surface

Store the assistant `agent_message` per the closing-store step and link it `PART_OF` the conversation.

Reply with:
- **Headline** — e.g. `Meeting with Alice and Bob — value: high · 3 decisions · 6 tasks · 4 research findings · 2 follow-ups recommended.`
- **Value + relationships** — one line overall, one line per participant with trajectory.
- **Recommended follow-ups** — one line each: `→ <Name> (<channel>, <timing>): <intent>`. No message content.
- **Recommended recap page** — `<recommended|not recommended>` + audience + target instance + rationale, or the reason against.
- **My action items** — numbered, max 5, with due dates.
- **Research** — one line per finding: `<question> → <conclusion>` (+ what went unresearched).
- **Graph changes** — counts: entities updated, corrected, created; tasks created and closed.
- **Proposed issues** — `<owner/repo>: <title> — <URL | draft>`.
- **Transcription** — grade + any lesson recorded for next time.
- Absolute report path, `meeting_analysis` entity id, and the created entity ids.

Render the `🧠 Neotoma` section per the display rule, grouping Created / Updated / Retrieved.

---

## Concurrency

Phases 1–4 are sequential — everything downstream depends on preserved source, a transcript, and resolved identities. Once Phase 4 completes, dispatch Phases 7, 8, and 9 as parallel subagents (topic sub-analyses fan out one per topic; research fans out one per inquiry). Phase 6 runs on the main thread because it writes corrections and must not race itself. Phases 10 and 11 join after 6–9 return. Phases 12–13 run after 11.

Do not fan out beyond the depth caps — `standard` means 3 topics and 5 inquiries, and exceeding it quietly is the same failure as truncating it quietly.

## Behavior rules

- **Draft nothing outbound.** No recap emails, no messages, no Gmail drafts, no `rendered_page` bodies, no guest tokens. Phases 12–13 recommend and brief; they never write copy. A drafted sentence a recipient could receive verbatim is a contract violation regardless of where it appears.
- **Preserve before you analyze.** A run that analyzed a recording it failed to store is a failed run. Report `preservation_status` honestly.
- **Never repeat a known-bad expensive operation.** Phase 3 exists to be read, not just written. A run that re-transcribes a good transcript, or re-runs an operation a prior profile marked useless, wasted the operator's money.
- **No invented commitments, quotes, emotions, or praise.** Every action item, decision, position, and relationship claim traces to specific transcript text or a stored entity. Paraphrase is labeled.
- **Resolve the matter, not just the people.** Phase 4 step 4 is required; skipping it forks a parallel thread beside an existing one.
- **Reuse entities; never duplicate.** Resolve before creating, every time. Ambiguity gets recorded and filed, not guessed.
- **Corrections carry evidence.** Every `correct` cites the transcript quote or research source that justifies it.
- **PII scrubbing is non-negotiable for public text**, and happens before filing.
- **Phase 8 and Phase 11 content never leaves the graph** — not into a recommendation, not into a page brief, not into the reply's recipient-facing lines.
- **Recommending nothing is a valid output.** `channel: none` and `recommended: false` are first-class results; recommending a message to every participant is the failure mode Phase 12 exists to prevent.
- **Every recommendation gets a task.** Otherwise it will not happen.
- **Instance routing is a disclosure decision.** When the target instance is ambiguous, mark `operator_required` rather than guessing.
- **Attribution confidence gates conclusions.** A `degraded` transcript cannot support a confident participant-position profile. Mark it and say so.
- **No silent caps.** Anything skipped for depth, cost, or frame count is named in the report.
- **Stable shape.** Empty sections render `_None._` and store as empty arrays.
- **Skip silently on non-meetings.** Solo voice memos and accidental recordings get preservation (Phase 1) and transcription only. The auto-invoke gate requires at least one of: ≥2 distinct speakers, ≥200 words, or a second-person pronoun followed by a verb of commitment.
- **No claims about side effects you did not perform.** Unopened issues are "drafted"; recommended messages are "recommended", never "drafted" or "staged".

## Out of scope

- **Writing any follow-up message** — Phase 12 recommends; the operator or a later drafting run writes and sends.
- **Building or publishing the recap page** — Phase 13 recommends; [`draft-rendered-page`](../draft-rendered-page/SKILL.md) builds it, mints the guest link, and owns the page's design rules.
- Sending anything, ever.
- Closing the loop on action items — other workflows own completion.
- The Neotoma customer-development analysis — [`analyze-neotoma-feedback`](../analyze-neotoma-feedback/SKILL.md) owns it; both fire when the Neotoma heuristic in [`record_meeting`](../record_meeting/SKILL.md) triggers.
- Diarization repair beyond `speaker_label_map` and `--participants` — deeper repair lives in `transcribe_audio.py`.
- Recording itself, and the consent guardrail that governs it — [`record_meeting`](../record_meeting/SKILL.md) owns both. Recording a call the operator is not a party to remains a hard refusal.
