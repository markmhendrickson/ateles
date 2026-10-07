#!/usr/bin/env bash
# Install the monedula daemon as a launchd calendar agent.
# Runs once daily at 07:00 UTC (09:00 Madrid summer / 08:00 winter).
set -euo pipefail

PLIST="com.markmhendrickson.monedula.plist"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LAUNCH_AGENTS="$HOME/Library/LaunchAgents"
DEST="$LAUNCH_AGENTS/$PLIST"

mkdir -p "$LAUNCH_AGENTS"

# Render and validate first, then swap: the shared step renders through the
# credential-free renderer into a scratch file, and only a clean render
# unloads the running agent, replaces the plist and loads it again. A refused
# template leaves the running agent and the installed plist untouched.
source "$SCRIPT_DIR/../_install_plist.sh"
install_rendered_plist "monedula" "com.markmhendrickson.monedula" "$SCRIPT_DIR/$PLIST" "$DEST"

echo "✓ monedula installed."
echo "  Schedule: daily at 07:00 UTC (09:00 Madrid summer / 08:00 winter)"
echo "  Logs: $HOME/Library/Logs/ateles/monedula.log"
echo ""
echo "To uninstall:"
echo "  launchctl unload $DEST && rm $DEST"
