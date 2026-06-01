#!/bin/bash
# Setup Cloudflare Tunnel for Parquet MCP Server with API Key Authentication
# Combines authenticated proxy bridge with Cloudflare tunnel for public access

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
HTTP_PORT=${1:-8080}
API_KEY=${2:-""}

echo "Setting up Authenticated Parquet MCP Server via Cloudflare Tunnel"
echo "================================================================="
echo ""

# Check dependencies
if ! command -v cloudflared &> /dev/null; then
    echo "Error: cloudflared is not installed"
    echo "Install with: brew install cloudflared"
    exit 1
fi

if ! python3 -c "import aiohttp" 2>/dev/null; then
    echo "Error: aiohttp is required"
    echo "Install with: pip install aiohttp"
    exit 1
fi

if ! command -v npx &> /dev/null; then
    echo "Error: npx (Node.js) is required"
    echo "Install Node.js: https://nodejs.org/"
    exit 1
fi

# Get auth token (MCP standard)
if [ -z "$API_KEY" ]; then
    AUTH_TOKEN="${MCP_AUTH_TOKEN:-}"
    
    # Backward compatibility
    if [ -z "$AUTH_TOKEN" ]; then
        AUTH_TOKEN="${MCP_PROXY_API_KEY:-}"
    fi
    
    if [ -z "$AUTH_TOKEN" ]; then
        if command -v op &> /dev/null; then
            echo "Attempting to get auth token from 1Password..."
            AUTH_TOKEN=$(op read "op://Private/Parquet MCP Proxy/MCP_AUTH_TOKEN" 2>/dev/null || \
                         op read "op://Private/Parquet MCP Proxy/API Key" 2>/dev/null || echo "")
        fi
    fi
    
    if [ -z "$AUTH_TOKEN" ]; then
        echo "No MCP auth token found. Options:"
        echo "  1. Generate new token: python3 $SCRIPT_DIR/parquet_mcp_authenticated_proxy.py --generate-key"
        echo "  2. Set environment variable: export MCP_AUTH_TOKEN='your-token' (MCP standard)"
        echo "  3. Pass as second argument: $0 $HTTP_PORT 'your-token'"
        exit 1
    fi
else
    AUTH_TOKEN="$API_KEY"
fi

echo "Step 1: Starting authenticated MCP proxy on port $HTTP_PORT..."
echo ""

# Start authenticated proxy in background
python3 "$SCRIPT_DIR/parquet_mcp_authenticated_proxy.py" \
    --port "$HTTP_PORT" \
    --auth-token "$AUTH_TOKEN" &
PROXY_PID=$!

# Wait for proxy to start
sleep 3

# Check if proxy is running
if ! lsof -Pi :$HTTP_PORT -sTCP:LISTEN -t >/dev/null ; then
    echo "Error: Authenticated proxy failed to start on port $HTTP_PORT"
    kill $PROXY_PID 2>/dev/null || true
    exit 1
fi

echo "✓ Authenticated proxy running on port $HTTP_PORT"
echo "  Auth Token: ${AUTH_TOKEN:0:8}...${AUTH_TOKEN: -4} (hidden)"
echo ""
echo "Step 2: Starting Cloudflare tunnel..."
echo "The public tunnel URL will be displayed below."
echo ""
echo "⚠️  IMPORTANT: Use the auth token in your client configuration (MCP standard):"
echo "   \"headers\": {\"Authorization\": \"Bearer ${AUTH_TOKEN}\"}"
echo ""
echo "Press Ctrl+C to stop both services"
echo ""

# Trap Ctrl+C to cleanup
trap "kill $PROXY_PID 2>/dev/null; exit" INT TERM

# Start Cloudflare tunnel
cloudflared tunnel --url http://localhost:$HTTP_PORT

# Cleanup on exit
kill $PROXY_PID 2>/dev/null || true
