#!/usr/bin/env bash
# Install the aquila daemon as a launchd calendar agent.
# Runs monthly on the 1st at 06:00 Madrid. Produces the cofounder report.
set -euo pipefail

PLIST="com.ateles.aquila.plist"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LAUNCH_AGENTS="$HOME/Library/LaunchAgents"
DEST="$LAUNCH_AGENTS/$PLIST"

mkdir -p "$LAUNCH_AGENTS"

# Render and validate first, then swap: the shared step renders through the
# credential-free renderer into a scratch file, and only a clean render
# unloads the running agent, replaces the plist and loads it again. A refused
# template leaves the running agent and the installed plist untouched.
source "$SCRIPT_DIR/../_install_plist.sh"
install_rendered_plist "aquila" "com.ateles.aquila" "$SCRIPT_DIR/$PLIST" "$DEST"

echo "✓ aquila installed."
echo "  Schedule: monthly on the 1st at 06:00 Madrid"
echo "  Logs: $HOME/Library/Logs/ateles/aquila.log"
echo ""
echo "Run on demand:"
echo "  python3 $SCRIPT_DIR/aquila.py --force"
echo ""
echo "To uninstall:"
echo "  launchctl unload $DEST && rm $DEST"
