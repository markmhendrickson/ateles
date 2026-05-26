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
  if [ ! -f "$PID_FILE" ]; then
    return 1
  fi
  local pid
  pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [ -z "$pid" ]; then
    return 1
  fi
  kill -0 "$pid" 2>/dev/null
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

  if is_running; then
    echo "Recording already running (pid $(cat "$PID_FILE"))."
    return 0
  fi

  : > "$LOG_FILE"
  mkdir -p "$DATA_ROOT/imports/audio"

  local record_args=("$VENV_PYTHON" "$RECORD_SCRIPT" --label "$label")
  if [ "${RECORD_MEETING_SEPARATE_SOURCES:-1}" != "0" ]; then
    record_args+=(--separate-sources)
  fi
  if [ -n "${RECORD_MEETING_REALTIME_INTERVAL:-}" ] && [ "${RECORD_MEETING_REALTIME_INTERVAL}" != "0" ]; then
    record_args+=(--realtime-interval "${RECORD_MEETING_REALTIME_INTERVAL}")
  fi
  nohup "${record_args[@]}" >"$LOG_FILE" 2>&1 &
  local pid="$!"
  echo "$pid" > "$PID_FILE"

  sleep 1
  if ! kill -0 "$pid" 2>/dev/null; then
    echo "Recorder failed to start. Check log: $LOG_FILE"
    return 1
  fi

  echo "Recording started in background (pid $pid)."
  echo "Log: $LOG_FILE"

  if should_record_video; then
    start_video_recording "$label" || true
  fi
}

stop_recording() {
  if ! is_running; then
    echo "No active recording process."
    exit 1
  fi

  local pid
  pid="$(cat "$PID_FILE")"
  kill -INT "$pid" 2>/dev/null || true

  local waited=0
  while kill -0 "$pid" 2>/dev/null; do
    sleep 1
    waited=$((waited + 1))
    if [ "$waited" -gt 20 ]; then
      kill -TERM "$pid" 2>/dev/null || true
      break
    fi
  done
  rm -f "$PID_FILE"

  local audio_path
  audio_path="$(extract_saved_audio_path)"
  if [ -z "$audio_path" ]; then
    echo "Could not detect saved audio path from log."
    echo "Log: $LOG_FILE"
    exit 1
  fi
  echo "$audio_path" > "$LAST_AUDIO_FILE"
  echo "Saved audio: $audio_path"

  stop_video_recording || true

  if [ ! -f "$audio_path" ]; then
    echo "Audio file not found: $audio_path"
    exit 1
  fi

  if [ "${RECORD_MEETING_SKIP_TRANSCRIBE:-0}" = "1" ]; then
    echo "Skipping transcription (RECORD_MEETING_SKIP_TRANSCRIBE=1)."
    echo "Audio source (WAV): $audio_path"
    echo "Transcribe later: $VENV_PYTHON $TRANSCRIBE_SCRIPT \"$audio_path\""
    return 0
  fi

  echo "Starting transcription..."
  local transcribe_cmd=("$VENV_PYTHON" "$TRANSCRIBE_SCRIPT" "$audio_path")
  if should_use_diarization; then
    transcribe_cmd+=("--diarize")
    echo "Diarization enabled (ELEVENLABS_API_KEY detected)."
  else
    echo "Diarization disabled (ELEVENLABS_API_KEY missing or RECORD_MEETING_DIARIZE=0)."
  fi

  if ! "${transcribe_cmd[@]}" | tee "$TRANSCRIBE_LOG_FILE"; then
    if should_use_diarization; then
      echo "Diarized transcription failed; retrying with OpenAI Whisper (--no-diarize)..."
      "$VENV_PYTHON" "$TRANSCRIBE_SCRIPT" "$audio_path" --no-diarize | tee "$TRANSCRIBE_LOG_FILE"
    else
      exit 1
    fi
  fi
  echo "Transcription complete."

  echo ""
  echo "--- TRANSCRIPTION ---"
  "$VENV_PYTHON" - "$TRANSCRIBE_LOG_FILE" <<'PY'
from pathlib import Path
import sys
log = Path(sys.argv[1])
if log.exists():
    lines = log.read_text(errors="ignore").splitlines()
    in_block = False
    for line in lines:
        if line.strip() == "Transcription text:":
            in_block = True
            continue
        if in_block and (line.startswith("Saved to Neotoma") or "--- END TRANSCRIPTION ---" in line):
            break
        if in_block:
            print(line)
PY
  echo "--- END TRANSCRIPTION ---"
  echo ""
  local neotoma_tid=""
  neotoma_tid="$(grep -E '^NEOTOMA_TRANSCRIPTION_ENTITY_ID=' "$TRANSCRIBE_LOG_FILE" 2>/dev/null | tail -1 | sed 's/^NEOTOMA_TRANSCRIPTION_ENTITY_ID=//' || true)"
  if [ -n "$neotoma_tid" ]; then
    echo "Neotoma transcription entity ID: $neotoma_tid"
  else
    echo "Neotoma transcription entity ID: (not found in log; ensure neotoma CLI works and transcribe_audio.py completed store)"
  fi
  echo "Audio source (WAV): $audio_path"

  # Extract frames from video if one was recorded.
  local video_path
  video_path="$(cat "$LAST_VIDEO_FILE" 2>/dev/null || true)"
  if [ -n "$video_path" ] && [ -f "$video_path" ]; then
    local frame_args=("$VENV_PYTHON" "$ROOT_DIR/execution/scripts/extract_meeting_frames.py" "$video_path")
    if [ -n "$neotoma_tid" ]; then
      frame_args+=(--transcription-id "$neotoma_tid")
    fi
    echo "Extracting video frames…"
    "${frame_args[@]}" || echo "Warning: frame extraction failed (video saved at $video_path)."
  fi
}

status_recording() {
  if is_running; then
    echo "Audio recording: running (pid $(cat "$PID_FILE"))."
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
