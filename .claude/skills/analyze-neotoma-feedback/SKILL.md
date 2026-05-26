---
name: analyze-neotoma-feedback
description: "Deep customer-development analysis of Neotoma-related feedback across need, positioning, efficacy, ICP fit, vocabulary, adoption triggers, activation conditions, content ideas, market insights, confidence delta, communication efficacy, and memetic ideas that reinforce Neotoma's narrative — and proposes concrete website changes (copy, FAQ, comparison pages, SEO, routes) grounded in the current `../neotoma` site. Runs per-item for one source, or in aggregate mode across the full corpus of feedback-type entities in Neotoma. Output lives entirely in the neotoma repo (markdown report plus structured Neotoma entity)."
triggers:
  - analyze neotoma feedback
  - analyze feedback
  - customer dev analysis
  - customer development analysis
  - /analyze-neotoma-feedback
  - /analyze-neotoma-feedback --aggregate
user_invocable: true
entity_id: ent_4681e3dc3118e51c5b639e71
---

# Analyze Neotoma Feedback

Produce axis-by-axis customer-development analysis grounded in Neotoma's canonical ICP and positioning.

Two modes:
- **Per-item mode** (default): one source in, one report + one `feedback_analysis` entity out.
- **Aggregate mode**: the entire corpus of feedback-type entities in Neotoma in, one cross-item synthesis report + one `feedback_aggregate_analysis` entity out. See [Aggregate Mode](#aggregate-mode) below.

This skill complements [`process-feedback`](../process-feedback/SKILL.md):
- `process-feedback` = triage and release-stage assessment (bucket, action, evidence threshold).
- `analyze-neotoma-feedback` = deep customer-development analysis (per-item or aggregate).

Run triage first when useful; this skill then goes deeper on items worth deeper analysis.

## Invocation

```
/analyze-neotoma-feedback <source>                # per-item
/analyze-neotoma-feedback --aggregate [--since YYYY-MM-DD] [--types t1,t2,...]
```

For per-item, `<source>` is auto-detected:
- **URL** (starts with `http://` or `https://`) — delegate to `user-web-scraper` MCP as in the `analyze` skill. Handles ChatGPT share, x.com/twitter, blog posts.
- **Absolute file path** (exists on disk) — read the file. Handles meeting transcripts (e.g. `.cursor/last_meeting_transcription.txt`), email attachments, images, screenshots.
- **Neotoma entity reference** — a UUID or a short identifier string (person name, email, conversation title). Resolve via `retrieve_entity_by_identifier` first, then `retrieve_entities` scoped to `entity_type` in {`product_feedback`, `agent_message`, `note`, `conversation`, `email_message`}.
- **Raw pasted text** — anything that doesn't match the above.
- **`--aggregate`** flag (with no positional source) — switches to aggregate mode.

If the resolution is ambiguous (e.g. the string matches multiple entities), list the top 3 candidates and ask the user to pick one.

## Step 1: Load canonical context

Read once per invocation, cite in the report where referenced. Skip any missing file with a `[gap: <path>]` note instead of failing.

Primary ICP source of truth (explicit per user):
- `/Users/markmhendrickson/repos/neotoma/docs/icp/primary_icp.md`

Supporting ICP grounding (read opportunistically):
- `/Users/markmhendrickson/repos/neotoma/docs/icp/profiles.md`
- `/Users/markmhendrickson/repos/neotoma/docs/icp/secondary_icps.md`
- `/Users/markmhendrickson/repos/neotoma/docs/icp/future_icps.md`
- `/Users/markmhendrickson/repos/neotoma/docs/icp/developer_release_targeting.md`
- `/Users/markmhendrickson/repos/neotoma/docs/icp/prioritized_pain_points_and_failure_modes.md`

Positioning and problem framing:
- `/Users/markmhendrickson/repos/neotoma/docs/foundation/product_positioning.md`
- `/Users/markmhendrickson/repos/neotoma/docs/foundation/problem_statement.md`
- `/Users/markmhendrickson/repos/neotoma/docs/foundation/core_identity.md`
- `/Users/markmhendrickson/repos/neotoma/docs/foundation/developer_release_principles.md`

Customer development pipeline context:
- `/Users/markmhendrickson/repos/neotoma/docs/private/strategy/customer_development_plan.md`

Website surfaces (read the canonical catalogs once, then open specific component files only if the feedback implicates that surface — e.g. read `ComparisonPage.tsx` or `NeotomaVsMem0Page.tsx` only if the feedback references a competitor):

- `/Users/markmhendrickson/repos/neotoma/frontend/src/site/site_data.ts`
- `/Users/markmhendrickson/repos/neotoma/frontend/src/site/site_data_core.ts`
- `/Users/markmhendrickson/repos/neotoma/frontend/src/site/site_data_localized.ts`
- `/Users/markmhendrickson/repos/neotoma/frontend/src/site/seo_metadata.ts`
- `/Users/markmhendrickson/repos/neotoma/frontend/src/site/faq_items.ts`
- `/Users/markmhendrickson/repos/neotoma/frontend/src/site/full_page_paths.ts`
- `/Users/markmhendrickson/repos/neotoma/frontend/src/site/docs_sidebar_nav.ts`
- `/Users/markmhendrickson/repos/neotoma/frontend/src/App.tsx` (router / route-to-component map)
- `/Users/markmhendrickson/repos/neotoma/frontend/src/components/subpages/` (vertical, comparison, connector, landing pages — open only the ones implicated by the feedback)
- `/Users/markmhendrickson/repos/neotoma/frontend/src/components/SitePage.tsx`, `SitePageAlt.tsx`, `Layout.tsx`, `SiteHeaderNav.tsx` (home, chrome, nav)
- `/Users/markmhendrickson/repos/neotoma/frontend/src/i18n/locales/` (localized strings; only open when the proposed change touches copy and localization matters for the target)
- `/Users/markmhendrickson/repos/neotoma/site_pages/` (static/markdown-mirror pages at their public URL slug)

These files are consulted only to ground the "Proposed website changes" section below. Do not read the whole tree pre-emptively — open files on demand based on which surfaces the feedback substantively touches.

## Step 2: Resolve the source + extract verbatim quotes

1. Resolve per input mode above and capture the raw content.
2. Identify the contact: name, channel, date. If the contact exists as a `contact` or `person` in Neotoma, capture their `entity_id`; otherwise plan to create one in Step 5.
3. Identify any pre-existing `product_feedback` or `conversation` entity for this item via `retrieve_entity_by_identifier` / `retrieve_entities`. Reuse if present.
4. Pull verbatim quotes from the source. Never paraphrase into quote syntax — paraphrased content must be labeled as such.

## Step 3: Score axes

Canonical axes. Evaluate only those the feedback substantively touches. Emit the non-covered ones as explicit skip markers so the report shape stays stable across items and cross-item queries are reliable.

Each covered axis produces: short synthesis + at least one verbatim quote (when the source contains one) + explicit action (report-only note, proposed doc edit with file path and replacement language, or a follow-up task).

1. **Need validation** — does the person exhibit the chronic/acute pain Neotoma addresses? Look for stated need, revealed behavior, workarounds.
2. **Positioning resonance** — how Neotoma's current messaging landed vs. confused them; note which specific phrases they reacted to.
3. **Solution efficacy** — for users who actually ran Neotoma: did it solve the need; friction points; where it fell short.
4. **ICP fit + ICP updates** — map to `primary_icp.md` archetype/tier; propose concrete edits to ICP docs when signal warrants (additions, sharpening, disqualifications).
5. **Vocabulary in their own words** — direct quotes for how they describe their problem, Neotoma itself, and the category.
6. **Adoption triggers** — what would concretely motivate them to start using Neotoma (demo, benchmark, peer signal, integration with X tool).
7. **Activation conditions** — what they need from Neotoma or surrounding tooling to succeed post-install (docs, integration, specific schema, specific agent).
8. **Content marketing ideas** — questions they raised that deserve scalable answers (blog post, landing page, FAQ item, comparison page).
9. **Market / technology insights** — general signal about where the market or tech is moving that isn't Neotoma-specific.
10. **Confidence delta + evidence to raise confidence** — does this feedback increase or decrease confidence in (a) the problem and (b) the solution; what specific additional evidence would raise confidence further. Mirrors `process-feedback`'s evidence-threshold field.
11. **Communication efficacy** — REQUIRED when the source is an exchange involving the user (transcript of a live conversation, email thread, DM exchange). What framings and answers landed well; what to tighten or cut; specific drop-in replacements.
12. **Memetic ideas / narrative reinforcement** — sticky, compressible, reusable ideas surfaced by the source that can reinforce Neotoma's narrative. This is a narrative-harvesting axis, distinct from Vocabulary (which captures how *this* person describes things) and Content marketing ideas (which captures questions worth scalable answers). For each meme, capture:
    - **Kind**: `phrase` (tight verbatim turn of phrase worth quoting verbatim) · `frame` (how they framed the problem or the solution in their head) · `metaphor` / `analogy` (e.g. "it's like X but Y") · `contrast` (binary opposition that sharpens the story, e.g. "memory vs truth", "scratchpad vs ledger") · `story` / `scenario` (short narrative hook that dramatizes pain or fix) · `coined_term` (a new label they invented or adopted).
    - **Verbatim source**: exact quote from the feedback (required) — never invent or smooth out.
    - **Compression**: one-sentence distillation of what the meme encodes.
    - **Narrative alignment**: which existing Neotoma narrative beat it reinforces, sharpens, or challenges — cite the specific file and section in `docs/foundation/product_positioning.md`, `docs/foundation/core_identity.md`, `docs/foundation/problem_statement.md`, or `docs/foundation/developer_release_principles.md`. If it does not align with any existing beat but is strong signal, flag it as a candidate *new* narrative beat.
    - **Proposed reuse**: concrete surface where the meme could live — tagline, landing hero, landing section header, FAQ answer, blog post title, social post hook, intro paragraph of a positioning doc, or a specific replacement line in an existing foundation doc (with file path and before/after language when a direct edit is warranted).
    - **Stickiness rationale**: short note on why it is memorable (brevity, contrast, concreteness, unexpected framing, emotional resonance). Skip if not obvious; do not pad.

    Source the raw candidates only from the feedback itself — do not mix in memes you generate. If the only memetic signal is paraphrased (e.g. a meeting recap that summarizes but does not quote), note that explicitly and mark the compression as `paraphrase` rather than `verbatim`.
13. **Other axes (evaluate when present)** — willingness-to-pay / pricing sensitivity; referral / advocacy potential; objection patterns (repeatable across evaluators); emotional valence and intensity; trust / credibility signals (security, privacy, permanence); competitor references they make unprompted.

## Step 3b: Derive website change proposals

Website changes are not a separate axis — they are concrete, grounded consequences of the axes above. Generate proposals only when the feedback implies a specific site change; do not invent cosmetic edits.

Trigger axes (use as a checklist — each proposal must cite one or more):
- **Positioning resonance** → landing hero copy, homepage sections, tagline, core-identity framing.
- **Solution efficacy** (when friction is site-discoverable) → docs sidebar nav, getting-started / walkthrough pages, connector walkthroughs.
- **Vocabulary in their own words** → replace jargon with user language on the relevant surface; propose exact before/after.
- **Adoption triggers** → CTA block, `HomeEvaluatePromptBlock`, hero secondary CTA, `/evaluate` or `/install` flow, ICP vertical landing pages.
- **Activation conditions** → docs pages, integration pages, connector pages (Claude, ChatGPT, Cursor, Codex, OpenClaw), `IntegrationsPage`.
- **Content marketing ideas** → new FAQ entry in `faq_items.ts`, new comparison page under `neotoma-vs-*`, new vertical landing page, new SEO-targeted slug in `full_page_paths.ts`.
- **Memetic ideas** → drop a verbatim phrase into a landing hero, section header, FAQ answer, meta description, or page title; name the exact surface.
- **ICP fit** → vertical landing page copy (`AiInfrastructureEngineersPage`, `AgenticSystemsBuildersPage`, `AiNativeOperatorsPage`, `KnowledgeWorkersPage`, etc.), navigation emphasis, SEO meta for those slugs.
- **Confidence delta / trust signals** → proof surfaces (logos, testimonials, guarantees page, `MemoryGuaranteesTable`, `auditable-change-log`, `architecture`).
- **Objections / competitor mentions** → comparison pages, build-vs-buy page, memory-vendors page.

For each proposal capture:
- **Surface type**: `landing_hero` · `landing_section` · `home_cta` · `vertical_page` · `comparison_page` · `connector_page` · `faq` · `docs_page` · `seo_metadata` · `nav_or_sidebar` · `new_page` · `route_or_slug` · `illustration_or_media`.
- **Route** (public URL path, e.g. `/`, `/neotoma-vs-mem0`, `/ai-infrastructure-engineers`, `/faq`, or the proposed new slug).
- **Source file(s)**: exact absolute path(s) that would need to change (React component, `site_data*.ts`, `faq_items.ts`, `seo_metadata.ts`, `full_page_paths.ts`, i18n locale file, a new `subpages/*Page.tsx`, or a new `site_pages/<slug>/` directory).
- **Change kind**: `copy_edit` · `new_section` · `new_page` · `restructure` · `remove` · `metadata_edit` · `faq_add` · `faq_update` · `nav_add` · `nav_reorder` · `i18n_add`.
- **Current state**: for copy edits, a verbatim excerpt of the existing site text you read (quote it exactly from the source file — if you have not read it, open it first). For structural changes, a short description of the current state.
- **Proposed change**:
    - For copy: a drop-in `before` / `after` block with the exact replacement language.
    - For structural (new page, new FAQ, nav reorder): a concrete specification — title, URL slug, sections, CTAs, and the one or two key lines of copy.
- **Backed by**: the triggering axis or axes + a verbatim quote (or labeled paraphrase) from the feedback, and the contact's `entity_id` when stored.
- **Effort estimate**: `trivial` (single copy tweak) · `small` (single component or FAQ add) · `medium` (new subpage + routing + SEO) · `large` (new section spanning multiple surfaces and i18n).
- **Confidence**: `high` (explicit ask or direct confusion quote pointing at a named surface) · `medium` (implied by axis signal) · `low` (speculative — include only if the leverage justifies surfacing and label it clearly).

If the feedback implies no concrete site change, the section is an explicit empty marker, not omitted. Do not propose site changes grounded purely in items you wish the feedback had said.

## Step 4: Write the report

Output location: `/Users/markmhendrickson/repos/neotoma/docs/private/customer-development/<slug>_feedback_analysis.md`

`<slug>` = lowercased, underscore-separated, derived from contact + short topic (for example `dick_hardt_readme_robot_written`, `evaluator_call_2026_04_21_activation`). If the directory does not yet exist, create it — it is a new sibling to the existing `docs/private/insights/`, `docs/private/icp/`, `docs/private/strategy/`. The report-first format matches the shape of items already in `docs/private/insights/`.

Template:

```markdown
# Feedback Analysis: <short title>

**Source:** <type> — <who / when / where>
**Input mode:** text | file | URL | entity
**Analyst turn:** <conversation_id>:<turn_id>
**Related entities:** contact=<id>, product_feedback=<id>, conversation=<id>

## Summary

- 3-5 bullets capturing the highest-signal takeaways.

## Contact + ICP fit

- Contact: name, role, channel.
- Mapped against `docs/icp/primary_icp.md` archetype and tier.
- If outside the primary ICP archetype, state that up front — positioning resonance and solution efficacy below are weighted accordingly.

## Axis analysis

### Need validation
<synthesis + verbatim quote(s) + action>

### Positioning resonance
<synthesis + verbatim quote(s) + action>

### Solution efficacy
<synthesis + verbatim quote(s) + action>

### ICP fit and proposed ICP updates
<synthesis + verbatim quote(s) + concrete doc edits>

### Vocabulary in their own words
- Problem: "<quote>"
- Neotoma: "<quote>"
- Category: "<quote>"

### Adoption triggers
<synthesis + verbatim quote(s) + action>

### Activation conditions
<synthesis + verbatim quote(s) + action>

### Content marketing ideas
- Question they raised: "<quote>" → proposed asset: <post | landing page | FAQ | comparison>

### Market / technology insights
<synthesis + verbatim quote(s)>

### Confidence delta + evidence to raise confidence
- Problem confidence delta: + / - / 0 — <why>
- Solution confidence delta: + / - / 0 — <why>
- Evidence that would raise confidence further: <specific, measurable>

### Communication efficacy (if exchange)
- What landed: "<quote from me>" — <why it worked>
- What to tighten: "<quote from me>" → drop-in replacement: "<revised>"

### Memetic ideas / narrative reinforcement
- **<kind>** — "<verbatim quote>" — compression: <one-sentence distillation>
  - Reinforces / sharpens / challenges: `<file>` § <section>
  - Proposed reuse: <tagline | landing hero | section header | FAQ | blog title | social hook | doc edit with file path + before/after>
  - Stickiness: <short rationale, omit if not obvious>
- **<kind>** — ...

### Other axes (WTP, referral, objections, valence, trust, competitors)
<only include subsections with signal>

## Proposed doc edits

- `docs/icp/primary_icp.md` — <specific change with replacement language>
- `docs/foundation/product_positioning.md` — <specific change with replacement language>

## Proposed website changes

One entry per proposal. If no concrete site change is warranted, render `_No website changes warranted by this feedback._` and stop — do not invent filler.

- **<surface_type>** — route `<path>` — <change_kind> — effort: <trivial|small|medium|large> · confidence: <high|medium|low>
  - File(s): `frontend/src/.../<File>.tsx` (or `site_data.ts`, `faq_items.ts`, `seo_metadata.ts`, `full_page_paths.ts`, `site_pages/<slug>/`, locale file)
  - Current:
    > <verbatim excerpt of existing site copy, OR short description of current structure>
  - Proposed:
    - before → after (for copy edits), OR
    - full concrete spec (title, slug, sections, CTAs, key lines) for structural changes
  - Backed by: <axis> — "<verbatim feedback quote>" (contact=<entity_id>)
- **<surface_type>** — ...

## Memetic shortlist

- Top 1–3 memes from the Memetic axis above that are highest-leverage for reuse this week. Each line: the verbatim phrase/frame + the single surface to ship it on next (e.g. "ship as landing hero replacement in `docs/foundation/product_positioning.md` § Hero").

## Follow-up tasks

- At most 3 highest-leverage actions. Each links to the contact.
```

Axes not substantively covered render as:

```markdown
### <axis name>
_Not covered in this feedback._
```

## Step 5: Persist to Neotoma (same turn)

Follow the Neotoma MCP turn lifecycle and `[PROVENANCE]` rules. All of the following happens in the same turn as the user request.

### Entity-type reuse check (before first store in a fresh session)

Before storing with `feedback_analysis`, call `list_entity_types` with keyword `feedback` to check whether a semantically equivalent type already exists (for example `feedback_analysis`, `customer_feedback_analysis`, or a singular/plural variant). If a match exists, reuse that exact type string. Only introduce a new type when no equivalent exists. This prevents type proliferation per the MCP entity-type reuse rule.

### Schema durability for ``feedback_analysis``

The active schema is **v1.1** (registered 2026-05-15) and supports 32 fields including the 10 gate-evidence fields below. All observations will project cleanly into the snapshot without raw_fragments.

### Link meeting audio → evaluator graph (recording pipeline)

When the source is a WAV produced by ``/record_meeting``, drop a sidecar next to
the audio **before** stop/transcribe (or set env in ``.env``) so
``transcribe_audio.py`` can attach ``REFERS_TO`` from the new ``transcription`` to
the evaluator ``contact`` and the ``feedback_analysis`` row for that call:

- File: ``<stem>_neotoma_relations.json`` beside the ``.wav`` (see ``record_meeting`` skill).
- Example keys: ``relate_contact_entity_ids``, ``relate_feedback_analysis_entity_id``.

### Retrieve first

- `contact` / `person` by name or email.
- Any pre-existing `product_feedback` for this item (by identifier or recent listing).
- `conversation` entity if the source is a chat, meeting, or email thread that is already stored.

### Store

Single `store_structured` call when batchable. Entities (in order of typical indices):

1. `conversation` — the current chat conversation (per turn lifecycle).
2. `agent_message` — the current user message, `turn_key: "{conversation_id}:{turn_id}"`.
3. `feedback_analysis` (or the reused type from the entity-type check) with these fields — axes not covered are empty arrays, not omitted, so cross-item queries are reliable:
   - `title`
   - `contact_name`
   - `source_type` (text | file | url | entity)
   - `source_reference` (path, URL, or entity_id)
   - `report_path` (absolute path of the written markdown file)
   - `need_validation`
   - `positioning_resonance`
   - `solution_efficacy`
   - `icp_fit`
   - `icp_update_suggestions`
   - `vocabulary_quotes` (object with `problem`, `neotoma`, `category`)
   - `adoption_triggers`
   - `activation_conditions`
   - `content_marketing_ideas`
   - `market_insights`
   - `confidence_delta` (object with `problem`, `solution`, `evidence_to_raise`)
   - `communication_efficacy` (object with `what_landed`, `what_to_tighten`)
   - `memetic_ideas` (array; each item: `{ kind, verbatim, compression, narrative_alignment: { file, section, relation }, proposed_reuse: { surface, file?, before?, after? }, stickiness? }` — empty array when not covered)
   - `memetic_shortlist` (array of `{ verbatim, surface, rationale }` — the top 1–3 from `memetic_ideas` flagged for near-term reuse; empty array when none)
   - `proposed_website_changes` (array; each item: `{ surface_type, route, source_files: [], change_kind, current: { excerpt?, description? }, proposed: { before?, after?, spec? }, backed_by: { axes: [], quote, contact_entity_id? }, effort, confidence }` — empty array when the feedback warrants none)
   - `other_axes` (object with `wtp`, `referral`, `objections`, `valence`, `trust`, `competitors`)
   - `axes_not_covered` (array of axis names)
   - `data_source` (for tool- or URL-sourced data per `[PROVENANCE]`)
   - `source_file` (basename when a file is involved)
   - `source_quality` — enum: `full_transcript_diarized` | `full_transcript_no_diarization` | `partial_transcript` | `notes_only` | `recall_only`. Set based on what the source actually provides, not what was hoped for.
   - **Gate-evidence fields** (developer release exit gates; set based on what the feedback directly evidences — omit / null when not evidenced):
     - `gate1_weekly_usage_signal` — `observed` | `not_observed` | `unknown`; is this person storing observations weekly?
     - `gate1_entity_type_breadth` — `narrow` | `moderate` | `broad` | `unknown`; how many distinct entity types are in active use (3+ = broad)?
     - `gate1_workaround_reversion` — boolean; did this person revert to markdown, Notion, or manual re-prompting for Neotoma-covered domains?
     - `gate2_retention_signal` — `sustained_4wk` | `plateau_before_4wk` | `churned` | `too_early` | `unknown`; sustained weekly usage ≥4 weeks?
     - `gate3_unassisted_activation` — boolean; did this person go from discovery → active use without direct intervention (DM, walkthrough, personal setup help)?
     - `gate4_cognitive_coldstart` — `blocked` | `friction` | `resolved` | `not_applicable`; did "what should I store?" confusion arise?
     - `gate4_ux_friction` — `blocked` | `friction` | `resolved` | `not_applicable`; UX friction (generic errors, no storage confirmation, MCP restart, duplicates)?
     - `gate4_trust_barrier` — `blocked` | `friction` | `resolved` | `not_applicable`; supply-chain / security concerns blocked or slowed adoption?
     - `gate4_prior_bad_experience` — `blocked` | `friction` | `resolved` | `not_applicable`; prior bad experience with fuzzy / native memory created distrust?
4. `product_feedback` — create only if none exists; reuse the retrieved one otherwise.
5. `contact` — create only if the contact does not exist.

Attach the raw source via `file_path` (for file input) or `file_content` + `mime_type` (for pasted text) on the combined store path, so the raw artifact is preserved per `[PROVENANCE]`. URLs capture the scraper response payload in `api_response_data` or as a saved scrape artifact passed via `file_path`.

### Relationships

Batch in the same `store_structured` call via `relationships` when possible:

- `PART_OF`: `agent_message` → `conversation`.
- `REFERS_TO`: `agent_message` → `feedback_analysis`.
- `REFERS_TO`: `feedback_analysis` → `product_feedback`.
- `REFERS_TO`: `feedback_analysis` → `contact`.
- `REFERS_TO`: `feedback_analysis` → `conversation` (the source conversation if distinct from the current chat conversation).
- `EMBEDS`: `agent_message` → file asset returned by the unstructured path (call `create_relationship` after the combined store, using `unstructured.asset_entity_id`).

### Idempotency

- Turn idempotency key: `conversation-{conversation_id}-{turn_id}-analyze-feedback-{timestamp_ms}`.
- Optional `file_idempotency_key`: `file-<short-slug-of-source>`.

## Step 6: Close the turn

After producing the user-visible reply, store the assistant `agent_message` per the closing-store step of the Neotoma MCP turn lifecycle and link `PART_OF` to the same conversation entity.

## Step 7: Surface to the user

Reply with:
- One-line headline (strongest signal in this feedback).
- Top 3 follow-up actions, each linked to the contact.
- Absolute report path.
- Any proposed ICP or positioning doc edits.
- Memetic shortlist: up to 3 verbatim phrases/frames + the single surface to ship each on next (empty list if the source yielded no memetic signal).
- Proposed website changes: up to 3 highest-leverage site changes — each as `<surface_type> @ <route>` with the exact source file and a one-line change summary (empty list if none warranted).

Render the `Neotoma` section per the `[COMMUNICATION & DISPLAY]` display rule, listing the `feedback_analysis` and any created `product_feedback` / `contact` entities under Created (plus any retrieved entities under Retrieved).

## Behavior rules

- **No invented quotes — in the report or in stored entity fields.** Every quote block in the markdown report AND every quote string stored in `vocabulary_quotes`, `memetic_ideas`, or any other entity field MUST be verbatim from the source. Paraphrase is labeled `paraphrase: ...` in both the report and the stored field. Never smooth out, clean up, or reconstruct a quote — use the raw text or label it explicitly. If the source contains no direct quotes (e.g. recall_only or notes_only), every axis that would normally carry a verbatim quote MUST carry a `paraphrase: ...` label instead.
- **Concrete doc edits.** Proposed ICP / positioning edits MUST cite the exact file path and propose concrete replacement language, not a vague "update ICP".
- **Communication efficacy is required for exchanges.** If the source is a conversation involving the user, the Communication efficacy axis is REQUIRED, not optional.
- **Flag non-ICP signal.** If the feedback is from someone clearly outside the primary ICP archetype in `primary_icp.md`, explicitly note that before scoring positioning resonance / solution efficacy. This prevents over-weighting non-ICP signal and stays consistent with `process-feedback`'s `addressability_timing` bucket.
- **Bounded action list.** Surface at most 3 highest-leverage follow-up actions at the bottom. Everything else lives in the body of the report.
- **Stable shape.** Emit uncovered axes as explicit skip markers in the report AND as empty arrays in the stored entity, so future aggregation queries are reliable.
- **Memes must be sourced.** Every entry in the Memetic axis MUST have a verbatim quote from the feedback, or be explicitly labeled `paraphrase` when only a paraphrased recap exists. Do not mint memes the source did not express, even if they sound on-brand. Do not promote a meme into the shortlist without naming a specific surface (tagline, landing hero, section header, FAQ, blog title, social hook, or a concrete before/after doc edit).
- **Memes cite existing narrative.** Every meme entry must name the specific foundation doc + section it reinforces, sharpens, or challenges — or explicitly flag itself as a candidate new narrative beat. No ungrounded "on-brand" claims.
- **Website changes are grounded in current site state.** Every "Proposed website changes" entry MUST (a) cite the exact absolute file path(s) in `../neotoma/frontend/src/...`, `../neotoma/site_pages/...`, or `../neotoma/frontend/src/i18n/locales/...`; (b) cite the public route; (c) for copy edits, quote the existing site text verbatim (open the file if you have not yet read it — do not guess) before proposing the replacement; (d) cite at least one backing axis and a verbatim feedback quote. Never propose a site change the feedback did not motivate, even if it would improve the site on general principle. A proposal without a current-state excerpt (copy) or current-state description (structural) is not a proposal — cut it.
- **No speculative ship claims.** The skill proposes changes; it does not edit the site or claim anything has shipped. Follow-up task verbs stay at the "propose / draft / ship" boundary.

## Aggregate Mode

Invoked as `/analyze-neotoma-feedback --aggregate [--since YYYY-MM-DD] [--types t1,t2,...]`.

Produces one cross-item synthesis across the full feedback corpus instead of per-item reports. It does NOT replace per-item analyses — it complements them by summarizing themes, counting signal, and pointing at the highest-leverage items worth deeper per-item runs.

### Step A: Enumerate feedback corpus

Entity types in scope by default (reflect Neotoma-specific feedback signal captured so far):
`product_feedback`, `feedback_note`, `tester_feedback`, `ui_feedback`, `ui_bug_report`, `ui_copy_feedback`, `user_feedback`, `feedback`, `feedback_artifact`, `feedback_finding`, `feedback_scan`, `social_feedback`, `documentation_feedback`, `design_feedback`, `process_feedback`, `meeting_note`, `meeting_notes`, `meeting_transcription`.

`--types` overrides the default list. `--since` filters by `last_observation_at >= YYYY-MM-DD`.

For each type, call `retrieve_entities` with `include_snapshots: true`, paginated by `limit` + `offset`, sorted by `last_observation_at desc`. Cap total items examined at 300 unless the user explicitly raises the cap — this keeps one run bounded.

For each item capture: `entity_id`, `entity_type`, `canonical_name`, `snapshot` fields (`title`, `summary`, `feedback_text`, `content`, `key_themes`, `topic`, `feature`, `source`, `feedback_channel`, `feedback_date`, `full_content`, `observations`, `tags`). Record `last_observation_at` for recency weighting.

### Step B: Cluster + synthesize

For each canonical axis (Need validation, Positioning resonance, Solution efficacy, ICP fit, Vocabulary, Adoption triggers, Activation conditions, Content ideas, Market insights, Confidence delta, Communication efficacy, Memetic ideas, Other), produce:
- **Themes**: recurring patterns across ≥2 items. For each theme, list item count, representative verbatim quotes (with `entity_id` citation), and which ICP operational mode (infra, builder, operator) it tends to come from.
- **Outliers**: single high-signal items worth a per-item deep dive. Flag them explicitly.
- **Contradictions**: themes that conflict across items — surface the disagreement instead of averaging it away.

For the **Memetic ideas** axis specifically, cluster by shared kind (phrase, frame, metaphor, analogy, contrast, story, coined_term) AND by shared underlying idea. A meme "cluster" is any idea expressed — verbatim or paraphrased — by ≥2 distinct feedback items. For each cluster capture: kind, representative verbatim quotes with entity_ids, item count, ICP mode mix, which Neotoma narrative beat it reinforces or challenges, and a single proposed reuse surface (the sharpest fit across the cluster). Solo memes (single-item) with exceptional stickiness are promoted to outliers, not discarded.

Also produce **website change clusters** derived from per-item proposals and from aggregate axis themes. Group by `surface_type` + `route` + `change_kind` — so repeated "FAQ add: what is the difference between Neotoma and Mem0?" across 4 items becomes one cluster with 4 backing entity_ids, not 4 separate rows. Each cluster carries: surface_type, route, source_files, change_kind, representative current-state excerpt, proposed change (before/after or spec), item count, ICP mode mix, backing entity_ids, aggregate effort estimate, and aggregate confidence.

Never invent quotes. Every quote carries the source `entity_id`. If the snapshot only has a paraphrase, label it `paraphrase (entity_id)` not a quote.

### Step C: ICP / positioning impact

Compare the themes to `docs/icp/primary_icp.md` and `docs/foundation/product_positioning.md`. For each theme that has implications:
- **Confirms**: ICP archetype, adoption trigger, qualification criterion, or positioning phrase supported by ≥3 items.
- **Sharpens**: a phrase or claim that should be tightened; propose concrete replacement language with file path.
- **Challenges**: a theme that pressures the current framing; propose what to investigate or reframe.

### Step D: Write aggregate report

Output location: `/Users/markmhendrickson/repos/neotoma/docs/private/customer-development/aggregate_YYYY_MM_DD_feedback_analysis.md`

Template:

```markdown
# Aggregate Feedback Analysis — <date>

**Window:** <since> to <now> · **Items analyzed:** N across <K> entity types
**Analyst turn:** <conversation_id>:<turn_id>

## Headline

- 3–5 bullets capturing the strongest cross-corpus signals.

## Corpus breakdown

- Items per entity type, items per ICP operational mode (when inferable), items per feedback channel.

## Axis synthesis

### Need validation
- Theme: <name> — <N items> — representative quotes (cited by entity_id) — ICP mode mix
- Theme: ...
- Outliers: entity_id — one-line why
- Contradictions: ...

### Positioning resonance
<same shape>

### Solution efficacy
<same shape>

### ICP fit
<same shape; include confirmed archetype matches, non-ICP signal share, and any proposed ICP doc edits>

### Vocabulary in users' own words
- Problem: list of verbatim phrases with entity_id citations
- Neotoma: ...
- Category: ...

### Adoption triggers
<same shape>

### Activation conditions
<same shape>

### Content marketing ideas
- Questions raised repeatedly → proposed asset (post, landing page, FAQ, comparison)

### Market / technology insights
<same shape>

### Confidence delta
- Problem confidence: + / - / 0 — aggregated reasoning with counts
- Solution confidence: + / - / 0 — aggregated reasoning with counts
- Evidence most likely to raise confidence next: ranked list

### Communication efficacy
- What framings landed repeatedly — with entity_id citations
- What to tighten or cut — drop-in replacements

### Memetic ideas / narrative reinforcement
- **Cluster: <short name>** — kind — <N items> — representative verbatim quotes with entity_ids — ICP mode mix
  - Reinforces / sharpens / challenges: `<file>` § <section>
  - Proposed reuse surface: <tagline | landing hero | section header | FAQ | blog title | social hook | concrete doc edit with before/after>
- **Cluster: ...**
- Outliers: solo memes with exceptional stickiness — entity_id — one-line why
- Candidate new narrative beats: memes that don't map onto any existing foundation beat but show cross-item signal — each with entity_ids and proposed beat phrasing

### Other axes (WTP, referral, objections, valence, trust, competitors)
<only include subsections with theme-level signal>

## Proposed doc edits

- `docs/icp/primary_icp.md` — <specific change with replacement language> — backed by <entity_ids>
- `docs/foundation/product_positioning.md` — <specific change with replacement language> — backed by <entity_ids>

## Proposed website changes (corpus-wide)

Group proposals into **clusters** (repeated across ≥2 items) and **singletons** (high-confidence single-source proposals worth surfacing). If neither exists, render `_No website changes warranted by this corpus._`.

- **Cluster: <short name>** — <surface_type> @ route `<path>` — <change_kind> — <N items> · effort: <trivial|small|medium|large> · confidence: <high|medium|low>
  - File(s): absolute paths
  - Current:
    > <verbatim excerpt of existing site copy or short description of current structure>
  - Proposed: before → after OR concrete structural spec
  - Backed by: <axes> — <representative verbatim quotes with entity_ids>
- **Singleton: <short name>** — <same shape> — backed by <entity_id>

## Memetic shortlist (corpus-wide)

- Top 3–5 memes (cluster or outlier) highest-leverage to ship this week. Each line: verbatim phrase/frame + the single surface to ship on + backing entity_ids.

## Highest-leverage per-item deep dives

- Top 5 items worth running per-item `/analyze-neotoma-feedback` on next, each with entity_id + one-line rationale.

## Follow-up tasks

- At most 5 highest-leverage actions across the whole corpus.

## Appendix: corpus index

Table of all examined items: entity_id · entity_type · title · date · primary axis touched.
```

### Step E: Persist aggregate entity

Reuse the `feedback_aggregate_analysis` entity type if one already exists (check `list_entity_types` with keyword `aggregate` or `feedback`); otherwise create it. Fields:
- `title`, `analysis_date`, `window_start`, `window_end`
- `items_analyzed_count`, `entity_types_scope` (array)
- `report_path`
- `axis_themes` (object keyed by axis; each value is an array of `{ theme, item_count, representative_entity_ids, representative_quotes, icp_mode_mix }`)
- `axis_outliers` (object keyed by axis; array of `{ entity_id, reason }`)
- `axis_contradictions` (object keyed by axis; array of `{ description, entity_ids }`)
- `vocabulary_quotes` (same structure as per-item)
- `confidence_delta` (object: `problem`, `solution`, `evidence_to_raise`)
- `memetic_clusters` (array of `{ name, kind, item_count, representative_entity_ids, representative_quotes, icp_mode_mix, narrative_alignment: { file, section, relation }, proposed_reuse: { surface, file?, before?, after? } }`)
- `memetic_outliers` (array of `{ entity_id, kind, verbatim, reason, proposed_reuse }`)
- `candidate_new_narrative_beats` (array of `{ proposed_beat, backed_by_entity_ids, rationale }`)
- `memetic_shortlist` (array of `{ verbatim, surface, backed_by_entity_ids, rationale }` — top 3–5 corpus-wide memes flagged for near-term reuse)
- `proposed_doc_edits` (array of `{ path, change, backed_by_entity_ids }`)
- `proposed_website_changes` (array of `{ kind: "cluster"|"singleton", name, surface_type, route, source_files: [], change_kind, current: { excerpt?, description? }, proposed: { before?, after?, spec? }, item_count, icp_mode_mix, backed_by_entity_ids, axes: [], effort, confidence }`)
- `website_changes_shortlist` (array of `{ surface_type, route, source_file, one_line_summary, backed_by_entity_ids }` — top 3–5 corpus-wide site changes flagged for near-term shipping)
- `top_deep_dive_candidates` (array of `{ entity_id, rationale }`)
- `corpus_index` (array of `{ entity_id, entity_type, title, date, primary_axis }`)
- `data_source` (for tool-sourced data per `[PROVENANCE]`)

Relationships: `REFERS_TO` from `feedback_aggregate_analysis` to every entity listed in `corpus_index`, batched via `create_relationship` after the initial `store_structured`. If the corpus exceeds ~50 items, link only the top ~25 by signal (those cited in themes, outliers, vocabulary, or deep-dive candidates) and record the remaining entity_ids inside `corpus_index` — this keeps the graph compact without losing provenance.

Also `REFERS_TO` from the current user `agent_message` to the `feedback_aggregate_analysis`.

### Step F: Surface to the user

Reply with:
- One-line headline of the strongest corpus-wide signal.
- Top 5 highest-leverage follow-up actions.
- Absolute report path.
- Any proposed ICP / positioning doc edits.
- Memetic shortlist: up to 5 corpus-wide memes + the single surface to ship each on, with backing entity_ids.
- Website changes shortlist: up to 5 corpus-wide site changes — each as `<surface_type> @ <route>` with file path and one-line summary, with backing entity_ids.
- Top 5 per-item deep-dive candidates with entity_ids.

## Out of scope

- Updating ICP / positioning docs directly — this skill only proposes edits; the user approves and applies them.
- Editing the `../neotoma` website directly — this skill only proposes concrete, grounded website changes; the user (or a subsequent agent turn in the neotoma repo) applies them.
- Release-stage triage logic — handled by [`process-feedback`](../process-feedback/SKILL.md).
- Running per-item analyses automatically inside an aggregate run — aggregate mode surfaces candidates; the user chooses which to run per-item next.
