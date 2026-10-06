#!/usr/bin/env bash
# Install Cotinga launchd agent (runs daily at 05:30 Madrid time).
# Run once from the cotinga/ directory.
set -euo pipefail

PLIST="com.ateles.cotinga.plist"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="$HOME/Library/LaunchAgents/$PLIST"

# Render through the shared renderer rather than copying: it leaves
# credential-named variables out of the installed plist (daemons load them
# from the secrets store at start) and refuses a template it cannot render
# safely.
python3 "$SCRIPT_DIR/../../scripts/render_daemon_plist.py" "$SCRIPT_DIR/$PLIST" "$DEST"
launchctl load "$DEST"
echo "Cotinga installed and loaded. Next run: 05:30 tomorrow."
echo "To test immediately: launchctl start com.ateles.cotinga"
echo "Logs: ~/Library/Logs/ateles/cotinga.log"
