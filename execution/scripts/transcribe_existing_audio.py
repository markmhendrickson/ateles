#!/usr/bin/env python3.11
"""
Transcribe all audio files already in imports directory.

This script processes audio files that are already in the imports directory,
useful when you can't access the source Voice Memos directory due to permissions.
"""

import os
import sys
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

# Add project root to path
PROJECT_ROOT = Path(
    __file__
).parent.parent.parent  # execution/scripts -> execution -> personal
sys.path.insert(0, str(PROJECT_ROOT))

# Load environment variables (must be before importing transcribe_audio)
env_path = PROJECT_ROOT / ".env"
if env_path.exists():
    # Capture stderr to detect dotenv parsing warnings
    import contextlib
    import io

    stderr_capture = io.StringIO()
    with contextlib.redirect_stderr(stderr_capture):
        result = load_dotenv(env_path, override=True)

    stderr_output = stderr_capture.getvalue()
    if "could not parse statement" in stderr_output.lower():
        print(f"\n{'=' * 80}")
        print("ERROR: .env file parsing errors detected")
        print(f"{'=' * 80}")
        print(stderr_output.strip())
        print(f"\n.env file path: {env_path}")
        print(
            "Fix the .env file syntax errors (check lines mentioned above) before continuing."
        )
        print(f"{'=' * 80}\n")
        sys.exit(1)

    if not result:
        print("Warning: No variables loaded from .env file")

    print(f"Loaded .env from: {env_path}")
    if "OPENAI_API_KEY" in os.environ:
        print("✓ OPENAI_API_KEY found in environment")
    else:
        print(f"\n{'=' * 80}")
        print("ERROR: OPENAI_API_KEY not found in environment")
        print(f"{'=' * 80}")
        print("The .env file was loaded but OPENAI_API_KEY is missing.")
        print("Add OPENAI_API_KEY to your .env file before continuing.")
        print(f"{'=' * 80}\n")
        sys.exit(1)
else:
    print(f"\n{'=' * 80}")
    print(f"ERROR: .env file not found at {env_path}")
    print(f"{'=' * 80}")
    print("Create a .env file with OPENAI_API_KEY before continuing.")
    print(f"{'=' * 80}\n")
    sys.exit(1)

# Get data directory from config (before importing transcribe_audio)
sys.path.insert(0, str(Path(__file__).parent))
try:
    from config import get_data_dir
except ImportError:
    from scripts.config import get_data_dir
DATA_DIR = get_data_dir()
IMPORTS_AUDIO_DIR = DATA_DIR / "imports" / "audio"

# Import from transcribe_audio module
from transcribe_audio import (
    is_already_transcribed,
    save_transcription,
    transcribe_audio_file,
)

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


