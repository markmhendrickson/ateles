#!/usr/bin/env bash
# Install Strix (meeting/ambient audio recorder) as a launchd login agent.
set -euo pipefail

PLIST="com.ateles.strix.plist"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LAUNCH_AGENTS="$HOME/Library/LaunchAgents"
DEST="$LAUNCH_AGENTS/$PLIST"

mkdir -p "$LAUNCH_AGENTS"

# Unload if already installed.
if launchctl list | grep -q "com.ateles.strix" 2>/dev/null; then
  echo "Unloading existing agent..."
  launchctl unload "$DEST" 2>/dev/null || true
fi

cp "$SCRIPT_DIR/$PLIST" "$DEST"
launchctl load "$DEST"

echo "✓ strix installed and started."
echo "  Menu bar icon: 🔴 recording  ⚫ idle"
echo "  Logs: /tmp/com.ateles.strix.{log,err}"
echo ""
echo "To uninstall:"
echo "  launchctl unload $DEST && rm $DEST"
