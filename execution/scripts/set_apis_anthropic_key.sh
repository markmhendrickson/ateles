#!/usr/bin/env bash
# set_apis_anthropic_key.sh — give the Apis daemon a long-lived ANTHROPIC_API_KEY
# WITHOUT 1Password CLI (for remote/headless hosts where the op desktop-app
# integration can't unlock). You supply the key via the ANTHROPIC_API_KEY env
# var (read it in so it isn't typed into shell history), and this writes it
# idempotently to ~/.config/neotoma/.env (the file apis.py loads at startup,
# so the `claude --print` review panel inherits it) and reloads Apis.
#
# Usage (key not echoed to history):
#   read -rs ANTHROPIC_API_KEY; export ANTHROPIC_API_KEY   # paste key, press enter
#   bash execution/scripts/set_apis_anthropic_key.sh
#
# (Prefer fix_apis_anthropic_key.sh when the op CLI is usable — it pulls from
#  1Password directly. This variant is the remote/headless fallback.)
set -euo pipefail

ENV_FILE="$HOME/.config/neotoma/.env"
PLIST="$HOME/Library/LaunchAgents/com.ateles.apis.plist"

KEY="${ANTHROPIC_API_KEY:-}"
[ -n "$KEY" ] || { echo "FATAL: set ANTHROPIC_API_KEY first, e.g.:  read -rs ANTHROPIC_API_KEY; export ANTHROPIC_API_KEY"; exit 1; }
case "$KEY" in sk-ant-*) : ;; *) echo "WARN: value doesn't look like an Anthropic key (expected sk-ant-…). Aborting."; exit 1;; esac

touch "$ENV_FILE"; chmod 600 "$ENV_FILE"
tmp="$(mktemp)"; grep -v '^ANTHROPIC_API_KEY=' "$ENV_FILE" > "$tmp" || true
printf 'ANTHROPIC_API_KEY=%s\n' "$KEY" >> "$tmp"
mv "$tmp" "$ENV_FILE"; chmod 600 "$ENV_FILE"
echo "ANTHROPIC_API_KEY written to $ENV_FILE (key not displayed)."

echo "Reloading com.ateles.apis…"
launchctl bootout "gui/$(id -u)/com.ateles.apis" 2>/dev/null || launchctl unload "$PLIST" 2>/dev/null || true
sleep 1
launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null || launchctl load "$PLIST" 2>/dev/null || true
echo "Reloaded. Watch the next PR review aggregate a real verdict:"
echo "  tail -f /tmp/com.ateles.apis.err.log"
echo "Tip: also clear the exported key from your shell:  unset ANTHROPIC_API_KEY"
