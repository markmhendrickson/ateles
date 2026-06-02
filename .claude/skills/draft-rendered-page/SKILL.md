---
name: draft-rendered-page
description: "Produce a shareable, hosted, visually-designed page — a Neotoma `rendered_page` entity — as a follow-up artifact for a specific contact or audience. Use when a written message or email isn't enough and the follow-through deserves its own URL (proposal pages, briefing pages, narrative recaps, demo-of-thinking artifacts). Output is a live page at `https://neotoma.markmhendrickson.com/entities/<id>/html?access_token=<token>` plus the underlying entity in Neotoma. Can be invoked via /draft-rendered-page."
triggers:
  - draft rendered page
  - draft a rendered page
  - draft proposal page
  - draft briefing page
  - draft shareable page
  - shareable artifact
  - rendered_page artifact
  - hosted follow-up page
  - /draft-rendered-page
user_invocable: true
---

# Draft Rendered Page

Produce a bespoke, visually-designed HTML page hosted as a Neotoma `rendered_page` entity at a stable public URL (via guest access token). The skill exists because some follow-throughs deserve more than an email: a proposal that needs visual scaffolding, a briefing that benefits from charts and structured layout, a narrative recap whose content carries better as a *page* than as paragraphs in a thread.

The page lives next to the structured data it discusses. The recipient gets a single link. The artifact persists at the same URL indefinitely.

## When to use

- A meeting just concluded and `analyze-meeting` flagged the follow-up as warranting a rendered-page artifact (the typical invocation path — see `analyze-meeting` Step 4b).
- User explicitly asks to draft a page for a specific contact ("draft a proposal page for Jamie", "make a briefing page about X for Y").
- A written exchange has reached a point where the next message would be too long, too structural, or too visual for a chat or email body.

**Skip when:**
- The follow-up fits in a 200-word email. Pages are higher-effort than they look — reserve them for content that justifies the lift.
- The recipient is unknown. The skill needs a specific contact (or named audience) to calibrate tone and stakes.
- There is no structured content to convey — just sentiment or a quick reply. Use email instead.

One rendered_page can serve **multiple** contacts when the audience genuinely shares the same context (e.g. a briefing for three co-founders of one company). Otherwise prefer one page per contact — the calibration matters more than the reuse.

## Invocation

```
/draft-rendered-page <topic-or-purpose> --for "Name <email>"
/draft-rendered-page <topic-or-purpose> --for "Name" --source <meeting_analysis_entity_id>
/draft-rendered-page <topic-or-purpose> --for "Name" --reference <neotoma_entity_id> --reference <...>
/draft-rendered-page <topic-or-purpose> --for "Name" --no-publish     # produce locally, do not store as rendered_page yet
/draft-rendered-page <topic-or-purpose> --for "Name" --revise <rendered_page_entity_id>   # iterate on existing page
```

Flags:
- `--for` (required): the recipient. `"Name"` alone, or `"Name <email>"` to attach the recipient `contact` entity automatically. Repeat for multi-recipient pages.
- `--source`: a `meeting_analysis` (or other) entity id that grounds this page — inherits participant context, decisions, and tone.
- `--reference`: any number of Neotoma entity ids the page should `REFERS_TO` once stored (the artifact links itself to the data it discusses).
- `--no-publish`: write the HTML + CSS to disk only (under `data/imports/rendered_pages/`); do not create the Neotoma entity or mint a guest token. Use when iterating before commit.
- `--revise`: iterate on an existing page. The skill reads the current `rendered_page` snapshot, applies the requested changes, and pushes via `correct` (does not create a new entity).

If `--for` is missing, ask for the recipient before proceeding. Recipient calibration is the single most important input — don't draft into a void.

## Step 1: Establish context

1. **Identify the recipient.** Resolve via `retrieve_entity_by_identifier` against `contact` / `person`. Capture `entity_id` and `email` when present. If no match, note that and proceed (a stub contact can be created in Step 6 if needed).
2. **Establish stakes and tone.** What's the relationship? What did the recipient last say? What did Mark commit to? When `--source` is a `meeting_analysis`, pull its `summary_bullets`, `action_items_mine`, and `risks_or_blockers` to ground the artifact in what was actually discussed. Read the source transcript directly if the analysis is thin.
3. **Establish purpose.** One sentence: *"This page is for X so that Y."* Examples:
   - *"This page is for Jamie so that the experiment I proposed on our call is visible, scoped, and clearly low-stakes for him."*
   - *"This page is for the BambooHR team so that the integration we discussed has a single shared reference they can route around internally."*
   If the one-sentence purpose isn't clear, stop and ask the user. Don't draft generically.
