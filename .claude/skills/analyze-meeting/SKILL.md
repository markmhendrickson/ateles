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

Auto-invoked by Tyto daemon after transcription completes (or manually via /analyze-meeting). Full pipeline:

STEP 0 — IDEMPOTENCY GUARD (run before any extraction, persistence, or task spawning):
Resolve the canonical `source_reference` first — the transcription entity ID (preferred), or a stable hash/path of the source transcript if no entity. Then query Neotoma for existing `meeting_analysis` entities whose `source_reference` matches (use retrieve_entities entity_type=meeting_analysis and filter by source_reference, or retrieve_entity_by_identifier on the source_reference). 
- If one or more analyses already exist for this exact source_reference: DO NOT create a new meeting_analysis and DO NOT spawn a fresh task set. Instead, treat this as a RE-RUN: correct/supersede the existing analysis in place (use mcp__mcpsrv_neotoma__correct on the existing entity's fields, or add a superseding observation), and reconcile tasks by matching on (description-normalized + source_reference) — correct existing task entities rather than creating parallel duplicates. Record `data_source` noting "re-run, superseded prior analysis ent_…". 
- Only if NO analysis exists for this source_reference do you proceed to create new entities.
- This guard is MANDATORY and exists because Tyto (or manual re-invocation) can fire the skill multiple times on the same recording; without it, each run emits a duplicate analysis + a full duplicate task set (observed: 8 near-identical analyses + ~7× duplicate tasks for one meeting). When splitting a recording into N segments (STEP 0b), key idempotency on (source_reference + segment_index), not just source_reference, so legitimate per-segment analyses are not collapsed into one.

STEP 0b — Back-to-back meeting detection (after the idempotency guard, before extraction):
Scan the full transcript for session boundaries: greetings ("hi", "hello", "good morning", "nice to meet you", "thanks for joining"), farewells ("bye", "talk soon", "thanks everyone", "take care", "have a good one", "see you later"), and significant topic/participant discontinuities. If two or more distinct sessions are detected (farewell + new greeting, distinct participant sets), treat each as a separate meeting and run the full pipeline independently for each, producing one meeting_analysis entity per segment numbered "Meeting N of M". Record segment_start_approx / segment_end_approx from transcript word timestamps. Each segment gets a distinct (source_reference + segment_index) idempotency key. When in doubt, prefer splitting.

STEP 1 — Resolve source + participants + calendar:
- Resolve transcript from entity ID, file path, or pasted text.
- Identify participants from diarized speaker labels ([You], [Speaker_0], etc.), name mentions, salutations, --participants overrides.
- Resolve each participant against Neotoma contact/person entities. ALWAYS call retrieve_entity_by_identifier for each participant name and write the resolved entity IDs into `participant_contact_entity_ids` — never leave it as an empty array when names were extracted.
- Google Calendar lookup: use recording_timestamp (passed by Tyto as YYYY-MM-DDTHH:MM UTC, or inferred from filename/mtime) to query `gws calendar events list --timezone Europe/Madrid` within ±90min. For each segment, use that segment's approximate start time. Cross-reference attendees with speaker labels to resolve real names for [Speaker_N] labels. Store calendar_event entity and link to meeting_analysis. If no match: note _Calendar: no matching event found._ and proceed.
- Classify meeting type: customer_call | partner_call | 1_on_1 | internal | interview | other.

DATA MINIMIZATION (RGPD legitimate-interest discipline — see CLAUDE.md "People-data processing"): Neotoma's storage of meeting participants and transcripts runs under RGPD Art. 6(1)(f) legitimate interest, not the household exemption, because the data drives professional action. When extracting people into durable contact profiles: keep relationship-relevant facts (role, context, commitments, follow-ups); do NOT persist incidental Art. 9 sensitive disclosures (health, finances, family situations, political/religious views) into contact profiles unless directly relevant to a stored task — summarize rather than store verbatim when a sensitive detail is incidental. This is in addition to the existing PII-scrub-before-public-issues rule (which governs the outbound direction); minimization governs what enters the graph in the first place.

STEPS 2–11 — Standard flow: structured analysis (summary, decisions, action items mine/theirs/joint, open questions, topics, risks, repo signal, PII inventory), PII scrubbing, proposed GitHub issues, recap messages (email via gws gmail draft when address known, generic message otherwise)

EMAIL DRAFTS MUST BE HTML (multipart/alternative) — not plain text:
When drafting a recap/follow-up email via gws gmail drafts, build a **multipart/alternative** message with BOTH a text/plain AND a text/html part — never a single text/plain part. Plain-text drafts render URLs as bare unclickable strings and look bare next to any artifact they reference. Build it with Python's email.mime (MIMEMultipart("alternative") + two MIMEText parts), base64url-encode the full message, and pass as {message:{raw:...}}. In the HTML part: real <a href> links (named, not raw URLs), <ul> for next-steps/highlights, simple inline styles. Verify after creating: the draft's payload mimeType is multipart/alternative with a text/html part containing <a href> links. This has regressed twice — the printf|base64 shortcut only ever produces text/plain; do not use it for emails., report written to disk, persist to Neotoma (meeting_analysis + task + recap_message + proposed_github_issue + calendar_event entities), deliver recap drafts, optionally open GH issues (--open-issues only), surface to user.

STEP 11.5 — COMMUNICATION SELF-REVIEW (run on EVERY meeting where Mark is a speaker; the operator uses this to practice incrementally):
Purpose: turn each call into one data point in a longitudinal coaching loop so Mark can flag concrete things to practice and watch them trend. Produces exactly one `communication_review` entity per (source_reference + segment_index), linked REFERS_TO → the meeting_analysis and the transcription. Idempotent: on re-run, correct the existing review, don't duplicate.

Skip only when Mark is not a participant, or the transcript is rapport-only with <150 of Mark's words (too little signal). Family/personal calls ARE reviewed but flagged context=personal so they don't pollute the professional trend.

A) OBJECTIVE TIC METRICS — compute from MARK'S LINES ONLY ([Mic] in dual-channel, [You] in diarized). All rates per 100 words of Mark's speech:
- hedge_rate: como/like/sort of/kind of/just/maybe/probably/I think/I guess/creo que/quizás/digamos/básicamente/un poco
- filler_rate: eh/em/ehm/mmm/um/uh/you know/well/so/bueno/o sea
- restart_rate: mid-word self-corrections (regex word-fragment hyphenations, e.g. "fon-fondo", "pla-place")
- click_rate: tongue-clicks — match ALL variants: [chasquido de lengua], [lip smack(s)], [lips smack], [smacks lips], [clicks tongue], [tsk(s/ing)], [tongue click]
- word_count, language (es|en), and stakes_context (rapport|peer|prospect|partner|investor|interview|family)

