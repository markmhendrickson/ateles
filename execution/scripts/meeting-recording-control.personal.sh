#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

# Load repo .env so RECORD_MEETING_DEVICE, RECORD_MEETING_MIC, API keys, etc. apply.
ENV_FILE="$ROOT_DIR/.env"
if [ -f "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090,SC1091
  . "$ENV_FILE"
  set +a
fi

VENV_PYTHON="$ROOT_DIR/execution/venv/bin/python"
if [ ! -x "$VENV_PYTHON" ]; then
  VENV_PYTHON="$(command -v python3 || true)"
fi

if [ -z "${VENV_PYTHON:-}" ]; then
  echo "No python runtime found. Install python3 or create execution/venv first."
  exit 1
fi

RECORD_SCRIPT="$ROOT_DIR/execution/scripts/record_meeting_audio.py"
TRANSCRIBE_SCRIPT="$ROOT_DIR/execution/scripts/transcribe_audio.py"

if [ -n "${DATA_DIR:-}" ]; then
  DATA_ROOT="$DATA_DIR"
elif [ "$(uname -s)" = "Darwin" ]; then
  DATA_ROOT="$HOME/Library/Mobile Documents/com~apple~CloudDocs/Documents/data"
else
  DATA_ROOT="$ROOT_DIR/data"
fi

STATE_DIR="$DATA_ROOT/logs/meeting_recording"
PID_FILE="$STATE_DIR/recording.pid"
LOG_FILE="$STATE_DIR/recording.log"
TRANSCRIBE_LOG_FILE="$STATE_DIR/transcribe.log"
LAST_AUDIO_FILE="$STATE_DIR/last_audio_path.txt"
VIDEO_PID_FILE="$STATE_DIR/video_recording.pid"
VIDEO_LOG_FILE="$STATE_DIR/video_recording.log"
LAST_VIDEO_FILE="$STATE_DIR/last_video_path.txt"

mkdir -p "$STATE_DIR"

# Set RECORD_MEETING_SKIP_TRANSCRIBE=1 on stop (or toggle→stop) to save WAV only — no transcribe_audio / Neotoma transcription row.
# Useful when you will transcribe later manually.

# Diarization is enabled by default when ELEVENLABS_API_KEY is set.
# Set RECORD_MEETING_DIARIZE=0 to force plain transcription.
should_use_diarization() {
  if [ "${RECORD_MEETING_DIARIZE:-1}" = "0" ]; then
    return 1
  fi
  [ -n "${ELEVENLABS_API_KEY:-}" ]
}

should_record_video() {
  # Disabled when RECORD_MEETING_VIDEO=0; enabled by default when ffmpeg is available.
  if [ "${RECORD_MEETING_VIDEO:-1}" = "0" ]; then
    return 1
  fi
  command -v ffmpeg >/dev/null 2>&1
}

is_video_running() {
  if [ ! -f "$VIDEO_PID_FILE" ]; then
    return 1
  fi
  local pid
  pid="$(cat "$VIDEO_PID_FILE" 2>/dev/null || true)"
  [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
}

start_video_recording() {
  local label="${1:-meeting}"
  local timestamp
  timestamp="$(date +%Y-%m-%d_%H-%M-%S)"
  local safe_label
  safe_label="$(echo "$label" | tr -cd 'a-zA-Z0-9 _-' | tr ' ' '_')"
  local video_dir="$DATA_ROOT/imports/video"
  mkdir -p "$video_dir"
  local video_path="$video_dir/${timestamp}_${safe_label}.mp4"

  # Screen index from env (default 1 = primary display on macOS avfoundation).
  local screen_idx="${RECORD_MEETING_VIDEO_SCREEN:-1}"
  # Frames per second (default 2 — sufficient for slide/screen context; ~50 MB/hr).
  local fps="${RECORD_MEETING_VIDEO_FPS:-2}"

  : > "$VIDEO_LOG_FILE"
  # avfoundation: -i "<video_device>:<audio_device>" — use "none" for audio (audio captured separately).
  nohup ffmpeg -y \
    -f avfoundation \
    -r "$fps" \
    -i "${screen_idx}:none" \
    -vcodec libx264 \
    -preset ultrafast \
    -crf 35 \
    -pix_fmt yuv420p \
    "$video_path" \
    >"$VIDEO_LOG_FILE" 2>&1 &
  local vpid="$!"
  echo "$vpid" > "$VIDEO_PID_FILE"
  echo "$video_path" > "$LAST_VIDEO_FILE"

  sleep 1
  if ! kill -0 "$vpid" 2>/dev/null; then
    echo "Warning: video recorder failed to start. Check log: $VIDEO_LOG_FILE" >&2
    rm -f "$VIDEO_PID_FILE"
    return 1
  fi
  echo "Video recording started (pid $vpid, ${fps}fps) -> $video_path"
}

stop_video_recording() {
  if ! is_video_running; then
    return 0
  fi
  local vpid
  vpid="$(cat "$VIDEO_PID_FILE")"
  # Send 'q' to ffmpeg via SIGINT so it finalizes the MP4 container.
  kill -INT "$vpid" 2>/dev/null || true
  local waited=0
  while kill -0 "$vpid" 2>/dev/null; do
    sleep 1
    waited=$((waited + 1))
    if [ "$waited" -gt 15 ]; then
      kill -TERM "$vpid" 2>/dev/null || true
      break
    fi
  done
  rm -f "$VIDEO_PID_FILE"
  local video_path
  video_path="$(cat "$LAST_VIDEO_FILE" 2>/dev/null || true)"
  if [ -n "$video_path" ] && [ -f "$video_path" ]; then
    echo "Saved video: $video_path"
  fi
}

usage() {
  cat <<EOF
Usage:
  bash execution/scripts/meeting-recording-control.sh [toggle]
  bash execution/scripts/meeting-recording-control.sh start [label]
  bash execution/scripts/meeting-recording-control.sh stop
  bash execution/scripts/meeting-recording-control.sh status

Behavior:
  - (no arg or toggle): start if not running, stop if running
  - start: launches record_meeting_audio.py in background
  - stop: sends Ctrl+C signal, waits for save, then transcribes and stores transcription in Neotoma unless RECORD_MEETING_SKIP_TRANSCRIBE=1 (WAV only)
  - status: prints whether recording is running
EOF
}

is_running() {
  # Check if the Audio Hijack session is currently running via AppleScript.
  local session_name="${RECORD_MEETING_AH_SESSION:-Call Recording}"
  local result
  result="$(osascript -e "
    tell application \"Audio Hijack\"
      set s to first session whose name is \"${session_name}\"
      return running of s
    end tell
  " 2>/dev/null || echo "false")"
  [ "$result" = "true" ]
}

extract_saved_audio_path() {
  "$VENV_PYTHON" - "$LOG_FILE" <<'PY'
from pathlib import Path
import sys

log_path = Path(sys.argv[1])
if not log_path.exists():
    print("")
    raise SystemExit(0)

saved = ""
for line in log_path.read_text(errors="ignore").splitlines():
    if line.startswith("Saved: "):
        saved = line.replace("Saved: ", "", 1).strip()
print(saved)
PY
}

start_recording() {
  local label="${1:-meeting}"
  local session_name="${RECORD_MEETING_AH_SESSION:-Call Recording}"

  if is_running; then
    echo "Audio Hijack session '${session_name}' is already running."
    return 0
  fi

  osascript -e "
    tell application \"Audio Hijack\"
      activate
      set s to first session whose name is \"${session_name}\"
      startSession s
    end tell
  " 2>/dev/null
  sleep 1

  if is_running; then
    echo "Audio Hijack session '${session_name}' started."
  else
    echo "Failed to start Audio Hijack session '${session_name}'. Is Audio Hijack open with a session named '${session_name}'?"
    return 1
  fi

  if should_record_video; then
    start_video_recording "$label" || true
  fi
}

stop_recording() {
  local session_name="${RECORD_MEETING_AH_SESSION:-Call Recording}"

  if ! is_running; then
    echo "No active recording (Audio Hijack session '${session_name}' is not running)."
    exit 1
  fi

  # Stop the Audio Hijack session via AppleScript.
  osascript -e "
    tell application \"Audio Hijack\"
      set s to first session whose name is \"${session_name}\"
      stopSession s
    end tell
  " 2>/dev/null
  echo "Audio Hijack session '${session_name}' stopped."

  # Wait for AAC files to flush to disk (Audio Hijack writes on stop).
  local flush_wait="${RECORD_MEETING_FLUSH_WAIT:-5}"
  echo "Waiting ${flush_wait}s for files to flush..."
  sleep "$flush_wait"

  stop_video_recording || true

  # Find the most recent remote audio file in the recordings directory.
  local ah_dir="${RECORD_MEETING_DIR:-$HOME/Documents/data/recordings}"
  local audio_path
  audio_path="$(ls -t "$ah_dir"/*remote*.aac "$ah_dir"/*remote*.m4a 2>/dev/null | head -1 || true)"

  if [ -z "$audio_path" ] || [ ! -f "$audio_path" ]; then
    echo "Could not find a remote recording in $ah_dir"
    echo "Check Audio Hijack output folder setting."
    exit 1
  fi
  echo "$audio_path" > "$LAST_AUDIO_FILE"
  echo "Saved audio: $audio_path"

  echo "Audio source: $audio_path"
  echo "Transcription and analysis will be handled automatically by Tyto."

  # Extract video frames if a video was recorded.
  # Note: Tyto handles transcription, so frame extraction here is video-only.
  local video_path
  video_path="$(cat "$LAST_VIDEO_FILE" 2>/dev/null || true)"
  if [ -n "$video_path" ] && [ -f "$video_path" ]; then
    echo "Extracting video frames…"
    "$VENV_PYTHON" "$ROOT_DIR/execution/scripts/extract_meeting_frames.py" "$video_path" \
      || echo "Warning: frame extraction failed (video saved at $video_path)."
  fi
}

status_recording() {
  local session_name="${RECORD_MEETING_AH_SESSION:-Call Recording}"
  if is_running; then
    echo "Audio recording: running (Audio Hijack session '${session_name}')."
  else
    echo "Audio recording: not running."
  fi
  if is_video_running; then
    echo "Video recording: running (pid $(cat "$VIDEO_PID_FILE"))."
  else
    echo "Video recording: not running."
  fi
}

toggle_recording() {
  if is_running; then
    stop_recording
  else
    start_recording "${1:-meeting}"
  fi
}

transcribe_audiohijack() {
  # Transcribe the most recent Audio Hijack recording pair (remote + mic).
  # Audio Hijack saves files named "[Date] [Time] remote.aac" and "[Date] [Time] mic.aac"
  # into RECORD_MEETING_DIR (default: ~/Documents/data/recordings).
  local ah_dir="${RECORD_MEETING_DIR:-$HOME/Documents/data/recordings}"
  if [ ! -d "$ah_dir" ]; then
    echo "Audio Hijack recordings directory not found: $ah_dir"
    exit 1
  fi

  # Find the most recent remote file
  local remote_file
  remote_file="$(ls -t "$ah_dir"/*remote*.aac "$ah_dir"/*remote*.m4a 2>/dev/null | head -1 || true)"
  if [ -z "$remote_file" ]; then
    echo "No remote recording found in $ah_dir"
    exit 1
  fi

  echo "Remote file: $remote_file"
  echo "Transcribing..."
  "$VENV_PYTHON" "$TRANSCRIBE_SCRIPT" "$remote_file" --diarize | tee "$TRANSCRIBE_LOG_FILE"
}

main() {
  local command="${1:-toggle}"
  case "$command" in
    start)
      start_recording "${2:-meeting}"
      ;;
    stop)
      stop_recording
      ;;
    status)
      status_recording
      ;;
    toggle)
      toggle_recording "${2:-meeting}"
      ;;
    transcribe-audiohijack)
      transcribe_audiohijack
      ;;
    "")
      toggle_recording "meeting"
      ;;
    *)
      usage
      exit 1
      ;;
  esac
}

main "${1:-}" "${2:-}"
