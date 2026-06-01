#!/bin/bash
# Test OAuth 2.0 authentication for MCP proxy

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
TEST_PORT=8080
TEST_SERVER_NAME="parquet"
TEST_SERVER_SCRIPT="$PROJECT_ROOT/mcp/parquet/parquet_mcp_server.py"

# Colors
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# Check if proxy is running
if ! lsof -i :$TEST_PORT > /dev/null 2>&1; then
    echo -e "${RED}Error: No proxy running on port $TEST_PORT${NC}"
    echo "Start the proxy first with OAuth:"
    echo ""
    echo "./execution/scripts/setup_mcp_server_tunnel.sh \\"
    echo "    $TEST_SERVER_NAME \\"
    echo "    $TEST_SERVER_SCRIPT \\"
    echo "    $TEST_PORT \\"
    echo "    \"\" \\"
    echo "    --oauth-client-id \"YOUR_CLIENT_ID\" \\"
    echo "    --oauth-client-secret \"YOUR_CLIENT_SECRET\" \\"
    echo "    --oauth-redirect-uri \"https://dev.neotoma.io/mcp/oauth/callback\""
    exit 1
fi

echo -e "${BLUE}Testing OAuth 2.0 Authentication for MCP Proxy${NC}"
echo ""

# Get OAuth credentials from user or use test credentials
if [ -z "$OAUTH_CLIENT_ID" ] || [ -z "$OAUTH_CLIENT_SECRET" ]; then
    echo "OAuth credentials not set in environment."
    echo ""
    read -p "Enter OAuth Client ID (or press Enter to generate test credentials): " CLIENT_ID
    if [ -z "$CLIENT_ID" ]; then
        CLIENT_ID="test-client-$(openssl rand -hex 8)"
        CLIENT_SECRET="test-secret-$(openssl rand -hex 32)"
        echo -e "${YELLOW}Using test credentials:${NC}"
        echo "  Client ID: $CLIENT_ID"
        echo "  Client Secret: ${CLIENT_SECRET:0:8}...${CLIENT_SECRET: -4}"
        echo ""
        echo -e "${YELLOW}Note: These won't work with an existing proxy.${NC}"
        echo "Either:"
        echo "  1. Restart proxy with these credentials, or"
        echo "  2. Enter the credentials used when starting the proxy"
        echo ""
        read -p "Continue with test? (y/N): " -n 1 -r
        echo
        if [[ ! $REPLY =~ ^[Yy]$ ]]; then
            exit 0
        fi
    else
        read -p "Enter OAuth Client Secret: " CLIENT_SECRET
    fi
else
    CLIENT_ID="$OAUTH_CLIENT_ID"
    CLIENT_SECRET="$OAUTH_CLIENT_SECRET"
    echo "Using credentials from environment variables"
fi

# Determine base URL (local or remote)
BASE_URL=${BASE_URL:-"https://dev.neotoma.io/mcp"}
echo -e "${BLUE}Testing against: $BASE_URL${NC}"
echo ""

# Test 1: Get OAuth access token
echo -e "${BLUE}Test 1: Getting OAuth access token...${NC}"
TOKEN_RESPONSE=$(curl -s -X POST "$BASE_URL/oauth/token" \
    -H "Content-Type: application/json" \
    -d "{
        \"grant_type\": \"client_credentials\",
        \"client_id\": \"$CLIENT_ID\",
        \"client_secret\": \"$CLIENT_SECRET\"
    }")

HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$BASE_URL/oauth/token" \
    -H "Content-Type: application/json" \
    -d "{
        \"grant_type\": \"client_credentials\",
        \"client_id\": \"$CLIENT_ID\",
        \"client_secret\": \"$CLIENT_SECRET\"
    }")

if [ "$HTTP_CODE" = "200" ]; then
    ACCESS_TOKEN=$(echo "$TOKEN_RESPONSE" | python3 -c "import sys, json; print(json.load(sys.stdin).get('access_token', ''))" 2>/dev/null)
    if [ -n "$ACCESS_TOKEN" ]; then
        echo -e "${GREEN}✓ Successfully obtained access token${NC}"
        echo "  Token: ${ACCESS_TOKEN:0:16}...${ACCESS_TOKEN: -8}"
        echo ""
    else
        echo -e "${RED}✗ Failed to extract access token from response${NC}"
        echo "Response: $TOKEN_RESPONSE"
        exit 1
    fi
else
    echo -e "${RED}✗ Failed to get access token (HTTP $HTTP_CODE)${NC}"
    echo "Response: $TOKEN_RESPONSE"
    exit 1
fi

# Test 2: Use access token to authenticate MCP request
if [ -n "$ACCESS_TOKEN" ]; then
    echo -e "${BLUE}Test 2: Using access token for MCP request...${NC}"
    MCP_RESPONSE=$(curl -s -w "\n%{http_code}" \
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
                "clientInfo": {
                    "name": "oauth-test",
                    "version": "1.0"
                }
            }
        }')
    
    HTTP_CODE=$(echo "$MCP_RESPONSE" | tail -1)
    BODY=$(echo "$MCP_RESPONSE" | sed '$d')
    
    if [ "$HTTP_CODE" = "200" ] || [ "$HTTP_CODE" = "201" ]; then
        echo -e "${GREEN}✓ Successfully authenticated with OAuth token${NC}"
        echo "  HTTP Status: $HTTP_CODE"
        echo "  Response preview: $(echo "$BODY" | head -c 100)..."
        echo ""
    elif [ "$HTTP_CODE" = "401" ]; then
        echo -e "${RED}✗ Authentication failed (401 Unauthorized)${NC}"
        echo "  Response: $BODY"
        echo ""
        echo "Possible issues:"
        echo "  - Token expired or invalid"
        echo "  - Proxy not configured with matching OAuth credentials"
        exit 1
    else
        echo -e "${YELLOW}⚠ Unexpected response (HTTP $HTTP_CODE)${NC}"
        echo "  Response: $BODY"
    fi
fi

# Test 3: Test invalid credentials
echo -e "${BLUE}Test 3: Testing invalid credentials (should fail)...${NC}"
INVALID_RESPONSE=$(curl -s -w "\n%{http_code}" -X POST "$BASE_URL/oauth/token" \
    -H "Content-Type: application/json" \
    -d '{
        "grant_type": "client_credentials",
        "client_id": "invalid",
        "client_secret": "invalid"
    }')

INVALID_HTTP_CODE=$(echo "$INVALID_RESPONSE" | tail -1)

if [ "$INVALID_HTTP_CODE" = "401" ]; then
    echo -e "${GREEN}✓ Invalid credentials correctly rejected (401)${NC}"
else
    echo -e "${YELLOW}⚠ Expected 401 for invalid credentials, got $INVALID_HTTP_CODE${NC}"
fi

echo ""
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}OAuth Test Summary${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""
echo -e "${GREEN}✓ OAuth token endpoint working${NC}"
echo -e "${GREEN}✓ Access token obtained${NC}"
if [ "$HTTP_CODE" = "200" ] || [ "$HTTP_CODE" = "201" ]; then
    echo -e "${GREEN}✓ OAuth authentication successful${NC}"
    echo ""
    echo "Your OAuth setup is working correctly!"
    echo ""
    echo "To use in Claude Desktop custom connector:"
    echo "  URL: $BASE_URL"
    echo "  OAuth Client ID: $CLIENT_ID"
    echo "  OAuth Client Secret: $CLIENT_SECRET"
else
    echo -e "${YELLOW}⚠ OAuth token obtained but MCP request had issues${NC}"
fi
