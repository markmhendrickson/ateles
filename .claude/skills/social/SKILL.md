---
name: social
description: "Process blog posts into platform-optimized social media share material, and analyze external X posts for reply vs QT strategy, reply drafts, and follow recommendations among repliers. Use when the user wants tweets, LinkedIn, Bluesky, share schedules, or when they paste an x.com/twitter status URL and want reply suggestions or who to follow in the reply thread. Trigger on \"social\", \"share material\", \"reply to this tweet\", \"who should I follow\", \"repliers\", or similar. Can run after /publish. Invoked via /social."
triggers:
  - social
  - social share
  - share material
  - social posts from blog
  - tweet drafts from post
  - distribute a post
  - social media schedule
  - reply to this tweet
  - tweet reply
  - who to follow
  - repliers
  - "/social"
user_invocable: true
entity_id: ent_fca5a02ff506767396bf9d1d
---

# Social

Turn blog posts into platform-optimized share material: punchy drafts the author edits in 60 seconds, not fully automated content. For **blog-derived work**, use single-post extraction or multi-post weekly schedules. For **external X posts** (someone else's tweet), recommend **Reply vs QT vs skip**, produce **reply drafts** (not only QTs), and when reply-thread context exists, **follow recommendations for repliers** (tiered, manual follow only).

## When to use

- User provides a blog post URL or text and wants social share material
- User provides an **X/Twitter status URL** and wants quote-tweet drafts, **plain reply drafts**, or both
- User wants **follow recommendations** for accounts that replied to a tweet (requires reply list from user paste, export, or other captured context; see below)
- User asks for tweets, threads, LinkedIn posts, or Bluesky posts derived from existing writing
- User wants a distribution schedule for a published post
- User asks to "process" or "extract" share content from a post
- User asks to generate a week of social content from their blog or recent posts
- After the `/publish` skill completes for a new blog post (suggest running this skill as a follow-up)

## Always include: top pick and timing

Every output from this skill must include a **"Top pick and timing"** section near the top of the markdown file AND in the conversational response to the user. This section must:

1. **Include the full verbatim text of the recommended draft(s)** right at the top, ready to copy-paste. Do not make the user scroll to find the text. This is the first thing in the output after the header and source metadata.
2. **Name each recommended draft** with its label (e.g., "QT 1", "Reply B") and **give concrete timing guidance** per the rules below. Include where the link goes.
3. **Explain in 1-3 sentences why that pick beats the alternatives** (after the copy-paste text, not before it).
4. **Relevance gate:** Before recommending a draft as top pick, verify it answers or directly engages with OP's actual question/frame. A draft that pivots to your own concern (even a valid one) instead of staying in OP's problem space is not the top pick for a QT or reply. Save tangential-but-strong drafts for standalone posts or reserves.

**Timing rules by post type:**

- **QTs and standalone posts** can be scheduled. Give a specific time window relative to the user's timezone (CEST) and target audience timezone (typically US Eastern). These benefit from timezone optimization.
- **Replies to someone else's tweet cannot be scheduled** via standard tools. Say "post directly when ready" instead of a clock time. Replies are live engagement: post when you can stick around for 10-15 minutes to respond if OP or others reply back. For time-sensitive threads (viral tweets aging past 24h), note urgency ("thread is aging, post today" or "still active, no rush").
- **Self-replies** (link-in-reply, thread continuations) are posted immediately after the parent. No separate timing needed.

For external OP tweets: timing should also factor tweet age and reply velocity (a 2-hour-old viral tweet has different urgency than a 2-day-old one).

## Philosophy

Each blog post contains 5-10 discrete shareable units: a specific claim, a surprising data point, a named framework, a story beat, a provocative reframe. This skill extracts those units and compresses them into platform-native formats. The goal is drafts the author touches up quickly, not fire-and-forget automation.

**Critical principle: interlace, don't drip.** Never schedule more than 3-4 shareable units from a single post in a given week. A week's content should draw from 2-3 different posts so the feed reads like a person thinking across domains, not a content marketer promoting one article. Spread each post's best units across 2-3 weeks. Reserve the remaining extracted units for opportunistic use (when a topic becomes relevant again, a news hook appears, or someone else tweets about the same theme).

**Critical principle: vary the language, not just the source.** Before drafting, inspect the most recent 3-5 files in `execution/website/markmhendrickson/social/` and note repeated hooks, closers, metaphors, and pet phrases. If the new draft repeats a recent phrase or sentence shape, rewrite it unless the exact wording is indispensable.

## Style note: em dashes in social content

The `content_style_enforcement` rule prohibits em dashes in blog posts, docs, and marketing copy. Social media drafts are exempt from this constraint. Em dashes are effective in tweets and short-form posts for compression and pacing. Use them freely in social drafts. Do not carry them into blog or doc content.

## Viral structure principles

These principles are derived from analyzing which posts break out (15-20x baseline impressions) versus which stay at baseline. Apply them to every draft.

### 1. Lead with the most surprising single fact or moment

The punchline IS the first sentence. Never set up context before the hook.

- BAD: "I asked 18 of my contacts to prompt their AI agents to evaluate Neotoma against their real workflows. The agents were more honest..."
- GOOD: "An AI agent told its owner my product wasn't for them. The owner forwarded the response without disagreeing."

The context comes after the hook, not before it.

### 2. Disaster-then-redemption is the highest-performing narrative structure

If the post contains a failure, mistake, or crisis followed by recovery or insight, the tweet should lead with the disaster and delay the resolution. Do not compress both into one sentence.

- BAD: "I wiped my database and recovered it because observations are immutable."
- GOOD: "I mass-deleted my production database on a Tuesday afternoon. 6,174 observations. Gone. Then I remembered how the system was built."

### 3. Personal vulnerability + structural insight = breakout

The highest-performing combination is "I lived this" + "here's the systemic reason why." Neither alone is enough. A personal story without a structural takeaway is a diary entry. A structural insight without personal stakes is a lecture.

### 4. Absolve individuals, indict systems

Posts that say "not a failure of people, a failure of incentive structure" get retweeted because people share things that absolve them while explaining their experience.

### 5. Never resolve the tension in the main tweet

Leave one thread open for someone else to pull. The reply chain is where the strongest algorithmic signal lives. Every tweet should leave the reader with something to agree with, disagree with, or answer.

### 6. End with stakes or questions, never summaries

- BAD: "Full writeup here: [link]"
- GOOD: "Tell me which one I'm wrong about."
- GOOD: "What's the worst accidental-delete you've recovered from?"
- GOOD: "How do you tell the difference between real demand and narrative FOMO while you're inside it?"

The best questions are ones the audience has personal experience with. "What's your model stack?" works because everyone has one. "Thoughts?" does not work because it's too vague. **Rhetorical questions do not count.** If the implied answer is obvious ("bad things happen"), readers nod and scroll. The question must be one people answer with a story, a workaround, or a yes/no that triggers a follow-up. Test: could 5 different readers give 5 different real answers? If not, rephrase.

### 7. Make readers feel like insiders

Shareable one-liners make the reader feel smarter for having read them. These are the phrases that get screenshot-shared and bookmarked.

- "The cheapest token is the one you never have to burn."
- "Your AI remembers your vibe but not your work."
- "Markdown files are memory-as-vibes."
- "'It works on my machine' is not a release. It's a demo you haven't stress-tested yet."

When extracting shareable units, look for sentences in the blog post that could function as standalone insider phrases. If the post doesn't have one, compress its core argument into one.

### 8. Disagree with bigger accounts, respectfully and specifically

Quote-tweeting a large account with "I agree and here's my version" gets minimal distribution. Quote-tweeting with "this is the right instinct but the wrong artifact" gets you into their audience's reply feed. The disagreement should be structural, not personal: affirm the problem, challenge the solution.

## Step 1: Fetch and analyze the post

**If given an X/Twitter status URL** (`x.com/.../status/...` or `twitter.com/.../status/...`): Use the workspace **web-scraper** MCP `scrape_content` with that URL to retrieve the tweet text and metadata (author, reply/like counts). Do not rely on `WebFetch` for x.com; it often fails. Then continue to **External OP tweets** (below) in addition to any blog-style extraction if the user also linked a post.

**If given a non-X URL:** Use the `WebFetch` tool to retrieve the full post content. If the URL is a local blog post (markmhendrickson.com), you can also read the source markdown directly from `execution/website/markmhendrickson/react-app/src/content/posts/` using the post slug.

**If given a slug or title:** Read the post from `execution/website/markmhendrickson/react-app/src/content/posts/{slug}.md` (published) or `drafts/{slug}.md` (draft).

Read the post carefully and extract:

1. **Concrete specifics** - numbers, counts, timelines, before/after comparisons (e.g., "6,174 to 84", "18 evaluators", "11 releases in 5 weeks")
2. **Named frameworks or distinctions** - any original terminology or conceptual split the author introduces (e.g., "write-and-derive vs. write-only", "three kinds of memory")
3. **Story beats** - personal narrative moments with tension, surprise, or resolution. Prioritize moments of failure, disaster, or surprise. These are the highest-value shareable units.
4. **Provocative reframes** - claims that challenge conventional wisdom or restate a familiar idea in unfamiliar terms. Look especially for sentences that could function as standalone "insider phrases."
5. **Reference-worthy takeaways** - lists, models, decision criteria, or structured insights people would bookmark
6. **Disagreement surfaces** - places where the author's position differs from a prominent person, common practice, or prevailing narrative. These become QT-ready drafts.

Aim for 6-10 shareable units per post. Label each with its type. Rank them by viral potential: story beats with disaster/redemption arcs and provocative reframes with insider phrases rank highest.

### Step 1.5: Audit recent language before drafting

Read the most recent 3-5 markdown files in `execution/website/markmhendrickson/social/` before writing new drafts. Build a short scratch list of:

- Repeated hooks
- Repeated contrasts
- Repeated questions
- Repeated rescue stories or examples
- Repeated sentence shapes

Pay special attention to phrases that have already appeared multiple times recently, such as:

- "right instinct"
- "append observations" / "append-only observations"
- "nothing gets silently overwritten"
- "same question, different answer next week"
- "what did I believe on March 15"
- "silently rewrite"
- "truth layer"
- "transcripts are drafts"

These are not banned forever, but they are **cooldown phrases**. Prefer fresh wording if any of them appeared in the recent sample.

When the same underlying idea still needs to be expressed, rotate the framing:

- version drift
- contradiction handling
- receipts / traceability
- mutable summaries
- as-of history
- conflict resolution
- source-aware memory
- stale merges

Before finalizing the **Top pick and timing** section, do a reuse pass:

1. Check whether the top pick's first sentence sounds too close to a recent top pick
2. Check whether the close repeats a recent question pattern
3. Check whether the same anecdote is being used again (`6,174 observations`, `March 15`, `same answer next week`, etc.)
4. If yes, either rewrite the phrasing or demote that draft in favor of a fresher option

### External OP tweets: replies, QTs, and replier follows

When the source is someone else's tweet (not the author's blog), treat it as **OP** (original post). Produce three outputs when relevant: **(1) triage**, **(2) reply and QT drafts**, **(3) follow picks among repliers**.

