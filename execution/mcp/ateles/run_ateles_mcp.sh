#!/usr/bin/env bash
# MCP stdio launcher for the Ateles swarm server (execution/mcp/ateles/server.py).
#
# ~/.claude.json has referenced this path since the `ateles` MCP entry was added,
# but the script itself was never committed, so every session started with the
# server failing to spawn (ENOENT) and the mcp__ateles__* tools — dispatch health,
# gate status, checkpoints, route_task — silently unavailable. A session could
# not tell "the swarm has nothing to report" from "the swarm is unreachable".
#
# The token is read here rather than passed in ~/.claude.json (world-readable) or
# on the command line (argv is visible in `ps` to every local process).
set -euo pipefail

SERVER="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/server.py"
if [ ! -r "$SERVER" ]; then
  echo "[ateles-mcp] server not found at $SERVER" >&2
  exit 1
fi

# NEOTOMA_BEARER_TOKEN: honour an inherited value, else read the operator env
# file. server.py sends it as the Authorization header on every Neotoma call.
ENV_FILE="${NEOTOMA_ENV_FILE:-$HOME/.config/neotoma/.env}"
TOKEN="${NEOTOMA_BEARER_TOKEN:-}"
if [ -z "$TOKEN" ] && [ -r "$ENV_FILE" ]; then
  TOKEN="$(grep -m1 '^NEOTOMA_BEARER_TOKEN=' "$ENV_FILE" | cut -d= -f2- | tr -d '"'\''' | tr -d '[:space:]')"
fi
if [ -z "$TOKEN" ]; then
  echo "[ateles-mcp] no NEOTOMA_BEARER_TOKEN in env or $ENV_FILE" >&2
  exit 1
fi
export NEOTOMA_BEARER_TOKEN="$TOKEN"

# server.py defaults NEOTOMA_BASE_URL to the hosted instance and SWARM_ROSTER_KEY
# to "default"; both stay overridable from the environment. Local hosting was
# retired 2026-08-04, so never fall back to a localhost address here.

# Pick an interpreter that actually has `mcp` and `httpx`. The repo venv is NOT
# a safe default: it carries httpx but not mcp, so launching from it fails at
# import with a message the harness reports only as a dead server.
PY=""
for candidate in \
  "${ATELES_MCP_PYTHON:-}" \
  /opt/homebrew/bin/python3 \
  /usr/local/bin/python3 \
  "$(command -v python3 2>/dev/null || true)"; do
  [ -n "$candidate" ] && [ -x "$candidate" ] || continue
  if "$candidate" -c 'import mcp, httpx' >/dev/null 2>&1; then
    PY="$candidate"
    break
  fi
done
if [ -z "$PY" ]; then
  echo "[ateles-mcp] no python3 with both 'mcp' and 'httpx' found; install them (see requirements.txt)" >&2
  exit 1
fi

exec "$PY" "$SERVER" "$@"
