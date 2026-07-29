# Slack integration

Gives the swarm Slack access — search, channel history, and **operator-gated
posting** — the way `gws` gives it Google Workspace. Implemented by
`execution/scripts/slack_cli.py`.

Before this, the only Slack touchpoint was the **outbound** watchdog webhook
(`OPENCLAW_WATCHDOG_WEBHOOK_URL`). Nothing could read, so material shared in
Slack (contact exports, decks, screenshots) was invisible to agents and had to
be relayed by hand. Writing back (e.g. replying in a thread) had to be done by
the operator pasting by hand.

## Posture

- **Reads unrestricted; writes operator-gated.** The `post` subcommand can send
  a message or thread reply, but posting to a shared team workspace is an
  outward-facing, non-reversible action. `post` is therefore a **dry-run unless
  `--yes` is passed**: an agent's default invocation prints exactly what would
  be sent and exits non-zero, so the operator inspects and approves before it
  fires. This mirrors how the swarm gates other high-blast outbound actions
  (Vanellus merges, Monedula payments). Automated alert posts still use the
  webhook.
- **Narrowest scope that works.** Prefer `search:read.public`. The legacy
  `search:read` scope also returns **DM content**, and this token reads a
  **shared team workspace** — it can see other people's messages, not just the
  operator's. Grant `search:read.private` only if a specific need justifies it,
  and say so in the `vendor_binding`.
- **User token, not bot token.** `search.messages` is only available to user
  tokens. That means the token acts as the operator: everything it can read is
  what the operator can already read — and everything `post` sends is authored
  **as the operator** (there is no separate bot identity). Because a post is
  indistinguishable from the operator typing it, the `--yes` gate is the only
  thing standing between an agent and a message under the operator's name; keep
  it.

## One-time setup (operator)

These steps need a human — app creation and scope approval are credential
operations, and the Bottega8 workspace may require admin consent.

1. Create an internal app at <https://api.slack.com/apps> → **From scratch**,
   scoped to the target workspace.
2. Under **OAuth & Permissions → User Token Scopes**, add:
   - `search:read.public` — search public channels
   - `channels:history`, `channels:read` — read/list public channels
   - `chat:write` — **required for the `post` subcommand.** Posts are authored
     as the operator's user; the operator must be a member of any channel it
     posts to (public channels the user is in; invite for private).
   - (only if justified) `groups:history`, `groups:read`,
     `search:read.private` — private channels

   Adding `chat:write` is a scope escalation — it needs a re-install and, in a
   restricted workspace, admin re-approval. If you want reads live before the
   write path is approved, install with the read scopes first and add
   `chat:write` in a later re-install.
3. **Install to Workspace** and approve. If the workspace restricts app
   installs, this needs a workspace admin.
4. Copy the **User OAuth Token** (`xoxp-…`).
5. Store it in 1Password, then publish to the encrypted snapshot:
   - add an `env_var_mapping` for `SLACK_USER_TOKEN` → its `op://` reference
   - `python3 execution/scripts/secrets_publish.py`
   - materialize where needed: `python3 execution/scripts/secrets_materialize.py`
6. Verify: `python3 execution/scripts/slack_cli.py whoami`

## Usage

```bash
# who is this token?
python3 execution/scripts/slack_cli.py whoami

# search the workspace
python3 execution/scripts/slack_cli.py search "connections export" --count 20

# list channels (which ones the token can see)
python3 execution/scripts/slack_cli.py channels

# read a channel
python3 execution/scripts/slack_cli.py history C0123ABC --limit 100

# machine-readable
python3 execution/scripts/slack_cli.py search "leads deck" --json
```

### Posting (operator-gated)

```bash
# DRY RUN (default): prints exactly what would be sent, exits non-zero, sends nothing.
python3 execution/scripts/slack_cli.py post C0123ABC --text "Handling this now."

# Reply within a thread — pass the parent message's ts (from search/history output):
python3 execution/scripts/slack_cli.py post C0123ABC \
  --text "Done — 946 new contacts loaded." --thread-ts 1785000000.123456

# Long / multi-line body: read from stdin so shell quoting doesn't fight you.
cat reply.md | python3 execution/scripts/slack_cli.py post C0123ABC --text - --thread-ts 1785000000.123456

# ACTUALLY SEND: add --yes. This is the operator's approval step.
python3 execution/scripts/slack_cli.py post C0123ABC --text - --yes
```

The intended agent flow: draft the message, run `post` **without** `--yes` to
show the operator the exact payload, and only re-run with `--yes` after the
operator approves. Agents should treat a `post` as a checkpoint, not a
free-fire action — the CLI enforces this, but the skill should too.

`history` prints any attached files (name, type, permalink), which is the
usual way decks and exports surface.

## Notes and limits

- **Search returns only what the token's user can see** — channels they're a
  member of, plus public channels. A deck in a channel the operator never
  joined will not appear.
- **Slack platform changes (2026).** Slack now steers apps toward the
  Real-time Search API (`assistant.search.context`) with granular
  `search:read.public` / `search:read.private` scopes rather than legacy
  `search:read`. Apps distributed outside the Marketplace are also subject to
  posted limits from 2026-03-03. If `search.messages` is restricted for this
  app, switch `cmd_search` to `assistant.search.context` — the CLI surface
  stays the same.
- **Rate limits** apply per method; the CLI does no retry/backoff yet. Add it
  if a daemon starts polling rather than running interactively.

## Swarm wiring

Per the context-entity rule in `CLAUDE.md`, agents should not hardcode
workspace or channel specifics:

- Bind the tool as a `vendor_binding` capability slot (e.g. `chat_search`), so
  an agent asks for the capability rather than for "Slack".
- Resolve channel IDs from `channel_config`, not literals in prompts.
- Give a missing-token fallback: surface a blocker, never invent results.

## RGPD note

A shared-workspace read token brings third-party personal data into scope
(colleagues' messages). The people-data discipline in `CLAUDE.md` applies:
minimize at capture, purpose-bind to the actual relationship work, and do not
persist incidental sensitive disclosures into durable records just because
they were searchable.
