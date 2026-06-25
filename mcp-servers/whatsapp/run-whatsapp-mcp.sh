#!/bin/bash
# Wrapper script for WhatsApp MCP server
# Loads environment variables from .env file

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Check if server file exists (WhatsApp is not a submodule)
if [ ! -f "$SCRIPT_DIR/whatsapp_mcp_server.py" ]; then
    echo "Error: WhatsApp MCP server not found." >&2
    echo "" >&2
    echo "Expected file: $SCRIPT_DIR/whatsapp_mcp_server.py" >&2
    exit 1
fi

# Load .env file if it exists
if [ -f "$REPO_ROOT/.env" ]; then
    set -a  # Automatically export all variables
    source "$REPO_ROOT/.env"
    set +a
fi

# Use venv Python if available, otherwise system Python
if [ -f "$REPO_ROOT/execution/venv/bin/python3" ]; then
    exec "$REPO_ROOT/execution/venv/bin/python3" "$SCRIPT_DIR/whatsapp_mcp_server.py"
else
    exec python3 "$SCRIPT_DIR/whatsapp_mcp_server.py"
fi
