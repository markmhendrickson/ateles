#!/bin/bash
# Setup script for Asana sync LaunchAgent
# This installs the LaunchAgent to run the sync service on system startup

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLIST_FILE="$SCRIPT_DIR/com.finances.asana-sync.plist"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
INSTALLED_PLIST="$LAUNCH_AGENTS_DIR/com.finances.asana-sync.plist"

echo "Setting up Asana sync LaunchAgent..."

# Check if plist file exists
if [ ! -f "$PLIST_FILE" ]; then
    echo "Error: Plist file not found: $PLIST_FILE" >&2
    exit 1
fi

# Check if wrapper script exists and is executable
WRAPPER_SCRIPT="$SCRIPT_DIR/asana_sync_wrapper.sh"
if [ ! -f "$WRAPPER_SCRIPT" ]; then
    echo "Error: Wrapper script not found: $WRAPPER_SCRIPT" >&2
    exit 1
fi
chmod +x "$WRAPPER_SCRIPT"

# Create LaunchAgents directory if it doesn't exist
mkdir -p "$LAUNCH_AGENTS_DIR"

# Unload existing agent if running
if [ -f "$INSTALLED_PLIST" ]; then
    echo "Unloading existing LaunchAgent..."
    launchctl unload "$INSTALLED_PLIST" 2>/dev/null || true
fi

# Auto-detect repo root
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Generate plist file with correct paths
echo "Generating LaunchAgent plist with repo path: $REPO_ROOT"
sed "s|/Users/markmhendrickson/Projects/personal|$REPO_ROOT|g" "$PLIST_FILE" > "$INSTALLED_PLIST"

# Load the LaunchAgent
echo "Loading LaunchAgent..."
launchctl load "$INSTALLED_PLIST"

echo ""
echo "Setup complete!"
echo ""
echo "The Asana sync service will now start automatically on system startup."
echo "It runs in daemon mode, syncing every 60 seconds."
echo ""
echo "Useful commands:"
echo "  Check status:   launchctl list | grep asana-sync"
echo "  Stop service:   launchctl unload $INSTALLED_PLIST"
echo "  Start service:  launchctl load $INSTALLED_PLIST"
echo "  View logs:      tail -f $REPO_ROOT/data/logs/asana_sync.log"
echo "  View errors:    tail -f $REPO_ROOT/data/logs/asana_sync.error.log"
echo ""
echo "To run a one-time sync instead:"
echo "  python scripts/sync_asana_tasks.py"
echo ""








