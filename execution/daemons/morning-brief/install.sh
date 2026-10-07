#!/usr/bin/env bash
# Install morning-brief launchd agent (runs daily at 05:30 Madrid time).
# Run once from the morning-brief/ directory.
set -euo pipefail

PLIST="com.ateles.morning-brief.plist"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="$HOME/Library/LaunchAgents/$PLIST"

# Render and validate first, then swap: the shared step renders through the
# credential-free renderer into a scratch file, and only a clean render
# unloads the running agent, replaces the plist and loads it again. A refused
# template leaves the running agent and the installed plist untouched.
source "$SCRIPT_DIR/../_install_plist.sh"
install_rendered_plist "morning-brief" "com.ateles.morning-brief" "$SCRIPT_DIR/$PLIST" "$DEST"
echo "Morning Brief installed and loaded. Next run: 05:30 tomorrow."
echo "To test immediately: launchctl start com.ateles.morning-brief"
echo "Logs: ~/Library/Logs/ateles/morning-brief.log"
