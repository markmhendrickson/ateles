#!/bin/bash
# Typefully MCP via official npm package (see https://support.typefully.com/en/articles/13128440-typefully-mcp-server)
# Loads TYPEFULLY_API_KEY from repo .env (or environment).

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

export NVM_DIR="${NVM_DIR:-$HOME/.nvm}"
[ -s "$NVM_DIR/nvm.sh" ] && \. "$NVM_DIR/nvm.sh"

if [ -f "$REPO_ROOT/.env" ]; then
    set -a
    # shellcheck source=/dev/null
    source "$REPO_ROOT/.env"
    set +a
fi

if [ -z "${TYPEFULLY_API_KEY:-}" ]; then
    echo "Error: TYPEFULLY_API_KEY is not set." >&2
    echo "Add it to $REPO_ROOT/.env or export it. Create a key: Typefully → Settings → API / Integrations." >&2
    exit 1
fi

exec npx -y typefully-mcp-server "$@"
