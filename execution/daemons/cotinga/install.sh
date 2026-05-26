#!/usr/bin/env bash
# Install Cotinga launchd agent (runs daily at 05:30 Madrid time).
# Run once from the cotinga/ directory.
set -euo pipefail

PLIST="com.ateles.cotinga.plist"
DEST="$HOME/Library/LaunchAgents/$PLIST"

cp "$PLIST" "$DEST"
launchctl load "$DEST"
echo "Cotinga installed and loaded. Next run: 05:30 tomorrow."
echo "To test immediately: launchctl start com.ateles.cotinga"
echo "Logs: ~/Library/Logs/ateles/cotinga.log"
