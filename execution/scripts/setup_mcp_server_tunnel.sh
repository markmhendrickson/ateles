#!/bin/bash
# Generic setup script to expose any MCP server via HTTP tunnel with API key authentication
# This creates an authenticated bridge from stdio MCP server to HTTP transport

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# PROJECT_ROOT is repo root (two levels up from execution/scripts/)
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# Load .env file if it exists (for OAuth credentials and other environment variables)
if [ -f "$PROJECT_ROOT/.env" ]; then
    set -a  # Automatically export all variables
    source "$PROJECT_ROOT/.env"
    set +a  # Stop automatically exporting
fi

# Usage: ./setup_mcp_server_tunnel.sh <server-name> <server-script-path> [port] [auth-token] [--oauth-client-id ID] [--oauth-client-secret SECRET] [--oauth-redirect-uri URI]
SERVER_NAME=${1:-""}
SERVER_SCRIPT=${2:-""}
HTTP_PORT=${3:-8080}
AUTH_TOKEN=${4:-""}
OAUTH_CLIENT_ID=""
OAUTH_CLIENT_SECRET=""
OAUTH_REDIRECT_URI=""

# Parse OAuth arguments
shift 4 2>/dev/null || shift 3 2>/dev/null || true
while [[ $# -gt 0 ]]; do
    case $1 in
        --oauth-client-id)
            OAUTH_CLIENT_ID="$2"
            shift 2
            ;;
        --oauth-client-secret)
            OAUTH_CLIENT_SECRET="$2"
            shift 2
            ;;
        --oauth-redirect-uri)
            OAUTH_REDIRECT_URI="$2"
            shift 2
            ;;
        *)
            shift
            ;;
    esac
done

if [ -z "$SERVER_NAME" ] || [ -z "$SERVER_SCRIPT" ]; then
    echo "Usage: $0 <server-name> <server-script-path> [port] [auth-token]"
    echo ""
    echo "Examples:"
    echo "  $0 parquet mcp/parquet/parquet_mcp_server.py 8080"
    echo "  $0 dnsimple mcp/dnsimple/dnsimple_mcp_server.py 8081"
    echo "  $0 gmail mcp/gmail/run-gmail-mcp.sh 8082"
    echo ""
    exit 1
fi

