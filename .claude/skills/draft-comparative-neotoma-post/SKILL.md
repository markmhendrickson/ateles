---
name: draft-comparative-neotoma-post
description: "Draft a comparative Neotoma post (memory & truth-layer series) that compares a product or API's memory to a truth layer. Use when user says \"draft comparative neotoma post\", \"memory series post\", \"truth layer comparison post\", or similar. Can be invoked via /draft-comparative-neotoma-post."
triggers:
  - comparative neotoma post
  - draft comparative neotoma post
  - memory series post
  - truth layer comparison post
  - write memory series post
  - draft-comparative-neotoma-post
  - "/draft-comparative-neotoma-post"
user_invocable: true
entity_id: ent_0ad72641935a0c908fc2e807
---

# Draft Comparative Neotoma Post

Draft a post in the **memory & truth-layer series**: it compares a specific product or API's memory (e.g. Claude Memory Tool, OpenAI API, ChatGPT Memory, Claude app Memory) to a [truth layer](/posts/truth-layer-agent-memory) and explains when each fits. The format is flexible so posts stay consistent without sounding cookie-cutter.

## Source of truth for format

**Always use the format defined in the posts README.** Before drafting or editing, read:

`execution/website/markmhendrickson/react-app/src/content/posts/README.md` — section **"Format: memory & truth-layer series"**.

That section defines the recurring beats and how to avoid cookie-cutter repetition. Do not duplicate the full format here; the README is the single source of truth.

## When to use this skill

- User asks to draft a **new** post comparing [Product/API] memory to a truth layer (e.g. "draft a memory series post for Gemini").
- User asks to **edit** or extend an existing comparative Neotoma post and you need the series format in context.
- User says "comparative neotoma post", "memory series post", or "truth layer comparison post" in the context of writing.

## Workflow

1. **Resolve slug and scope**
   - If user named a product/API (e.g. Gemini, OpenAI API, ChatGPT), derive a slug (e.g. `gemini-memory-and-truth-layer`, `openai-api-memory-and-truth-layer`). Use lowercase, hyphens, no special characters.
   - If editing an existing draft, use its slug from `execution/website/markmhendrickson/react-app/src/content/posts/drafts/`.

2. **Load format**
   - Read the README section **"Format: memory & truth-layer series"** (recurring beats, how to avoid cookie-cutter).
   - Apply it to the specific product/API: choose section titles that fit (e.g. "What's missing" vs "Where it falls short"), include optional sections (e.g. "How configuration works" for developer posts, comparison table when it adds clarity).

3. **Draft the post**
   - Create or edit `execution/website/markmhendrickson/react-app/src/content/posts/drafts/{slug}.md`.
   - Include YAML frontmatter: `title`, `excerpt`.
   - Follow the beats: opening (what it is + scope), what it is, where it excels, where it falls short, when a truth layer makes sense, optional config/FAQ, what I'm building (Neotoma paragraph).
   - Link to `/posts/truth-layer-agent-memory` and to other series posts where relevant (e.g. Claude developer post, ChatGPT Memory post).
   - Keep voice consistent; use product-specific examples and avoid filler phrases repeated verbatim.

4. **Supporting files (draft)**
   - **Summary:** Add `drafts/{slug}.summary.md` with 3–6 key takeaways (short lines or bullets).
   - **Tweet:** Add `drafts/{slug}.tweet.md` with share text (under 280 chars, include post URL and relevant @mentions if any).
   - **Hero images:** For new posts, create hero assets per the README "Post Image Assets Checklist" and "Hero Image Style Guide": black background, white line-art only, no text. Use `public/images/posts/{slug}-hero.png` (and -hero-square, -hero-og, `og/{slug}-1200x630.jpg`). Add `{slug}-hero-style.txt` with `keep-proportions`. Optionally use `execution/website/markmhendrickson/react-app/scripts/regenerate-hero-assets-centered.mjs` with a source image to generate all sizes.

5. **Cache**
   - After adding or changing a draft or hero assets, regenerate the cache so the site (and draft-only metadata) picks up the post and hero:
   - From repo root: `python3 execution/scripts/generate_posts_cache.py`.

## Reference posts (examples in this series)

- Claude Memory Tool (developer): `drafts/claude-memory-and-the-truth-layer.md`
- OpenAI API: `drafts/openai-api-memory-and-truth-layer.md`
- ChatGPT Memory (user): `drafts/chatgpt-memory-and-truth-layer.md`
- Claude app Memory (user): `drafts/claude-app-memory-and-truth-layer.md`

Use them for tone and structure only; keep content product-specific.

## Inputs

- **Product/API or slug:** User may say "Gemini", "OpenAI API", "ChatGPT", or a slug. Derive the slug and the post’s focus (developer vs user, what the product calls its memory, official docs links).
- **Intent:** New draft vs edit existing. If editing, load the existing draft and apply format checks or requested changes.
