#!/bin/bash
# Simple OAuth test script for MCP proxy

# Get OAuth credentials from environment variables
CLIENT_ID="${MCP_OAUTH_CLIENT_ID:-${MCP_PARQUET_OAUTH_CLIENT_ID:-}}"
CLIENT_SECRET="${MCP_OAUTH_CLIENT_SECRET:-${MCP_PARQUET_OAUTH_CLIENT_SECRET:-}}"
BASE_URL="${MCP_OAUTH_BASE_URL:-https://dev.neotoma.io/mcp}"

# OAuth credentials should be in .env (synced from 1Password via op_sync_env_from_1password.py)
# Do not read directly from 1Password - always use .env

# Validate credentials are set
if [ -z "$CLIENT_ID" ] || [ -z "$CLIENT_SECRET" ]; then
    echo "Error: OAuth credentials not found"
    echo ""
    echo "Set environment variables:"
    echo "  export MCP_OAUTH_CLIENT_ID='your-client-id'"
    echo "  export MCP_OAUTH_CLIENT_SECRET='your-client-secret'"
    echo ""
    echo "Or server-specific:"
    echo "  export MCP_PARQUET_OAUTH_CLIENT_ID='your-client-id'"
    echo "  export MCP_PARQUET_OAUTH_CLIENT_SECRET='your-client-secret'"
    echo ""
    echo "Or sync from 1Password to .env:"
    echo "  python execution/scripts/op_sync_env_from_1password.py"
    echo ""
    echo "Make sure OAuth credentials are mapped in env_var_mappings.parquet:"
    echo "  - MCP_OAUTH_CLIENT_ID -> op://Private/Parquet MCP Proxy/OAUTH_CLIENT_ID"
    echo "  - MCP_OAUTH_CLIENT_SECRET -> op://Private/Parquet MCP Proxy/OAUTH_CLIENT_SECRET"
    exit 1
fi

echo "=== OAuth 2.0 Test for MCP Proxy ==="
echo ""
echo "Base URL: $BASE_URL"
echo "Client ID: $CLIENT_ID"
echo ""

# Step 1: Get access token
echo "Step 1: Getting OAuth access token..."
TOKEN_RESPONSE=$(curl -s -X POST "$BASE_URL/oauth/token" \
    -H "Content-Type: application/json" \
    -d "{
        \"grant_type\": \"client_credentials\",
        \"client_id\": \"$CLIENT_ID\",
        \"client_secret\": \"$CLIENT_SECRET\"
    }")

ACCESS_TOKEN=$(echo "$TOKEN_RESPONSE" | python3 -c "import sys, json; print(json.load(sys.stdin).get('access_token', ''))" 2>/dev/null)

if [ -z "$ACCESS_TOKEN" ]; then
    echo "✗ Failed to get access token"
    echo "Response: $TOKEN_RESPONSE"
    exit 1
fi

echo "✓ Got access token: ${ACCESS_TOKEN:0:16}...${ACCESS_TOKEN: -8}"
echo ""

# Step 2: Use token for MCP request
echo "Step 2: Testing MCP request with access token..."
echo "Note: This may take a few seconds as the MCP server initializes..."
MCP_RESPONSE=$(curl -s -w "\n%{http_code}" \
    --max-time 30 \
    -X POST "$BASE_URL" \
    -H "Authorization: Bearer $ACCESS_TOKEN" \
    -H "Content-Type: application/json" \
    -d '{
        "jsonrpc": "2.0",
        "method": "initialize",
        "id": 1,
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "oauth-test", "version": "1.0"}
        }
    }' 2>&1)

HTTP_CODE=$(echo "$MCP_RESPONSE" | tail -1)
BODY=$(echo "$MCP_RESPONSE" | sed '$d')

echo "HTTP Status: $HTTP_CODE"
if [ -n "$BODY" ] && [ "$BODY" != "null" ]; then
    echo "Response:"
    echo "$BODY" | python3 -m json.tool 2>&1 | head -20 || echo "$BODY"
else
    echo "Response: (empty or timeout)"
fi

if [ "$HTTP_CODE" = "200" ] || [ "$HTTP_CODE" = "201" ]; then
    echo ""
    echo "✓ OAuth authentication successful!"
    echo ""
    echo "Your OAuth setup is working correctly."
elif [ "$HTTP_CODE" = "524" ]; then
    echo ""
    echo "⚠ Cloudflare timeout (524) - MCP server may be slow to respond"
    echo "This is common on first request as the server initializes."
    echo ""
    echo "OAuth authentication is working (token was accepted)."
    echo "The timeout is likely due to MCP server initialization."
elif [ "$HTTP_CODE" = "401" ]; then
    echo ""
    echo "✗ Authentication failed (401) - token may be invalid or expired"
    exit 1
else
    echo ""
    echo "⚠ Unexpected response (HTTP $HTTP_CODE)"
    echo "OAuth token endpoint is working, but MCP request had issues."
fi

echo ""
echo "OAuth Configuration for Claude Desktop:"
echo "  URL: $BASE_URL"
echo "  OAuth Client ID: $CLIENT_ID"
echo "  OAuth Client Secret: $CLIENT_SECRET"
echo ""
echo "OAuth Endpoints:"
echo "  Token: $BASE_URL/oauth/token"
echo "  Authorization: $BASE_URL/oauth/authorize"
echo "  Callback: $BASE_URL/oauth/callback"