#### 1) Does this tweet deserve your reply?

Give a clear **recommendation: Reply / QT only / Skip / Reply + QT** with one or two sentences of reasoning.

**Green lights (often deserve a reply or QT):**

- OP asks a real question or names a pain where you have non-obvious expertise
- The frame is mostly right but misses a structural distinction you can add in one or two short paragraphs
- Thread is civil and high-signal; your angle is additive, not pile-on
- Audience overlaps AI builders, operators, or researchers you want to reach

**Red lights (prefer skip, or QT with distance, avoid reply under OP):**

- Harassment, bad-faith dunk culture, or coordinated outrage
- Reply sections that are pure meme spam or bot-like promo unless you see clear humans to engage
- Topic far from your positioning unless you have a genuine bridge narrative
- You would only be saying "great thread" or restating the post (forbidden; see anti-patterns)

**Scale heuristic:** Very large reply counts (hundreds or thousands) mean a good reply can still be buried. Favor **QT** for reframes that need your audience; use **reply** when answering OP directly or joining a sub-thread with traction.

#### 2) Reply vs QT (when both are on the table)

| Channel | Best for |
|--------|-----------|
| **Reply** | Direct answer to OP, short technical fix, rapport, joining a specific sub-thread. Shows up under the tweet; OP may see it. |
| **QT** | Reframe for your followers, structural disagreement, narrative that would be lost as reply #900. Surfaces to quoted tweet's audience too. |

