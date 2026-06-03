# OAuth 2.0 Setup for MCP Remote Access

This guide explains how to set up OAuth 2.0 authentication for web-based access to MCP servers.

## Overview

The MCP proxy supports two authentication methods:
1. **Bearer Token** (MCP Standard) - Simple token-based auth
2. **OAuth 2.0** - For web usage with custom connectors

## OAuth 2.0 Setup

### Step 1: Generate OAuth Client Credentials

You can use any OAuth client ID and secret. For simplicity, you can generate them:

```bash
# Generate client ID and secret
CLIENT_ID=$(python3 -c "import secrets; print(secrets.token_urlsafe(16))")
CLIENT_SECRET=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
echo "Client ID: $CLIENT_ID"
echo "Client Secret: $CLIENT_SECRET"
```

### Step 2: Start Proxy with OAuth

```bash
./execution/scripts/setup_mcp_server_tunnel.sh \
    parquet \
    mcp/parquet/parquet_mcp_server.py \
    8080 \
    "" \
    --oauth-client-id "your-client-id" \
    --oauth-client-secret "your-client-secret" \
    --oauth-redirect-uri "https://your-tunnel-url.trycloudflare.com/oauth/callback"
```

### Step 3: Get Access Token

**Option A: Client Credentials Flow (Recommended for Custom Connectors)**

```bash
curl -X POST https://your-tunnel-url.trycloudflare.com/oauth/token \
    -H "Content-Type: application/json" \
    -d '{
        "grant_type": "client_credentials",
        "client_id": "your-client-id",
        "client_secret": "your-client-secret"
    }'
```

Response:
```json
{
    "access_token": "generated-access-token",
    "token_type": "Bearer",
    "expires_in": 3600,
    "scope": "mcp"
}
```

**Option B: Authorization Code Flow**

1. Visit: `https://your-tunnel-url.trycloudflare.com/oauth/authorize`
2. Redirects to callback with authorization code
3. Exchange code for access token (handled automatically)

### Step 4: Use Access Token

Use the access token as a Bearer token:

```bash
curl -X POST https://your-tunnel-url.trycloudflare.com \
    -H "Authorization: Bearer generated-access-token" \
    -H "Content-Type: application/json" \
    -d '{"jsonrpc":"2.0","method":"initialize","id":1}'
```

## Custom Domain Setup (Optional)

For a professional custom domain like `dev.neotoma.io/mcp` instead of random `trycloudflare.com` URLs:

```bash
./execution/scripts/setup_mcp_custom_domain_tunnel.sh 8080 dev.neotoma.io mcp
```

This creates a persistent Cloudflare tunnel with your custom domain. See `CUSTOM_DOMAIN_SETUP.md` for complete instructions.

**Benefits:**
- Stable URL: `https://dev.neotoma.io/mcp` (doesn't change)
- Professional appearance
- Path-based routing for multiple services

## Custom Connector Configuration

For Claude's custom connector dialog:

**With Custom Domain (`dev.neotoma.io/mcp`):**
- **Connector Name**: `Parquet` (or your server name)
- **URL**: `https://dev.neotoma.io/mcp`
- **OAuth Client ID**: Your generated client ID
- **OAuth Client Secret**: Your generated client secret

**With trycloudflare.com:**
- **Connector Name**: `Parquet` (or your server name)
- **URL**: `https://your-tunnel-url.trycloudflare.com`
- **OAuth Client ID**: Your generated client ID
- **OAuth Client Secret**: Your generated client secret

The custom connector will:
1. Call `/oauth/token` with client credentials
2. Receive an access token
3. Use the access token as `Authorization: Bearer <token>` for all requests

**OAuth Endpoints (with custom domain):**
- Token: `https://dev.neotoma.io/mcp/oauth/token`
- Authorization: `https://dev.neotoma.io/mcp/oauth/authorize`
- Callback: `https://dev.neotoma.io/mcp/oauth/callback`

## Security Notes

- **Client Secret**: Keep this secure - it's like a password
- **Access Tokens**: Valid for 1 hour (3600 seconds) by default
- **HTTPS**: Always use HTTPS in production (via Cloudflare tunnel)
- **Token Storage**: In-memory storage (use Redis/DB in production)

## Example: Full Setup

```bash
# 1. Generate credentials
CLIENT_ID="parquet-client-$(openssl rand -hex 8)"
CLIENT_SECRET="$(openssl rand -hex 32)"

# 2. Start proxy with OAuth
./execution/scripts/setup_mcp_server_tunnel.sh \
    parquet \
    mcp/parquet/parquet_mcp_server.py \
    8080 \
    "" \
    --oauth-client-id "$CLIENT_ID" \
    --oauth-client-secret "$CLIENT_SECRET" \
    --oauth-redirect-uri "https://occurring-closest-republic-cheaper.trycloudflare.com/oauth/callback"

# 3. Start Cloudflare tunnel (in another terminal)
cloudflared tunnel --url http://localhost:8080

# 4. Use in custom connector:
#    URL: https://occurring-closest-republic-cheaper.trycloudflare.com
#    OAuth Client ID: $CLIENT_ID
#    OAuth Client Secret: $CLIENT_SECRET
```

## Troubleshooting

**"Invalid client credentials"**
- Verify client ID and secret match what you passed to the proxy
- Check that you're using the correct grant type (`client_credentials`)

**"Unauthorized" after getting token**
- Verify you're using the access token (not client secret) in the Authorization header
- Check token hasn't expired (default: 1 hour)

**Custom connector not working**
- Verify the OAuth token endpoint is accessible: `https://your-url/oauth/token`
- Check that client credentials flow is supported (grant_type: client_credentials)
