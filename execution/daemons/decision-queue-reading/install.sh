#!/usr/bin/env bash
# Install the daily decision queue reading launchd agent.
# Renders and validates the template first; only a clean render replaces the
# installed plist (see ../_install_plist.sh).
set -euo pipefail

TEMPLATE="com.ateles.decision-queue-reading.plist.tmpl"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="$HOME/Library/LaunchAgents/com.ateles.decision-queue-reading.plist"

source "$SCRIPT_DIR/../_install_plist.sh"
install_rendered_plist "decision-queue-reading" "com.ateles.decision-queue-reading" "$SCRIPT_DIR/$TEMPLATE" "$DEST"
ateles_install_summary "decision-queue-reading"
echo "Schedule: daily at 07:30 local time."
echo "The reading is published to ~/.local/state/ateles/decision-queue-reading.json and stored as two metric_reading records. This job sends nothing to anyone."
echo "Dry run: python3 decision_queue_reading.py --once --dry-run"
echo "Logs: ~/Library/Logs/ateles/decision-queue-reading.log"