#!/usr/bin/env bash
# provision_mcp.sh — One-tap MCP installer for the Ateles operator.
#
# USAGE
#   provision_mcp.sh <mcp_id> [--settings <path>] [--no-restart]
#
# Run this after approving a mcp_not_available escalation from Anthus.
# Adds the MCP entry to Claude Code settings.json (or a specified path),
# then signals the harness to restart.
#
# The operator taps "approve" on Telegram → Onychomys runs this script.
# This script does NOT install autonomously; it always requires prior
# operator approval (the trust boundary is the approve action).
#
# SUPPORTED MCP IDs
#   typefully       Typefully API — X, LinkedIn, Bluesky, Threads scheduling
#   medium          Medium REST API — publish/draft posts
#   substack        Substack (unofficial) — via substack-api Python CLI
#   outstand        Outstand MCP — multi-platform social scheduling
#   (custom)        Any MCP with a known npm package — pass MCP_PACKAGE env var
#
# ENVIRONMENT
#   TYPEFULLY_API_KEY       Required for typefully
#   MEDIUM_INTEGRATION_TOKEN  Required for medium
#   SUBSTACK_COOKIE         Required for substack (session cookie)
#   ATELES_REPO_ROOT        Override repo root (default: two levels up from script)
#   MCP_PACKAGE             npm package name for unknown mcp_id values
#   MCP_COMMAND             override the command field in settings.json entry
#   CLAUDE_SETTINGS         override path to settings.json

set -euo pipefail

# ── Paths ─────────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${ATELES_REPO_ROOT:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
DEFAULT_SETTINGS="$HOME/.claude/settings.json"

# ── Args ──────────────────────────────────────────────────────────────────────
MCP_ID="${1:-}"
SETTINGS_PATH="${CLAUDE_SETTINGS:-$DEFAULT_SETTINGS}"
NO_RESTART=0

shift 1 2>/dev/null || true
while [[ $# -gt 0 ]]; do
  case "$1" in
    --settings) SETTINGS_PATH="$2"; shift 2 ;;
    --no-restart) NO_RESTART=1; shift ;;
    *) echo "Unknown arg: $1" >&2; exit 1 ;;
  esac
done

if [[ -z "$MCP_ID" ]]; then
  echo "Usage: provision_mcp.sh <mcp_id> [--settings <path>] [--no-restart]" >&2
  exit 1
fi

echo "▶ Provisioning MCP: $MCP_ID"
echo "  Settings: $SETTINGS_PATH"

# ── MCP registry ──────────────────────────────────────────────────────────────
# Each entry: name, command_type (npx|python|bash), package/path, env_check
case "$MCP_ID" in
  typefully)
    MCP_NAME="typefully"
    MCP_CMD_TYPE="npx"
    MCP_PACKAGE="${MCP_PACKAGE:-@typefully/mcp-server}"
    ENV_REQUIRED="TYPEFULLY_API_KEY"
    MCP_ARGS='["-y", "@typefully/mcp-server"]'
    MCP_ENV_KEY="TYPEFULLY_API_KEY"
    ;;
  medium)
    MCP_NAME="medium"
    MCP_CMD_TYPE="npx"
    MCP_PACKAGE="${MCP_PACKAGE:-@medium/mcp-server}"
    ENV_REQUIRED="MEDIUM_INTEGRATION_TOKEN"
    MCP_ARGS='["-y", "@medium/mcp-server"]'
    MCP_ENV_KEY="MEDIUM_INTEGRATION_TOKEN"
    ;;
  substack)
    # Substack has no official MCP — installs the unofficial Python CLI
    # and wraps it as a stdio MCP shim.
    MCP_NAME="substack"
    MCP_CMD_TYPE="python"
    MCP_PACKAGE="${MCP_PACKAGE:-substack-api}"
    ENV_REQUIRED="SUBSTACK_COOKIE"
    MCP_ARGS=""  # No MCP server; Corvus uses Bash tool with substack-api CLI
    MCP_ENV_KEY="SUBSTACK_COOKIE"
    echo "  ℹ  Substack has no official MCP. Installing substack-api Python package."
    pip install --quiet substack-api 2>/dev/null || pip3 install --quiet substack-api
    echo "  ✓ substack-api installed. Corvus will invoke it via Bash."
    echo "  ℹ  No settings.json entry needed — Bash tool handles invocation."
    echo "  ✓ Done. Restart not required."
    exit 0
    ;;
  outstand)
    MCP_NAME="outstand"
    MCP_CMD_TYPE="npx"
    MCP_PACKAGE="${MCP_PACKAGE:-@outstand/mcp}"
    ENV_REQUIRED="OUTSTAND_API_KEY"
    MCP_ARGS='["-y", "@outstand/mcp"]'
    MCP_ENV_KEY="OUTSTAND_API_KEY"
    ;;
  *)
    # Custom / unknown MCP
    MCP_NAME="$MCP_ID"
    MCP_PACKAGE="${MCP_PACKAGE:-}"
    if [[ -z "$MCP_PACKAGE" ]]; then
      echo "Unknown mcp_id '$MCP_ID'. Set MCP_PACKAGE env var for custom MCPs." >&2
      exit 1
    fi
    MCP_CMD_TYPE="npx"
    MCP_ARGS="[\"-y\", \"$MCP_PACKAGE\"]"
    ENV_REQUIRED=""
    MCP_ENV_KEY=""
    ;;
