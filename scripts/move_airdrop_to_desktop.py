#!/usr/bin/env python3
"""
Automatically moves AirDrop files from Downloads to Desktop.

Since macOS doesn't allow changing the AirDrop save location (it always saves to Downloads),
this script monitors the Downloads folder and automatically moves new files to Desktop.

Usage:
    python3 scripts/move_airdrop_to_desktop.py

To run in background:
    nohup python3 scripts/move_airdrop_to_desktop.py > /dev/null 2>&1 &
"""

import shutil
import sys
import time
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

# Paths
DOWNLOADS_DIR = Path.home() / "Downloads"
DESKTOP_DIR = Path.home() / "Desktop"

# Track files we've already processed to avoid duplicates
processed_files = set()


class AirDropHandler(FileSystemEventHandler):
    """Handles file system events for AirDrop files."""

    def __init__(self):
        super().__init__()
        # Initialize with existing files to avoid moving old files
        if DOWNLOADS_DIR.exists():
            for file in DOWNLOADS_DIR.iterdir():
                if file.is_file():
                    processed_files.add(file.name)

    def on_created(self, event):
        """Called when a new file is created in Downloads."""
        if event.is_directory:
            return

        file_path = Path(event.src_path)

        # Skip if we've already processed this file
        if file_path.name in processed_files:
            return

        # Wait a moment to ensure file is fully written
        time.sleep(0.5)

        # Skip if file doesn't exist (might have been deleted quickly)
        if not file_path.exists():
            return

        # Skip hidden files and system files
        if file_path.name.startswith("."):
            return

        # Move file to Desktop
        try:
            destination = DESKTOP_DIR / file_path.name

            # Handle name conflicts
            counter = 1
            original_destination = destination
            while destination.exists():
                stem = original_destination.stem
                suffix = original_destination.suffix
                destination = DESKTOP_DIR / f"{stem} {counter}{suffix}"
                counter += 1

            shutil.move(str(file_path), str(destination))
            print(f"✓ Moved {file_path.name} to Desktop")
            processed_files.add(file_path.name)

        except Exception as e:
            print(f"✗ Error moving {file_path.name}: {e}", file=sys.stderr)


def main():
    """Main function to start monitoring Downloads folder."""
    # Verify directories exist
    if not DOWNLOADS_DIR.exists():
        print(f"Error: Downloads directory not found: {DOWNLOADS_DIR}", file=sys.stderr)
        sys.exit(1)

    if not DESKTOP_DIR.exists():
        print(f"Error: Desktop directory not found: {DESKTOP_DIR}", file=sys.stderr)
        sys.exit(1)

    print(f"Monitoring {DOWNLOADS_DIR} for new files...")
    print(f"Files will be automatically moved to {DESKTOP_DIR}")
    print("Press Ctrl+C to stop\n")

    # Set up file system observer
    event_handler = AirDropHandler()
    observer = Observer()
    observer.schedule(event_handler, str(DOWNLOADS_DIR), recursive=False)
    observer.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping monitor...")
        observer.stop()

    observer.join()
    print("Monitor stopped.")


if __name__ == "__main__":
    main()
