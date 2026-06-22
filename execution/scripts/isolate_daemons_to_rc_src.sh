#!/usr/bin/env bash
# isolate_daemons_to_rc_src.sh — repoint every Ateles daemon off the shared
# interactive checkout (~/repos/ateles) onto the stable RC checkout
# (~/ateles-rc-src), eliminating the branch-flip / stale-code collision between
# interactive git work and running daemons. HOST-SIDE, operator-run.
#
# Safe by construction: backs up each plist, rewrites the path in a copy,
# validates with plutil, and reloads ONE daemon at a time so a bad rewrite
# can't take down the whole swarm. Dry-run by default.
#
# Usage:
#   bash execution/scripts/isolate_daemons_to_rc_src.sh            # dry run (show plan)
#   bash execution/scripts/isolate_daemons_to_rc_src.sh --apply    # rewrite + reload
set -euo pipefail

SHARED="${ATELES_SHARED_CHECKOUT:-$HOME/repos/ateles}"
RC="${ATELES_REPO_PATH:-$HOME/ateles-rc-src}"
LA="$HOME/Library/LaunchAgents"
APPLY="${1:-}"
BACKUP="$HOME/.config/ateles/plist-backups/$(date +%Y%m%d-%H%M%S 2>/dev/null || echo manual)"

if [ ! -d "$RC" ]; then echo "FATAL: $RC missing — create the RC checkout first."; exit 1; fi

shopt -s nullglob
changed=0
for plist in "$LA"/com.ateles.*.plist; do
  nm=$(basename "$plist")
  # Skip dangling symlinks / unreadable files, and plists that don't
  # reference the shared checkout.
  [ -f "$plist" ] && [ -r "$plist" ] || continue
  if ! grep -q "$SHARED/" "$plist" 2>/dev/null; then continue; fi
  changed=$((changed+1))
  echo "── $nm references shared checkout"
  if [ "$APPLY" = "--apply" ]; then
    mkdir -p "$BACKUP"; cp "$plist" "$BACKUP/$nm"
    tmp=$(mktemp)
    sed "s#$SHARED/#$RC/#g" "$plist" > "$tmp"
    if ! plutil -lint "$tmp" >/dev/null 2>&1; then echo "  ABORT: rewritten plist invalid, leaving $nm untouched"; rm -f "$tmp"; continue; fi
    label="${nm%.plist}"
    launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || launchctl unload "$plist" 2>/dev/null || true
    mv "$tmp" "$plist"
    launchctl bootstrap "gui/$(id -u)" "$plist" 2>/dev/null || launchctl load "$plist" 2>/dev/null || true
    echo "  reloaded $label from RC (backup: $BACKUP/$nm)"
    sleep 1
  fi
done

if [ "$APPLY" != "--apply" ]; then
  echo; echo "DRY RUN — $changed daemon(s) still on the shared checkout. Re-run with --apply to repoint + reload."
else
  echo; echo "Done — $changed daemon(s) repointed to $RC. Backups in $BACKUP."
  echo "Verify: tail the daemon logs and confirm SSE reconnect; rollback = restore from $BACKUP and reload."
fi
