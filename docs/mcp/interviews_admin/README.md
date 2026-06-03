# Interviews admin MCP server

MCP wrapper around the interviews admin API.

## Auth

Uses Bearer auth with the admin passphrase:

- `INTERVIEWS_ADMIN_PASSPHRASE` (preferred), or
- `ADMIN_PASSPHRASE` (fallback)

`run_interviews_admin_mcp.sh` loads **ateles repo root** `.env` before starting the server, so defining either variable there is enough for local MCP.

Optional base URL override:

- `INTERVIEWS_ADMIN_BASE_URL` (default: `https://interviews.markmhendrickson.com`)

## Environment enforcement (dev/prod)

This MCP can enforce app/Neotoma environment alignment on every request:

- `INTERVIEWS_ADMIN_ENFORCE_NEOTOMA_ENV` (default: `1`; set `0` to disable)
- `INTERVIEWS_ADMIN_ENV` (`dev` or `prod`; default: `dev`, fallback inference from base URL for invalid values)
- `INTERVIEWS_ADMIN_NEOTOMA_ENV` (`dev` or `prod`; default: `dev`)
  - fallback aliases: `NEOTOMA_ENV`, `NEOTOMA_TARGET_ENV`

When enforcement is enabled, calls fail if environments do not match:

- app `dev` requires Neotoma `dev`
- app `prod` requires Neotoma `prod`

## Tools

- `interviews_admin_get_overview`
- `interviews_admin_list_results`
- `interviews_admin_get_result`
- `interviews_admin_delete_result`
- `interviews_admin_list_contacts`
- `interviews_admin_upsert_contact`
- `interviews_admin_delete_contact`
- `interviews_admin_list_events`
- `interviews_admin_send_invite`
- `interviews_admin_get_text_invite`
- `interviews_admin_confirm_text_invite`

## Local run

```bash
cd mcp/interviews_admin
pip install -r requirements.txt
./run_interviews_admin_mcp.sh
```
