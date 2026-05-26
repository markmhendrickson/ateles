#!/usr/bin/env bash
# Install morning-brief launchd agent (runs daily at 05:30 Madrid time).
# Run once from the morning-brief/ directory.
set -euo pipefail

PLIST="com.ateles.morning-brief.plist"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="$HOME/Library/LaunchAgents/$PLIST"

if launchctl list 2>/dev/null | grep -q "com.ateles.morning-brief"; then
    echo "Unloading existing morning-brief agent..."
    launchctl unload "$DEST" 2>/dev/null || true
fi

cp "$SCRIPT_DIR/$PLIST" "$DEST"
launchctl load "$DEST"
echo "Morning Brief installed and loaded. Next run: 05:30 tomorrow."
echo "To test immediately: launchctl start com.ateles.morning-brief"
echo "Logs: ~/Library/Logs/ateles/morning-brief.log"