4. **Establish what this page is NOT.** A page that doesn't know what it isn't tends to bloat. List 2–4 things the page is deliberately not doing (not a pitch, not an ask, not a benchmark, not a request for new work). These become the closing "What this is not" beat that calibrates expectations.

## Step 2: Plan the page

Before writing any HTML, sketch the page structure as a numbered outline. A typical structure (adapt freely — do not cookie-cutter):

1. **Hero** — page title, one-sentence subtitle, a small kicker that names the recipient and date.
2. **The bet / the frame** — the central claim or framing in one paragraph + a two-up comparison or contrast card.
3. **A grounding quote** — something the recipient said (or something the user said that the recipient agreed with). Quote bands break up the page rhythmically and signal "I listened."
4. **The substantive middle** — the actual content. Usually one or more of:
   - A flow / step-by-step
   - A scenario (concrete, named, with a chart or diagram)
   - An architecture diagram
   - A side-by-side comparison
5. **Asides / parallels** — callouts that connect back to the recipient's own framings, vocabulary, or known concerns. These do most of the "I heard you" work.
6. **What this is not** — from Step 1.4.
7. **What's needed from the recipient** — usually nothing, or one small thing.
8. **Footer** — author, date, link back to the user's domain.

The structure earns each section by serving the purpose. If a section doesn't, cut it.

## Step 3: Draft HTML + CSS

Write two files under `data/imports/rendered_pages/<slug>/`:
- `<slug>_body.html` — the body fragment (no `<html>`, `<head>`, `<body>` wrappers — the Neotoma server wraps it).
- `<slug>_styles.css` — the custom CSS, scoped to the page.

**HTML conventions:**
- Use semantic sections: `<section class="hero">`, `<section class="band">`, `<section class="quote-band">`, `<section class="band band-final">`, `<section class="footer-band">`. The body is a series of vertically-stacked sections.
- Charts and diagrams are **inline SVG**. The server's CSP forbids `<script>`, so no JS, no canvas-based libraries, no external chart embeds. Inline SVG is the only option — and it renders crisply at any width.
- Code-like spans (entity types, identifiers, technical terms) use `<code>` for visual texture and grounding.
- Links open in a new tab (`target="_blank" rel="noreferrer"`) when they point off-domain.

