#!/bin/bash
# Setup script for Home Assistant LaunchAgent
# This installs the LaunchAgent to run Home Assistant on system startup

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLIST_FILE="$SCRIPT_DIR/com.homeassistant.server.plist"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
INSTALLED_PLIST="$LAUNCH_AGENTS_DIR/com.homeassistant.server.plist"

echo "Setting up Home Assistant LaunchAgent..."

# Check if plist file exists
if [ ! -f "$PLIST_FILE" ]; then
    echo "Error: Plist file not found: $PLIST_FILE" >&2
    exit 1
fi

# Auto-detect repo root
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Check if Home Assistant venv exists in repo
HASS_VENV="$REPO_ROOT/execution/homeassistant-venv/bin/hass"
if [ ! -f "$HASS_VENV" ]; then
    echo "Error: Home Assistant not found at: $HASS_VENV" >&2
    echo "Please install Home Assistant first." >&2
    exit 1
fi

DATA_DIR=$(python3 -c "
import os
import platform
from pathlib import Path
from dotenv import load_dotenv

# Load .env from repo root
env_file = Path('$REPO_ROOT') / '.env'
if env_file.exists():
    load_dotenv(env_file)

# Check for environment variable override first
env_data_dir = os.getenv('DATA_DIR')
if env_data_dir:
    print(Path(env_data_dir).expanduser())
    exit(0)

# Default to iCloud path on macOS
if platform.system() == 'Darwin':
    icloud_path = Path.home() / 'Library' / 'Mobile Documents' / 'com~apple~CloudDocs' / 'Documents' / 'data'
    print(icloud_path)
    exit(0)

# Fallback to project-relative path on other platforms
print(Path('$REPO_ROOT') / 'data')
")

# Create logs directory if it doesn't exist
LOGS_DIR="$DATA_DIR/logs"
mkdir -p "$LOGS_DIR"
echo "Using DATA_DIR: $DATA_DIR"
echo "Logs directory: $LOGS_DIR"

# Create LaunchAgents directory if it doesn't exist
mkdir -p "$LAUNCH_AGENTS_DIR"

# Unload existing agent if running
if [ -f "$INSTALLED_PLIST" ]; then
    echo "Unloading existing LaunchAgent..."
    launchctl unload "$INSTALLED_PLIST" 2>/dev/null || true
fi

# Generate plist file with correct paths
echo "Generating LaunchAgent plist..."
# Replace placeholder paths with actual repo root and DATA_DIR
sed -e "s|/Users/markmhendrickson/Projects/personal|$REPO_ROOT|g" \
    -e "s|/Users/markmhendrickson/Projects/personal/data/logs|$LOGS_DIR|g" \
    "$PLIST_FILE" > "$INSTALLED_PLIST"

# Load the LaunchAgent
echo "Loading LaunchAgent..."
launchctl load "$INSTALLED_PLIST"

echo ""
echo "Setup complete!"
echo ""
echo "Home Assistant will now start automatically on system startup."
echo ""
echo "Useful commands:"
echo "  Check status:   launchctl list | grep homeassistant"
echo "  Stop service:   launchctl unload $INSTALLED_PLIST"
echo "  Start service:  launchctl load $INSTALLED_PLIST"
echo "  View logs:      tail -f $LOGS_DIR/homeassistant.log"
echo "  View errors:    tail -f $LOGS_DIR/homeassistant.error.log"
echo ""
echo "Access Home Assistant:"
echo "  http://localhost:8123"
echo ""
