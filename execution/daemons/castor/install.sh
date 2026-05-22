#!/usr/bin/env bash
# Install Castor (neotoma-repo automation daemon) as a launchd agent.
set -euo pipefail

PLIST="com.ateles.castor.plist"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LAUNCH_AGENTS="$HOME/Library/LaunchAgents"
DEST="$LAUNCH_AGENTS/$PLIST"

mkdir -p "$LAUNCH_AGENTS"

# Unload if already installed.
if launchctl list | grep -q "com.ateles.castor" 2>/dev/null; then
  echo "Unloading existing agent..."
  launchctl unload "$DEST" 2>/dev/null || true
fi

cp "$SCRIPT_DIR/$PLIST" "$DEST"
launchctl load "$DEST"

echo "✓ castor installed and started."
echo "  Logs: /tmp/com.ateles.castor.{log,err}"
echo ""
echo "To uninstall:"
echo "  launchctl unload $DEST && rm $DEST"
