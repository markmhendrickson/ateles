#!/bin/bash
# Wrapper script for HomeKit MCP server
# Loads environment variables from .env file and ensures submodule is initialized

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Check if submodule is initialized or has content (HomeKit is not a submodule, so check for file)
if [ ! -f "$SCRIPT_DIR/homekit_mcp_server.py" ]; then
    echo "Error: HomeKit MCP server not found." >&2
    echo "" >&2
    echo "Expected file: $SCRIPT_DIR/homekit_mcp_server.py" >&2
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
    exec "$REPO_ROOT/execution/venv/bin/python3" "$SCRIPT_DIR/homekit_mcp_server.py"
else
    exec python3 "$SCRIPT_DIR/homekit_mcp_server.py"
fi
