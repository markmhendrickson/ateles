#!/usr/bin/env bash
# Install Strix (meeting/ambient audio recorder) as a launchd login agent.
set -euo pipefail

PLIST="com.ateles.strix.plist"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LAUNCH_AGENTS="$HOME/Library/LaunchAgents"
DEST="$LAUNCH_AGENTS/$PLIST"

mkdir -p "$LAUNCH_AGENTS"

# Render and validate first, then swap: the shared step renders through the
# credential-free renderer into a scratch file, and only a clean render
# unloads the running agent, replaces the plist and loads it again. A refused
# template leaves the running agent and the installed plist untouched.
source "$SCRIPT_DIR/../_install_plist.sh"
install_rendered_plist "strix" "com.ateles.strix" "$SCRIPT_DIR/$PLIST" "$DEST"

echo "✓ strix installed and started."
echo "  Menu bar icon: 🔴 recording  ⚫ idle"
echo "  Logs: /tmp/com.ateles.strix.{log,err}"
echo ""
echo "To uninstall:"
echo "  launchctl unload $DEST && rm $DEST"
