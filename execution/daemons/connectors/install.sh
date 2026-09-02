#!/usr/bin/env bash
# Install the connector daemon as a launchd resident agent.
#
# This script is the point of the whole stage. The library it starts is only
# worth having if something runs it — `sync_issues` is an MCP tool with no
# daemon and no scheduled caller, and it has never once run.
set -euo pipefail

PLIST="com.ateles.connectors.plist"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
LAUNCH_AGENTS="$HOME/Library/LaunchAgents"
DEST="$LAUNCH_AGENTS/$PLIST"
DOMAIN="gui/$UID"

mkdir -p "$LAUNCH_AGENTS"
mkdir -p "$HOME/Library/Logs/ateles"

PLIST_SRC="$SCRIPT_DIR/$PLIST"
if [[ ! -f "$PLIST_SRC" ]]; then
  echo "ERROR: missing launchd plist: $PLIST_SRC" >&2
  echo "Expected $PLIST committed alongside install.sh." >&2
  echo "Run from repo root after pulling latest, or install manually:" >&2
  echo "  python3 $SCRIPT_DIR/connectors_daemon.py --once   # one pass, no schedule" >&2
  exit 1
fi

# Unload if already installed, so re-running this is safe.
if launchctl list 2>/dev/null | grep -q "com.ateles.connectors"; then
  echo "Unloading existing connectors agent..."
  launchctl bootout "$DOMAIN" "$DEST" 2>/dev/null || launchctl unload "$DEST" 2>/dev/null || true
fi

TMP_PLIST="$(mktemp)"
python3 - "$PLIST_SRC" "$TMP_PLIST" "$REPO_ROOT" "$HOME" <<'PY'
import plistlib
import sys

src, dest, repo_root, home = sys.argv[1:]
with open(src, "rb") as fh:
    plist = plistlib.load(fh)

plist["ProgramArguments"] = [
    f"{repo_root}/.venv/bin/python3",
    f"{repo_root}/execution/daemons/connectors/connectors_daemon.py",
]
plist["WorkingDirectory"] = repo_root
plist["StandardOutPath"] = f"{home}/Library/Logs/ateles/connectors.log"
plist["StandardErrorPath"] = f"{home}/Library/Logs/ateles/connectors.log"
plist.setdefault("EnvironmentVariables", {})["HOME"] = home

with open(dest, "wb") as fh:
    plistlib.dump(plist, fh)
PY
cp "$TMP_PLIST" "$DEST"
rm -f "$TMP_PLIST"
chmod 0644 "$DEST"
if ! launchctl bootstrap "$DOMAIN" "$DEST"; then
  echo "ERROR: launchctl failed to load com.ateles.connectors." >&2
  echo "Inspect: launchctl print $DOMAIN/com.ateles.connectors" >&2
  echo "Logs: $HOME/Library/Logs/ateles/connectors.log" >&2
  exit 1
fi

if ! launchctl list 2>/dev/null | grep -q "com.ateles.connectors"; then
  echo "ERROR: launchctl reported success but com.ateles.connectors is not listed." >&2
  echo "Inspect: launchctl print $DOMAIN/com.ateles.connectors" >&2
  echo "Logs: $HOME/Library/Logs/ateles/connectors.log" >&2
  exit 1
fi

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
echo "  launchctl bootout $DOMAIN $DEST && rm $DEST"
