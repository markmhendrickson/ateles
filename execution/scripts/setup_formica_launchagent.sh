#!/usr/bin/env bash
# Install LaunchAgent for Formica (Neotoma issue daemon) against prod HTTP API.
#
# Prereq: ateles repo **`.env`** at `$REPO_ROOT/.env` with at least **`NEOTOMA_BEARER_TOKEN`**
# (and any other keys Formica needs — same file as the rest of the repo). Runtime loads
# that file via **`execution/daemons/formica/load_ateles_repo_env.sh`**. Optional machine
# overrides: set **`FORMICA_ENV_FILE`** in the plist or environment to source a second file.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
PLIST_TEMPLATE="$SCRIPT_DIR/com.markmhendrickson.formica.plist"
PLIST_NAME="com.markmhendrickson.formica.plist"
LAUNCH_AGENTS_DIR="${HOME}/Library/LaunchAgents"
INSTALLED_PLIST="$LAUNCH_AGENTS_DIR/$PLIST_NAME"
LOG_DIR="$REPO_ROOT/data/logs"

echo "Formica LaunchAgent setup"
echo "========================"

if [[ ! -f "$PLIST_TEMPLATE" ]]; then
  echo "Error: missing $PLIST_TEMPLATE" >&2
  exit 1
fi

if [[ ! -x "$SCRIPT_DIR/run_formica_launchd.sh" ]]; then
  chmod +x "$SCRIPT_DIR/run_formica_launchd.sh"
fi

mkdir -p "$LOG_DIR" "$LAUNCH_AGENTS_DIR"

if [[ -f "$INSTALLED_PLIST" ]]; then
  echo "Unloading existing agent..."
  launchctl unload "$INSTALLED_PLIST" 2>/dev/null || true
fi

echo "Writing $INSTALLED_PLIST"
sed -e "s|@ATELES_ROOT@|$REPO_ROOT|g" "$PLIST_TEMPLATE" >"$INSTALLED_PLIST"

echo "Loading LaunchAgent..."
launchctl load "$INSTALLED_PLIST"

echo ""
echo "Installed. Logs:"
echo "  tail -f $LOG_DIR/formica_launchd.log"
echo "  tail -f $LOG_DIR/formica_launchd.error.log"
echo ""
echo "Unload: launchctl unload \"$INSTALLED_PLIST\""
