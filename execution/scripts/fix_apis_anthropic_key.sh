#!/usr/bin/env bash
# fix_apis_anthropic_key.sh — give the Apis daemon a long-lived ANTHROPIC_API_KEY
# so its `claude --print` review panel stops 401ing on an expired OAuth session.
#
# Reads the key from 1Password (never echoed / never in shell history), appends
# it idempotently to ~/.config/neotoma/.env (the file apis.py loads at startup),
# and reloads the Apis daemon. Operator-run on the host.
#
# Prereqs: `op signin` done in this shell (eval "$(op signin)").
# Usage:   bash execution/scripts/fix_apis_anthropic_key.sh
set -euo pipefail

ENV_FILE="$HOME/.config/neotoma/.env"
PLIST="$HOME/Library/LaunchAgents/com.ateles.apis.plist"
# Anthropic/Claude 1Password item — the shared api key field (openclaw/neotoma/ateles).
OP_ITEM="Anthropic / Claude"
OP_FIELD="api key – shared (openclaw, neotoma, ateles)"

command -v op >/dev/null || { echo "FATAL: 1Password CLI 'op' not found."; exit 1; }
op whoami >/dev/null 2>&1 || { echo "FATAL: not signed in to op. Run:  eval \"\$(op signin)\""; exit 1; }

echo "Reading ANTHROPIC_API_KEY from 1Password ($OP_ITEM)…"
KEY="$(op item get "$OP_ITEM" --fields label="$OP_FIELD" --reveal 2>/dev/null)"
[ -n "$KEY" ] || { echo "FATAL: could not read the key field from 1Password — check item/field names."; exit 1; }
case "$KEY" in sk-ant-*) : ;; *) echo "WARN: value doesn't look like an Anthropic key (expected sk-ant-…). Aborting to be safe."; exit 1;; esac

# Idempotent: replace an existing line or append. Never prints the key.
touch "$ENV_FILE"; chmod 600 "$ENV_FILE"
tmp="$(mktemp)"; grep -v '^ANTHROPIC_API_KEY=' "$ENV_FILE" > "$tmp" || true
printf 'ANTHROPIC_API_KEY=%s\n' "$KEY" >> "$tmp"
mv "$tmp" "$ENV_FILE"; chmod 600 "$ENV_FILE"
echo "ANTHROPIC_API_KEY written to $ENV_FILE (key not displayed)."

echo "Reloading com.ateles.apis…"
launchctl bootout "gui/$(id -u)/com.ateles.apis" 2>/dev/null || launchctl unload "$PLIST" 2>/dev/null || true
sleep 1
launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null || launchctl load "$PLIST" 2>/dev/null || true
echo "Reloaded. Verify the next PR review aggregates a real verdict; watch logs:"
echo "  tail -f /tmp/com.ateles.apis.err.log"
