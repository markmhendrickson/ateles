#!/usr/bin/env bash
# Install Mimus launchd agent (runs daily at 06:00 Madrid time).
# Run once from the mimus/ directory.
set -euo pipefail

PLIST="com.ateles.mimus.plist"
DEST="$HOME/Library/LaunchAgents/$PLIST"

cp "$PLIST" "$DEST"
launchctl load "$DEST"
echo "Mimus installed and loaded. Next run: 06:00 tomorrow."
echo "To test immediately: launchctl start com.ateles.mimus"
echo "Logs: ~/Library/Logs/ateles/mimus.log"
