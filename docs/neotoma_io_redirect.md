# Temporary redirect: neotoma.io → Neotoma repo

**Goal:** Have neotoma.io (and www.neotoma.io) temporarily redirect to the Neotoma GitHub repo: https://github.com/markmhendrickson/neotoma

**Current DNS:** neotoma.io nameservers point to **Cloudflare**, so the redirect is configured via the Cloudflare API (script below).

## Cloudflare redirect (API)

Configure or remove the redirect via the script (uses Cloudflare API; token from env or 1Password):

```bash
# From repo root
python execution/scripts/configure_cloudflare_neotoma_redirect.py          # add or update redirect
python execution/scripts/configure_cloudflare_neotoma_redirect.py --remove # remove redirect
```

**Requirements:**
- `neotoma.io` zone must exist in Cloudflare.
- `CLOUDFLARE_API_TOKEN` set, or 1Password item "Cloudflare" with an API token. Token needs **Zone > Single Redirect > Edit** (or equivalent ruleset permission).

The script sets a 302 redirect for `neotoma.io` and `www.neotoma.io` to the GitHub repo. To revert, run with `--remove`.

## Option B: DNSimple URL record (if you move NS back to DNSimple)

If you point neotoma.io’s nameservers back to DNSimple:

1. In DNSimple: **DNS** → **Records** for neotoma.io.
2. Add a **URL** record:
   - **Name:** (empty for apex)
   - **URL:** `https://github.com/markmhendrickson/neotoma`
   - **TTL:** 3600 (or default)

Or via MCP (after restarting the DNSimple MCP so it picks up the updated schema):

- `configure_dns_record`: `domain_name=neotoma.io`, `name=""`, `type=URL`, `content=https://github.com/markmhendrickson/neotoma`.

**Code change:** The DNSimple MCP server supports the **URL** record type (see `mcp/dnsimple/dnsimple_mcp_server.py` and `mcp/dnsimple/README.md`). Use it once the zone is served by DNSimple.

## Reverting the redirect

- **Cloudflare:** Run `python execution/scripts/configure_cloudflare_neotoma_redirect.py --remove`.
- **DNSimple:** Delete the URL record for the apex (and www if added).
