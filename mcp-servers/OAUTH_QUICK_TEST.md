# Quick OAuth Test Guide

## OAuth Credentials Configuration

OAuth credentials should be stored in environment variables or 1Password, not hardcoded.

### Environment Variables

**Generic (shared across all MCP servers):**
```bash
export MCP_OAUTH_CLIENT_ID='your-client-id'
export MCP_OAUTH_CLIENT_SECRET='your-client-secret'
export MCP_OAUTH_REDIRECT_URI='https://dev.neotoma.io/mcp/oauth/callback'
```

**Server-specific (for parquet server):**
```bash
export MCP_PARQUET_OAUTH_CLIENT_ID='your-client-id'
export MCP_PARQUET_OAUTH_CLIENT_SECRET='your-client-secret'
export MCP_PARQUET_OAUTH_REDIRECT_URI='https://dev.neotoma.io/mcp/oauth/callback'
```

### 1Password Storage

Store in 1Password item:
- **Item name:** `Parquet MCP Proxy` or `MCP Proxy`
- **Fields:**
  - `OAUTH_CLIENT_ID`
  - `OAUTH_CLIENT_SECRET`
  - `OAUTH_REDIRECT_URI` (optional, can be set via env var)

### Base URL

**Default:** `https://dev.neotoma.io/mcp`  
**Override:** Set `MCP_OAUTH_BASE_URL` environment variable

## Quick Test

```bash
./execution/scripts/test_oauth_simple.sh
```

## Manual Test Steps

### 1. Get Access Token

```bash
# Get credentials from environment
CLIENT_ID="${MCP_OAUTH_CLIENT_ID:-${MCP_PARQUET_OAUTH_CLIENT_ID:-}}"
CLIENT_SECRET="${MCP_OAUTH_CLIENT_SECRET:-${MCP_PARQUET_OAUTH_CLIENT_SECRET:-}}"
BASE_URL="${MCP_OAUTH_BASE_URL:-https://dev.neotoma.io/mcp}"

curl -X POST "$BASE_URL/oauth/token" \
    -H "Content-Type: application/json" \
    -d "{
        \"grant_type\": \"client_credentials\",
        \"client_id\": \"$CLIENT_ID\",
        \"client_secret\": \"$CLIENT_SECRET\"
    }"
```

**Expected Response:**
```json
{
    "access_token": "generated-token-here",
    "token_type": "Bearer",
    "expires_in": 3600,
    "scope": "mcp"
}
```

### 2. Use Access Token

```bash
# Extract token from response
ACCESS_TOKEN="your-access-token-here"

# Make MCP request
curl -X POST https://dev.neotoma.io/mcp \
    -H "Authorization: Bearer $ACCESS_TOKEN" \
    -H "Content-Type: application/json" \
    -d '{
        "jsonrpc": "2.0",
        "method": "initialize",
        "id": 1,
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1.0"}
        }
    }'
```

## Claude Desktop Custom Connector

**Settings:**
- **Connector Name:** `Parquet MCP`
- **URL:** `https://dev.neotoma.io/mcp` (or set via `MCP_OAUTH_BASE_URL`)
- **OAuth Client ID:** Get from `MCP_OAUTH_CLIENT_ID` or `MCP_PARQUET_OAUTH_CLIENT_ID`
- **OAuth Client Secret:** Get from `MCP_OAUTH_CLIENT_SECRET` or `MCP_PARQUET_OAUTH_CLIENT_SECRET`

Claude Desktop will automatically:
1. Call `/oauth/token` with client credentials
2. Get access token
3. Use token for all MCP requests

## Troubleshooting

**HTTP 524 (Cloudflare Timeout):**
- MCP server may be slow to initialize on first request
- OAuth is working (token was accepted)
- Try the request again after a few seconds

**HTTP 401 (Unauthorized):**
- Token expired (tokens last 1 hour)
- Get a new token from `/oauth/token` endpoint

**OAuth endpoint returns 401:**
- Verify client ID and secret match what the proxy was started with
- Check proxy is running with OAuth enabled