esac

# ── Env check ─────────────────────────────────────────────────────────────────
if [[ -n "$ENV_REQUIRED" ]] && [[ -z "${!ENV_REQUIRED:-}" ]]; then
  echo "  ✗ Required env var $ENV_REQUIRED is not set." >&2
  echo "    Set it in your shell or ateles-private/.env before running." >&2
  exit 1
fi

# ── Read current settings.json ────────────────────────────────────────────────
if [[ ! -f "$SETTINGS_PATH" ]]; then
  echo "  ✗ settings.json not found at $SETTINGS_PATH" >&2
  exit 1
fi

# Check if already present
if python3 -c "
import json, sys
with open('$SETTINGS_PATH') as f:
    s = json.load(f)
mcps = s.get('mcpServers', {})
if '$MCP_NAME' in mcps:
    sys.exit(0)
sys.exit(1)
" 2>/dev/null; then
  echo "  ℹ  '$MCP_NAME' already present in settings.json — skipping."
  exit 0
fi

# ── Build the new entry ───────────────────────────────────────────────────────
ENV_BLOCK=""
if [[ -n "$MCP_ENV_KEY" ]]; then
  ENV_BLOCK=", \"env\": {\"$MCP_ENV_KEY\": \"\${$MCP_ENV_KEY}\"}"
fi

NEW_ENTRY="{\"command\": \"npx\", \"args\": $MCP_ARGS$ENV_BLOCK}"

# ── Patch settings.json ───────────────────────────────────────────────────────
BACKUP="${SETTINGS_PATH}.bak.$(date +%s)"
cp "$SETTINGS_PATH" "$BACKUP"
echo "  ✓ Backed up settings.json → $BACKUP"

python3 - <<PYEOF
import json

with open('$SETTINGS_PATH') as f:
    settings = json.load(f)

if 'mcpServers' not in settings:
    settings['mcpServers'] = {}

import json as _json
entry = _json.loads('''$NEW_ENTRY''')
settings['mcpServers']['$MCP_NAME'] = entry

with open('$SETTINGS_PATH', 'w') as f:
    json.dump(settings, f, indent=2)
    f.write('\n')

print('  ✓ Added $MCP_NAME to mcpServers in $SETTINGS_PATH')
PYEOF

# ── Restart harness ───────────────────────────────────────────────────────────
if [[ "$NO_RESTART" -eq 1 ]]; then
  echo "  ℹ  --no-restart set. Restart Claude Code / OpenClaw manually to activate."
  exit 0
fi

echo "  ℹ  MCP config updated. Claude Code reads settings.json on startup."
echo "     Restart Claude Code or the relevant harness to activate '$MCP_NAME'."
echo ""
echo "  ✓ Provisioning complete: $MCP_ID"
echo "     Next: restart harness, then resolve the mcp_not_available escalation in Neotoma."
