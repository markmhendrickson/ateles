# Mimus

Conversation-corpus content-idea daemon, named after *Mimus polyglottos* (the
northern mockingbird) — it echoes back the swarm's best material as publishable
content ideas. See [`SPEC.md`](./SPEC.md) for the full design and rationale.

Runs once daily at **06:00 Madrid** (after Cotinga's 05:30 briefing) via launchd.
Each run sweeps a slice of the Neotoma conversation corpus, extracts candidate
content ideas, dedupes them against existing `post_idea` entities, stores the
survivors at `status="idea"`, and sends a ranked Telegram digest for operator
approval. **It never drafts or publishes** — promotion past `idea` is manual.

## How it works

The daemon is a thin scheduler (mirroring Cotinga): Python handles the lock,
once-per-day guard, and an offset-based backlog cursor, then spawns a blocking
`claude --print` agent per batch. That agent does all retrieval, extraction,
dedup, storage, and notification through the Neotoma MCP server, and prints a
`MIMUS_RESULT {...}` sentinel that the scheduler parses to advance the cursor —
only on a clean run, so a failed batch is simply retried next time (fail-open).

The cursor sweeps `conversation` entities oldest-first. The first runs drain the
full backlog (~5,701 conversations); once drained the daemon tails newly-updated
conversations.

## Setup

```bash
cd execution/daemons/mimus
chmod +x install.sh
./install.sh
```

## Environment variables

Loaded automatically from `~/.config/neotoma/.env` at startup.

| Variable | Purpose |
|----------|---------|
| `NEOTOMA_BEARER_TOKEN` | Neotoma API auth (used by the spawned agent's MCP server) |
| `TELEGRAM_BOT_TOKEN` | Bot token |
| `TELEGRAM_CHAT_ID` | Target chat/group ID |
| `TELEGRAM_TOPIC_CONTENT` | Thread ID for the content topic |

### Tunables (optional, env-overridable)

| Variable | Default | Purpose |
|----------|---------|---------|
| `MIMUS_BATCH_SIZE` | `100` | Conversations retrieved per batch |
| `MIMUS_MAX_BATCHES_PER_RUN` | `5` | Batches per daily run (~500 convos/day → backlog drains in ~12 days) |
| `MIMUS_DIGEST_LIMIT` | `5` | Max ideas surfaced per Telegram digest |
| `MIMUS_AGENT_TIMEOUT_SEC` | `1800` | Per-batch agent timeout |

## Logs

`~/Library/Logs/ateles/mimus.log`

## State files (gitignored, in the daemon directory)

| File | Purpose |
|------|---------|
| `.mimus_last_run` | Date of last run — enforces once-per-day idempotency |
| `.mimus_cursor` | Backlog cursor: `{offset, drained, updated_at}` |
| `.mimus_lock` | PID lock preventing overlapping instances |

## Constraints

Standing rules enforced on every extraction (see project `CLAUDE.md`):

- **RGPD Art. 6(1)(f) minimization.** Ideas capture themes and angles only;
  incidental sensitive disclosures (Art. 9 categories — health, finances,
  family, political/religious views) are never persisted into an idea.
- **PII scrubbing.** Usernames, worktree names, internal platform names, and
  private identifiers are stripped before any idea is stored — these feed
  outward-facing content downstream.
- **Operator gate.** Mimus only ever writes `post_idea` at `status="idea"`.
  Drafting and publishing stay manual.
- **Fail-open.** Any error logs and exits without advancing the cursor; a failed
  batch is retried on the next run.
