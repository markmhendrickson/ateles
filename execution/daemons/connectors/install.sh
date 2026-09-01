#!/usr/bin/env bash
# Install the connector daemon as a launchd resident agent.
#
# This script is the point of the whole stage. The library it starts is only
# worth having if something runs it — `sync_issues` is an MCP tool with no
# daemon and no scheduled caller, and it has never once run.
set -euo pipefail

PLIST="com.ateles.connectors.plist"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LAUNCH_AGENTS="$HOME/Library/LaunchAgents"
DEST="$LAUNCH_AGENTS/$PLIST"

mkdir -p "$LAUNCH_AGENTS"
mkdir -p "$HOME/Library/Logs/ateles"

# Unload if already installed, so re-running this is safe.
if launchctl list 2>/dev/null | grep -q "com.ateles.connectors"; then
  echo "Unloading existing connectors agent..."
  launchctl unload "$DEST" 2>/dev/null || true
fi

cp "$SCRIPT_DIR/$PLIST" "$DEST"
launchctl load "$DEST"

echo "✓ connectors installed."
echo "  Cadence: every CONNECTOR_POLL_SECONDS (default 900s / 15 min)"
echo "  Logs: $HOME/Library/Logs/ateles/connectors.log"
echo ""
echo "Verify it is actually running (the thing that matters):"
echo "  launchctl list | grep com.ateles.connectors"
echo "  tail -f $HOME/Library/Logs/ateles/connectors.log"
echo ""
echo "Run one pass by hand, without touching the schedule:"
echo "  python3 $SCRIPT_DIR/connectors_daemon.py --once"
echo ""
echo "Limit which connectors run (unset means all):"
echo "  ATELES_CONNECTORS=fly python3 $SCRIPT_DIR/connectors_daemon.py --once"
echo ""
echo "To uninstall:"
echo "  launchctl unload $DEST && rm $DEST"
