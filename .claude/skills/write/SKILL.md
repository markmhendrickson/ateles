---
name: write
description: Produce publication-ready content in any format from any seed input.
triggers:
  - write
  - /write
user_invocable: true
entity_id: ent_f40e2bf818a6fc6cfd443005
---

[Body content following all content_style_enforcement rules]
```

Accompanied by `.summary.md`, `.tweet.md`, `.linkedin.md`, and `.share.md` files.

## Viral structure principles

These principles are derived from analyzing which posts break out (15-20x baseline) versus baseline. Apply to every draft.

### Lead with the most surprising single fact or moment

The punchline IS the first sentence. Never set up context before the hook.

### Disaster-then-redemption is the highest-performing narrative structure

If the piece contains a failure, mistake, or crisis followed by recovery or insight, lead with the disaster. Delay the resolution.

### Personal vulnerability + structural insight = breakout

"I lived this" + "here's the systemic reason why." Neither alone is enough.

### Absolve individuals, indict systems

Posts that say "not a failure of people, a failure of incentive structure" get retweeted because people share things that absolve them while explaining their experience.

### Never resolve the tension in the main tweet

Leave one thread open. The reply chain is where the strongest algorithmic signal lives.

### End with stakes or questions, never summaries

The best questions are ones the audience has personal experience with. Could 5 different readers give 5 different real answers? If not, rephrase. Rhetorical questions do not count.

### Make readers feel like insiders

Shareable one-liners make the reader feel smarter for having read them. These are the phrases that get screenshot-shared and bookmarked. Extract or compress one per piece.

### Disagree with bigger accounts, respectfully and specifically

Affirm the problem, challenge the solution. The disagreement should be structural, not personal.

## Style notes

### Community engagement voice

Reddit replies, Substack comments, GitHub discussions, and forum posts use a warmer register than social media or website posts. The goal is rapport and credibility with peer builders, not compression or virality.

**Soften authority on others' work.** Never issue definitive architectural verdicts on someone else's system. "That's the right idea" not "That's the right architecture." Acknowledge and ask, don't evaluate and pronounce. The author respects that other builders know their own system better than he does.

**Warmer punctuation.** Use "Thanks!" not "Thanks." when responding to someone who shared useful work or a substantive reply. Brief warmth signals genuine appreciation without being effusive.

**First-person question closers.** "I'm curious about the transport layer specifically" not "Curious about the transport layer." Community register is more conversational than Twitter's telegraphic compression. Adding "I'm" and specificity makes the question feel like genuine interest rather than an interview prompt.

**Sentence flow over staccato.** Prefer joining short related clauses into one flowing sentence with lighter punctuation (commas, en dashes) over statement-period-statement sequences. Community replies read as conversation, not bullet points.

**Em dashes and en dashes are acceptable** in community engagement. Same exemption as social media drafts. The `content_style_enforcement` no-dash rule applies to website posts, docs, and marketing copy only.

**End with a real question.** Every community reply should close with a specific, answerable question that demonstrates you read their work and want to understand their system better. Generic "thoughts?" closers don't count. "How does your policy layer decide what gets written?" counts.

### Em dashes in social content

The `content_style_enforcement` rule prohibits em dashes in blog posts, docs, and marketing copy. Social media drafts and community engagement are exempt. Em dashes are effective in tweets for compression and pacing.

### Pain-first vocabulary for social

Lead with pain and outcomes in hooks. Use architectural language only as explanation underneath.

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

The first sentence of any social draft should use social vocabulary. Architectural vocabulary can appear in thread bodies, bookmark bait, or self-replies.

### Audience context

The author's X follower base is primarily from a previous career in crypto. Drafts should attract a new audience of AI/agent builders. QTs of AI/agent accounts are high-priority. Bluesky is high-priority (growing AI builder community, no legacy follower mismatch). Cross-domain narratives (founder FOMO, startup lessons, technology-and-philosophy) bridge both audiences.

## Anti-patterns (never produce)

- **The announcement**: "I shipped X / added Y / wrote about Z." Reframe as what broke or what it means.
- **The self-congratulatory validation**: "I predicted X and Y confirmed it." Lead with Y's data, not self-reference.
- **The compressed resolution**: Problem and solution in one sentence. Lead with disaster, delay recovery.
- **The context-first preamble**: Context before hook. Reverse it.
- **The agreeable QT**: "I agree and here's my related work." Reframe as structural disagreement.
- **The question-free post**: Ends with a period instead of open tension.
- **The link dump**: Primary purpose is driving traffic. Link always in self-reply on X.
- **The recycled anecdote**: Same story used to make the same point it already made in a recent piece. Find a new example or a new angle for the old one.
- **The vocabulary rut**: Using the same phrases across multiple pieces because they worked once. Rotate.
- **The unsupported claim**: Any substantive assertion without named evidence. Find the data or flag the gap.

## What this skill does NOT do

- It does not auto-publish. It produces drafts.
- It does not auto-follow anyone. Follow suggestions are manual.
- It does not create images or video (except hero images for posts per the Hero Image Style Guide).
- It does not produce engagement-bait that misrepresents content.
- It does not produce announcements, summaries, or resolved arguments as standalone tweets.
- It does not fabricate reply threads. If repliers were not provided, it asks for data.
- It does not skip the prior-work audit. Ever.