Apply the same viral principles: hook first, pain-first vocabulary on X, end with a **specific** question when possible. **Replies** are often one notch shorter and more conversational than QTs.

**Links in replies (X):** Same as main skill: avoid a naked link in the first reply if you care about distribution; put the link in a **second reply** to yourself when needed.

**Product mention:** Do not lead with "I built X." Lead with the insight; product or post link comes after value, unless the user explicitly wants a launch-style reply.

**280-char fold in replies:** X only shows the first 280 characters of a reply on the timeline; the rest requires "Show more." When drafting replies, check whether the strongest line (mic-drop closer, key insight, question) lands above or below that fold. If the closer is below 280 chars, either tighten the setup so it fits, or make sure the visible portion is interesting enough on its own to earn the click.

Produce **2-4 reply drafts** (varied angles: helpful answer, structural reframe, clarifying question, lived-experience line) and **1-2 QT drafts** when QT is still justified.

#### 3) Follow recommendations for repliers

**Data gap:** The workspace `scrape_content` tool fetches a **single tweet** or a **profile's tweets**; it does **not** return the reply thread. To recommend follows:

1. Ask the user to **paste** a handful of high-signal replies (handle + text), **or** paste a list of `@handles`, **or** provide a screenshot you can read, **or** use **browser tools** if available to snapshot reply authors from the thread.
2. If no reply data exists, **say so explicitly** and give the exact next step (e.g. "Open replies, copy 5-10 that look like builders, paste here").

**What to recommend:**

- **Strong follow:** Original technical or operator signal, consistent builder/researcher vibe, plausible mutual relevance to AI/agent/memory/infra audience, low spam markers (generic praise-only, engagement-bait patterns, repetitive CTAs).
- **Worth a look:** Good one-off reply; scan timeline before following.
- **Skip:** Bots, engagement farmers, off-topic, or not enough signal from one reply.

Cap **Strong follow** at roughly **5-10** handles per request unless the user asks for more. These are **recommendations only**; the user follows manually. **Never auto-follow** or imply the author endorses every account.

Optional: if the user supplies only `@handles`, use `scrape_content` on `https://x.com/{handle}` (profile) to sample recent tweets **before** scoring follow-worthiness, when the tool is available and the user has not opted out of extra fetches.

#### Anti-patterns specific to replies

- **The agreeable reply:** "This." / "So true." / "Great question" with no new substance.
- **The drive-by pitch:** First sentence is product or signup link.
- **The tone-deaf correction:** Talking down to OP on a viral tweet; keep disagreement structural and respectful.
- **Feed pollution:** Recommending follows for accounts that are pure outrage or engagement hacks.

## Step 2: Produce platform-specific drafts

For each shareable unit, produce drafts for the requested platforms. Default to X/Twitter, LinkedIn, and Bluesky unless the user specifies otherwise.

### X/Twitter rules

These rules are derived from the open-sourced X algorithm (2023 release and January 2026 Grok-powered update) and empirical engagement data.