def main():
    start_time = time.time()

    if not IMPORTS_AUDIO_DIR.exists():
        print(f"Error: Audio imports directory not found: {IMPORTS_AUDIO_DIR}")
        sys.exit(1)

    print("=" * 80)
    print("VOICE MEMO TRANSCRIPTION")
    print("=" * 80)
    print(f"Start time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Audio directory: {IMPORTS_AUDIO_DIR}")
    print()

    # Find all audio files
    print("Scanning for audio files...")
    audio_files = []
    for ext in AUDIO_EXTENSIONS:
        audio_files.extend(IMPORTS_AUDIO_DIR.glob(f"*{ext}"))
        audio_files.extend(IMPORTS_AUDIO_DIR.glob(f"*{ext.upper()}"))

    if not audio_files:
        print(f"No audio files found in {IMPORTS_AUDIO_DIR}")
        sys.exit(0)

    # Sort by filename for consistent processing
    audio_files = sorted(audio_files)
    total_files = len(audio_files)

    print(f"Found {total_files} audio file(s) to process")
    print("=" * 80)
    print()

    transcribed = 0
    failed = 0
    skipped = 0
    file_times = []

    for idx, audio_file in enumerate(audio_files, 1):
        file_start_time = time.time()

        # Progress header
        print(f"[{idx}/{total_files}] Processing: {audio_file.name}")
        print(f"  File size: {audio_file.stat().st_size / 1024 / 1024:.2f} MB")

        # Check if already transcribed
        if is_already_transcribed(audio_file):
            file_time = time.time() - file_start_time
            print(f"  ⊘ Skipped after {file_time:.1f}s: Already transcribed")
            skipped += 1
            continue

        try:
            # Transcribe
            transcription_result = transcribe_audio_file(
                audio_file, language=None, verbose=True
            )

            print("  Saving transcription to Neotoma...")
            save_transcription(audio_file, transcription_result)
            print("  ✓ Saved to Neotoma")

            file_time = time.time() - file_start_time
            file_times.append(file_time)

            # Calculate ETA
            avg_time = sum(file_times) / len(file_times)
            remaining_files = total_files - idx
            eta_seconds = avg_time * remaining_files
            eta_minutes = int(eta_seconds / 60)
            eta_seconds_remainder = int(eta_seconds % 60)

            print(f"  ✓ Transcribed successfully ({file_time:.1f}s)")
            print(f"  Language: {transcription_result.get('language', 'auto')}")
            print(
                f"  Audio duration: {transcription_result.get('audio_duration_seconds', 'N/A')}s"
            )
            print(
                f"  Transcription length: {len(transcription_result.get('transcription_text', ''))} characters"
            )
            print(f"  ETA: {eta_minutes}m {eta_seconds_remainder}s remaining")
            transcribed += 1

        except KeyboardInterrupt:
            print("\n  ⚠ Interrupted by user")
            print(
                f"\nProgress so far: {transcribed} transcribed, {failed} failed, {skipped} skipped"
            )
            sys.exit(1)

        except ValueError as e:
            # Handle skip-able errors (like file too short)
            if "SKIP:" in str(e):
                file_time = time.time() - file_start_time
                print(f"  ⊘ Skipped after {file_time:.1f}s: {e}")
                skipped += 1
                continue  # Skip to next file
            else:
                # Other ValueError - stop for debugging
                file_time = time.time() - file_start_time
                print(f"\n{'=' * 80}")
                print(f"ERROR: Transcription failed for {audio_file.name}")
                print(f"{'=' * 80}")
                print(f"Error after {file_time:.1f}s: {type(e).__name__}: {e}")
                print()
                print("Full traceback:")
                import traceback

                traceback.print_exc()
                print()
                print("File details:")
                print(f"  Path: {audio_file}")
                print(f"  Size: {audio_file.stat().st_size / 1024 / 1024:.2f} MB")
                print(f"  Exists: {audio_file.exists()}")
                print()
                print(
                    f"Progress so far: {transcribed} transcribed, {failed} failed, {skipped} skipped"
                )
                print(f"{'=' * 80}")
                print()
                print(
                    "STOPPING for debugging. Fix the error and re-run the script to continue."
                )
                print("The script will skip already-transcribed files on the next run.")
                sys.exit(1)
        except Exception as e:
            file_time = time.time() - file_start_time
            print(f"\n{'=' * 80}")
            print(f"ERROR: Transcription failed for {audio_file.name}")
            print(f"{'=' * 80}")
            print(f"Error after {file_time:.1f}s: {type(e).__name__}: {e}")
            print()
            print("Full traceback:")
            import traceback

            traceback.print_exc()
            print()
            print("File details:")
            print(f"  Path: {audio_file}")
            print(f"  Size: {audio_file.stat().st_size / 1024 / 1024:.2f} MB")
            print(f"  Exists: {audio_file.exists()}")
            print()
            print(
                f"Progress so far: {transcribed} transcribed, {failed} failed, {skipped} skipped"
            )
            print(f"{'=' * 80}")
            print()
            print(
                "STOPPING for debugging. Fix the error and re-run the script to continue."
            )
            print("The script will skip already-transcribed files on the next run.")
            sys.exit(1)

        print()

    # Final summary
    total_time = time.time() - start_time
    total_minutes = int(total_time / 60)
    total_seconds = int(total_time % 60)

    print("=" * 80)
    print("TRANSCRIPTION COMPLETE")
    print("=" * 80)
    print(f"Total time: {total_minutes}m {total_seconds}s")
    print(f"Files processed: {total_files}")
    print(f"  ✓ Transcribed: {transcribed}")
    print(f"  ✗ Failed: {failed}")
    print(f"  ⊘ Skipped: {skipped}")

    if transcribed > 0:
        avg_time = sum(file_times) / len(file_times) if file_times else 0
        print(f"\nAverage time per file: {avg_time:.1f}s")
        print(
            "Transcriptions saved to Neotoma (transcription entities + WAV when present)."
        )

    if failed > 0:
        print(f"\n⚠ {failed} file(s) failed to transcribe. Check errors above.")

    print("=" * 80)


if __name__ == "__main__":
    main()
