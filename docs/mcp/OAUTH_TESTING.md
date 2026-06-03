# OAuth Testing Guide

How to test OAuth 2.0 authentication for MCP proxy.

## Quick Test

```bash
./execution/scripts/test_oauth_mcp_proxy.sh
```

This script will:
1. Check if proxy is running
2. Prompt for OAuth credentials (or use test credentials)
3. Test OAuth token endpoint
4. Test access token authentication
5. Test invalid credentials rejection

## Manual Testing

### Step 1: Start Proxy with OAuth

```bash
# Generate credentials
CLIENT_ID="parquet-$(openssl rand -hex 8)"
CLIENT_SECRET="$(openssl rand -hex 32)"

# Start proxy
./execution/scripts/setup_mcp_server_tunnel.sh \
    parquet \
    mcp/parquet/parquet_mcp_server.py \
    8080 \
    "" \
    --oauth-client-id "$CLIENT_ID" \
    --oauth-client-secret "$CLIENT_SECRET" \
    --oauth-redirect-uri "https://dev.neotoma.io/mcp/oauth/callback"
```

### Step 2: Get Access Token

**With custom domain (`dev.neotoma.io`):**
```bash
curl -X POST https://dev.neotoma.io/mcp/oauth/token \
    -H "Content-Type: application/json" \
    -d "{
        \"grant_type\": \"client_credentials\",
        \"client_id\": \"$CLIENT_ID\",
        \"client_secret\": \"$CLIENT_SECRET\"
    }"
```

**Response:**
```json
{
    "access_token": "generated-token-here",
    "token_type": "Bearer",
    "expires_in": 3600,
    "scope": "mcp"
}
```

### Step 3: Use Access Token

```bash
# Extract token from response
ACCESS_TOKEN=$(curl -s -X POST https://dev.neotoma.io/mcp/oauth/token \
    -H "Content-Type: application/json" \
    -d "{
        \"grant_type\": \"client_credentials\",
        \"client_id\": \"$CLIENT_ID\",
        \"client_secret\": \"$CLIENT_SECRET\"
    }" | python3 -c "import sys, json; print(json.load(sys.stdin)['access_token'])")

# Use token for MCP request
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
            "clientInfo": {
                "name": "test-client",
                "version": "1.0"
            }
        }
    }'
```

### Step 4: Test Invalid Credentials

```bash
# Should return 401
curl -X POST https://dev.neotoma.io/mcp/oauth/token \
    -H "Content-Type: application/json" \
    -d '{
        "grant_type": "client_credentials",
        "client_id": "invalid",
        "client_secret": "invalid"
    }'
```

Expected: `{"error": "invalid_client", ...}` with HTTP 401

## Testing with Claude Desktop Custom Connector

1. **Get OAuth credentials** (from proxy startup or generate new ones)

2. **In Claude Desktop:**
   - Go to Settings → Custom Connectors
   - Click "Add Connector"
   - Enter:
     - **Name**: `Parquet MCP`
     - **URL**: `https://dev.neotoma.io/mcp`
     - **OAuth Client ID**: Your client ID
     - **OAuth Client Secret**: Your client secret

3. **Claude will automatically:**
   - Call `/oauth/token` with client credentials
   - Get access token
   - Use token for all MCP requests

## OAuth Endpoints

With custom domain `dev.neotoma.io/mcp`:

- **Token Endpoint**: `https://dev.neotoma.io/mcp/oauth/token`
  - Method: POST
  - Body: `{"grant_type": "client_credentials", "client_id": "...", "client_secret": "..."}`
  - Returns: `{"access_token": "...", "token_type": "Bearer", "expires_in": 3600}`

- **Authorization Endpoint**: `https://dev.neotoma.io/mcp/oauth/authorize`
  - Method: GET
  - For authorization code flow (not typically used with custom connectors)

- **Callback Endpoint**: `https://dev.neotoma.io/mcp/oauth/callback`
  - Method: GET
  - For authorization code flow

## Troubleshooting

### "Invalid client credentials"

- Verify client ID and secret match what you passed to the proxy
- Check proxy logs for OAuth configuration
- Ensure you're using `grant_type: "client_credentials"`

### "Unauthorized" after getting token

- Verify you're using the `access_token` (not client secret) in Authorization header
- Check token hasn't expired (default: 1 hour)
- Ensure proxy is running with OAuth enabled

### Token endpoint returns 404

- Verify proxy is running with `--oauth-client-id` flag
- Check that `/oauth/token` endpoint is accessible
- Ensure you're using the correct base URL (with `/mcp` path if using custom domain)

### Custom connector not working

- Verify OAuth token endpoint is accessible: `curl https://dev.neotoma.io/mcp/oauth/token`
- Check that client credentials flow is supported (`grant_type: client_credentials`)
- Verify client ID and secret are correct in Claude Desktop settings

## Example: Complete OAuth Test

```bash
#!/bin/bash
# Complete OAuth test

CLIENT_ID="parquet-$(openssl rand -hex 8)"
CLIENT_SECRET="$(openssl rand -hex 32)"
BASE_URL="https://dev.neotoma.io/mcp"

echo "Client ID: $CLIENT_ID"
echo "Client Secret: ${CLIENT_SECRET:0:8}...${CLIENT_SECRET: -4}"
echo ""

# 1. Get token
echo "Getting access token..."
TOKEN_RESPONSE=$(curl -s -X POST "$BASE_URL/oauth/token" \
    -H "Content-Type: application/json" \
    -d "{
        \"grant_type\": \"client_credentials\",
        \"client_id\": \"$CLIENT_ID\",
        \"client_secret\": \"$CLIENT_SECRET\"
    }")

ACCESS_TOKEN=$(echo "$TOKEN_RESPONSE" | python3 -c "import sys, json; print(json.load(sys.stdin).get('access_token', ''))")

if [ -z "$ACCESS_TOKEN" ]; then
    echo "Failed to get token"
    echo "Response: $TOKEN_RESPONSE"
    exit 1
fi

echo "✓ Got access token: ${ACCESS_TOKEN:0:16}..."
echo ""

# 2. Use token
echo "Testing MCP request with token..."
MCP_RESPONSE=$(curl -s -X POST "$BASE_URL" \
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
    }')

echo "Response: $MCP_RESPONSE"
```
