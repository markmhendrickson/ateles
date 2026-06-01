#!/bin/bash
# Setup script for audio transcription watcher LaunchAgent
# This installs the LaunchAgent to run the watcher on system startup

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLIST_FILE="$SCRIPT_DIR/com.finances.audio-transcription-watcher.plist"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
INSTALLED_PLIST="$LAUNCH_AGENTS_DIR/com.finances.audio-transcription-watcher.plist"

echo "Setting up audio transcription watcher LaunchAgent..."

# Check if plist file exists
if [ ! -f "$PLIST_FILE" ]; then
    echo "Error: Plist file not found: $PLIST_FILE" >&2
    exit 1
fi

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
echo "The audio transcription watcher will now start automatically on system startup."
echo ""
echo "Useful commands:"
echo "  Check status:   launchctl list | grep audio-transcription"
echo "  Stop watcher:   launchctl unload $INSTALLED_PLIST"
echo "  Start watcher:  launchctl load $INSTALLED_PLIST"
echo "  View logs:      tail -f $REPO_ROOT/data/logs/audio_transcription_watcher.log"
echo "  View errors:    tail -f $REPO_ROOT/data/logs/audio_transcription_watcher.error.log"
echo ""



