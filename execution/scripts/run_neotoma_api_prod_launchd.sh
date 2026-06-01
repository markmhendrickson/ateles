#!/usr/bin/env bash
# Long-running Neotoma HTTP API for launchd (prod, port 3180 by default).
# Pair with ~/Library/LaunchAgents/com.cloudflare.mcp-servers-tunnel.plist so
# https://neotoma.markmhendrickson.com/health reaches this process.
#
# Env (optional, after sourcing ateles/.env if present):
#   MCP_PROXY_NEOTOMA_REPO   Neotoma git checkout (default: ~/repos/neotoma)
#   ATELES_ROOT              Ateles checkout for .env (default: parent of execution/)
#   NEOTOMA_LAUNCHD_SKIP_BUILD  If "1" and dist/actions.js exists, skip
#                               `npm run build:server` for faster boot.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_ATELES="$(cd "$SCRIPT_DIR/../.." && pwd)"
ATELES_ROOT="${ATELES_ROOT:-$DEFAULT_ATELES}"
NEOTOMA_REPO="${MCP_PROXY_NEOTOMA_REPO:-${HOME:-}/repos/neotoma}"

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"

if [[ -f "$ATELES_ROOT/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$ATELES_ROOT/.env"
  set +a
fi

if [[ ! -d "$NEOTOMA_REPO" ]]; then
  echo "run_neotoma_api_prod_launchd: NEOTOMA_REPO not found: $NEOTOMA_REPO" >&2
  exit 1
fi

cd "$NEOTOMA_REPO"

if ! command -v npm >/dev/null 2>&1; then
  echo "run_neotoma_api_prod_launchd: npm not on PATH (check LaunchAgent PATH)" >&2
  exit 1
fi

if [[ "${NEOTOMA_LAUNCHD_SKIP_BUILD:-}" == "1" ]] && [[ -f dist/actions.js ]]; then
  export NEOTOMA_ENV=production
  exec node scripts/pick-port.js 3180 -- node dist/actions.js
fi

exec npm run start:api:prod
