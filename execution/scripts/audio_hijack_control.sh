#!/usr/bin/env bash
#
# audio_hijack_control.sh — start/stop/status/toggle an Audio Hijack session via AppleScript.
#
# Shared control surface for meeting recording now that local BlackHole capture
# (record_meeting_audio.py) is retired. Audio Hijack records the call and writes
# paired *remote* / *mic* files into its output folder; the Tyto daemon watches
# that folder, transcribes, and analyzes. Both the Strix menu-bar toggle and the
# /record_meeting skill drive recording through THIS script so there is a single
# mechanism.
#
# Audio Hijack must be running and must contain a session whose name matches
# AUDIO_HIJACK_SESSION (default "Tyto"). The session's recorder block produces
# the *remote*/*mic* files Tyto consumes.
#
# Usage:
#   audio_hijack_control.sh start     # start the session (begin recording)
#   audio_hijack_control.sh stop      # stop the session (end recording)
#   audio_hijack_control.sh status    # print "running" or "stopped"
#   audio_hijack_control.sh toggle    # start if stopped, stop if running
#
# Env:
#   AUDIO_HIJACK_SESSION   Session name to control (default: "Tyto")
#   AUDIO_HIJACK_APP       App name for AppleScript `tell` (default: "Audio Hijack")
#
# Exit codes: 0 success; 1 usage error; 2 osascript/Audio Hijack unavailable or
# the named session cannot be found.

set -euo pipefail

SESSION="${AUDIO_HIJACK_SESSION:-Tyto}"
APP="${AUDIO_HIJACK_APP:-Audio Hijack}"

if ! command -v osascript >/dev/null 2>&1; then
  echo "osascript not found (this script requires macOS)" >&2
  exit 2
fi

# Run an AppleScript snippet against Audio Hijack. The snippet body operates on
# a variable `theSession` already bound to the session named "$SESSION".
_ah() {
  local body="$1"
  osascript <<APPLESCRIPT
tell application "$APP"
  set matchingSessions to (every session whose name is "$SESSION")
  if (count of matchingSessions) is 0 then
    error "Audio Hijack session named \"$SESSION\" not found" number 2
  end if
  set theSession to item 1 of matchingSessions
  $body
end tell
APPLESCRIPT
}

ah_status() {
  local out
  out="$(_ah 'if running of theSession then
    return "running"
  else
    return "stopped"
  end if')"
  echo "$out"
}

ah_start() {
  if [ "$(ah_status)" = "running" ]; then
    echo "running"; return 0
  fi
  _ah 'start theSession' >/dev/null
  echo "running"
}

ah_stop() {
  if [ "$(ah_status)" = "stopped" ]; then
    echo "stopped"; return 0
  fi
  _ah 'stop theSession' >/dev/null
  echo "stopped"
}

cmd="${1:-toggle}"
case "$cmd" in
  start)  ah_start ;;
  stop)   ah_stop ;;
  status) ah_status ;;
  toggle|"")
    if [ "$(ah_status)" = "running" ]; then ah_stop; else ah_start; fi
    ;;
  *)
    echo "Usage: $(basename "$0") {start|stop|status|toggle}" >&2
    exit 1
    ;;
esac
