#!/usr/bin/env python3
"""
Audio File Watcher for Automatic Transcription

Monitors data/imports directory for new audio files and automatically transcribes them.

Usage:
    python watch_audio_transcribe.py [--watch-dir <directory>] [--language <language_code>]

Examples:
    python watch_audio_transcribe.py
    python watch_audio_transcribe.py --watch-dir data/imports/audio --language en
"""

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

try:
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer
except ImportError:
    print("Error: watchdog library not installed. Install with: pip install watchdog")
    sys.exit(1)

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Configuration
from scripts.config import get_data_dir

DATA_DIR = get_data_dir()
IMPORTS_DIR = DATA_DIR / "imports"
PROCESSED_FILE = DATA_DIR / "logs" / "transcribed_audio_files.txt"

# Audio file extensions to process
AUDIO_EXTENSIONS = {
    ".wav",
    ".mp3",
    ".m4a",
    ".ogg",
    ".flac",
    ".aac",
    ".wma",
    ".mp4",
    ".webm",
}

# Ensure logs directory exists
PROCESSED_FILE.parent.mkdir(parents=True, exist_ok=True)


def load_processed_files() -> set[str]:
    """Load set of already processed audio file paths."""
    if PROCESSED_FILE.exists():
        with open(PROCESSED_FILE) as f:
            return {line.strip() for line in f if line.strip()}
    return set()


def mark_file_processed(file_path: Path):
    """Mark a file as processed by adding it to the processed files list."""
    with open(PROCESSED_FILE, "a") as f:
        f.write(str(file_path) + "\n")


class AudioFileHandler(FileSystemEventHandler):
    """Handler for audio file events."""

    def __init__(self, language: str | None = None):
        self.language = language
        self.processed_files = load_processed_files()
        self.script_dir = Path(__file__).parent
        self.transcribe_script = self.script_dir / "transcribe_audio.py"

    def is_audio_file(self, file_path: Path) -> bool:
        """Check if file is an audio file based on extension."""
        return file_path.suffix.lower() in AUDIO_EXTENSIONS

    def should_process_file(self, file_path: Path) -> bool:
        """Check if file should be processed (is audio and not already processed)."""
        if not self.is_audio_file(file_path):
            return False

        file_path_str = str(file_path.resolve())
        if file_path_str in self.processed_files:
            return False

        # Check if file is still being written (wait a bit for copy to complete)
        if not file_path.exists():
            return False

        return True

    def transcribe_file(self, file_path: Path):
        """Transcribe an audio file using the transcribe_audio.py script."""
        print(f"\n[Transcription] Processing new audio file: {file_path.name}")

        try:
            # Build command
            cmd = [sys.executable, str(self.transcribe_script), str(file_path)]
            if self.language:
                cmd.extend(["--language", self.language])

            # Run transcription script
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=3600,  # 1 hour timeout for long audio files
            )

            if result.returncode == 0:
                print(f"[Transcription] Successfully transcribed: {file_path.name}")
                mark_file_processed(file_path)
                self.processed_files.add(str(file_path.resolve()))
            else:
                print(f"[Transcription] Error transcribing {file_path.name}:")
                print(result.stderr)

        except subprocess.TimeoutExpired:
            print(
                f"[Transcription] Timeout transcribing {file_path.name} (file may be very long)"
            )
        except Exception as e:
            print(f"[Transcription] Exception transcribing {file_path.name}: {e}")

    def on_created(self, event):
        """Handle file creation event."""
        if event.is_directory:
            return

        file_path = Path(event.src_path)

        # Wait a moment for file to be fully written
        time.sleep(2)

        if self.should_process_file(file_path):
            self.transcribe_file(file_path)

    def on_moved(self, event):
        """Handle file move event (includes renames)."""
        if event.is_directory:
            return

        dest_path = Path(event.dest_path)

        # Wait a moment for file to be fully written
        time.sleep(2)

        if self.should_process_file(dest_path):
            self.transcribe_file(dest_path)


def scan_existing_files(watch_dir: Path, handler: AudioFileHandler):
    """Scan existing audio files in the watch directory and process any unprocessed ones."""
    print(f"\n[Scan] Scanning for existing audio files in {watch_dir}...")

    processed_count = 0
    for root, dirs, files in os.walk(watch_dir):
        for file in files:
            file_path = Path(root) / file
            if handler.should_process_file(file_path):
                print(
                    f"[Scan] Found unprocessed audio file: {file_path.relative_to(PROJECT_ROOT)}"
                )
                handler.transcribe_file(file_path)
                processed_count += 1

    if processed_count == 0:
        print("[Scan] No unprocessed audio files found.")
    else:
        print(f"[Scan] Processed {processed_count} existing audio file(s).")


def main():
    parser = argparse.ArgumentParser(
        description="Watch for audio files in $DATA_DIR/imports and automatically transcribe them"
    )
    parser.add_argument(
        "--watch-dir",
        type=str,
        default=str(IMPORTS_DIR),
        help="Directory to watch for audio files (default: data/imports)",
    )
    parser.add_argument(
        "--language",
        type=str,
        default=None,
        help="Language code for transcription (e.g., en, es). If not provided, auto-detect.",
    )
    parser.add_argument(
        "--scan-only",
        action="store_true",
        help="Only scan existing files, do not start file watcher",
    )

    args = parser.parse_args()

    watch_dir = Path(args.watch_dir).resolve()

    if not watch_dir.exists():
        print(f"Error: Watch directory does not exist: {watch_dir}", file=sys.stderr)
        sys.exit(1)

    handler = AudioFileHandler(language=args.language)

    # Scan existing files first
    scan_existing_files(watch_dir, handler)

    if args.scan_only:
        print("\n[Scan] Scan-only mode: exiting after processing existing files.")
        return

    # Start file watcher
    observer = Observer()
    observer.schedule(handler, str(watch_dir), recursive=True)
    observer.start()

    print(f"\n[Watcher] Monitoring {watch_dir} for new audio files...")
    print("[Watcher] Press Ctrl+C to stop")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[Watcher] Stopping file watcher...")
        observer.stop()

    observer.join()
    print("[Watcher] File watcher stopped.")


if __name__ == "__main__":
    main()
