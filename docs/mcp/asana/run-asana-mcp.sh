#!/bin/bash
# Wrapper script for Asana MCP server
# Loads environment variables from .env file and ensures submodule is initialized

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Check if submodule is initialized or has content
# Submodules have a .git file (not directory) pointing to the git dir
if [ ! -f "$SCRIPT_DIR/.git" ] && [ ! -d "$SCRIPT_DIR/.git" ] && [ ! -f "$SCRIPT_DIR/asana_mcp_server.py" ]; then
    echo "Error: Asana MCP server submodule not initialized." >&2
    echo "" >&2
    echo "The submodule is defined in .gitmodules but not initialized." >&2
    echo "" >&2
    echo "To initialize:" >&2
    echo "  cd $REPO_ROOT" >&2
    echo "  git submodule update --init mcp/asana" >&2
    echo "" >&2
    echo "Or run:" >&2
    echo "  $REPO_ROOT/scripts/init_submodules.sh" >&2
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
    exec "$REPO_ROOT/execution/venv/bin/python3" "$SCRIPT_DIR/asana_mcp_server.py"
else
    exec python3 "$SCRIPT_DIR/asana_mcp_server.py"
fi
