#!/usr/bin/env bash
# install.sh — Set up Apus daemon + Cloudflare Tunnel route
#
# Run once after cloning. Requires:
#   - cloudflared authenticated (cert.pem present in ~/.cloudflared)
#   - launchctl (macOS)
#
# Usage:
#   bash execution/daemons/apus/install.sh

set -euo pipefail

DAEMON_DIR="$(cd "$(dirname "$0")" && pwd)"
TUNNEL_NAME="mcp-servers"
HOSTNAME="apus.markmhendrickson.com"
PLIST_SRC="$DAEMON_DIR/com.ateles.apus.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.ateles.apus.plist"

echo "==> Apus install"

# 1. Add DNS CNAME via cloudflared (idempotent with --overwrite-dns)
echo "==> Routing DNS: $HOSTNAME → tunnel $TUNNEL_NAME"
cloudflared tunnel route dns --overwrite-dns "$TUNNEL_NAME" "$HOSTNAME"

# 2. Install launchd agent
echo "==> Installing launchd agent to $PLIST_DST"
cp "$PLIST_SRC" "$PLIST_DST"

# 3. Load (or reload if already loaded)
echo "==> Loading launchd agent"
launchctl bootout "gui/$(id -u)" "$PLIST_DST" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_DST"

echo "==> Done. Apus should be live at https://$HOSTNAME/health"
echo "    To tail logs: tail -f /tmp/com.ateles.apus.err.log"
echo ""
echo "    Reload after config change:"
echo "      launchctl bootout gui/\$(id -u) ~/Library/LaunchAgents/com.ateles.apus.plist"
echo "      launchctl bootstrap gui/\$(id -u) ~/Library/LaunchAgents/com.ateles.apus.plist"
echo ""
echo "    Restart tunnel to pick up new ingress rule:"
echo "      launchctl kickstart -k gui/\$(id -u)/com.ateles.cloudflared 2>/dev/null || \\"
echo "        cloudflared tunnel run $TUNNEL_NAME &"
