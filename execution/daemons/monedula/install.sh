#!/usr/bin/env bash
# Install the monedula daemon as a launchd calendar agent.
# Runs once daily at 07:00 UTC (09:00 Madrid summer / 08:00 winter).
set -euo pipefail

PLIST="com.markmhendrickson.monedula.plist"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LAUNCH_AGENTS="$HOME/Library/LaunchAgents"
DEST="$LAUNCH_AGENTS/$PLIST"

mkdir -p "$LAUNCH_AGENTS"

# Unload monedula if already installed.
if launchctl list 2>/dev/null | grep -q "com.markmhendrickson.monedula"; then
  echo "Unloading existing monedula agent..."
  launchctl unload "$DEST" 2>/dev/null || true
fi

# Render through the shared renderer rather than copying: it leaves
# credential-named variables out of the installed plist (daemons load them
# from the secrets store at start) and refuses a template it cannot render
# safely.
python3 "$SCRIPT_DIR/../../scripts/render_daemon_plist.py" "$SCRIPT_DIR/$PLIST" "$DEST"
launchctl load "$DEST"

echo "✓ monedula installed."
echo "  Schedule: daily at 07:00 UTC (09:00 Madrid summer / 08:00 winter)"
echo "  Logs: $HOME/Library/Logs/ateles/monedula.log"
echo ""
echo "To uninstall:"
echo "  launchctl unload $DEST && rm $DEST"
