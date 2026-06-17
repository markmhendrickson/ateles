#!/usr/bin/env bash
# Install the aquila daemon as a launchd calendar agent.
# Runs monthly on the 1st at 06:00 Madrid. Produces the cofounder report.
set -euo pipefail

PLIST="com.ateles.aquila.plist"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LAUNCH_AGENTS="$HOME/Library/LaunchAgents"
DEST="$LAUNCH_AGENTS/$PLIST"

mkdir -p "$LAUNCH_AGENTS"

# Unload aquila if already installed.
if launchctl list 2>/dev/null | grep -q "com.ateles.aquila"; then
  echo "Unloading existing aquila agent..."
  launchctl unload "$DEST" 2>/dev/null || true
fi

cp "$SCRIPT_DIR/$PLIST" "$DEST"
launchctl load "$DEST"

echo "✓ aquila installed."
echo "  Schedule: monthly on the 1st at 06:00 Madrid"
echo "  Logs: $HOME/Library/Logs/ateles/aquila.log"
echo ""
echo "Run on demand:"
echo "  python3 $SCRIPT_DIR/aquila.py --force"
echo ""
echo "To uninstall:"
echo "  launchctl unload $DEST && rm $DEST"
