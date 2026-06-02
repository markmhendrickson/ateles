#!/usr/bin/env bash
# provision_mcp.sh — One-tap MCP installer for the Ateles operator.
#
# USAGE
#   provision_mcp.sh <mcp_id> [--no-restart]
#
# Run this after approving a mcp_not_available escalation from Anthus.
# Registers the MCP via `claude mcp add` (writes to .claude.json for the
# ateles project scope), which is how Claude Code manages MCP servers.
# Note: settings.json mcpServers is a Claude Desktop convention — not used here.
#
# The operator taps "approve" on Telegram → Onychomys runs this script.
# This script does NOT install autonomously; it always requires prior
# operator approval (the trust boundary is the approve action).
#
# SUPPORTED MCP IDs
#   typefully       Typefully API — X, LinkedIn, Bluesky, Threads scheduling (HTTP)
#   substack        Substack (unofficial) — via substack-api Python CLI (no MCP entry)
#   outstand        Outstand MCP — multi-platform social scheduling (HTTP)
#   (custom)        Any HTTP MCP — pass MCP_URL and optionally MCP_API_KEY env vars
#
# ENVIRONMENT
#   TYPEFULLY_API_KEY       Required for typefully
#   OUTSTAND_API_KEY        Required for outstand
#   SUBSTACK_COOKIE         Required for substack (session cookie)
#   MCP_URL                 URL for custom HTTP MCPs
#   MCP_API_KEY             Bearer token for custom HTTP MCPs

set -euo pipefail

# ── Paths ─────────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${ATELES_REPO_ROOT:-$(cd "$SCRIPT_DIR/../.." && pwd)}"

# ── Args ──────────────────────────────────────────────────────────────────────
MCP_ID="${1:-}"
NO_RESTART=0

shift 1 2>/dev/null || true
while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-restart) NO_RESTART=1; shift ;;
    *) echo "Unknown arg: $1" >&2; exit 1 ;;
  esac
done

if [[ -z "$MCP_ID" ]]; then
  echo "Usage: provision_mcp.sh <mcp_id> [--no-restart]" >&2
  exit 1
fi

echo "▶ Provisioning MCP: $MCP_ID"

# ── Check if already registered ───────────────────────────────────────────────
if claude mcp get "$MCP_ID" >/dev/null 2>&1; then
  echo "  ℹ  '$MCP_ID' already registered — skipping."
  exit 0
fi

# ── MCP registry ──────────────────────────────────────────────────────────────
# Claude Code uses HTTP transport MCPs registered via `claude mcp add --transport http`.
# No npm packages needed for HTTP MCPs.
case "$MCP_ID" in
  typefully)
    ENV_REQUIRED="TYPEFULLY_API_KEY"
    MCP_URL="https://mcp.typefully.com/mcp"
    MCP_AUTH_ENV="TYPEFULLY_API_KEY"
    ;;
  outstand)
    ENV_REQUIRED="OUTSTAND_API_KEY"
    MCP_URL="https://mcp.outstand.so/mcp"
    MCP_AUTH_ENV="OUTSTAND_API_KEY"
    ;;
  substack)
    # Substack has no MCP — installs the unofficial Python CLI only.
    echo "  ℹ  Substack has no MCP. Installing substack-api Python package."
    pip install --quiet substack-api 2>/dev/null || pip3 install --quiet substack-api
    echo "  ✓ substack-api installed. Corvus invokes it via Bash tool."
    echo "  ✓ Done. No MCP registration needed."
    exit 0
    ;;
  *)
    # Custom HTTP MCP — caller must provide MCP_URL and optionally MCP_API_KEY
    MCP_URL="${MCP_URL:-}"
    MCP_AUTH_ENV="${MCP_API_KEY:+MCP_API_KEY}"
    ENV_REQUIRED=""
    if [[ -z "$MCP_URL" ]]; then
      echo "Unknown mcp_id '$MCP_ID'. Set MCP_URL env var for custom HTTP MCPs." >&2
      exit 1
    fi
    ;;
esac

# ── Env check ─────────────────────────────────────────────────────────────────
if [[ -n "${ENV_REQUIRED:-}" ]] && [[ -z "${!ENV_REQUIRED:-}" ]]; then
  echo "  ✗ Required env var $ENV_REQUIRED is not set." >&2
  echo "    Set it in your shell or ateles-private/.env before running." >&2
  exit 1
fi

# ── Register via claude mcp add ───────────────────────────────────────────────
API_KEY_VALUE="${!MCP_AUTH_ENV:-}"
if [[ -n "$API_KEY_VALUE" ]]; then
  claude mcp add --transport http "$MCP_ID" "$MCP_URL" \
    --header "Authorization: Bearer ${API_KEY_VALUE}"
else
  claude mcp add --transport http "$MCP_ID" "$MCP_URL"
fi

echo "  ✓ Registered $MCP_ID → $MCP_URL"
echo "  ✓ MCP is active immediately — no restart required for HTTP MCPs."
echo ""
echo "  Verify: claude mcp get $MCP_ID"
echo "  Next: resolve the mcp_not_available escalation entity in Neotoma."
