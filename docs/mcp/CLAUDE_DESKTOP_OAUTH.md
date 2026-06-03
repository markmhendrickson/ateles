# Claude Desktop OAuth Configuration

## Issue

Claude Desktop constructs OAuth URLs at `/authorize` (without `/oauth/` prefix) even when the base URL is `https://dev.neotoma.io/mcp`.

**Example:** Claude Desktop requests:
```
https://dev.neotoma.io/authorize?response_type=code&client_id=...
```

Instead of:
```
https://dev.neotoma.io/oauth/authorize?response_type=code&client_id=...
```

## Solution

Both `/authorize` and `/oauth/authorize` endpoints are now supported for Claude Desktop compatibility.

### Tunnel Configuration

The Cloudflare tunnel (`~/.cloudflared/config.yml`) routes `/authorize` to the proxy:

```yaml
ingress:
  # Claude Desktop uses /authorize (without /oauth/)
  - hostname: dev.neotoma.io
    path: /authorize
    service: http://localhost:8080
  # Standard OAuth endpoints
  - hostname: dev.neotoma.io
    path: /oauth/token
    service: http://localhost:8080
  - hostname: dev.neotoma.io
    path: /oauth/authorize
    service: http://localhost:8080
  - hostname: dev.neotoma.io
    path: /oauth/callback
    service: http://localhost:8080
```

### Proxy Configuration

The proxy (`mcp_authenticated_proxy.py`) registers both routes:

```python
# Register /authorize (Claude Desktop compatibility) and /oauth/authorize
app.router.add_route("GET", "/authorize", oauth_authorize_handler)
app.router.add_route("GET", "/oauth/authorize", oauth_authorize_handler)
app.router.add_route("GET", "/mcp/oauth/authorize", oauth_authorize_handler)
```

## Supported OAuth Endpoints

All of these paths work identically:

- `https://dev.neotoma.io/authorize` (Claude Desktop compatibility)
- `https://dev.neotoma.io/oauth/authorize` (Standard OAuth)
- `https://dev.neotoma.io/mcp/oauth/authorize` (With path prefix)

## Claude Desktop Configuration

**Custom Connector Settings:**
- **Connector Name:** `Parquet MCP`
- **URL:** `https://dev.neotoma.io/mcp`
- **OAuth Client ID:** `parquet-<client-id>` (from `.env` or 1Password)
- **OAuth Client Secret:** (from `.env` or 1Password)

**Note:** Claude Desktop will automatically construct the authorization URL based on the base URL. It may use `/authorize` or `/oauth/authorize` - both are supported.

## Testing

Test both endpoints:

```bash
# Claude Desktop style (without /oauth/)
curl "https://dev.neotoma.io/authorize?client_id=parquet-<client-id>&redirect_uri=https://dev.neotoma.io/mcp/oauth/callback"

# Standard OAuth style
curl "https://dev.neotoma.io/oauth/authorize?client_id=parquet-<client-id>&redirect_uri=https://dev.neotoma.io/mcp/oauth/callback"
```

Both should return a 302 redirect to the callback URL with an authorization code.

## Restart After Changes

After updating tunnel config or proxy code:

```bash
# Restart tunnel
launchctl unload ~/Library/LaunchAgents/com.cloudflare.mcp-servers-tunnel.plist
launchctl load ~/Library/LaunchAgents/com.cloudflare.mcp-servers-tunnel.plist

# Restart proxy
pkill -f "mcp_authenticated_proxy.py"
./execution/scripts/setup_mcp_server_tunnel.sh parquet mcp/parquet/parquet_mcp_server.py 8080
```
