#!/usr/bin/env bash
# Install the hourly Codex delivery check launchd agent.
# Renders and validates the template first; only a clean render replaces the
# installed plist (see ../_install_plist.sh).
set -euo pipefail

TEMPLATE="com.ateles.codex-delivery-check.plist.tmpl"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="$HOME/Library/LaunchAgents/com.ateles.codex-delivery-check.plist"

source "$SCRIPT_DIR/../_install_plist.sh"
install_rendered_plist "codex-delivery-check" "com.ateles.codex-delivery-check" "$SCRIPT_DIR/$TEMPLATE" "$DEST"
ateles_install_summary "codex-delivery-check"
echo "Schedule: hourly, at seven minutes past."
echo "Dry run: python3 codex_delivery_check.py --once --dry-run"
echo "Logs: ~/Library/Logs/ateles/codex-delivery-check.log"
