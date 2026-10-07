#!/usr/bin/env bash
# Shared launchd plist install step for the daemon installers.
#
# Source this file, then call:
#
#   install_rendered_plist NAME LABEL TEMPLATE DEST
#
# The order is the point. The template is rendered and validated into a
# scratch file FIRST, while any running agent is untouched. Only a plist that
# rendered cleanly is allowed to replace the installed one.
#
# Every statement about the job is read from `launchctl list` AFTER the
# operation it describes, never inferred from an exit status. The states are:
#
#   running (pid N)  - registered and a process is alive
#   registered       - registered with launchd and idle; a scheduled job
#                      waits here until its next trigger
#   not registered   - launchd does not list the job
#   unknown          - launchd could not be queried; nothing is claimed
#
# Outcomes:
#   * Render refused: nothing is unloaded and the installed plist is not
#     modified. Exit status 1.
#   * Unload failed (or the job is still listed afterwards): the update stops
#     before the installed plist is replaced; the job is left as it is and the
#     observed state is reported. Exit status 1.
#   * Load failed after the swap: the previous plist is put back and loaded
#     again; the message gives the configuration outcome and the OBSERVED job
#     state separately, with the next action for that state. Exit status 1.
#   * Success: the observed state is reported; a scheduled job is described as
#     registered, never as running.
#
# Requires the caller to set SCRIPT_DIR (the installer's own directory). The
# renderer path can be overridden with ATELES_PLIST_RENDERER (tests only).

_ateles_plist_renderer() {
  printf '%s' "${ATELES_PLIST_RENDERER:-$SCRIPT_DIR/../../scripts/render_daemon_plist.py}"
}

# Prints one of: "running (pid N)", "registered", "not registered", "unknown".
_ateles_job_state() {
  local label="$1" listing row pid
  if ! listing="$(launchctl list 2>/dev/null)"; then
    printf 'unknown'
    return 0
  fi
  row="$(awk -v label="$label" '$3 == label { print $1; exit }' <<<"$listing")"
  if [ -z "$row" ]; then
    printf 'not registered'
  elif [ "$row" = "-" ]; then
    printf 'registered'
  else
    pid="$row"
    printf 'running (pid %s)' "$pid"
  fi
}

# Wording for a state, for messages that describe what was observed.
_ateles_describe_state() {
  case "$1" in
    registered) printf 'registered (scheduled; runs at its next trigger)' ;;
    unknown) printf 'unknown (launchd could not be queried)' ;;
    *) printf '%s' "$1" ;;
  esac
}

# The recovery line that fits an observed state.
_ateles_next_action() {
  local name="$1" state="$2" dest="$3"
  case "$state" in
    "not registered")
      printf 'Next: launchctl load %s' "$dest" ;;
    unknown)
      printf 'Next: check `launchctl list` for %s yourself; launchctl load %s only if it is not listed' "$name" "$dest" ;;
    *)
      printf 'Next: nothing is needed for the job; fix the cause above and re-run this installer' ;;
  esac
}

install_rendered_plist() {
  local name="$1" label="$2" template="$3" dest="$4"
  local dir rendered backup="" before after unload_status

  dir="$(dirname "$dest")"
  mkdir -p "$dir"

  rendered="$(mktemp "$dir/.${name}-render.XXXXXX")" || return 1
  if ! python3 "$(_ateles_plist_renderer)" "$template" "$rendered"; then
    rm -f "$rendered"
    echo "ERROR: $name was not installed: its plist template could not be rendered safely." >&2
    echo "       Nothing was changed. Any registered $name job was left untouched." >&2
    return 1
  fi

  before="$(_ateles_job_state "$label")"
  if [ "$before" = "unknown" ]; then
    rm -f "$rendered"
    echo "ERROR: $name was not updated: launchd could not be queried, so the state of the existing job is unknown." >&2
    echo "       Nothing was changed: the installed plist and any registered job are as they were." >&2
    echo "       Next: check \`launchctl list\` for $name, then re-run this installer." >&2
    return 1
  fi

  if [ -f "$dest" ]; then
    backup="$(mktemp "$dir/.${name}-previous.XXXXXX")" || {
      rm -f "$rendered"
      echo "ERROR: $name was not updated: could not keep a copy of the current plist. Nothing was changed." >&2
      return 1
    }
    cp -p "$dest" "$backup"
  fi

  if [ "$before" != "not registered" ]; then
    echo "Unloading existing $name job (was: $(_ateles_describe_state "$before"))..."
    unload_status=0
    launchctl unload "$dest" >/dev/null 2>&1 || unload_status=$?
    after="$(_ateles_job_state "$label")"
    if [ "$unload_status" -ne 0 ] || { [ "$after" != "not registered" ] && [ "$after" != "unknown" ]; }; then
      rm -f "$rendered"
      if [ -n "$backup" ]; then rm -f "$backup"; fi
      echo "ERROR: $name was not updated: launchctl could not unload the existing job (exit $unload_status)." >&2
      echo "       Configuration: the installed plist was NOT replaced; it is unchanged." >&2
      echo "       Job state now observed: $(_ateles_describe_state "$after")." >&2
      echo "       $(_ateles_next_action "$name" "$after" "$dest")" >&2
      return 1
    fi
  fi

  mv -f "$rendered" "$dest"

  if launchctl load "$dest" >/dev/null 2>&1; then
    after="$(_ateles_job_state "$label")"
    case "$after" in
      "not registered")
        echo "ERROR: launchctl reported success but $name is not listed by launchd." >&2
        echo "       Configuration: the new plist is installed at $dest." >&2
        echo "       Job state now observed: not registered." >&2
        echo "       $(_ateles_next_action "$name" "$after" "$dest")" >&2
        if [ -n "$backup" ]; then rm -f "$backup"; fi
        return 1
        ;;
      *)
        if [ -n "$backup" ]; then rm -f "$backup"; fi
        echo "$name job: $(_ateles_describe_state "$after")."
        return 0
        ;;
    esac
  fi

  echo "ERROR: launchctl could not load the new $name plist." >&2
  if [ -n "$backup" ]; then
    mv -f "$backup" "$dest"
    launchctl load "$dest" >/dev/null 2>&1 || true
    after="$(_ateles_job_state "$label")"
    echo "       Configuration: the previous plist was put back at $dest." >&2
    echo "       Job state now observed: $(_ateles_describe_state "$after")." >&2
    echo "       $(_ateles_next_action "$name" "$after" "$dest")" >&2
  else
    rm -f "$dest"
    after="$(_ateles_job_state "$label")"
    echo "       Configuration: there was no previous plist; the new one was removed." >&2
    echo "       Job state now observed: $(_ateles_describe_state "$after")." >&2
    echo "       Next: fix the cause and re-run this installer." >&2
  fi
  return 1
}
