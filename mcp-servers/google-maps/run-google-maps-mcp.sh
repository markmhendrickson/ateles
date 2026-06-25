#!/usr/bin/env bash
# Runs google-maps-mcp-server (npx) with GOOGLE_MAPS_API_KEY from repo .env.
# https://github.com/david-pivonka/google-maps-mcp-server

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

if [[ -f "$REPO_ROOT/.env" ]]; then
  set -a
  # shellcheck source=/dev/null
  source "$REPO_ROOT/.env"
  set +a
fi

if [[ -z "${GOOGLE_MAPS_API_KEY:-}" ]]; then
  echo "Error: GOOGLE_MAPS_API_KEY is not set." >&2
  echo "Add it to $REPO_ROOT/.env (see mcp/google-maps/SETUP.md)." >&2
  exit 1
fi

export GOOGLE_MAPS_API_KEY

exec npx -y google-maps-mcp-server "$@"
