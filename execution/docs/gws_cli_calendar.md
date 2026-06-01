# Google Workspace CLI (`gws`) — Calendar

## Install

`gws` is the [Google Workspace CLI](https://github.com/googleworkspace/cli) (`@googleworkspace/cli`).

```bash
# Option A — npm (matches typical Node installs)
npm install -g @googleworkspace/cli

# Option B — Homebrew (macOS/Linux)
brew install googleworkspace-cli
```

Verify: `gws --version`

## One-time project + OAuth client

- **Automated:** `gws auth setup` (uses `gcloud` to create/select a project, enable APIs, then can run login).  
- **Manual:** Desktop OAuth client JSON → save as `~/.config/gws/client_secret.json`. Add your Google account under **OAuth consent screen → Test users** while the app is in Testing.

This repo’s finance docs already reference project **`ateles2`** and Desktop OAuth for `gws`; Calendar API should be enabled on that project (`gws auth status` lists `calendar-json.googleapis.com` when enabled).

## Authenticate for Calendar

Prefer **narrow scopes** (especially in OAuth **Testing** mode, where Google caps how many scopes you can request at once):

```bash
gws auth login -s calendar
```

Open the printed URL, choose the account, accept **Google Calendar** + profile scopes, and wait until the CLI reports success (localhost callback).

For Sheets + Gmail + Calendar together (still under testing caps if possible):

```bash
gws auth login -s drive,gmail,sheets,calendar
```

Check state:

```bash
gws auth status
```

Expect `"token_valid": true` after a successful login. Also confirm **`scopes`** in that JSON include **`https://www.googleapis.com/auth/calendar`**. If the token is valid but Calendar calls return **403 insufficient authentication scopes**, consent did not grant Calendar — run **`gws auth login -s calendar`** again and ensure the Calendar scope is checked (or use **Select all** on the consent screen while the app is in Testing).

## Verify Calendar

```bash
# Upcoming agenda (helper)
gws calendar +agenda

# Raw API: primary calendar, next few events
gws calendar events list \
  --params '{"calendarId":"primary","maxResults":10,"singleEvents":true,"orderBy":"startTime","timeMin":"2026-05-01T00:00:00Z"}'
```

## Config paths

| Path | Role |
|------|------|
| `~/.config/gws/client_secret.json` | OAuth desktop client JSON |
| `~/.config/gws/credentials.enc` | Encrypted refresh/access material (after login) |
| Keyring / `~/.config/gws/.encryption_key` | Encryption key for credentials |

## Troubleshooting

- **Background `gws auth login --help` aborted:** In some environments `gws auth login --help` starts the OAuth listener instead of printing help, then the process sits until timeout. Ignore that outcome; use `gws auth --help` for auth subcommand help, and run `gws auth login -s calendar` only when you intend to authenticate.
- **`gws auth status` shows Calendar in `scopes` but Calendar API returns 403 scope insufficient:** The **access token** in `~/.config/gws/token_cache.json` can be stale relative to the refresh token. Quit `gws`, move that file aside (backup), then retry the Calendar command so `gws` mints a fresh access token (e.g. `mv ~/.config/gws/token_cache.json ~/.config/gws/token_cache.json.bak` then `gws calendar +agenda`).

## Relation to Cursor Calendar MCP

Calendar **MCP** and **`gws`** can share the same GCP project but are separate token stores. Use MCP inside Cursor for agent tools; use **`gws`** in the terminal and scripts (for example finance sheet pulls). Do not replace MCP with shelling out to `gws` per agent turn unless you intend that architecture; see `docs/private/finances/main_financial_accounts_registry.md`.

## Therapy scope (agents)

When confirming therapy payment scope, agents should read the **latest Calendar event whose title contains `Therapy`** once Calendar access works. See `.cursor/rules/therapy_payment_scope.mdc`.