**CSS conventions:**
- Reset the default `rendered_page` template's `main` constraints first (`main { max-width: none; padding: 0; }`) so the custom layout fills the viewport.
- Use a small, deliberate color palette — ideally 2 brand colors + neutrals. For pages aimed at a specific company contact, borrow their palette (read their site if you don't know it). For Mark-default pages, the existing Neotoma palette is cyan `#00d4ff` / navy `#0a1729`.
- Typography: system font stack for default body; reserve a display weight (700+) for the hero `<h1>` and band headlines.
- Responsive: include a `@media (max-width: 720px)` block that collapses multi-column layouts to single-column.

**Reference implementation:** the Jamie proposal page is the current best example of structure, palette, and component patterns. Its body lives at `rendered_page` entity `ent_3c3ecdb10c7ea84a94396c7c` — retrieve and read it as a reference when drafting a new page in the same shape.

## Step 4: Iterate locally before publishing

Before storing as a `rendered_page`:
1. Render the page locally by wrapping the body in a minimal HTML scaffold + the CSS, and open in a browser. (A throwaway `preview.html` under the same `data/imports/rendered_pages/<slug>/` dir is fine.)
2. Read the page critically: would the recipient feel addressed, or generically lectured at? Are the "I listened" beats grounded in actual recipient words?
3. Tighten before commit. Pages that ship with filler don't get retroactively edited — the recipient sees v1.

When `--no-publish` is set, stop here.

## Step 5: Publish to Neotoma

1. **Check schema durability.** Call `list_entity_types` with keyword `rendered_page` to confirm the type exists (it does as of 2026-05-27, registered globally via `seedRenderedPageSchema`). Note the active schema version.

2. **Store the entity.** Single `store` call with `entity_type: rendered_page` and fields:
   - `title` (required) — the page title
   - `html_body` (required) — the body fragment
   - `custom_css` — the CSS
   - `meta_description` — one-line description for `<meta>` tag
   - `slug` — URL-safe identifier (e.g. `omni-neotoma-experiment-jamie-2026-05-27`)
   - `created_at` — timestamp

   Use idempotency key `rendered-page-{slug}-{timestamp_ms}`.

3. **Relationships.** Create `REFERS_TO` edges from the new `rendered_page` to:
   - Each recipient `contact` entity
   - The `--source` entity (e.g. `meeting_analysis`)
   - Each `--reference` entity
   - The grounding `transcription` if the page was meeting-derived

4. **Mint a guest access token.** Use the existing utility script:
   ```
   NEOTOMA_ENV=production npx tsx /Users/markmhendrickson/repos/neotoma/scripts/mint_guest_token_for_entity.mts <entity_id>
   ```
   Capture the returned `token`. The token is bound to that entity and grants read-only access via the `submitter_scoped` guest policy.

5. **Assemble the shareable URL:**
   ```
   https://neotoma.markmhendrickson.com/entities/<entity_id>/html?access_token=<token>
   ```

6. **Verify.** `curl -s -o /dev/null -w "%{http_code}\n" "<url>"` should return `200`. If it 404s, the entity wasn't created; if 401, the token didn't bind — do not surface a broken URL to the user.

## Step 6: Iterate via `--revise`

When asked to revise an existing page:
1. `retrieve_entity_snapshot` the `rendered_page` to get current `html_body` and `custom_css`.
2. Apply requested changes locally to the snapshot.
3. Push updates via `correct`:
   ```
   correct(entity_id, entity_type: "rendered_page", field: "html_body", value: <new>, idempotency_key: "rendered-page-{entity_id}-body-{timestamp_ms}")
   correct(entity_id, entity_type: "rendered_page", field: "custom_css", value: <new>, idempotency_key: "rendered-page-{entity_id}-css-{timestamp_ms}")
   ```
4. Re-verify the live URL returns 200 and contains the new content (grep for a known new string).
5. The guest token does not need to be re-minted — it persists across revisions.

## Step 7: Persist + surface

Same-turn closing store per the Neotoma MCP turn lifecycle. Reply to the user with:
- One-line headline (e.g. `Rendered page for Jamie published — 41KB, status 200.`).
- The live URL.
- The `rendered_page` `entity_id`.
- A short bulleted list of what the page contains (sections / scenarios / diagrams).
- The recipient `contact` entity id(s).

Render the `Neotoma` section per the `[COMMUNICATION & DISPLAY]` display rule.

## Behavior rules

- **Recipient calibration is non-negotiable.** Pages drafted "in general" land worse than no page at all. If `--for` is missing or vague, ask before proceeding.
- **Quote what the recipient said.** When `--source` is a meeting_analysis or transcription, mine the actual transcript for the recipient's own framings and use them verbatim where they fit. Attribution must be correct — do not mis-attribute quotes between participants.
- **The "What this is not" beat is required.** It pre-empts the unspoken question every recipient has ("what are you really asking me for?"). Pages without it read as soft pitches.
- **No JavaScript.** The server CSP blocks it. Charts are inline SVG. Animations are CSS-only.
- **No external font / CSS / image loads** unless they're images on `https:` domains. The CSP allows `img-src 'self' data: https:` but blocks scripts and external stylesheets.
- **`html_body` is not escaped on render.** Anything you put in `html_body` reaches the browser as-is. Do not interpolate untrusted strings.
- **Iterate before committing.** Use `--no-publish` (or local preview) for v1 drafts. The recipient sees the first version they receive a URL to.
- **Skip on insufficient grounding.** If you can't quote the recipient, name the stakes, or articulate the purpose in one sentence, the page isn't ready — stop and gather more context.
- **One page per recipient unless the audience is genuinely shared.** Calibration > reuse.

## Out of scope

- Sending the URL to the recipient — handled by the user (or by the `analyze-meeting` recap_message draft that links to the page).
- Tracking page views or engagement — the artifact is fire-and-forget.
- Editing the recipient's perception or response — the page does the work it does; what happens next is the recipient's call.
- General website / blog post drafting — those are `/write-blog-post`, `/write`. This skill is specifically for single-recipient (or named-small-audience) shareable artifacts with a stable URL.

## Reference

Worked example: the Jamie Davidson Omni × Neotoma proposal page (`rendered_page` entity `ent_3c3ecdb10c7ea84a94396c7c`, contact `ent_244928fcb1eddaf707b1e710`). Its structure, palette choices, scenario blocks, and "What this is not" beat are the current best reference for new pages in the same shape.
