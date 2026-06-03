---
name: draft-rendered-page
description: "Draft a visual HTML outreach/proposal page stored as a Neotoma rendered_page entity, served at a stable guest URL. Grounds scenarios in the recipient's own words and case studies, applies their brand palette, frames as exploration not proposal, and ships mobile-responsive from the first draft."
triggers:
  - draft rendered page
  - draft proposal page
  - create rendered page
  - rendered page
  - /draft-rendered-page
user_invocable: true
entity_id: ent_20d1e8a0419632311ac3c88d
learnings_note: ent_b5bfbffe2b70f9ba9e5649c5
learnings_mirror: docs/private/skills/draft_rendered_page_learnings.md
---

# Draft rendered page

Draft a visual HTML outreach/proposal page and store it as a Neotoma `rendered_page` entity (`html_body` + `custom_css`), served at `GET /entities/:id/html` via a guest access token. The page is for a specific named recipient (a peer, a prospective partner). CSP blocks all JS — inline SVG only, no JavaScript.

Goal of this skill: produce a FIRST draft close to what the user wants, minimizing iteration. The rules below are distilled from a prior multi-iteration session. Source of truth is the Neotoma `skill` entity `ent_20d1e8a0419632311ac3c88d`; full learnings live in `note` `ent_b5bfbffe2b70f9ba9e5649c5` (mirror: `docs/private/skills/draft_rendered_page_learnings.md`).

## Process (do these in order)

1. **Scrape the recipient's website FIRST.** Before drafting, pull their marketing pages, case studies, and blog for the customer pains they already dramatize, and their brand palette. Use those as scenario anchors and colors. This is step 1, not an afterthought.
2. **Draft the email and the page together.** They are coupled — the email references the page; the page should reflect the email's framing. Don't do them sequentially.
3. **Author `html_body` + `custom_css`, mobile-responsive from the start.**
4. **Push both fields in a single correction pass** (they are tightly coupled).
5. **Mint/verify the guest token and the shareable URL.**

## Content / tone rules

- Frame as an **exploration, not a proposal**, unless told otherwise. Peer outreach reads better in that register.
- **Ground every major claim** in something the recipient actually said or a source they trust (their own case studies). Generic positioning prose is a placeholder, not a draft.
- A **quote from the recipient** beats a quote from the sender. Verify quote attribution; never guess speaker from an undiarized transcript.
- **No self-referential meta-commentary** about the medium in the body. Mention the infrastructure (entity type, stable URL, guest token) exactly once, in the footer.
- **band-lead copy concrete and muscular**, not balanced. Lead with a claim, let the second sentence do the synthesis.
- **Hyperlink first mentions** of product/company names (light underline, not bold/colored).
- **Omit by default:** CTA pills, "reply needed"/urgency labels, "What This Is Not" sections, "What I Need From You" sections, and "Report I'll Send You" deliverable grids. They obligate the recipient or signal insecurity. The hero ends after the subtitle.

## Structure rules

- Include a **"The Experiment, Specifically"** section by default (what I'd use from their product / what mine would hold + a 2×2 observation grid). The recipient needs the concrete proposal before they can say yes.
- Ground scenarios in the recipient's existing customer stories; don't invent.
- The **loop/flow must be complete**: (1) query, (2) persist decision, (3) act, (4) return data, (5) read own state first next loop. Don't truncate the return path.
- Every flow step gets a concrete **flow-example** sub-paragraph (entity types, JSON, example queries).
- Credit what the third party's product already does / what the experiment depends on, before explaining your own layer.
- Each scenario = tag badge + dramatic headline + chart (SVG) + actor-labeled story steps. Don't omit the chart or the story steps.
- Architecture diagrams must show the **return loop** (bidirectional: agent outputs become analytics inputs).

## CSS / design rules

- **Mobile responsive from the first draft:** `@media (max-width: 720px)` and `@media (max-width: 420px)` for two-up cards, flow steps, story steps, diagrams, quote bands.
- Story step layout: fixed first column at mobile (76px 1fr at 720px, 64px 1fr at 420px), not `1fr` alone.
- Wide multi-box SVG diagrams (>4 boxes): hide on mobile (`display:none` at 720px); ensure surrounding text/caption carries the content.
- SVG chart labels must not overlap — fixed positioning tested at all widths; whitespace around crossing-point annotations.
- Two-up comparison cards: equal visual weight by default (identical badge bg/color; differentiate by border accent only if needed).
- **Apply the recipient's brand palette** (from their site), not a generic SaaS palette.
- Always include `* { box-sizing: border-box; }` and `main { max-width: none; padding: 0; }` to reset the host template.
- Use band-lead size (1.85rem) sparingly — one intro sentence per major section; body copy 1.02–1.1rem.

## Neotoma persistence

- Store the page as a `rendered_page` entity; push `html_body` and `custom_css` via `correct` (priority 1000), both in the same pass, with a fresh idempotency key per push.
- After substantive iteration, capture new learnings as a `note` linked REFERS_TO this skill entity (`ent_20d1e8a0419632311ac3c88d`) — NOT a new entity type — and mirror to `docs/private/skills/draft_rendered_page_learnings.md`.
