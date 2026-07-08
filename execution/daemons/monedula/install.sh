#!/usr/bin/env bash
# Install the monedula daemon as a launchd agent.
# Polls every 15 minutes (StartInterval=900): notifies per session as it ends
# and sweeps email replies for payment approvals.
set -euo pipefail

PLIST="com.ateles.monedula.plist"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LAUNCH_AGENTS="$HOME/Library/LaunchAgents"
DEST="$LAUNCH_AGENTS/$PLIST"

mkdir -p "$LAUNCH_AGENTS"

# Unload monedula if already installed.
if launchctl list 2>/dev/null | grep -q "com.ateles.monedula"; then
  echo "Unloading existing monedula agent..."
  launchctl unload "$DEST" 2>/dev/null || true
fi

cp "$SCRIPT_DIR/$PLIST" "$DEST"
launchctl load "$DEST"

echo "✓ monedula installed."
echo "  Schedule: polls every 15 minutes (StartInterval=900)"
echo "  Logs: $HOME/Library/Logs/ateles/monedula.log"
echo ""
echo "To uninstall:"
echo "  launchctl unload $DEST && rm $DEST"
