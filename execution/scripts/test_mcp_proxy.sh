#!/bin/bash
# Automated test script for MCP proxy
# Tests both Bearer token and OAuth authentication methods

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# PROJECT_ROOT is repo root (two levels up from execution/scripts/)
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
TEST_PORT=8090
TEST_STDIO_PORT=8091
TEST_SERVER_NAME="test-parquet"
TEST_SERVER_SCRIPT="$PROJECT_ROOT/mcp/parquet/parquet_mcp_server.py"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Test counters
TESTS_PASSED=0
TESTS_FAILED=0
PROXY_PID=""
STDIO_PROXY_PID=""

# Cleanup function
cleanup() {
    echo ""
    echo "Cleaning up..."
    
    # Kill proxy processes
    if [ -n "$PROXY_PID" ]; then
        kill "$PROXY_PID" 2>/dev/null || true
        wait "$PROXY_PID" 2>/dev/null || true
    fi
    
    if [ -n "$STDIO_PROXY_PID" ]; then
        kill "$STDIO_PROXY_PID" 2>/dev/null || true
        wait "$STDIO_PROXY_PID" 2>/dev/null || true
    fi
    
    # Kill any remaining processes on test ports
    lsof -ti :$TEST_PORT | xargs kill -9 2>/dev/null || true
    lsof -ti :$TEST_STDIO_PORT | xargs kill -9 2>/dev/null || true
    
    echo "Cleanup complete"
}

# Trap cleanup on exit
trap cleanup EXIT INT TERM

# Test helper functions
test_pass() {
    echo -e "${GREEN}✓ PASS${NC}: $1"
    TESTS_PASSED=$((TESTS_PASSED + 1))
}

test_fail() {
    echo -e "${RED}✗ FAIL${NC}: $1"
    TESTS_FAILED=$((TESTS_FAILED + 1))
}

test_info() {
    echo -e "${YELLOW}ℹ INFO${NC}: $1"
}

# Cleanup any existing processes on test ports
echo "Cleaning up any existing processes on test ports..."
lsof -ti :$TEST_PORT | xargs kill -9 2>/dev/null || true
lsof -ti :$TEST_STDIO_PORT | xargs kill -9 2>/dev/null || true
sleep 1

# Check prerequisites
echo "Checking prerequisites..."
echo ""

# Check Python
if ! command -v python3 &> /dev/null; then
    test_fail "python3 not found"
    exit 1
fi
test_pass "python3 found"

# Check aiohttp and find working python3
PYTHON3_CMD=""
for cmd in python3 /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
    if command -v "$cmd" >/dev/null 2>&1 && "$cmd" -c "import aiohttp" 2>/dev/null; then
        PYTHON3_CMD="$cmd"
        break
    fi
done

if [ -z "$PYTHON3_CMD" ]; then
    test_fail "aiohttp not installed or no working python3 found"
    echo "Install with: pip3 install --break-system-packages aiohttp"
    exit 1
fi
test_pass "aiohttp installed (using $PYTHON3_CMD)"

# Check npx (for mcp-proxy)
if ! command -v npx &> /dev/null; then
    test_fail "npx not found (Node.js required for mcp-proxy)"
    exit 1
fi
test_pass "npx found"

# Check server script exists
if [ ! -f "$TEST_SERVER_SCRIPT" ]; then
    test_fail "MCP server script not found: $TEST_SERVER_SCRIPT"
    exit 1
fi
test_pass "MCP server script found"

echo ""
echo "=========================================="
echo "Starting MCP Proxy Tests"
echo "=========================================="
echo ""

# Generate test credentials
TEST_BEARER_TOKEN=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
TEST_OAUTH_CLIENT_ID="test-client-$(openssl rand -hex 8)"
TEST_OAUTH_CLIENT_SECRET="test-secret-$(openssl rand -hex 32)"

test_info "Test Bearer Token: ${TEST_BEARER_TOKEN:0:8}...${TEST_BEARER_TOKEN: -4}"
test_info "Test OAuth Client ID: $TEST_OAUTH_CLIENT_ID"
test_info "Test OAuth Client Secret: ${TEST_OAUTH_CLIENT_SECRET:0:8}...${TEST_OAUTH_CLIENT_SECRET: -4}"
echo ""

# Test 1: Start proxy with Bearer token
echo "Test 1: Starting proxy with Bearer token authentication..."
# Use explicit stdio-proxy-port to avoid conflicts
"$PYTHON3_CMD" "$SCRIPT_DIR/mcp_authenticated_proxy.py" \
    --server-name "$TEST_SERVER_NAME" \
    --server-script "$TEST_SERVER_SCRIPT" \
    --port "$TEST_PORT" \
    --stdio-proxy-port "$TEST_STDIO_PORT" \
    --auth-token "$TEST_BEARER_TOKEN" \
    > /tmp/mcp_proxy_test.log 2>&1 &
