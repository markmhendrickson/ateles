#!/bin/bash
# Sets up automatic AirDrop-to-Desktop service using launchd.
# This creates a launchd plist that runs the move_airdrop_to_desktop.py script
# automatically when you log in.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PYTHON_SCRIPT="$REPO_ROOT/scripts/move_airdrop_to_desktop.py"
PLIST_NAME="com.markmhendrickson.airdrop-to-desktop"
PLIST_PATH="$HOME/Library/LaunchAgents/${PLIST_NAME}.plist"

# Create plist content
cat > "$PLIST_PATH" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${PLIST_NAME}</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/python3</string>
        <string>${PYTHON_SCRIPT}</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>${HOME}/Library/Logs/${PLIST_NAME}.log</string>
    <key>StandardErrorPath</key>
    <string>${HOME}/Library/Logs/${PLIST_NAME}.error.log</string>
</dict>
</plist>
EOF

# Load the service
launchctl unload "$PLIST_PATH" 2>/dev/null || true
launchctl load "$PLIST_PATH"

echo "✓ AirDrop-to-Desktop service installed and started"
echo "  Service will run automatically on login"
echo "  Logs: ~/Library/Logs/${PLIST_NAME}.log"
echo ""
echo "To stop the service:"
echo "  launchctl unload $PLIST_PATH"
echo ""
echo "To start manually:"
echo "  launchctl load $PLIST_PATH"
