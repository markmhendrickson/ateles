# Mimus — Conversation-Corpus Content-Idea Daemon (SPEC)

> Status: **implemented (initial)** — `mimus.py`, `install.sh`, `README.md` landed
> on branch `claude/agent-content-extraction-ec6ngz`. Not yet installed/run.
> Named after *Mimus polyglottos* (the northern mockingbird), which echoes
> back the songs it hears — fitting for a daemon that listens to the
> conversation corpus and re-voices its best material as content ideas.

## Purpose

Sweep the Neotoma conversation corpus on a schedule, extract candidate
**content ideas** inspired by what the operator and the swarm actually
discussed, dedupe them against existing ideas, and surface a ranked digest
to the operator over Telegram for approval. Approved ideas advance through
the content pipeline that already exists; nothing is published automatically.

## Why this daemon (state of the world, 2026-06-15)

The corpus and the downstream pipeline already exist — only the automation
linking them is missing:

| Fact | Value |
|---|---|
| `conversation` entities in Neotoma | ~5,701 |
| `conversation_message` rows | ~22,542 |
| `agent_message` rows | ~3,570 |
| `analysis` entities (pre-distilled session themes) | ~236 |
| `post_idea` entities (schema v1.4.0) | ~60 |
| Downstream | `blog_post_draft`, `post`, `social_share_draft`, `social_post_draft` |

Today `post_idea` entities are created **reactively and manually** — the
operator says, inside a chat, "save all posts in this chat as post ideas,"
and only that one chat is mined (existing ideas link `REFERS_TO` the
triggering `agent_message`). No daemon, skill, or scheduled job sweeps the
backlog or runs periodically. Mimus closes that gap.

## Design

Reuses the Cotinga/Monedula daemon pattern: stdlib-only Python, env loaded
from `~/.config/neotoma/.env`, `lib.notify.Notifier` for Telegram, launchd
schedule, idempotent per-run guard.

### Schedule

Once daily (proposed **06:00 Madrid**, after Cotinga's 05:30 briefing) via
launchd `StartCalendarInterval`. Idempotency guard: `.mimus_last_run` records
the date; same-day re-invocations exit immediately.

### Phase 1 — sweep & extract (fast)

1. **Watermark.** Read `.mimus_watermark` (ISO timestamp of the last swept
   `updated_at`). **First run sweeps the full backlog** (~5,701 conversations)
   in batches (operator decision, 2026-06-19); the watermark is only advanced
   once the backlog is drained, after which runs are incremental.
2. **Retrieve.** `retrieve_entities(entity_type="analysis", updated_since=…)`
   first (already-distilled signal, cheapest), then `conversation` /
   `agent_message` for sessions without an `analysis`. Page with
   `limit`/`offset`; cap per-run volume.
3. **Extract.** Run a Claude pass (`claude --print`) over each batch with an
   extraction prompt adapted from the `analyze-neotoma-feedback` skill
   (content-marketing-ideas + memetic-ideas sections). Emit candidate ideas:
   `{title, summary, pillar, format, platforms, series?, tags, confidence}`.

### Phase 2 — dedupe, store, notify

4. **Dedupe.** For each candidate, `retrieve_entities(entity_type="post_idea",
   search=<title/summary>)` with a strict `similarity_threshold`; drop
   near-duplicates of the existing ~60 and of each other within the run.
5. **Store.** Surviving candidates → `store` as `post_idea` with:
   - `status: "idea"`
   - `source: "conversation_sweep"`  ← new provenance value (free-text
     `source` field; no schema migration needed, additive only)
   - `REFERS_TO` → the source `conversation` (and/or `analysis`) entity
   - `PART_OF` → the Ateles plan `ent_99ace4dd6673aa36ed08b1fe`
6. **Notify.** One ranked Telegram digest (by `confidence`) to the content
   topic: title, one-line summary, suggested format/platform, source link.
   Operator replies to approve (`status: "next"`) or dismiss.
7. **Advance watermark.**

### Operator gate (hard boundary)

- Mimus only ever writes `post_idea` at `status: "idea"`. Promotion to
  `next` / draft is operator-initiated.
- Approved ideas hand off to the existing `write-blog-post` / `social`
  skills. **Auto-publish stays manual** — Mimus never drafts or publishes
  outward-facing content unattended.

## Constraints (standing rules — see project `CLAUDE.md`)

- **RGPD / people-data.** Conversations contain third-party personal data
  under Art. 6(1)(f) legitimate interest. Extraction MUST minimize: ideas
  capture themes and angles, never incidental sensitive disclosures (Art. 9
  categories). No person-specific profiling becomes a public content idea.
- **PII scrubbing.** Strip usernames, worktree names, internal platform
  names, and private identifiers from every stored idea — these feed
  outward-facing content downstream.
- **No secrets** inlined; all config from env.
- **Idempotent** per day; watermark prevents reprocessing.
- **Fail-open / read-mostly.** Any error → log and exit 0; never crash, never
  partial-write an idea without its `REFERS_TO` + `PART_OF` links.

## Files (when implemented)

```
execution/daemons/mimus/
  mimus.py                     # sweep + extract + dedupe + notify
  com.ateles.mimus.plist       # launchd, 06:00 Madrid daily
  install.sh                   # cp plist + launchctl load
  README.md                    # operator-facing docs (Monedula style)
```

## Environment variables

| Variable | Purpose |
|---|---|
| `NEOTOMA_BEARER_TOKEN` | Neotoma API auth |
| `TELEGRAM_BOT_TOKEN` | Bot token |
| `TELEGRAM_CHAT_ID` | Target chat/group ID |
| `TELEGRAM_TOPIC_CONTENT` | Thread ID for the content topic (new) |
| `ANTHROPIC_API_KEY` | Extraction pass via `claude --print` |

## Decisions

- **Backlog vs. forward-only** — *resolved 2026-06-19:* first run sweeps the
  full ~5,701-conversation backlog in batches, then runs incrementally off the
  watermark.

## Open questions for the operator

1. **Cadence & volume.** Daily, or weekly? Cap of N ideas per digest to avoid
   flooding (proposed: top 5 by confidence).
2. **Scope.** All conversations, or only those linked to the plan / tagged
   topics worth writing about publicly?
