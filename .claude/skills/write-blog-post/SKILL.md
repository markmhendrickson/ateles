---
name: write-blog-post
description: "Write blog posts for the markmhendrickson website per repo style guides and post workflow. Use when user says \"draft post\", \"create post\", \"write post\", or similar. Can be invoked via /write-blog-post."
triggers:
  - draft post
  - create post
  - write post
  - write-blog-post
user_invocable: true
entity_id: ent_8d327ce3e5a0cc74a2cac710
---

# Write Blog Post

Create or finalize blog posts for the markmhendrickson website following content style rules, draft location, hero image style, and completion requirements. All steps are mandatory; do not treat a post as complete without them.

## When to Use

Use this skill when:
- User says "draft post", "create post", "write post", or similar
- User asks for a new blog post or essay for the markmhendrickson site
- User explicitly invokes `/write-blog-post`

## Required Documents (load first)

1. **Content style:** `docs/content_style_enforcement_rules.mdc` — Anti-AI patterns, title/excerpt/body rules, build-in-public requirements, post completion (key takeaways, hero images, share tweet)
2. **Draft location:** `docs/blog_posts_draft_location_rules.mdc` — Where to create posts and manifest entry
3. **Posts structure and hero images:** `execution/website/markmhendrickson/react-app/src/content/posts/README.md` — Hero Image Style Guide, asset checklist, cache workflow
4. **Build-in-public (if applicable):** `strategy/operations/blog-build-in-public-directives.md` — First-person, no CTAs, "How I'm approaching the build"

## Workflow

### Step 1: Content and location

1. Create or edit post content (markdown body, title, excerpt).
2. **Location:** Content file in `execution/website/markmhendrickson/react-app/src/content/posts/{slug}.md` (or `drafts/{slug}.md` for drafts).
3. **Manifest:** Ensure entry in `execution/website/markmhendrickson/react-app/src/content/posts/posts.json` with `slug`, `title`, `excerpt`, `published` (false for drafts), `category`, `readTime`, `tags`, `createdDate`, `updatedDate`. Do not create drafts in `reports/` or `reports/drafts/`.
4. Apply **content style** to all properties (title, excerpt, body):
   - Sentence case for title and headers; no dashes (—, –, -) in title or excerpt; no semicolons in excerpt.
   - Anti-AI patterns (no "Furthermore", "leverage", "empower", em dashes, soft questions, motivational language).
   - Body starts with content (executive synthesis or first section); do not repeat title in body.
   - Do not mention ateles (use "my agentic stack", "my setup").
   - Excerpt distinct from executive synthesis bullets; all bullet points end with periods.
   - For build-in-public posts: first-person, "How I'm approaching the build" instead of "Getting started", per `blog-build-in-public-directives.md`.

### Step 2: Store post

1. Store in Neotoma first via Neotoma MCP (`store` or `store_structured`, `entity_type: "post"`). Include all fields (slug, title, excerpt, body, published, published_date, category, read_time, tags, etc.).
2. If posts are still in Parquet for this workflow, also add/update via Parquet MCP (`mcp_parquet_add_record` or `mcp_parquet_update_records`, `data_type="posts"`) per `docs/neotoma_parquet_migration_rules.mdc`.
3. Follow `$DATA_DIR/schemas/posts_schema.json`. Set `published: false` for drafts.

### Step 3: Key takeaways

1. Create or update `{slug}.summary.md` in `execution/website/markmhendrickson/react-app/src/content/posts/` (or `drafts/` for drafts).
2. Bullet-point key takeaways derived from post body; each bullet must end with a period. Takeaways must be distinct from the excerpt.
3. After adding or changing, run posts cache script (Step 7).

### Step 4: Hero images (three composed assets)

1. Follow Hero Image Style Guide in `execution/website/markmhendrickson/react-app/src/content/posts/README.md`: solid black background, white line-art only, no typography, minimalist. Reference: `truth-layer-agent-memory-hero.png`, `agentic-search-and-the-truth-layer-hero.png`.
2. Create three assets (do not crop; compose for each format):
   - **Hero:** `{slug}-hero.png` in `execution/website/markmhendrickson/react-app/public/images/posts/`. Add `{slug}-hero-style.txt` with `keep-proportions`.
   - **Square:** `{slug}-hero-square.png` composed for 1:1 (posts list, home, prev/next).
   - **OG source:** `{slug}-hero-og.png` composed for 1200×630 landscape (social previews).
3. From react-app directory: `npm run generate:og:post -- {slug}` to produce `og/{slug}-1200x630.jpg`.
4. After adding images, run posts cache script (Step 7).

### Step 5: Share tweet (drafts)

1. For draft posts only: create `{slug}.tweet.md` in `execution/website/markmhendrickson/react-app/src/content/posts/drafts/`.
2. Tweet aligned with title and excerpt (same message/tone); include relevant URLs and @ mentions; under 280 characters. Same style as post (no AI phrases, sentence case).
3. Run posts cache script so tweet syncs to parquet (`share_tweet`).

### Step 6: Quality check (before marking complete)

Per `docs/content_style_enforcement_rules.mdc` Step 3:
- Style applied to title, excerpt, body; no dashes in title/excerpt; no em dashes in body; sentence case; excerpt distinct from executive synthesis; bullets end with periods; no ateles mention; build-in-public rules if applicable.

### Step 7: Regenerate cache

From repo root: `python3 execution/scripts/generate_posts_cache.py` (or `--from-neotoma-json <path>` if using custom export). Run after changing post content, summary, tweet, or hero metadata.

## Constraints

- Do not create draft posts in `reports/` or `reports/drafts/`.
- Do not treat a post as complete without key takeaways, hero assets (and OG generation), and share tweet (for drafts).
- Do not skip content style enforcement on any property.
- Store in Neotoma first; use Parquet only as fallback per migration rules.

## Related Rules

- `docs/content_style_enforcement_rules.mdc` — Full style and completion requirements
- `docs/blog_posts_draft_location_rules.mdc` — Where to draft and manifest
- `docs/workflow_specifics_rules.mdc` — Blog Post Creation section (same steps, summarized)
- `execution/website/markmhendrickson/react-app/src/content/posts/README.md` — Hero Image Style Guide, asset checklist, cache
