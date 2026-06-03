#!/bin/bash
# Wrapper script for Google Search Console MCP server (Node.js/TypeScript)
# Loads environment variables from .env file and sets credential paths to repo
# Properly handles git submodule initialization

# Get repo root (assuming script is in mcp/google-search-console/)
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Check if submodule is initialized or has content
if [ ! -f "$SCRIPT_DIR/.git" ] && [ ! -d "$SCRIPT_DIR/.git" ] && [ ! -f "$SCRIPT_DIR/package.json" ]; then
    echo "Error: Google Search Console MCP server submodule not initialized." >&2
    echo "" >&2
    echo "The submodule is defined in .gitmodules but not initialized." >&2
    echo "" >&2
    echo "To initialize:" >&2
    echo "  1. Add submodule to git index (if not already):" >&2
    echo "     git submodule add <repository-url> mcp/google-search-console" >&2
    echo "" >&2
    echo "  2. Or initialize existing submodule:" >&2
    echo "     cd $REPO_ROOT" >&2
    echo "     git submodule update --init mcp/google-search-console" >&2
    echo "" >&2
    echo "  3. Or run the init script:" >&2
    echo "     $REPO_ROOT/scripts/init_submodules.sh" >&2
    exit 1
fi

# Source nvm if it exists (for Node.js)
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && \. "$NVM_DIR/nvm.sh"

# Load .env file if it exists
if [ -f "$REPO_ROOT/.env" ]; then
    set -a  # Automatically export all variables
    source "$REPO_ROOT/.env"
    set +a
fi

# Set credential paths to repo .creds directory (never use ~/.local defaults)
export SEARCH_CONSOLE_OAUTH_PATH="$REPO_ROOT/.creds/gcp-oauth.keys.json"
export SEARCH_CONSOLE_CREDENTIALS_PATH="$REPO_ROOT/.creds/search-console-credentials.json"

# Check if server is built
if [ ! -f "$SCRIPT_DIR/dist/index.js" ]; then
    # Try to build if dist doesn't exist
    if [ -f "$SCRIPT_DIR/package.json" ]; then
        echo "Building Google Search Console MCP server..." >&2
        cd "$SCRIPT_DIR"
        # Use local cache to avoid npm permission issues with ~/.npm
        npm install --cache .npm-cache 2>&1 || {
            echo "Error: npm install failed. You may need to fix npm permissions:" >&2
            echo "  sudo chown -R $(id -u):$(id -g) ~/.npm" >&2
            exit 1
        }
        npm run build 2>&1 || {
            echo "Error: npm build failed. Check dependencies are installed." >&2
            exit 1
        }
    else
        echo "Error: Google Search Console MCP server not found at $SCRIPT_DIR/dist/index.js" >&2
        echo "Please ensure the server is installed and built:" >&2
        echo "  cd $SCRIPT_DIR" >&2
        echo "  npm install" >&2
        echo "  npm run build" >&2
        exit 1
    fi
fi

# Run the MCP server
exec node "$SCRIPT_DIR/dist/index.js" "$@"
