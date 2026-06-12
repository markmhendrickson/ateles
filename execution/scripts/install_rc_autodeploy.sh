#!/usr/bin/env bash
#
# install_rc_autodeploy.sh — provision the Ateles "rolling main = RC" deploy
# checkout and its autodeploy LaunchAgent on this machine.
#
# What it sets up (mirrors Neotoma's ~/neotoma-rc-src + com.neotoma.rc-autodeploy):
#   1. ~/ateles-rc-src — a clean checkout pinned to origin/main, the ONLY tree
#      the T3 daemons run from (never the dev checkout, whose branch churns).
#   2. ~/ateles-rc-src/.venv — daemon runtime venv from daemon-requirements.txt.
#   3. com.ateles.rc-autodeploy LaunchAgent — polls origin/main every 120s and
#      fast-forwards + hard-restarts the daemons via redeploy_daemons_from_main.sh.
#
# It does NOT repoint the per-daemon plists (com.ateles.{apis,formica,
# neotoma-agent}); those are machine-local and must point their
# ProgramArguments at ~/ateles-rc-src and set ATELES_PRIVATE_KEYS_DIR to the
# operator's real ateles-private/keys (the deploy checkout has no sibling
# overlay). See docs/daemon_rc_autodeploy.md.
#
# Idempotent: safe to re-run.

set -euo pipefail

REPO_REMOTE="${REPO_REMOTE:-https://github.com/markmhendrickson/ateles.git}"
RC_DIR="${RC_DIR:-$HOME/ateles-rc-src}"
BRANCH="${BRANCH:-main}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LAUNCH_AGENTS="$HOME/Library/LaunchAgents"
PLIST_LABEL="com.ateles.rc-autodeploy"
PLIST_DEST="$LAUNCH_AGENTS/$PLIST_LABEL.plist"

echo "==> Provisioning deploy checkout at $RC_DIR"
if [ ! -d "$RC_DIR/.git" ]; then
  git clone --branch "$BRANCH" "$REPO_REMOTE" "$RC_DIR"
else
  git -C "$RC_DIR" fetch origin "$BRANCH" --quiet
  git -C "$RC_DIR" reset --hard "origin/$BRANCH" --quiet
fi

echo "==> Provisioning daemon venv"
if [ ! -x "$RC_DIR/.venv/bin/python3" ]; then
  python3 -m venv "$RC_DIR/.venv"
fi
"$RC_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$RC_DIR/.venv/bin/pip" install --quiet -r "$RC_DIR/execution/scripts/daemon-requirements.txt"

echo "==> Installing $PLIST_LABEL LaunchAgent"
mkdir -p "$LAUNCH_AGENTS"
sed "s|{{HOME}}|$HOME|g" \
  "$SCRIPT_DIR/com.ateles.rc-autodeploy.plist.template" > "$PLIST_DEST"

if launchctl list | grep -q "$PLIST_LABEL" 2>/dev/null; then
  launchctl unload "$PLIST_DEST" 2>/dev/null || true
fi
launchctl load "$PLIST_DEST"

echo "==> Done."
echo "    Deploy checkout: $RC_DIR ($(git -C "$RC_DIR" rev-parse --short HEAD))"
echo "    Autodeploy polls origin/$BRANCH every 120s; logs at /tmp/$PLIST_LABEL.{log,err}"
echo ""
echo "    Next: point the per-daemon plists at \$RC_DIR and set"
echo "    ATELES_PRIVATE_KEYS_DIR=\$HOME/repos/ateles-private/keys in each."