PROXY_PID=$!

# Wait for proxy to start (with timeout)
MAX_WAIT=10
WAITED=0
while [ $WAITED -lt $MAX_WAIT ]; do
    if lsof -i :$TEST_PORT > /dev/null 2>&1; then
        break
    fi
    sleep 1
    WAITED=$((WAITED + 1))
done

# Check if proxy is running
if ps -p "$PROXY_PID" > /dev/null 2>&1; then
    test_pass "Proxy process started (PID: $PROXY_PID)"
else
    test_fail "Proxy process failed to start"
    echo "Logs:"
    tail -20 /tmp/mcp_proxy_test.log
    exit 1
fi

# Check if port is listening
if lsof -i :$TEST_PORT > /dev/null 2>&1; then
    test_pass "Proxy listening on port $TEST_PORT"
else
    test_fail "Proxy not listening on port $TEST_PORT"
    tail -20 /tmp/mcp_proxy_test.log
    exit 1
fi

# Test 2: Test Bearer token authentication (should fail without token)
echo ""
echo "Test 2: Testing Bearer token authentication..."
RESPONSE=$(curl -s -w "\n%{http_code}" http://localhost:$TEST_PORT 2>&1)
HTTP_CODE=$(echo "$RESPONSE" | tail -1)
BODY=$(echo "$RESPONSE" | sed '$d')

if [ "$HTTP_CODE" = "401" ]; then
    test_pass "Unauthorized request returns 401 (correct)"
else
    test_fail "Expected 401, got $HTTP_CODE"
    echo "Response: $BODY"
fi

# Test 3: Test Bearer token authentication (should succeed with token)
echo ""
echo "Test 3: Testing Bearer token authentication with valid token..."
RESPONSE=$(curl -s --max-time 10 -w "\n%{http_code}" \
    -H "Authorization: Bearer $TEST_BEARER_TOKEN" \
    -H "Content-Type: application/json" \
    -X POST \
    -d '{"jsonrpc":"2.0","method":"initialize","id":1,"params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}}}' \
    http://localhost:$TEST_PORT 2>&1)
HTTP_CODE=$(echo "$RESPONSE" | tail -1)
BODY=$(echo "$RESPONSE" | sed '$d')

if [ "$HTTP_CODE" = "200" ] || [ "$HTTP_CODE" = "201" ]; then
    test_pass "Authenticated request succeeds (HTTP $HTTP_CODE)"
elif [ "$HTTP_CODE" = "502" ]; then
    test_fail "Got 502 - stdio proxy connection issue (HTTP $HTTP_CODE)"
    echo "Response: $BODY"
    echo "Note: This may indicate the MCP server is slow to initialize"
else
    test_fail "Expected 200/201, got $HTTP_CODE"
    echo "Response: $BODY"
fi

# Test 4: Test invalid Bearer token
echo ""
echo "Test 4: Testing invalid Bearer token..."
RESPONSE=$(curl -s -w "\n%{http_code}" \
    -H "Authorization: Bearer invalid-token" \
    http://localhost:$TEST_PORT 2>&1)
HTTP_CODE=$(echo "$RESPONSE" | tail -1)

if [ "$HTTP_CODE" = "401" ]; then
    test_pass "Invalid token returns 401 (correct)"
else
    test_fail "Expected 401 for invalid token, got $HTTP_CODE"
fi

# Stop Bearer token proxy
kill "$PROXY_PID" 2>/dev/null || true
wait "$PROXY_PID" 2>/dev/null || true
sleep 2

# Test 5: Start proxy with OAuth
echo ""
echo "Test 5: Starting proxy with OAuth authentication..."
# Use explicit stdio-proxy-port to avoid conflicts
"$PYTHON3_CMD" "$SCRIPT_DIR/mcp_authenticated_proxy.py" \
    --server-name "$TEST_SERVER_NAME" \
    --server-script "$TEST_SERVER_SCRIPT" \
    --port "$TEST_PORT" \
    --stdio-proxy-port "$TEST_STDIO_PORT" \
    --oauth-client-id "$TEST_OAUTH_CLIENT_ID" \
    --oauth-client-secret "$TEST_OAUTH_CLIENT_SECRET" \
    --oauth-redirect-uri "https://test.example.com/oauth/callback" \
    > /tmp/mcp_proxy_oauth_test.log 2>&1 &
PROXY_PID=$!

# Wait for proxy to start (with timeout)
MAX_WAIT=10
WAITED=0
while [ $WAITED -lt $MAX_WAIT ]; do
    if lsof -i :$TEST_PORT > /dev/null 2>&1; then
        break
    fi
    sleep 1
    WAITED=$((WAITED + 1))
done

# Check if proxy is running
if ps -p "$PROXY_PID" > /dev/null 2>&1; then
    test_pass "OAuth proxy process started (PID: $PROXY_PID)"
else
    test_fail "OAuth proxy process failed to start"
    tail -20 /tmp/mcp_proxy_oauth_test.log
    exit 1
fi

# Test 6: Test OAuth token endpoint
echo ""
echo "Test 6: Testing OAuth token endpoint..."
RESPONSE=$(curl -s -w "\n%{http_code}" \
    -X POST \
    -H "Content-Type: application/json" \
    -d "{\"grant_type\":\"client_credentials\",\"client_id\":\"$TEST_OAUTH_CLIENT_ID\",\"client_secret\":\"$TEST_OAUTH_CLIENT_SECRET\"}" \
    http://localhost:$TEST_PORT/oauth/token 2>&1)
HTTP_CODE=$(echo "$RESPONSE" | tail -1)
BODY=$(echo "$RESPONSE" | sed '$d')

if [ "$HTTP_CODE" = "200" ]; then
    # Check if response contains access_token
    if echo "$BODY" | grep -q "access_token"; then
        ACCESS_TOKEN=$(echo "$BODY" | python3 -c "import sys, json; print(json.load(sys.stdin)['access_token'])" 2>/dev/null)
        if [ -n "$ACCESS_TOKEN" ]; then
            test_pass "OAuth token endpoint returns access token"
            test_info "Access token: ${ACCESS_TOKEN:0:8}...${ACCESS_TOKEN: -4}"
        else
            test_fail "OAuth token endpoint response missing access_token"
        fi
    else
        test_fail "OAuth token endpoint response doesn't contain access_token"
        echo "Response: $BODY"
    fi
else
    test_fail "OAuth token endpoint returned HTTP $HTTP_CODE"
    echo "Response: $BODY"
fi

# Test 7: Test OAuth access token authentication
if [ -n "$ACCESS_TOKEN" ]; then
    echo ""
    echo "Test 7: Testing OAuth access token authentication..."
    RESPONSE=$(curl -s --max-time 10 -w "\n%{http_code}" \
        -H "Authorization: Bearer $ACCESS_TOKEN" \
        -H "Content-Type: application/json" \
        -X POST \
        -d '{"jsonrpc":"2.0","method":"initialize","id":1,"params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}}}' \
        http://localhost:$TEST_PORT 2>&1)
    HTTP_CODE=$(echo "$RESPONSE" | tail -1)
    BODY=$(echo "$RESPONSE" | sed '$d')

    if [ "$HTTP_CODE" = "200" ] || [ "$HTTP_CODE" = "201" ]; then
        test_pass "OAuth access token authentication succeeds (HTTP $HTTP_CODE)"
    elif [ "$HTTP_CODE" = "502" ]; then
        test_fail "Got 502 - stdio proxy connection issue (HTTP $HTTP_CODE)"
        echo "Response: $BODY"
        echo "Note: This may indicate the MCP server is slow to initialize"
    else
        test_fail "Expected 200/201 with OAuth token, got $HTTP_CODE"
        echo "Response: $BODY"
    fi
fi

# Test 8: Test invalid OAuth credentials
echo ""
echo "Test 8: Testing invalid OAuth credentials..."
RESPONSE=$(curl -s -w "\n%{http_code}" \
    -X POST \
    -H "Content-Type: application/json" \
    -d '{"grant_type":"client_credentials","client_id":"invalid","client_secret":"invalid"}' \
    http://localhost:$TEST_PORT/oauth/token 2>&1)
HTTP_CODE=$(echo "$RESPONSE" | tail -1)

if [ "$HTTP_CODE" = "401" ]; then
    test_pass "Invalid OAuth credentials return 401 (correct)"
else
    test_fail "Expected 401 for invalid credentials, got $HTTP_CODE"
fi

# Test 9: Test OAuth authorization endpoint
echo ""
echo "Test 9: Testing OAuth authorization endpoint..."
RESPONSE=$(curl -s -w "\n%{http_code}" \
    -L \
    http://localhost:$TEST_PORT/oauth/authorize 2>&1)
HTTP_CODE=$(echo "$RESPONSE" | tail -1)

if [ "$HTTP_CODE" = "200" ] || [ "$HTTP_CODE" = "302" ]; then
    test_pass "OAuth authorization endpoint accessible (HTTP $HTTP_CODE)"
else
    test_fail "OAuth authorization endpoint returned HTTP $HTTP_CODE"
fi

# Summary
echo ""
echo "=========================================="
echo "Test Summary"
echo "=========================================="
echo -e "${GREEN}Tests Passed: $TESTS_PASSED${NC}"
echo -e "${RED}Tests Failed: $TESTS_FAILED${NC}"
echo ""

if [ $TESTS_FAILED -eq 0 ]; then
    echo -e "${GREEN}All tests passed! ✓${NC}"
    exit 0
else
    echo -e "${RED}Some tests failed. Check output above.${NC}"
    exit 1
fi
