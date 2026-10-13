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
#   registered       - registered with launchd and no process running (the
#                      listing alone says nothing about when it will run)
#   not registered   - launchd does not list the job
#   unknown          - launchd could not be queried; nothing is claimed, and
#                      an unknown state after an operation is a FAILURE: the
#                      installer never advances or reports success on it
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
#   * Unload or load succeeded but launchd could not be queried afterwards:
#     treated as a failure (exit 1). After an unload the plist is not
#     replaced; after a load the new plist stays installed, the previous one
#     is kept beside it, and the message says the job state is unknown.
#   * Success: only when the job is read back as running or registered. The
#     observed state is reported and left in ATELES_INSTALLED_JOB_STATE for
#     the caller's summary (`ateles_install_summary`); a registered job is
#     never described as running or started.
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
    registered) printf 'registered with launchd, no process running' ;;
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

# One line for the caller's closing summary, from the state read back after
# the load (never inferred): running, or registered with no process.
ateles_install_summary() {
  local name="$1"
  case "${ATELES_INSTALLED_JOB_STATE:-}" in
    "running (pid "*) printf '%s installed; job %s.\n' "$name" "$ATELES_INSTALLED_JOB_STATE" ;;
    registered) printf '%s installed; job registered with launchd, no process running yet.\n' "$name" ;;
    *) printf '%s installed; job state was not verified.\n' "$name" ;;
  esac
}

install_rendered_plist() {
  local name="$1" label="$2" template="$3" dest="$4"
  local dir rendered backup="" before after unload_status
  ATELES_INSTALLED_JOB_STATE=""

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
    if [ "$unload_status" -eq 0 ] && [ "$after" = "unknown" ]; then
      rm -f "$rendered"
      if [ -n "$backup" ]; then rm -f "$backup"; fi
      echo "ERROR: $name was not updated: launchd could not be queried after the unload, so whether the old job stopped is unknown." >&2
      echo "       Configuration: the installed plist was NOT replaced; it is unchanged." >&2
      echo "       Job state now observed: unknown (launchd could not be queried)." >&2
      echo "       $(_ateles_next_action "$name" "unknown" "$dest")" >&2
      return 1
    fi
    if [ "$unload_status" -ne 0 ] || [ "$after" != "not registered" ]; then
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
      "running (pid "*|registered)
        if [ -n "$backup" ]; then rm -f "$backup"; fi
        ATELES_INSTALLED_JOB_STATE="$after"
        echo "$name job: $(_ateles_describe_state "$after")."
        return 0
        ;;
      "not registered")
        echo "ERROR: launchctl reported success but $name is not listed by launchd." >&2
        echo "       Configuration: the new plist is installed at $dest." >&2
        echo "       Job state now observed: not registered." >&2
        echo "       $(_ateles_next_action "$name" "$after" "$dest")" >&2
        if [ -n "$backup" ]; then rm -f "$backup"; fi
        return 1
        ;;
      *)
        echo "ERROR: $name is not verified: launchctl load exited 0 but launchd could not be queried afterwards." >&2
        echo "       Configuration: the new plist is installed at $dest." >&2
        if [ -n "$backup" ]; then
          echo "       The previous plist is kept at $backup." >&2
        fi
        echo "       Job state now observed: unknown (launchd could not be queried)." >&2
        echo "       $(_ateles_next_action "$name" "unknown" "$dest")" >&2
        return 1
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
