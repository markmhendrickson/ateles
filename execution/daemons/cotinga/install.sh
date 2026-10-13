#!/usr/bin/env bash
# Install Cotinga launchd agent (runs daily at 05:30 Madrid time).
# Run once from the cotinga/ directory.
set -euo pipefail

PLIST="com.ateles.cotinga.plist"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="$HOME/Library/LaunchAgents/$PLIST"

# Render and validate first, then swap: the shared step renders through the
# credential-free renderer into a scratch file, and only a clean render
# unloads the running agent, replaces the plist and loads it again. Success is
# reported only for a job read back from launchd.
mkdir -p "$(dirname "$DEST")"
source "$SCRIPT_DIR/../_install_plist.sh"
install_rendered_plist "cotinga" "com.ateles.cotinga" "$SCRIPT_DIR/$PLIST" "$DEST"
ateles_install_summary "cotinga"
echo "Schedule: daily at 05:30."
echo "To test immediately: launchctl start com.ateles.cotinga"
echo "Logs: ~/Library/Logs/ateles/cotinga.log"