# Resolve server script path
if [[ "$SERVER_SCRIPT" != /* ]]; then
    # Relative path - resolve from project root
    SERVER_SCRIPT="$PROJECT_ROOT/$SERVER_SCRIPT"
    # Verify the file exists
    if [ ! -f "$SERVER_SCRIPT" ]; then
        echo "Error: MCP server script not found at $SERVER_SCRIPT"
        echo "Expected location: $PROJECT_ROOT/$2"
        exit 1
    fi
fi

echo "Setting up Authenticated MCP Server HTTP Tunnel: $SERVER_NAME"
echo "=============================================================="
echo ""

# Check Python dependencies
# Try multiple python3 commands to find one that works
PYTHON3_CMD=""
for cmd in python3 /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
    if command -v "$cmd" >/dev/null 2>&1 && "$cmd" -c "import aiohttp" 2>/dev/null; then
        PYTHON3_CMD="$cmd"
        break
    fi
done

if [ -z "$PYTHON3_CMD" ]; then
    echo "Error: aiohttp is required but not found"
    echo ""
    echo "Tried: python3, /opt/homebrew/bin/python3, /usr/local/bin/python3, /usr/bin/python3"
    echo ""
    echo "Install with:"
    echo "  pip3 install --break-system-packages aiohttp"
    echo "Or:"
    echo "  pip3 install --user aiohttp"
    echo ""
    echo "Then verify: python3 -c 'import aiohttp; print(\"OK\")'"
    exit 1
fi

# Check if npx is available (for stdio proxy)
if ! command -v npx &> /dev/null; then
    echo "Error: npx (Node.js) is required for mcp-proxy"
    echo "Install Node.js: https://nodejs.org/"
    exit 1
fi

# Check if server script exists
if [ ! -f "$SERVER_SCRIPT" ]; then
    echo "Error: MCP server script not found at $SERVER_SCRIPT"
    exit 1
fi

# Get OAuth credentials from environment if not provided
if [ -z "$OAUTH_CLIENT_ID" ]; then
    # Try server-specific OAuth env vars first
    SERVER_OAUTH_ID_VAR="MCP_$(echo "$SERVER_NAME" | tr '[:lower:]' '[:upper:]')_OAUTH_CLIENT_ID"
    OAUTH_CLIENT_ID="${!SERVER_OAUTH_ID_VAR:-}"
    
    # Try generic MCP OAuth env vars
    if [ -z "$OAUTH_CLIENT_ID" ]; then
        OAUTH_CLIENT_ID="${MCP_OAUTH_CLIENT_ID:-}"
    fi
    
    # OAuth credentials should be in .env (synced from 1Password via op_sync_env_from_1password.py)
    # If not found, prompt user to sync
    if [ -z "$OAUTH_CLIENT_ID" ]; then
        echo "OAuth Client ID not found in environment variables."
        echo "Sync credentials from 1Password to .env:"
        echo "  python execution/scripts/op_sync_env_from_1password.py"
        echo ""
        echo "Required environment variables:"
        echo "  MCP_OAUTH_CLIENT_ID or MCP_$(echo "$SERVER_NAME" | tr '[:lower:]' '[:upper:]')_OAUTH_CLIENT_ID"
    fi
fi

if [ -z "$OAUTH_CLIENT_SECRET" ] && [ -n "$OAUTH_CLIENT_ID" ]; then
    # Try server-specific OAuth env vars first
    SERVER_OAUTH_SECRET_VAR="MCP_$(echo "$SERVER_NAME" | tr '[:lower:]' '[:upper:]')_OAUTH_CLIENT_SECRET"
    OAUTH_CLIENT_SECRET="${!SERVER_OAUTH_SECRET_VAR:-}"
    
    # Try generic MCP OAuth env vars
    if [ -z "$OAUTH_CLIENT_SECRET" ]; then
        OAUTH_CLIENT_SECRET="${MCP_OAUTH_CLIENT_SECRET:-}"
    fi
    
    # OAuth credentials should be in .env (synced from 1Password)
    if [ -z "$OAUTH_CLIENT_SECRET" ]; then
        echo "OAuth Client Secret not found in environment variables."
        echo "Sync credentials from 1Password to .env:"
        echo "  python execution/scripts/op_sync_env_from_1password.py"
    fi
fi

if [ -z "$OAUTH_REDIRECT_URI" ] && [ -n "$OAUTH_CLIENT_ID" ]; then
    # Try server-specific OAuth env vars first
    SERVER_OAUTH_URI_VAR="MCP_$(echo "$SERVER_NAME" | tr '[:lower:]' '[:upper:]')_OAUTH_REDIRECT_URI"
    OAUTH_REDIRECT_URI="${!SERVER_OAUTH_URI_VAR:-}"
    
    # Try generic MCP OAuth env vars
    if [ -z "$OAUTH_REDIRECT_URI" ]; then
        OAUTH_REDIRECT_URI="${MCP_OAUTH_REDIRECT_URI:-}"
    fi
fi

# Get auth token (skip if OAuth is configured)
if [ -z "$AUTH_TOKEN" ] && [ -z "$OAUTH_CLIENT_ID" ]; then
    # Try server-specific token first (construct variable name)
    SERVER_TOKEN_VAR="MCP_$(echo "$SERVER_NAME" | tr '[:lower:]' '[:upper:]')_AUTH_TOKEN"
    AUTH_TOKEN="${!SERVER_TOKEN_VAR:-}"
    
    # Try MCP_AUTH_TOKEN (MCP standard, shared)
    if [ -z "$AUTH_TOKEN" ]; then
        AUTH_TOKEN="${MCP_AUTH_TOKEN:-}"
    fi
    
    # Backward compatibility
    if [ -z "$AUTH_TOKEN" ]; then
        AUTH_TOKEN="${MCP_PROXY_API_KEY:-}"
    fi
    
    if [ -z "$AUTH_TOKEN" ]; then
        # Try 1Password
        if command -v op &> /dev/null; then
            echo "Attempting to get auth token from 1Password..."
            # Capitalize first letter of server name for 1Password item name
            SERVER_NAME_CAPITALIZED="$(echo "$SERVER_NAME" | sed 's/^./\U&/')"
            AUTH_TOKEN=$(op read "op://Private/${SERVER_NAME_CAPITALIZED} MCP Proxy/MCP_AUTH_TOKEN" 2>/dev/null || \
                         op read "op://Private/MCP Proxy/MCP_AUTH_TOKEN" 2>/dev/null || \
                         op read "op://Private/${SERVER_NAME_CAPITALIZED} MCP Proxy/API Key" 2>/dev/null || echo "")
        fi
    fi
    
    if [ -z "$AUTH_TOKEN" ]; then
        echo "No MCP auth token found. Options:"
        echo "  1. Generate new token: $PYTHON3_CMD $SCRIPT_DIR/mcp_authenticated_proxy.py --server-name $SERVER_NAME --server-script $SERVER_SCRIPT --generate-token"
        echo "  2. Set environment variable: export MCP_AUTH_TOKEN='your-token' (MCP standard, shared)"
        echo "  3. Set server-specific: export MCP_$(echo "$SERVER_NAME" | tr '[:lower:]' '[:upper:]')_AUTH_TOKEN='your-token'"
        SERVER_NAME_CAPITALIZED="$(echo "$SERVER_NAME" | sed 's/^./\U&/')"
        echo "  4. Store in 1Password: '${SERVER_NAME_CAPITALIZED} MCP Proxy' item, 'MCP_AUTH_TOKEN' field"
        echo "  5. Pass as fourth argument: $0 $SERVER_NAME $SERVER_SCRIPT $HTTP_PORT 'your-token'"
        echo ""
        read -p "Generate a new auth token now? (y/N): " -n 1 -r
        echo
        if [[ $REPLY =~ ^[Yy]$ ]]; then
            NEW_TOKEN=$("$PYTHON3_CMD" "$SCRIPT_DIR/mcp_authenticated_proxy.py" \
                --server-name "$SERVER_NAME" \
                --server-script "$SERVER_SCRIPT" \
                --generate-token 2>&1 | grep "Generated MCP auth token" | cut -d' ' -f6)
            if [ -n "$NEW_TOKEN" ]; then
                AUTH_TOKEN="$NEW_TOKEN"
                echo ""
                echo "✓ Using generated auth token"
            else
                echo "Failed to generate token. Please run manually:"
                echo "  $PYTHON3_CMD $SCRIPT_DIR/mcp_authenticated_proxy.py --server-name $SERVER_NAME --server-script $SERVER_SCRIPT --generate-token"
                exit 1
            fi
        else
            exit 1
        fi
    fi
fi

echo "Server: $SERVER_NAME"
echo "Script: $SERVER_SCRIPT"
echo "HTTP port: $HTTP_PORT"
if [ -n "$OAUTH_CLIENT_ID" ]; then
    echo "OAuth Client ID: $OAUTH_CLIENT_ID"
    [ -n "$OAUTH_REDIRECT_URI" ] && echo "OAuth Redirect URI: $OAUTH_REDIRECT_URI"
    echo ""
    echo "Starting authenticated MCP proxy (OAuth 2.0)..."
else
    echo "Auth Token: ${AUTH_TOKEN:0:8}...${AUTH_TOKEN: -4} (hidden)"
    echo ""
    echo "Starting authenticated MCP proxy (MCP standard: Authorization: Bearer)..."
fi
echo ""

# Build proxy command (use the python3 that has aiohttp)
PROXY_CMD=(
    "$PYTHON3_CMD" "$SCRIPT_DIR/mcp_authenticated_proxy.py"
    --server-name "$SERVER_NAME"
    --server-script "$SERVER_SCRIPT"
    --port "$HTTP_PORT"
)

# Add authentication method
if [ -n "$OAUTH_CLIENT_ID" ]; then
    PROXY_CMD+=(--oauth-client-id "$OAUTH_CLIENT_ID")
    [ -n "$OAUTH_CLIENT_SECRET" ] && PROXY_CMD+=(--oauth-client-secret "$OAUTH_CLIENT_SECRET")
    [ -n "$OAUTH_REDIRECT_URI" ] && PROXY_CMD+=(--oauth-redirect-uri "$OAUTH_REDIRECT_URI")
else
    [ -n "$AUTH_TOKEN" ] && PROXY_CMD+=(--auth-token "$AUTH_TOKEN")
fi

# Start the authenticated proxy
"${PROXY_CMD[@]}"