**Format hierarchy:**
- Text-only posts and native video both perform well on X. Some data (Buffer) suggests text outperforms video by ~30%; other sources (Tweet Archivist) suggest native video gets up to 10x more engagement. The safest conclusion: both text and native video outperform images and links. External video links (e.g. YouTube) are penalized like any other external link.
- Images outperform links by ~12%
- Links get near-zero engagement for non-Premium accounts. Link suppression intensified in early 2026.

**Engagement weight reference:**

The directional hierarchy is confirmed across all sources and both open-source releases. The exact multipliers below are from the 2023 open-source code. The 2026 Grok-powered system uses a neural network with unpublished weights, but all sources confirm the same directional ranking:

- Replies and conversation depth are the strongest positive signal (~27-150x a like depending on source and whether the author replies back)
- Bookmarks are a strong signal (~10-20x a like), the "silent endorsement"
- Retweets/reposts are moderate (~20x a like)
- Likes are the weakest positive signal (baseline)
- Negative signals (block, mute, report, "not interested") actively suppress distribution

**Engagement velocity, the most important timing factor:**

How quickly engagement accumulates in the first 15-30 minutes after posting is the single strongest distribution signal. The algorithm shows your tweet to a small test group first (~5-10% of followers). If that seed group engages quickly, the algorithm amplifies to a wider audience. If they don't, the tweet dies. This means:
- Post when your aligned audience is most active
- Your hook must be strong enough to generate immediate engagement from the seed group
- A tweet that gets 5 replies in 15 minutes will vastly outperform one that gets 20 replies over 24 hours
- Time decay is steep: a tweet loses roughly half its visibility score every 6 hours

**Author diversity scoring:**

The algorithm limits how many of your posts appear in any single person's feed. If you post 6 tweets in a day, they compete against each other for slots. The system attenuates scores from repeated authors to ensure feed diversity. Spacing posts 30-60 minutes apart and capping at 2-3 per day is a structural constraint in the algorithm.

**The dark engagement problem:**

Long-form, information-dense tweets often generate high total engagement rates (6-10%) but the vast majority of that engagement is invisible: "see more" expansions, profile clicks, link clicks, media clicks. These are moderate algorithmic signals but the algorithm weights them far below replies, reposts, and bookmarks. An account can have 80%+ of its engagement be dark (clicks and expansions) with less than 20% being visible (replies, likes, reposts, bookmarks). The content is being actively consumed but generating almost no amplification signal.

The cause is structural: when readers finish consuming a dense, satisfying post, they've gotten what they need. Going back to the original tweet to reply or like requires friction (scrolling back up, navigating out of a thread). The content was complete, so there's no unfinished business pulling them to respond.

**Converting dark engagement to visible engagement:**

- Place the question or open tension at the END of the expanded content, not just in the first line. The reader who clicked "see more" finishes reading right where the reply box is. If the last thing they read is a question they have an opinion about, the friction to reply is minimal.
- For the densest ideas, split into a punchy hook tweet + a long-form self-reply. The hook tweet captures quick likes and retweets from feed scrollers (driving distribution). The depth lives in the reply for readers who click in. This separates the distribution vehicle from the content vehicle.
- Avoid resolved arguments as the final sentence. "Merge, recompute, done" closes the loop. "What's the worst accidental-delete you've recovered from?" keeps it open at the exact moment the reader is most primed to respond.

**Tactical rules:**
- NEVER put links in the main tweet body. Always use the "link-in-reply" pattern: the main tweet contains the hook/claim, a self-reply contains the link
- Keep punchy takes to 71-100 characters for highest engagement
- Keep meatier posts to 240-259 characters for maximum likes
- Use at most 1-2 niche hashtags. More than 2 triggers a ~40% reach penalty
- No all-caps
- Maintain constructive/positive tone. Grok's sentiment analysis penalizes combative tone even when it drives engagement
- Leave tension unresolved in the tweet to invite replies. Do NOT resolve the argument in the same tweet
- Lead with the most surprising single fact or moment, not context or preamble
- Every tweet should end with either an open question, an invitation to disagree, or unresolved tension. Never end with a summary or a link.
- Self-deprecation before resolution outperforms confident announcements. "Nobody understood what the product did" > "I've overhauled the site."
- When writing thread heads, front-load the strongest hook. Thread decay is steep: tweet 2 gets ~50% of tweet 1's impressions, tweet 3 gets ~30%. Put your best material first.
- Process updates and product announcements ("I shipped X", "I've added Y") are algorithmically dead unless reframed as stories or provocations.

**Quote tweet strategy:**

QTs of larger accounts are a reliable reach amplifier because the tweet surfaces to the quoted account's audience. When producing drafts, identify opportunities for the author to QT accounts in their space.

- QT with respectful structural disagreement: "the right instinct but the wrong artifact." Affirm the problem, challenge the solution.
- Never QT with "I agree and here's my version." That gets minimal distribution.
- End QTs with a question that the original poster's audience will want to answer.
- QTs consistently outperform standalone posts for accounts under 10K followers.
- **Context for unfamiliar readers:** Your followers see the QT text first; the quoted tweet is collapsed below. The QT must stand alone. Weave a brief paraphrase of OP's point into the first sentence or two (e.g., "Chamath wants his AI chats to auto-sync into a structured KB. The instinct is right..."). Do not use a preamble; fold the context into the hook. A QT that opens with a reference readers can't resolve without expanding ("The instinct is right" -- right about what?) loses everyone who doesn't click.