B) ENGINE TAG — MANDATORY, because cross-engine disfluency comparison is INVALID (proven 2026-06-16: Gemini strips fillers/clicks, ElevenLabs captures all; same speaker looked 2.5× more disfluent purely by transcriber). Record transcription_engine (elevenlabs|gemini|whisper|other), inferred from the transcript header/source. Gemini/cleaned transcripts: set click_rate and filler_rate to null (not 0) — they are not measured, not absent. ONLY trend a metric against priors from the SAME engine.

C) BASELINE-RELATIVE SCORING — never score against absolutes; score against Mark's established baselines:
- English hedge baseline ~4.8/100w; Spanish hedge baseline ~5/100w (hedging is a STABLE TRAIT, inverse to stakes — higher with friends/family, lower in sales/prospect calls; do NOT flag normal hedging as a problem).
- English click baseline ~0.2/100w (negligible); Spanish click baseline ~2/100w (the Spanish-vocabulary-load tell — expected, not alarming, in unrehearsed Spanish).
- restart_rate is a real-time-composition-load meter; peaks in fast dense technical talk (EN as much as ES). High restart = was composing hard live, not a language problem.
Retrieve the most recent prior communication_review of the SAME (language + engine) and report delta_vs_last for each metric (improving / steady / worse).

D) QUALITATIVE READS (the patterns that matter more than raw counts):
- pitch_landed: did Mark's core/differentiation explanation land? Evidence FOR = listener reflects it back accurately, supplies their own corroborating pain, or asks implementation/"how does it scale" questions. Evidence AGAINST = listener re-asks the same question, or re-frames Mark's point more simply than he did. Record verdict + the verbatim listener line that proves it. (Do NOT overclaim "landed/reframed" without a quote — this has been a recurring overstatement; require transcript evidence.)
- listener_type: builder (hands-on, has personally felt the problem — your abstraction lands because they bridge it) vs sampler (evaluating, no current pain — burden is fully on your crispness). This determines whether a rambly answer was survivable. Record which, with evidence.
- pitch_vs_listener_fit: did Mark calibrate? Builder→deep/discursive is fine; sampler→needs crisp+concrete fast. Flag mismatch (e.g. "gave the builder-mode answer to a sampler" = the Ivan failure mode).
- reciprocity: did the other side SPEND anything costly (market insight, intros, real engagement, structuring advice) or only cheap warmth? "Generous in tone, stingy in substance" is the watch-flag. (Especially for investors: this is the real interest signal, not friendliness.)
- frame_control: did Mark run the call (set agenda, asked questions back, set the next step himself) or absorb the other side's default (answered-only, let them close)? Note specifically whether Mark asked at least one real question back, and whether he set the next concrete step.

