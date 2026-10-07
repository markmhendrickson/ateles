#!/usr/bin/env bash
# Shared launchd plist install step for the daemon installers.
#
# Source this file, then call:
#
#   install_rendered_plist NAME LABEL TEMPLATE DEST
#
# The order is the point. The template is rendered and validated into a
# scratch file FIRST, while any running agent is untouched. Only a plist that
# rendered cleanly is allowed to replace the installed one, and only then is
# the running agent unloaded, the new plist moved into place and the agent
# loaded again.
#
#   * Render refused: nothing is unloaded and the installed plist is not
#     modified. The running agent keeps running. Exit status 1.
#   * Load refused after the swap: the previous plist is put back and loaded
#     again, and the message says whether that restored the agent. Exit
#     status 1 either way, with a recovery command if it is not running.
#
# Requires the caller to set SCRIPT_DIR (the installer's own directory). The
# renderer path can be overridden with ATELES_PLIST_RENDERER (tests only).

_ateles_plist_renderer() {
  printf '%s' "${ATELES_PLIST_RENDERER:-$SCRIPT_DIR/../../scripts/render_daemon_plist.py}"
}

_ateles_agent_loaded() {
  local listing
  listing="$(launchctl list 2>/dev/null || true)"
  grep -q -- "$1" <<<"$listing"
}

install_rendered_plist() {
  local name="$1" label="$2" template="$3" dest="$4"
  local dir rendered backup=""
  dir="$(dirname "$dest")"
  mkdir -p "$dir"

  rendered="$(mktemp "$dir/.${name}-render.XXXXXX")" || return 1
  if ! python3 "$(_ateles_plist_renderer)" "$template" "$rendered"; then
    rm -f "$rendered"
    echo "ERROR: $name was not installed: its plist template could not be rendered safely." >&2
    echo "       Nothing was changed. Any running $name agent was left untouched and keeps running." >&2
    return 1
  fi

  if [ -f "$dest" ]; then
    backup="$(mktemp "$dir/.${name}-previous.XXXXXX")" || {
      rm -f "$rendered"
      echo "ERROR: $name was not installed: could not keep a copy of the current plist. Nothing was changed." >&2
      return 1
    }
    cp -p "$dest" "$backup"
  fi

  if _ateles_agent_loaded "$label"; then
    echo "Unloading existing $name agent..."
    launchctl unload "$dest" 2>/dev/null || true
  fi
  mv -f "$rendered" "$dest"

  if launchctl load "$dest"; then
    if [ -n "$backup" ]; then rm -f "$backup"; fi
    return 0
  fi

  echo "ERROR: launchctl could not load the new $name plist." >&2
  if [ -n "$backup" ]; then
    mv -f "$backup" "$dest"
    if launchctl load "$dest"; then
      echo "       The previous plist was restored and loaded: $name is running as before." >&2
    else
      echo "       The previous plist was restored but could not be loaded: $name is NOT running." >&2
      echo "       Recover with: launchctl load $dest" >&2
    fi
  else
    rm -f "$dest"
    echo "       There was no previous plist: $name is NOT running." >&2
    echo "       Fix the cause and re-run this installer." >&2
  fi
  return 1
}