**Draft types to produce for X:**

1. **Conversation starters** (2-3 per post) - Lead with the most surprising fact or moment from the post. End with an open question the audience has personal experience with. "What's the worst X you've done?" and "How do you handle Y?" are high-reply-rate patterns. Optimized for reply chains (+75 signal).

2. **Bookmark bait** (1-2 per post) - Reference-worthy frameworks, distinctions, numbered lists with an invitation to disagree, or structured takeaways. These target the +10 bookmark signal. Can be slightly longer (up to 280 chars or a short list). Numbered theses with "which one am I wrong about?" are a strong variant.

3. **Punchy takes** (2-3 per post) - 71-100 characters. One sharp claim, no hedging. The compressed version of a larger argument. Aim for "insider phrase" quality: a sentence that makes the reader feel smarter for having read it. These get screenshot-shared.

4. **Thread opener** (0-1 per post) - First tweet of a 4-6 tweet thread. Only produce this if the post has a disaster-recovery arc, a multi-step narrative, or a numbered thesis set. Front-load the most dramatic or surprising moment. Include thread indicator. Recommend 1 thread per week max.

5. **Link-in-reply pairs** (1-2 per post) - Write the main tweet AND the reply that contains the link. The main tweet must be fully self-contained and interesting without the link. The reply should add context, not just paste a URL. Example reply: "Wrote up the full story of how this happened and what it revealed: [link]"

6. **Reactive QT drafts** (0-2 per post) - If the post's argument disagrees with or builds on a position from a prominent account, produce a draft designed to be posted as a QT when that account (or someone similar) next tweets about the topic. These go in the reserves section with a note like "QT this when someone posts about [topic]."

### LinkedIn rules

LinkedIn rewards different signals than X:

- Dwell time and comments are the top ranking signals
- Text + carousel (document posts) outperform other formats
- Content lifespan is 2-3 weeks (much longer than X's hours)
- No severe link penalty. Links are acceptable in the post body.
- Longer posts perform well (1,000-1,300 characters is the sweet spot)
- First line is critical. It determines whether people click "see more."
- Professional credibility framing matters more than provocation

**Draft types to produce for LinkedIn:**

1. **Insight post** (1-2 per post) - 1,000-1,300 characters. Hook in the first line, then a narrative or framework, then a clear takeaway. Can include the link naturally in the body.

2. **Lesson post** (1 per post) - "Here's what I learned from [specific experience]" format. LinkedIn audiences respond to earned-insight narratives from builders.

### Bluesky rules

Bluesky has become a primary venue for AI/agent builder discourse. Given the stated goal of reaching a new audience of AI builders (vs. the existing crypto-heavy X follower base), Bluesky is a high-priority platform.

**Platform characteristics:**
- Algorithmic feed is opt-in; chronological and custom feeds dominate. This means engagement velocity matters less than on X, but content quality and discoverability via custom feeds matter more.
- No link penalty. Links in post body are fine.
- 300-character post limit (shorter than X's 280 for non-Premium, but most AI builders on Bluesky have adapted to concise posts)
- Thread support via reply chains works the same as X
- Starter packs and custom feeds are the primary discovery mechanism. Getting included in AI/agent-focused feeds and starter packs is the growth lever.
- The audience skews heavily toward developers, researchers, and technical builders. Architectural vocabulary is more appropriate here than on X, though pain-first hooks still outperform.

**Tactical rules:**
- Links in the main post are acceptable and do not hurt distribution
- Hashtags are not a significant factor. Skip them.
- Engage with custom feed curators and starter pack maintainers in the AI/agent space
- Cross-post X threads as Bluesky threads, but tighten to 300-char limit per post
- The tone can be slightly more technical than X. The Bluesky AI community self-selects for depth.
- Quote posts work similarly to X QTs and are effective for the same reasons

**Draft types to produce for Bluesky:**

1. **Conversation starters** (1-2 per post) - Same principles as X but can include links directly. Tighten to 300 chars.

2. **Punchy takes** (1-2 per post) - Same as X. These cross-post directly.

3. **Thread opener** (0-1 per post) - Same as X but each post in the thread must fit 300 chars.

4. **Link posts** (1 per post) - Unlike X, a post with a link and a strong hook sentence performs well on Bluesky. No need for the link-in-reply pattern.

## Step 3: Build the distribution schedule

There are two scheduling modes depending on whether the user provides one post or multiple.

### Single-post mode

When processing a single post, extract 6-10 shareable units but only mark 3-4 as "schedule now." The rest go into a "reserves" section for future use. Do NOT fill an entire week from one post.

### Multi-post weekly schedule (preferred)

When generating a week of content, draw from 2-3 different posts. This is the preferred mode. If the user provides one URL, ask if they want to include other recent posts in the weekly schedule, or check their blog index for recent posts to interlace.

**Weekly template (Monday-Friday, X + LinkedIn + Bluesky):**

- **Monday AM:** Punchy take (Post A) on X + Bluesky cross-post. Pure reach, no link.
- **Tuesday AM:** Conversation starter (Post B) on X + Bluesky. Open question to drive reply chains.
- **Tuesday PM:** Bookmark bait (Post A) on X. Reference-worthy framework or list.
- **Wednesday AM:** Thread (Post C, or whichever post has strongest narrative arc) on X + Bluesky. 4-6 tweets, one thread per week max.
- **Wednesday (LinkedIn):** Insight post (Post B). 1,000-1,300 chars with link.
- **Thursday AM:** Conversation starter (Post A) on X + Bluesky. Different angle from Tuesday.
- **Thursday PM:** Link-in-reply pair (Post C) on X. Link post (Post C) on Bluesky.
- **Friday AM:** Punchy take (Post B) on X + Bluesky. Clean closer for the week.

This gives each post 2-3 appearances across the week without any post dominating. Each appearance uses a different draft type so the same idea never shows up in the same format twice.

**Timing defaults:**
- X: Post for your audience's active hours, not your own timezone morning. If your audience is primarily US-based, schedule posts to hit US morning/midday. For a CEST-based author with a US audience, this means posting 3-8 PM CEST (9 AM - 2 PM Eastern). The author's morning session should produce posts queued for afternoon delivery.
- LinkedIn: Tuesday-Thursday, 7-8 AM or 12-1 PM in the primary audience's timezone.
- Bluesky: Similar timing to X. The AI/agent builder audience is global but US-heavy.
- Space tweets 30-60 minutes apart if posting multiple per day.
- Cap at 2-3 X posts per day including replies and threads.

**Cross-week distribution:** When processing a backlog of posts, spread each post's best units across 2-3 weeks. A post published this week gets its strongest standalone take on publish day, then 1-2 more appearances next week interlaced with newer content. Reserve remaining units for opportunistic use.

## Step 4: Store in Neotoma

After generating the schedule and drafts, persist them in Neotoma for cross-session tracking. This enables querying reserves, tracking what's been posted, and building weekly schedules from accumulated material.

**Store the schedule:**

Use `store_structured` with:

```
entities: [
  {
    entity_type: "social_share_schedule",
    title: "Weekly Social Share Schedule: [Date Range]" or "Social Share Material: [Post Title]" or "Social: OP tweet [handle] [id]",
    mode: "weekly" | "single_post" | "external_tweet",
    source_posts: ["slug-1", "slug-2"],
    target_tweet_url: "https://x.com/.../status/... (when external_tweet)",
    date_range: "YYYY-MM-DD to YYYY-MM-DD",
    status: "draft"
  },
  // One entity per shareable unit:
  {
    entity_type: "social_share_draft",
    source_post_slug: "the-post-slug",
    platform: "x" | "linkedin" | "bluesky",
    draft_type: "conversation_starter" | "bookmark_bait" | "punchy_take" | "thread_opener" | "link_in_reply" | "reactive_qt" | "insight_post" | "lesson_post" | "link_post" | "reply_to_tweet" | "qt_of_external",
    content: "[the draft text]",
    reply_content: "[for link-in-reply pairs or second reply with link]",
    status: "scheduled" | "reserve" | "posted" | "expired",
    scheduled_slot: "Monday AM" | null,
    qt_target_topic: "[for reactive QTs, the topic to watch for]",
    qt_target_accounts: ["@handle1", "@handle2"],
    target_tweet_url: "[when reply_to_tweet or qt_of_external]",
    target_author_handle: "[OP @handle when external]"
  },
  // Optional: one entity per follow recommendation
  {
    entity_type: "social_follow_candidate",
    handle: "@username",
    tier: "strong" | "worth_a_look" | "skip",
    rationale: "[one line]",
    source_tweet_url: "[OP URL this was derived from]"
  }
]
relationships: [
  // PART_OF from each draft to the schedule
  { relationship_type: "PART_OF", source_index: N, target_index: 0 }
]
```

When the user marks a draft as posted, update its status. When generating a new weekly schedule, query existing reserves first:

```
retrieve_entities({ entity_type: "social_share_draft", limit: 20 })
```

Filter for `status: "reserve"` and interlace with new extractions.

## Step 5: Present the output

Save the full output as a markdown file at:

```
execution/website/markmhendrickson/social/{slug}-share-material.md
```

For weekly schedules:

```
execution/website/markmhendrickson/social/weekly-{YYYY-MM-DD}.md
```

For **external OP tweets** (status URL workflow):

```
execution/website/markmhendrickson/social/op-{username}-{tweet_id}-social.md
```

Create the `social/` directory if it doesn't exist.

### Copyable social copy format

Every social draft shown to the user, especially every **Top pick and timing** draft, MUST be presented in a fenced code block with a plain text info string:

````markdown
**Main tweet (copyable):**

```text
[full draft with real blank lines between paragraphs]
```
````

Do not present final social copy only as Markdown blockquotes (`>`), italics, or prose paragraphs, because those formats often remove copy buttons and collapse paragraph breaks in the rendered chat. Blockquotes are acceptable inside the saved markdown file only as explanatory formatting, not as the primary copy surface for text the user is expected to paste into X, LinkedIn, Bluesky, or another composer.

Inside copyable code blocks, preserve actual paragraph boundaries with blank lines between paragraphs. Avoid wrapping a multi-paragraph social draft as a single visual paragraph. If older template examples below use `>` blockquotes as placeholders, replace them with fenced `text` blocks when producing real copy.

### Echo updated copy in chat (mandatory)

Whenever this skill **creates or edits** any file under `execution/website/markmhendrickson/social/` (including `*-share-material.md`, weekly schedules, or OP-tweet files), the **same user-visible reply** must repeat every **body text** that changed so the user can copy without opening the file.

- Use fenced code blocks with the `text` info string (same format as above), one block per distinct postable unit (e.g. main tweet, self-reply with link, each scheduled draft, each thread tweet).
- Include at minimum: **Top pick** (main + link reply if any), plus **every other draft whose prose changed** in that edit.
- If only metadata or headings changed and all copy bodies inside those fences are identical to the prior version, say so explicitly (`Copy unchanged; metadata only.`) and still paste the **Top pick** blocks so the chat remains a single copy surface.

### Social writing style constraints

Apply these constraints to all social drafts unless the user explicitly asks for a different style:

- Avoid sentence fragments and ultra-short pseudo-sentences used for punch. If a phrase cannot stand as a real sentence, rewrite it into a complete sentence or join it to the surrounding sentence.
- In comma-delimited lists of values or attributes, include `and` before the final segment. Prefer `four knowledge domains, deep stakeholder relationships, and integrative judgment` over `four knowledge domains, deep stakeholder relationships, integrative judgment`.
- Do not start a sentence with a short setup segment followed by a colon, such as `One:` or `The move:`. Rewrite as a complete sentence, or fold the setup into the sentence that follows.

### Multi-post weekly schedule format

```markdown
# Weekly Social Share Schedule: [Date Range]

Generated: [date]
Posts included:
- Post A: [title] -- [URL]
- Post B: [title] -- [URL]
- Post C: [title] -- [URL]

## Top pick: copy and post

### Start the week with: [slot, e.g., "Monday AM punchy take from Post A"]

> [full verbatim text of the strongest draft, ready to copy-paste]

**Strongest single draft of the week:** [label + why in one line]
**Timezone note:** [e.g., "All times CEST; queue for US morning = 3-5 PM CEST"]

## Weekly Schedule

### Monday -- [Date]

**Platform: X + Bluesky | Source: Post A | Type: Punchy take | Time: [time]**
> [draft text]

### Tuesday -- [Date]

**Platform: X + Bluesky | Source: Post B | Type: Conversation starter | Time: [AM time]**
> [draft text]

**Platform: X | Source: Post A | Type: Bookmark bait | Time: [PM time]**
> [draft text]

### Wednesday -- [Date]
...

## Thread Draft (from Post C)

**Tweet 1/N:**
> [text]
...

## Link-in-Reply Pair (from Post C)

**Main tweet:**
> [text]

**Reply with link:**
> [text + URL]

## LinkedIn Post (from Post B)
> [draft text]

## Bluesky Link Post (from Post C)
> [draft text with inline link]

## Reserves (for future weeks / opportunistic use)

### From Post A
- [Type] -- [draft text]

### From Post B
- [Type] -- [draft text]

### From Post C
- [Type] -- [draft text]

## Reactive QT Drafts (post when relevant accounts tweet about these topics)

### Topic: [topic]
**Target accounts:** [@handle1, @handle2]
**Draft:**
> [text]
```

### Single-post extraction format

```markdown
# Social Share Material: [Post Title]

Source: [URL]
Generated: [date]

## Top pick: copy and post

### 1. [Draft label] (post first -- [when])

> [full verbatim text of recommended draft, ready to copy-paste]

**Self-reply with link (if applicable):**
> [link reply text]

### Why this one

[1-2 sentences]

## Shareable Units Extracted

1. [Type] -- [brief description]
2. [Type] -- [brief description]
...

## Scheduled Drafts (3-4 best units)

### Draft 1
**Type: [type] | Platform: [platform] | Suggested slot: [e.g., "Monday punchy take"]**
> [draft text]
...

## Link-in-Reply Pair
**Main tweet:**
> [text]

**Reply with link:**
> [text + URL]

## Bluesky Link Post
> [draft text with inline link]

## Reserves (for future weeks / opportunistic use)
- [Type] -- [draft text]
- [Type] -- [draft text]
```

### External OP tweet format (reply + QT + follows)

Use when the user supplied an X status URL (with or without pasted replies for follow scoring).

```markdown
# Social: OP tweet @{author} ({tweet_id})

**Source:** [URL]
**Generated:** [date]
**OP text (verbatim):** ...

## Top pick: copy and post

### 1. [Draft label] (post first -- [when])

> [full verbatim text of recommended draft, ready to copy-paste]

**Self-reply with link (if applicable):**
> [link reply text]

### 2. [Draft label] ([when, relative to first])

> [full verbatim text]

**Link note:** [where the link goes or "no link unless asked"]

### Why this combo

[1-3 sentences: why these beat the alternatives]

## Triage

**Recommendation:** Reply | QT only | Skip | Reply + QT
**Why:** ...

## Reply drafts (post under OP)

### Reply A
> [text]
**Second reply (link, optional):** ...

### Reply B
> [text]

## Quote-tweet drafts

### QT 1
> [text]
**Self-reply link (optional):** ...

## Follow recommendations (repliers)

**Note:** [how reply data was obtained: pasted / profile samples / incomplete]

### Strong follow
- @handle -- [one line]

### Worth a look
- @handle -- [one line]

### Skip
- @handle -- [one line]

## Reserves / follow-ups
- ...
```

## Voice calibration

When the user has a distinctive writing voice (most blog authors do), match these qualities in the drafts:

- **Register** - Match formality level. If the author writes in first person with concrete examples, the tweets should too. Don't academic-ify a builder's voice or casualize a scholar's.
- **Specificity** - If the author leads with numbers and specifics, the tweets should too. Never generalize what the author made concrete.
- **Compression, not flattening** - The tweet should feel like a compressed version of the author's thought, not a different person summarizing it. Cut words, not voice.
- **Tension preservation** - If the author's post contains genuine intellectual tension or disagreement with prevailing wisdom, the tweet should preserve that edge, not smooth it into safe consensus.

### Language variety across runs

Do not let the skill converge on one house phrasing for every memory post. Recent drafts in this repo already reuse a handful of strong lines; that is a warning sign, not a style guide.

Rules:

- Do not reuse the same hook structure in consecutive runs unless the user explicitly asks for a variant on a previous draft
- Do not reuse the same question close in consecutive runs
- Do not default to the same proof point or anecdote if the current source has its own stronger specifics
- If you reuse a core distinction, restate it from a different angle and with different nouns/verbs
- Prefer source-specific language from the current post over importing favorite phrases from older outputs

### Pain-first vocabulary

The author's blog posts and architecture docs use precise internal vocabulary: deterministic state, append-only observation logs, schema constraints, entity snapshots, provenance, reducers. This vocabulary is accurate but does not resonate with the broader audience the author needs to reach on social platforms.

The social voice should lead with pain and outcomes, matching the author's current site framing ("Your agents forget. Neotoma makes them remember"), and use architectural language only as the explanation underneath, never as the hook.

**Vocabulary translation table:**

| Internal (use in replies, threads, deep dives) | Social (use in hooks, main tweets, punchy takes) |
|---|---|
| deterministic state layer | your agent gives you a different answer every time |
| append-only observation log | nothing gets silently overwritten |
| schema constraints | invalid data gets rejected before it's stored |
| entity snapshots | you can see exactly what your agent knew on any date |
| provenance | you can trace every fact to its source |
| derived state / reducers | the system rebuilds itself from the raw history |
| write integrity | your agent's memory can't be corrupted |
| mutable markdown / mutable state | a wiki your LLM can silently rewrite at any time |
| cross-tool state | your agents remember the same things regardless of which tool you're in |

**The rule:** The first sentence of any draft should use social vocabulary. Architectural vocabulary can appear in the body of threads, bookmark-bait posts, or self-replies, never in hooks, punchy takes, or conversation starters.

**Exception:** When the draft is a QT of a technical post from another builder or researcher, architectural vocabulary is appropriate because the audience has self-selected for that depth. Even then, lead with the implication ("what this means") before the mechanism ("how it works").

### Audience context

The author's X follower base is primarily from a previous career in crypto (Stacks/Blockstack/Leather ecosystem). This audience is structurally misaligned with the current content about agent memory infrastructure. Drafts should be written to attract a *new* audience of AI/agent builders, not to engage the existing follower base. This means:

- QTs of accounts in the AI/agent space (Karpathy, Levie, Allie Miller, etc.) are high-priority because they surface the author to a new, aligned audience
- Bluesky is a high-priority platform because the AI/agent builder community there is growing and doesn't carry the legacy follower mismatch
- Crypto-adjacent framing (Bitcoin wallet MCP, Stacks references) should be used sparingly. It engages the old audience but doesn't build the new one.
- Cross-domain narratives (founder FOMO, startup lessons, technology-and-philosophy) bridge both audiences and should be interlaced with agent-specific content

## Anti-patterns to avoid

These patterns consistently underperform based on empirical analysis. The skill should never produce drafts that match these shapes.

**The announcement:** "I've shipped X / I've added Y / I've written about Z." Process updates without stakes or story. Reframe as: what broke, what you learned, or what it means for the reader.

**The self-congratulatory validation:** "A month ago I predicted X and this week Y confirmed it." Leads with self-reference. Reframe by leading with Y's most surprising data point, then add the implication nobody else is drawing.

**The compressed resolution:** Stating both the problem and the complete solution in one sentence. "I wiped my database and recovered it because observations are immutable." The resolution kills the tension. Lead with the disaster. Delay the recovery.

**The context-first preamble:** "I asked 18 of my contacts to prompt their AI agents to evaluate..." Context before hook. Reverse it: hook first, context second.

**The agreeable QT:** "I agree with this and here's my related work." No distribution value. Reframe as structural disagreement: "right instinct, wrong artifact."

**The question-free post:** Any post that ends with a period instead of a question mark or open tension. Almost every tweet should give the reader something to respond to.

**The link dump:** Any post where the primary purpose is driving traffic to a URL. The link always goes in a self-reply (on X). The main tweet must be valuable even if nobody clicks.

## What this skill does NOT do

- It does not auto-publish. It produces drafts.
- It does not **auto-follow** anyone. Follow suggestions are manual.
- It does not create images or video. It produces text-first content (which outperforms visual content on X).
- It does not cover more than 3 platforms per run unless the user asks.
- It does not produce engagement-bait that misrepresents the post's actual content.
- It does not produce announcements, summaries, or resolved arguments as standalone tweets.
- It does not fabricate a reply thread: if repliers were not provided, it asks for paste or capture instead of inventing handles.
