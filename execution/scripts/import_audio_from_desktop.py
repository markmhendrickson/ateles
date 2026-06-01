#!/usr/bin/env python3
"""
Import Audio Files from Desktop and Voice Memos

Scans Desktop and macOS Voice Memos for audio files, copies/moves them to
data/imports/, transcribes them, and emits ANALYZE_MEETING_TRIGGER lines for
the calling agent. Structured analysis (action items, recap email drafts,
proposed issues) is handled by the /analyze-meeting skill, auto-invoked by
/import-audio on each transcript.

Default scan sources (in order):
  1. ~/Desktop
  2. ~/Library/Group Containers/group.com.apple.VoiceMemos.shared/Recordings/

Usage:
    python import_audio_from_desktop.py [--source <path>] [--no-analyze] [--limit N]

Examples:
    python import_audio_from_desktop.py
    python import_audio_from_desktop.py --source ~/Desktop
    python import_audio_from_desktop.py --source ~/Desktop --source /path/to/more
    python import_audio_from_desktop.py --no-analyze
    python import_audio_from_desktop.py --limit 3
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "execution" / "scripts"))

# Load environment variables
load_dotenv(PROJECT_ROOT / ".env")

# Configuration
from config import get_data_dir

DATA_DIR = get_data_dir()
IMPORTS_DIR = DATA_DIR / "imports"
AUDIO_IMPORTS_DIR = IMPORTS_DIR / "audio"
TRANSCRIBE_SCRIPT = PROJECT_ROOT / "execution" / "scripts" / "transcribe_audio.py"

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
    ".qta",  # macOS Voice Memos format (QuickTime Audio; contains AAC stream)
}


VOICE_MEMOS_PATH = (
    Path.home()
    / "Library"
    / "Group Containers"
    / "group.com.apple.VoiceMemos.shared"
    / "Recordings"
)


def get_default_source_paths() -> list[Path]:
    """Return default audio source directories: Desktop + Voice Memos (if present)."""
    home = Path.home()
    sources = []

    # Desktop
    for candidate in [
        home / "Desktop",
        home / "desktop",
        home / "Documents" / "Desktop",
    ]:
        if candidate.exists() and candidate.is_dir():
            sources.append(candidate.resolve())
            break

    # macOS Voice Memos
    if VOICE_MEMOS_PATH.exists() and VOICE_MEMOS_PATH.is_dir():
        sources.append(VOICE_MEMOS_PATH.resolve())

    # Fallback: at least return Desktop even if missing (will error later with clear message)
    if not sources:
        sources.append(home / "Desktop")

    return sources


def find_audio_files(directory: Path) -> list[Path]:
    """Find all audio files in a directory (non-recursive)."""
    audio_files = []
    if not directory.exists():
        return audio_files

    for file_path in directory.iterdir():
        if file_path.is_file() and file_path.suffix.lower() in AUDIO_EXTENSIONS:
            audio_files.append(file_path)

    return sorted(audio_files)


def find_audio_files_in_sources(sources: list[Path]) -> list[tuple[Path, Path]]:
    """
    Find all audio files across multiple source directories.
    Returns list of (source_dir, file_path) tuples.
    """
    results = []
    for source in sources:
        for f in find_audio_files(source):
            results.append((source, f))
    return results


def import_audio_file(
    audio_file: Path, destination_dir: Path, copy: bool = False
) -> Path:
    """
    Move or copy audio file to imports directory with timestamped name.

    Voice Memos files are copied (system-managed); Desktop files are moved.
    Returns the new path in imports directory.
    """
    destination_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    new_name = f"{timestamp}_{audio_file.name}"
    destination_path = destination_dir / new_name

    if copy:
        shutil.copy2(str(audio_file), str(destination_path))
    else:
        shutil.move(str(audio_file), str(destination_path))

    return destination_path


def move_audio_file_to_imports(audio_file: Path, destination_dir: Path) -> Path:
    """Legacy wrapper: move audio file to imports directory. Use import_audio_file instead."""
    return import_audio_file(audio_file, destination_dir, copy=False)


def _neotoma_prod_base_url() -> str:
    return os.environ.get("NEOTOMA_PROD_BASE_URL", "http://localhost:3180")


def already_imported(original_filename: str) -> bool:
    """
    Return True if a transcription for this original file already exists in Neotoma.

    Uses `neotoma entities search --by original_source_file` (exact match on new
    field) then falls back to `--by audio_file_name` with the filename stem (for
    rows imported before original_source_file was added).
    Passes the current environment so the auth token env var is available.
    Safe to call when the CLI is unavailable (returns False).
    """
    if not shutil.which("neotoma"):
        return False

    def _search(field: str, value: str) -> bool:
        try:
            result = subprocess.run(
                [
                    "neotoma",
                    "--json",
                    "--api-only",
                    "--base-url",
                    _neotoma_prod_base_url(),
                    "entities",
                    "search",
                    value,
                    "--entity-type",
                    "transcription",
                    "--by",
                    field,
                    "--limit",
                    "5",
                ],
                capture_output=True,
                text=True,
                timeout=15,
                env=os.environ,
            )
            if result.returncode != 0 or not result.stdout.strip():
                return False
            data = json.loads(result.stdout)
            entities = data.get("entities") or data.get("results") or []
            return len(entities) > 0
        except Exception:
            return False

    # Exact match on new field first
    if _search("original_source_file", original_filename):
        return True
    # Stem-based fallback for pre-existing rows (audio_file_name stores renamed file
    # but includes the original stem e.g. "2026-05-20-100738_20250522 210022-0E6627E9.m4a")
    return _search("audio_file_name", Path(original_filename).stem)


def transcribe_file(
    audio_file: Path,
    language: str | None = None,
    original_source_file: str | None = None,
) -> dict:
    """Transcribe an audio file using the transcribe_audio.py script."""
    print(f"  Transcribing: {audio_file.name}...")

    cmd = [sys.executable, str(TRANSCRIBE_SCRIPT), str(audio_file)]
    if language:
        cmd.extend(["--language", language])
    if original_source_file:
        cmd.extend(["--original-source-file", original_source_file])

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=3600,  # 1 hour timeout
        )

        if result.returncode == 0:
            print("    ✓ Transcription complete")
            return {"success": True, "output": result.stdout}
        else:
            print(f"    ✗ Transcription failed: {result.stderr}")
            return {"success": False, "error": result.stderr}

    except subprocess.TimeoutExpired:
        error_msg = "Transcription timeout (file may be very long)"
        print(f"    ✗ {error_msg}")
        return {"success": False, "error": error_msg}
    except Exception as e:
        error_msg = f"Transcription error: {e}"
        print(f"    ✗ {error_msg}")
        return {"success": False, "error": error_msg}


def transcript_path_from_transcribe_stdout(stdout: str) -> str | None:
    """Parse the saved transcript file path emitted by transcribe_audio.py.

    transcribe_audio.py writes a sidecar transcript file alongside the WAV; the
    path is logged as ``Transcript written: <abs path>`` (when present). When
    no sidecar path is logged, return None and let the caller fall back to the
    ``last_meeting_transcription.txt`` path.
    """
    if not stdout:
        return None
    for prefix in ("Transcript written: ", "Transcript saved to: "):
        for line in stdout.splitlines():
            if line.startswith(prefix):
                return line[len(prefix):].strip() or None
    return None


def transcription_text_from_transcribe_stdout(stdout: str) -> str | None:
    """Parse plaintext transcription block from transcribe_audio.py stdout."""
    if not stdout:
        return None
    markers = ("\nTranscription text:\n", "Transcription text:\n")
    start_idx = -1
    for m in markers:
        i = stdout.find(m)
        if i != -1:
            start_idx = i + len(m)
            break
    if start_idx == -1:
        return None
    after = stdout[start_idx:]
    for end in ("\n\nSaved to Neotoma", "\nSaved to Neotoma"):
        j = after.find(end)
        if j != -1:
            after = after[:j]
            break
    text = after.strip()
    return text or None


def main():
    parser = argparse.ArgumentParser(
        description="Import audio files from Desktop and Voice Memos, transcribe, and analyze"
    )
    parser.add_argument(
        "--source",
        type=str,
        action="append",
        dest="sources",
        metavar="PATH",
        help=(
            "Source directory to scan (may be repeated). "
            "Default: Desktop + macOS Voice Memos."
        ),
    )
    parser.add_argument(
        "--desktop-path",
        type=str,
        default=None,
        help="Deprecated: use --source instead. Kept for backward compatibility.",
    )
    parser.add_argument(
        "--no-analyze",
        action="store_true",
        help=(
            "Skip the post-transcribe /analyze-meeting hint lines. By default,"
            " each successfully transcribed file emits"
            " 'ANALYZE_MEETING_TRIGGER: <transcript_path>' for the calling"
            " agent to fan out structured analysis."
        ),
    )
    parser.add_argument(
        "--language",
        type=str,
        default=None,
        help="Language code for transcription (e.g., en, es). If not provided, auto-detect.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum number of new files to import in this run.",
    )

    args = parser.parse_args()

    # Resolve source directories
    if args.sources:
        source_paths = [Path(s).expanduser().resolve() for s in args.sources]
    elif args.desktop_path:
        # Backward-compat: --desktop-path provided; also include Voice Memos
        source_paths = [Path(args.desktop_path).expanduser().resolve()]
        if VOICE_MEMOS_PATH.exists():
            source_paths.append(VOICE_MEMOS_PATH.resolve())
    else:
        source_paths = get_default_source_paths()

    print("Scanning for audio files in:")
    for p in source_paths:
        print(f"  {p}")

    # Find audio files across all sources
    source_file_pairs = find_audio_files_in_sources(source_paths)

    if not source_file_pairs:
        print("\nNo audio files found.")
        return

    print(f"\nFound {len(source_file_pairs)} audio file(s):")
    for source_dir, f in source_file_pairs:
        label = (
            "Voice Memos"
            if source_dir == VOICE_MEMOS_PATH.resolve()
            else source_dir.name
        )
        print(f"  - {f.name}  [{label}]")

    print("\nChecking for already-imported files...")
    skipped = 0
    to_import = []
    for source_dir, audio_file in source_file_pairs:
        if already_imported(audio_file.name):
            print(f"  ↷ Already imported: {audio_file.name}")
            skipped += 1
        else:
            to_import.append((source_dir, audio_file))
    if skipped:
        print(f"  Skipping {skipped} already-imported file(s).")

    if not to_import:
        print("\nAll files already imported. Nothing to do.")
        return

    if args.limit and len(to_import) > args.limit:
        print(f"  Limiting to {args.limit} file(s) (of {len(to_import)} new).")
        to_import = to_import[: args.limit]

    print(f"\nImporting {len(to_import)} new file(s) to {AUDIO_IMPORTS_DIR}...")

    # (source_dir, original_name, destination_path)
    moved_files: list[tuple[Path, str, Path]] = []
    for source_dir, audio_file in to_import:
        try:
            is_voice_memo = source_dir == VOICE_MEMOS_PATH.resolve()
            destination = import_audio_file(
                audio_file, AUDIO_IMPORTS_DIR, copy=is_voice_memo
            )
            moved_files.append((source_dir, audio_file.name, destination))
            verb = "Copied" if is_voice_memo else "Moved"
            print(f"  ✓ {verb}: {audio_file.name} → {destination.name}")
        except Exception as e:
            print(f"  ✗ Failed to import {audio_file.name}: {e}", file=sys.stderr)

    if not moved_files:
        print("\nNo files were imported successfully.")
        return

    print(f"\nTranscribing {len(moved_files)} file(s)...")

    transcription_results = []
    for _source_dir, original_name, audio_file in moved_files:
        result = transcribe_file(
            audio_file, language=args.language, original_source_file=original_name
        )
        transcription_results.append(
            {"file": audio_file, "transcription_result": result}
        )

    # Emit analyze-meeting triggers for the calling agent unless suppressed.
    if not args.no_analyze:
        for item in transcription_results:
            audio_file = item["file"]
            trans_result = item["transcription_result"]
            if not trans_result.get("success"):
                continue
            out = (
                (trans_result.get("output") or "")
                if isinstance(trans_result, dict)
                else ""
            )
            transcript_path = transcript_path_from_transcribe_stdout(out)
            target = transcript_path or str(audio_file)
            print(f"ANALYZE_MEETING_TRIGGER: {target}")

    print(f"\n✓ Import complete: {len(moved_files)} file(s) processed")
    if skipped:
        print(f"  {skipped} file(s) skipped (already imported).")
    print(f"  Files imported to: {AUDIO_IMPORTS_DIR}")
    print("  Transcriptions saved to Neotoma (transcription entities + WAV).")
    if not args.no_analyze:
        print(
            "  Run /analyze-meeting on each ANALYZE_MEETING_TRIGGER line above"
            " for structured analysis (handled automatically when invoked via"
            " /import-audio)."
        )


if __name__ == "__main__":
    main()
