#!/usr/bin/env bash
# Install LaunchAgent so Neotoma prod HTTP API starts at login and survives reboots.
# Prereqs:
#   - Neotoma repo at MCP_PROXY_NEOTOMA_REPO or ~/repos/neotoma
#   - Cloudflare tunnel already routes neotoma.markmhendrickson.com → localhost:3180
#     (same tunnel as MCP, e.g. com.cloudflare.mcp-servers-tunnel)
#   - ateles .env (optional) for secrets consumed by Neotoma when sourced

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
PLIST_TEMPLATE="$SCRIPT_DIR/com.markmhendrickson.neotoma-api.plist"
PLIST_NAME="com.markmhendrickson.neotoma-api.plist"
LAUNCH_AGENTS_DIR="${HOME}/Library/LaunchAgents"
INSTALLED_PLIST="$LAUNCH_AGENTS_DIR/$PLIST_NAME"
LOG_DIR="$REPO_ROOT/data/logs"

echo "Neotoma API LaunchAgent setup"
echo "============================="

if [[ ! -f "$PLIST_TEMPLATE" ]]; then
  echo "Error: missing $PLIST_TEMPLATE" >&2
  exit 1
fi

if [[ ! -x "$SCRIPT_DIR/run_neotoma_api_prod_launchd.sh" ]]; then
  chmod +x "$SCRIPT_DIR/run_neotoma_api_prod_launchd.sh"
fi

mkdir -p "$LOG_DIR" "$LAUNCH_AGENTS_DIR"

if [[ -f "$INSTALLED_PLIST" ]]; then
  echo "Unloading existing agent..."
  launchctl unload "$INSTALLED_PLIST" 2>/dev/null || true
fi

echo "Writing $INSTALLED_PLIST"
sed -e "s|@ATELES_ROOT@|$REPO_ROOT|g" "$PLIST_TEMPLATE" >"$INSTALLED_PLIST"

echo "Loading LaunchAgent..."
launchctl load "$INSTALLED_PLIST"

sleep 2
if launchctl list | grep -q "com.markmhendrickson.neotoma-api"; then
  echo ""
  echo "Installed. API should listen on port 3180 after build (check logs)."
  echo "  tail -f $LOG_DIR/neotoma_api_launchd.log"
  echo ""
  echo "Faster reboots after first successful boot: set NEOTOMA_LAUNCHD_SKIP_BUILD=1"
  echo "in the plist EnvironmentVariables dict, then unload/load again."
  echo ""
  echo "Tunnel (separate): ensure com.cloudflare.mcp-servers-tunnel is loaded and"
  echo "ingress maps neotoma.markmhendrickson.com to http://localhost:3180"
else
  echo "Warning: agent may still be starting; check:" >&2
  echo "  tail -f $LOG_DIR/neotoma_api_launchd.error.log" >&2
fi
