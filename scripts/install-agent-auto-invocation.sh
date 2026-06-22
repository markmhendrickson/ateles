#!/usr/bin/env bash
#
# Install (or remove) device-wide topic-based agent auto-invocation.
#
# "Device-wide" means: any Claude Code session on this machine — in any repo —
# gets the UserPromptSubmit nudge, AND the panel agents' slash commands resolve
# everywhere (not just inside the ateles checkout).
#
# Design: user-level config *points at* this ateles checkout rather than copying
# files. So the ateles repo stays the single source of truth — repo pulls and
# Apus SKILL.md re-mirrors are picked up automatically, with nothing to keep in
# sync.
#
#   ~/.claude/settings.json   <- UserPromptSubmit hook, absolute path to the
#                                hook script in this checkout (merged, not
#                                overwritten).
#   ~/.claude/skills/<name>   <- symlink -> <ateles>/.claude/skills/<name>, for
#                                each panel agent listed in agent-routing.json.
#
# The set of installed skills is read from agent-routing.json, so it always
# matches what the hook can actually recommend.
#
# Usage:
#   scripts/install-agent-auto-invocation.sh            # install
#   scripts/install-agent-auto-invocation.sh --uninstall
#
# Idempotent: re-running is safe and makes no duplicate changes.

set -euo pipefail

ATELES_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOOK_SCRIPT="$ATELES_ROOT/.claude/hooks/agent_auto_invocation.py"
MANIFEST="$ATELES_ROOT/.claude/agent-routing.json"
SKILLS_SRC="$ATELES_ROOT/.claude/skills"

CLAUDE_HOME="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
SETTINGS="$CLAUDE_HOME/settings.json"
SKILLS_DST="$CLAUDE_HOME/skills"

HOOK_CMD="python3 \"$HOOK_SCRIPT\""

need() { command -v "$1" >/dev/null 2>&1 || { echo "error: '$1' is required but not found" >&2; exit 1; }; }
need jq
need python3

[ -f "$HOOK_SCRIPT" ] || { echo "error: hook script not found at $HOOK_SCRIPT" >&2; exit 1; }
[ -f "$MANIFEST" ]    || { echo "error: manifest not found at $MANIFEST" >&2; exit 1; }

mapfile -t AGENTS < <(jq -r '.agents[].name' "$MANIFEST")

uninstall() {
  echo "Removing device-wide agent auto-invocation..."
  if [ -f "$SETTINGS" ]; then
    tmp="$(mktemp)"
    jq --arg cmd "$HOOK_CMD" '
      if .hooks.UserPromptSubmit then
        .hooks.UserPromptSubmit |= map(
          .hooks |= map(select(.command != $cmd))
        ) | .hooks.UserPromptSubmit |= map(select((.hooks | length) > 0))
      else . end
    ' "$SETTINGS" > "$tmp" && mv "$tmp" "$SETTINGS"
    echo "  - removed hook from $SETTINGS"
  fi
  for name in "${AGENTS[@]}"; do
    link="$SKILLS_DST/$name"
    if [ -L "$link" ]; then rm "$link"; echo "  - unlinked $link"; fi
  done
  echo "Done."
}

install() {
  echo "Installing device-wide agent auto-invocation (source of truth: $ATELES_ROOT)"
  mkdir -p "$CLAUDE_HOME" "$SKILLS_DST"

  # 1) Merge the hook into ~/.claude/settings.json without clobbering existing
  #    settings, and without adding a duplicate if it's already there.
  [ -f "$SETTINGS" ] || echo '{}' > "$SETTINGS"
  tmp="$(mktemp)"
  jq --arg cmd "$HOOK_CMD" '
    .hooks //= {} |
    .hooks.UserPromptSubmit //= [] |
    if any(.hooks.UserPromptSubmit[]?; .hooks[]?.command == $cmd)
    then .
    else .hooks.UserPromptSubmit += [ { "hooks": [ { "type": "command", "command": $cmd, "timeout": 10 } ] } ]
    end
  ' "$SETTINGS" > "$tmp" && mv "$tmp" "$SETTINGS"
  echo "  - hook registered in $SETTINGS"

  # 2) Symlink each panel agent skill into ~/.claude/skills so /<name> resolves
  #    in any repo. Skip safely if a real (non-symlink) skill already exists.
  for name in "${AGENTS[@]}"; do
    src="$SKILLS_SRC/$name"
    dst="$SKILLS_DST/$name"
    if [ ! -d "$src" ]; then
      echo "  ! skipping '$name' (no skill dir at $src)"
      continue
    fi
    if [ -L "$dst" ]; then
      ln -sfn "$src" "$dst"; echo "  - relinked skill '$name'"
    elif [ -e "$dst" ]; then
      echo "  ! skipping '$name' (a non-symlink already exists at $dst)"
    else
      ln -s "$src" "$dst"; echo "  - linked skill '$name' -> $src"
    fi
  done

  echo
  echo "Done. Restart Claude Code sessions to pick up the new hook."
  echo "Note: keep the project-level .claude/settings.json UNregistered to avoid"
  echo "      the hook firing twice inside the ateles repo."
}

case "${1:-}" in
  --uninstall|-u) uninstall ;;
  ""|--install)   install ;;
  *) echo "usage: $0 [--install|--uninstall]" >&2; exit 2 ;;
esac
