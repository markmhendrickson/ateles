#!/usr/bin/env bash
# Install Sylvia as a launchd daily agent.
# Runs once daily at 07:15 UTC (09:15 Madrid summer / 08:15 winter).
set -euo pipefail

PLIST="com.ateles.sylvia.plist"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LAUNCH_AGENTS="$HOME/Library/LaunchAgents"
DEST="$LAUNCH_AGENTS/$PLIST"

mkdir -p "$LAUNCH_AGENTS"

if launchctl list 2>/dev/null | grep -q "com.ateles.sylvia"; then
  echo "Unloading existing sylvia agent..."
  launchctl unload "$DEST" 2>/dev/null || true
fi

ln -sf "$SCRIPT_DIR/$PLIST" "$DEST"
launchctl load "$DEST"

echo "✓ sylvia installed."
echo "  Schedule: daily at 07:15 UTC (09:15 Madrid summer / 08:15 winter)"
echo "  Logs: $HOME/Library/Logs/ateles/sylvia.log (stdout+stderr)"
echo ""
echo "To uninstall:"
echo "  launchctl unload $DEST && rm $DEST"