E) CARRY-OUT ARTIFACT CHECK — Mark maintains standing communication artifacts (the canonical crisp differentiation pitch [ES + EN], and the builder-vs-sampler calibration habit). Check whether Mark APPLIED them this call (e.g. did he deliver a tight ≤30s differentiation answer, or revert to the ~250-word hedge-heavy version?). Record applied: yes/partial/no per artifact.

F) PRACTICE FOCUS — emit AT MOST 1–2 concrete, single-behavior focuses for the NEXT call (incremental, never a long list — overwhelming kills practice). Each focus = one observable behavior Mark can consciously attempt next time (e.g. "lead the differentiation answer with the one-line claim before any context", "ask one question back before finishing their question", "name the next step yourself at the close"). Carry an unmet focus forward to the next review until it shows as applied.

G) PERSIST: store a `communication_review` entity with fields: meeting_analysis_entity_id, transcription_entity_id, meeting_date, language, transcription_engine, stakes_context, word_count, hedge_rate, filler_rate (null if engine-stripped), restart_rate, click_rate (null if engine-stripped), delta_vs_last (object), pitch_landed (verdict+quote), listener_type (+evidence), pitch_vs_listener_fit, reciprocity, frame_control, carry_out_artifacts_applied (object), practice_focus (array, max 2), notes, data_source. Link REFERS_TO → meeting_analysis, transcription, and each participant contact. Idempotency key: comm-review-<source_reference>-<segment_index>.

H) SURFACE: in the user-facing reply, add a compact "🗣 Communication" block: the metrics with same-engine deltas (↑/↓/→), pitch_landed verdict, listener_type, reciprocity flag, and the 1–2 practice focuses for next time. Keep it short — this is a coaching nudge, not a re-derivation of the framework.

When persisting tasks: give each spawned task a stable idempotency_key derived from (source_reference + segment_index + normalized-description) so re-runs update rather than duplicate. Before creating a task, check whether a task with that key already exists for this source_reference and correct it instead of creating a new one.

When meeting is Neotoma-oriented (customer_call or partner_call where primary topic is Neotoma schema/API/MCP/product): ALSO run /analyze-neotoma-feedback in the same turn, producing a feedback_analysis entity linked to the same transcription and contact entities.

Default issue repos: markmhendrickson/ateles (non-Neotoma) or markmhendrickson/neotoma (Neotoma-specific). Skip silently on non-meeting transcripts (heuristic: ≥2 speakers OR ≥200 words + commitment verb).
