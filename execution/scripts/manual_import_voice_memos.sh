#!/bin/bash
# Manual Voice Memos Import Script
# Run this script manually to copy voice memos and transcribe them

set -e

PROJECT_ROOT="/Users/markmhendrickson/Projects/personal"
VOICE_MEMOS_DIR="$HOME/Library/Group Containers/group.com.apple.VoiceMemos.shared/Recordings"

# Require DATA_DIR environment variable
if [ -z "$DATA_DIR" ]; then
    echo "Error: DATA_DIR environment variable is not set"
    echo "Please set DATA_DIR to your data directory path, e.g.:"
    echo "  export DATA_DIR=\"/absolute/path/to/data\""
    exit 1
fi
IMPORT_DIR="$DATA_DIR/imports/audio"

echo "============================================================"
echo "MANUAL VOICE MEMOS IMPORT"
echo "============================================================"

# Ensure import directory exists
mkdir -p "$IMPORT_DIR"

# Check if voice memos directory exists
if [ ! -d "$VOICE_MEMOS_DIR" ]; then
    echo "Error: Voice Memos directory not found: $VOICE_MEMOS_DIR"
    exit 1
fi

# Count voice memos
MEMO_COUNT=$(find "$VOICE_MEMOS_DIR" -name "*.m4a" 2>/dev/null | wc -l | tr -d ' ')
echo "Found $MEMO_COUNT voice memo(s)"

if [ "$MEMO_COUNT" -eq 0 ]; then
    echo "No voice memos found to import"
    exit 0
fi

# Copy voice memos with timestamped names
echo "Copying voice memos to $IMPORT_DIR..."
COPIED=0
for memo in "$VOICE_MEMOS_DIR"/*.m4a; do
    if [ -f "$memo" ]; then
        # Get file modification time for timestamp
        TIMESTAMP=$(stat -f "%Sm" -t "%Y%m%d_%H%M%S" "$memo")
        BASENAME=$(basename "$memo" .m4a)
        NEW_NAME="voice_memo_${TIMESTAMP}_${BASENAME}.m4a"
        
        # Copy to imports directory
        cp "$memo" "$IMPORT_DIR/$NEW_NAME"
        echo "  Copied: $NEW_NAME"
        COPIED=$((COPIED + 1))
    fi
done

echo "Copied $COPIED voice memo(s)"
echo ""

# Transcribe all audio files in import directory
echo "============================================================"
echo "TRANSCRIBING AUDIO FILES"
echo "============================================================"

cd "$PROJECT_ROOT"
source execution/venv/bin/activate

TRANSCRIBED=0
for audio_file in "$IMPORT_DIR"/*.m4a; do
    if [ -f "$audio_file" ]; then
        echo "Transcribing: $(basename "$audio_file")"
        if python scripts/transcribe_audio.py "$audio_file"; then
            TRANSCRIBED=$((TRANSCRIBED + 1))
            echo "  ✓ Transcribed successfully"
        else
            echo "  ✗ Transcription failed"
        fi
        echo ""
    fi
done

echo "============================================================"
echo "IMPORT COMPLETE"
echo "============================================================"
echo "Copied: $COPIED voice memo(s)"
echo "Transcribed: $TRANSCRIBED audio file(s)"
echo "Transcriptions saved to Neotoma (transcription entities + WAV)."

