#!/usr/bin/env python3
"""
Import Apple Voice Messages and Voice Memos

Extracts voice message attachments from macOS Messages database and/or Voice Memos
from the Voice Memos app, copies them to data/imports/audio/, and optionally transcribes them.

Usage:
    python import_apple_voice_messages.py [--voice-messages] [--voice-memos] [--all]
                                          [--transcribe] [--language <language_code>] [--since <date>]

Examples:
    python import_apple_voice_messages.py --all
    python import_apple_voice_messages.py --voice-memos --transcribe
    python import_apple_voice_messages.py --voice-messages --transcribe --language es
    python import_apple_voice_messages.py --all --since 2025-01-01
"""

import argparse
import shutil
import sqlite3
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

# Add project root to path
PROJECT_ROOT = (
    Path(__file__).parent.parent.parent
)  # Go up to actual repo root (execution/scripts -> execution -> personal)
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(
    0, str(PROJECT_ROOT / "execution" / "scripts")
)  # Add scripts directory to path

# Load environment variables before importing config
from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env")

# Configuration
from config import get_data_dir

DATA_DIR = get_data_dir()

MESSAGES_DB = Path.home() / "Library" / "Messages" / "chat.db"
MESSAGES_ATTACHMENTS_DIR = Path.home() / "Library" / "Messages" / "Attachments"
VOICE_MEMOS_DIR = (
    Path.home()
    / "Library"
    / "Group Containers"
    / "group.com.apple.VoiceMemos.shared"
    / "Recordings"
)
AUDIO_IMPORTS_DIR = DATA_DIR / "imports" / "audio"
PROCESSED_VOICE_MESSAGES_FILE = DATA_DIR / "logs" / "imported_voice_messages.txt"
PROCESSED_VOICE_MEMOS_FILE = DATA_DIR / "logs" / "imported_voice_memos.txt"

# Audio MIME types for voice messages
VOICE_MESSAGE_MIME_TYPES = {
    "audio/aac",
    "audio/m4a",
    "audio/x-m4a",
    "audio/mp4",
    "audio/x-mp4",
}

# Ensure directories exist
AUDIO_IMPORTS_DIR.mkdir(parents=True, exist_ok=True)
PROCESSED_VOICE_MESSAGES_FILE.parent.mkdir(parents=True, exist_ok=True)
PROCESSED_VOICE_MEMOS_FILE.parent.mkdir(parents=True, exist_ok=True)


def load_processed_attachments() -> set:
    """Load set of already processed attachment GUIDs."""
    if PROCESSED_VOICE_MESSAGES_FILE.exists():
        with open(PROCESSED_VOICE_MESSAGES_FILE) as f:
            return {line.strip() for line in f if line.strip()}
    return set()


def mark_attachment_processed(attachment_guid: str):
    """Mark an attachment as processed."""
    with open(PROCESSED_VOICE_MESSAGES_FILE, "a") as f:
        f.write(attachment_guid + "\n")


def load_processed_voice_memos() -> set:
    """Load set of already processed voice memo file paths."""
    if PROCESSED_VOICE_MEMOS_FILE.exists():
        with open(PROCESSED_VOICE_MEMOS_FILE) as f:
            return {line.strip() for line in f if line.strip()}
    return set()


def mark_voice_memo_processed(voice_memo_path: Path):
    """Mark a voice memo as processed."""
    with open(PROCESSED_VOICE_MEMOS_FILE, "a") as f:
        f.write(str(voice_memo_path.resolve()) + "\n")


def find_attachment_file(attachment_guid: str) -> Path | None:
    """
    Find the actual attachment file in Messages Attachments directory.

    Attachments are stored in a nested structure:
    Attachments/{first_two_hex}/{second_two_hex}/{full_guid}/filename.m4a

    We search recursively for the GUID directory, then find audio files within it.
    """
    if not MESSAGES_ATTACHMENTS_DIR.exists():
        return None

    # Search recursively for directory matching the GUID
    # The GUID directory contains the actual audio file
    guid_dir = None
    for path in MESSAGES_ATTACHMENTS_DIR.rglob(attachment_guid):
        if path.is_dir():
            guid_dir = path
            break

    if not guid_dir:
        return None

    # Look for audio files in the GUID directory
    # Voice messages are typically .m4a, .aac, or .mp4 files
    audio_extensions = {".m4a", ".aac", ".mp4", ".wav", ".mp3"}
    for file_path in guid_dir.iterdir():
        if file_path.is_file() and file_path.suffix.lower() in audio_extensions:
            return file_path

    return None


def query_voice_messages(since_date: date | None = None) -> list[tuple]:
    """
    Query Messages database for voice message attachments.

    Returns list of tuples: (attachment_guid, filename, mime_type, date_sent, message_text)
    """
    if not MESSAGES_DB.exists():
        print(f"Error: Messages database not found at {MESSAGES_DB}", file=sys.stderr)
        print("Note: Full Disk Access permission may be required.", file=sys.stderr)
        return []

    try:
        conn = sqlite3.connect(str(MESSAGES_DB))
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # Query for voice message attachments
        # The attachment table contains metadata, and we join with message table
        # to get date and context
        query = """
        SELECT DISTINCT
            a.guid as attachment_guid,
            a.filename,
            a.mime_type,
            a.created_date,
            m.date as message_date,
            m.text as message_text,
            h.id as contact_id
        FROM attachment a
        LEFT JOIN message_attachment_join maj ON a.ROWID = maj.attachment_id
        LEFT JOIN message m ON maj.message_id = m.ROWID
        LEFT JOIN handle h ON m.handle_id = h.ROWID
        WHERE a.mime_type IN ({})
        """.format(",".join(["?" for _ in VOICE_MESSAGE_MIME_TYPES]))

        params = list(VOICE_MESSAGE_MIME_TYPES)

        # Add date filter if provided
        if since_date:
            # Messages date is in seconds since 2001-01-01
            # Convert since_date to that format
            epoch_2001 = datetime(2001, 1, 1)
            since_datetime = datetime.combine(since_date, datetime.min.time())
            seconds_since_2001 = int((since_datetime - epoch_2001).total_seconds())
            query += " AND (a.created_date >= ? OR m.date >= ?)"
            params.extend([seconds_since_2001, seconds_since_2001])

        query += " ORDER BY a.created_date DESC, m.date DESC"

        cursor.execute(query, params)
        results = cursor.fetchall()

        conn.close()

        return [
            (
                row["attachment_guid"],
                row["filename"] or f"voice_message_{row['attachment_guid']}",
                row["mime_type"],
                row["created_date"],
                row["message_date"],
                row["message_text"],
                row["contact_id"],
            )
            for row in results
        ]

    except sqlite3.Error as e:
        print(f"Error querying Messages database: {e}", file=sys.stderr)
        print("Note: Full Disk Access permission may be required.", file=sys.stderr)
        return []
    except Exception as e:
        print(f"Unexpected error accessing Messages database: {e}", file=sys.stderr)
        return []


def copy_attachment_to_imports(
    attachment_guid: str, original_filename: str, contact_id: str | None = None
) -> Path | None:
    """
    Copy attachment file to imports directory with timestamped name.

    Returns path to copied file, or None if copy failed.
    """
    # Find the attachment file
    source_file = find_attachment_file(attachment_guid)
    if not source_file or not source_file.exists():
        print(f"  Warning: Could not find attachment file for GUID {attachment_guid}")
        return None

    # Generate destination filename with timestamp
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base_name = Path(original_filename).stem
    extension = source_file.suffix or ".m4a"

    # Add contact identifier if available
    if contact_id:
        # Clean contact_id (remove special characters)
        contact_safe = "".join(
            c if c.isalnum() or c in "-_" else "_" for c in contact_id
        )
        contact_safe = contact_safe[:20]  # Limit length
        dest_filename = f"{timestamp}_{contact_safe}_{base_name}{extension}"
    else:
        dest_filename = f"{timestamp}_{base_name}{extension}"

    dest_path = AUDIO_IMPORTS_DIR / dest_filename

    # Avoid overwriting existing files
    counter = 1
    while dest_path.exists():
        dest_filename = f"{timestamp}_{base_name}_{counter}{extension}"
        dest_path = AUDIO_IMPORTS_DIR / dest_filename
        counter += 1

    try:
        shutil.copy2(source_file, dest_path)
        return dest_path
    except Exception as e:
        print(f"  Error copying file: {e}", file=sys.stderr)
        return None


def find_voice_memos(since_date: date | None = None) -> list[Path]:
    """
    Find voice memo files in the Voice Memos directory.

    Returns list of Path objects for voice memo files.
    """
    if not VOICE_MEMOS_DIR.exists():
        return []

    voice_memos = []
    audio_extensions = {".m4a", ".aac", ".mp4", ".qta"}

    try:
        file_list = list(VOICE_MEMOS_DIR.iterdir())
    except PermissionError as e:
        print(
            f"  Permission denied accessing Voice Memos directory: {e}", file=sys.stderr
        )
        print(
            "  Please grant Full Disk Access to Terminal in System Settings",
            file=sys.stderr,
        )
        return []

    for file_path in file_list:
        if file_path.is_file() and file_path.suffix.lower() in audio_extensions:
            # Filter by date if provided
            if since_date:
                file_mtime = datetime.fromtimestamp(file_path.stat().st_mtime)
                if file_mtime.date() < since_date:
                    continue
            voice_memos.append(file_path)

    # Sort by modification time (newest first)
    voice_memos.sort(key=lambda p: p.stat().st_mtime, reverse=True)

    return voice_memos


def copy_voice_memo_to_imports(voice_memo_path: Path) -> Path | None:
    """
    Copy voice memo file to imports directory with timestamped name.

    Returns path to copied file, or None if copy failed.
    """
    # Generate destination filename
    # Voice memos are typically named like "20230624 203531-AE2165A6.m4a"
    # We'll preserve the original name but add import timestamp prefix
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    extension = voice_memo_path.suffix or ".m4a"

    # Extract base name (remove extension)
    base_name = voice_memo_path.stem

    # Create destination filename: import_timestamp_original_name.ext
    dest_filename = f"{timestamp}_voicememo_{base_name}{extension}"
    dest_path = AUDIO_IMPORTS_DIR / dest_filename

    # Avoid overwriting existing files
    counter = 1
    while dest_path.exists():
        dest_filename = f"{timestamp}_voicememo_{base_name}_{counter}{extension}"
        dest_path = AUDIO_IMPORTS_DIR / dest_filename
        counter += 1

    try:
        shutil.copy2(voice_memo_path, dest_path)
        return dest_path
    except Exception as e:
        print(f"  Error copying file: {e}", file=sys.stderr)
        return None


def transcribe_file(audio_file: Path, language: str | None = None) -> bool:
    """Transcribe an audio file using the transcribe_audio.py script."""
    # Use absolute path to transcribe script
    transcribe_script = Path(__file__).parent / "transcribe_audio.py"

    try:
        # Use python3 from PATH, not sys.executable which may point to wrong Python
        import shutil

        python_cmd = shutil.which("python3") or sys.executable
        cmd = [python_cmd, str(transcribe_script), str(audio_file)]
        if language:
            cmd.extend(["--language", language])

        result = subprocess.run(cmd, capture_output=True, text=True)

        if result.returncode == 0:
            return True
        else:
            print(f"  Transcription error: {result.stderr}", file=sys.stderr)
            return False
    except Exception as e:
        print(f"  Error running transcription: {e}", file=sys.stderr)
        return False


def import_voice_messages(
    since_date: date | None,
    transcribe: bool,
    language: str | None,
    verbose: bool = False,
) -> tuple[int, int, int]:
    """Import voice messages from Messages app. Returns (imported_count, transcribed_count, skipped_count)."""
    if not MESSAGES_DB.exists():
        print(f"Error: Messages database not found at {MESSAGES_DB}", file=sys.stderr)
        print("Note: Full Disk Access permission may be required.", file=sys.stderr)
        return (0, 0, 0)

    print("Querying Messages database for voice messages...")
    if since_date:
        print(f"  Filtering messages since: {since_date}")

    # Query for voice messages
    voice_messages = query_voice_messages(since_date=since_date)

    if not voice_messages:
        print("No voice messages found.")
        return (0, 0, 0)

    print(f"\nFound {len(voice_messages)} voice message(s)")

    # Load processed attachments
    processed = load_processed_attachments()

    # Process each voice message
    imported_count = 0
    transcribed_count = 0
    skipped_count = 0

    for (
        attachment_guid,
        filename,
        mime_type,
        created_date,
        message_date,
        message_text,
        contact_id,
    ) in voice_messages:
        # Skip if already processed
        if attachment_guid in processed:
            skipped_count += 1
            if verbose:
                print(f"\nSkipping (already processed): {filename}")
            continue

        print(f"\nProcessing: {filename}")
        print(f"  GUID: {attachment_guid}")
        print(f"  MIME type: {mime_type}")
        if contact_id:
            print(f"  Contact: {contact_id}")

        # Copy to imports
        copied_file = copy_attachment_to_imports(attachment_guid, filename, contact_id)
        if not copied_file:
            print("  Skipping (file not found)")
            continue

        print(f"  Copied to: {copied_file.name}")
        imported_count += 1

        # Mark as processed
        mark_attachment_processed(attachment_guid)

        # Transcribe if requested
        if transcribe:
            print("  Transcribing...")
            if transcribe_file(copied_file, language=language):
                transcribed_count += 1
                print("  ✓ Transcribed")
            else:
                print("  ✗ Transcription failed")

    if skipped_count > 0 and not verbose:
        print(f"\nSkipped {skipped_count} already processed voice message(s)")

    return (imported_count, transcribed_count, skipped_count)


def import_voice_memos(
    since_date: date | None,
    transcribe: bool,
    language: str | None,
    verbose: bool = False,
) -> tuple[int, int, int]:
    """Import voice memos from Voice Memos app. Returns (imported_count, transcribed_count, skipped_count)."""
    if not VOICE_MEMOS_DIR.exists():
        print(f"Voice Memos directory not found at {VOICE_MEMOS_DIR}")
        return (0, 0, 0)

    print("Scanning Voice Memos directory...")
    if since_date:
        print(f"  Filtering memos since: {since_date}")

    # Find voice memos
    voice_memos = find_voice_memos(since_date=since_date)

    if not voice_memos:
        print("No voice memos found.")
        return (0, 0, 0)

    print(f"\nFound {len(voice_memos)} voice memo(s)")

    # Load processed voice memos
    processed = load_processed_voice_memos()

    # Process each voice memo
    imported_count = 0
    transcribed_count = 0
    skipped_count = 0

    for voice_memo_path in voice_memos:
        # Skip if already processed
        if str(voice_memo_path.resolve()) in processed:
            skipped_count += 1
            if verbose:
                print(f"\nSkipping (already processed): {voice_memo_path.name}")
            continue

        print(f"\nProcessing: {voice_memo_path.name}")

        # Copy to imports
        copied_file = copy_voice_memo_to_imports(voice_memo_path)
        if not copied_file:
            print("  Skipping (copy failed)")
            continue

        print(f"  Copied to: {copied_file.name}")
        imported_count += 1

        # Mark as processed
        mark_voice_memo_processed(voice_memo_path)

        # Transcribe if requested
        if transcribe:
            print("  Transcribing...")
            if transcribe_file(copied_file, language=language):
                transcribed_count += 1
                print("  ✓ Transcribed")
            else:
                print("  ✗ Transcription failed")

    if skipped_count > 0 and not verbose:
        print(f"\nSkipped {skipped_count} already processed voice memo(s)")

    return (imported_count, transcribed_count, skipped_count)


def main():
    parser = argparse.ArgumentParser(
        description="Import Apple voice messages and/or voice memos"
    )
    parser.add_argument(
        "--voice-messages",
        action="store_true",
        help="Import voice messages from Messages app",
    )
    parser.add_argument(
        "--voice-memos",
        action="store_true",
        help="Import voice memos from Voice Memos app",
    )
    parser.add_argument(
        "--all", action="store_true", help="Import both voice messages and voice memos"
    )
    parser.add_argument(
        "--transcribe",
        action="store_true",
        help="Transcribe imported audio files immediately",
    )
    parser.add_argument(
        "--language",
        type=str,
        default=None,
        help="Language code for transcription (e.g., en, es). If not provided, auto-detect.",
    )
    parser.add_argument(
        "--since",
        type=str,
        default=None,
        help="Only import files since this date (YYYY-MM-DD format)",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Show detailed output including skipped files",
    )

    args = parser.parse_args()

    # Determine what to import
    import_voice_messages_flag = args.voice_messages or args.all
    import_voice_memos_flag = args.voice_memos or args.all

    # If no flags specified, default to importing both
    if not import_voice_messages_flag and not import_voice_memos_flag:
        import_voice_messages_flag = True
        import_voice_memos_flag = True

    # Parse since date if provided
    since_date = None
    if args.since:
        try:
            since_date = datetime.strptime(args.since, "%Y-%m-%d").date()
        except ValueError:
            print(
                f"Error: Invalid date format '{args.since}'. Use YYYY-MM-DD format.",
                file=sys.stderr,
            )
            sys.exit(1)

    total_imported = 0
    total_transcribed = 0
    total_skipped = 0

    # Import voice messages
    if import_voice_messages_flag:
        print(f"\n{'=' * 60}")
        print("IMPORTING VOICE MESSAGES")
        print("=" * 60)
        imported, transcribed, skipped = import_voice_messages(
            since_date, args.transcribe, args.language, args.verbose
        )
        total_imported += imported
        total_transcribed += transcribed
        total_skipped += skipped

    # Import voice memos
    if import_voice_memos_flag:
        print(f"\n{'=' * 60}")
        print("IMPORTING VOICE MEMOS")
        print("=" * 60)
        imported, transcribed, skipped = import_voice_memos(
            since_date, args.transcribe, args.language, args.verbose
        )
        total_imported += imported
        total_transcribed += transcribed
        total_skipped += skipped

    # Summary
    print(f"\n{'=' * 60}")
    print("IMPORT SUMMARY")
    print("=" * 60)
    print(f"  Total imported: {total_imported} file(s)")
    if total_skipped > 0:
        print(f"  Total skipped (already processed): {total_skipped} file(s)")
    if args.transcribe:
        print(f"  Total transcribed: {total_transcribed} file(s)")
    print(f"\nFiles saved to: {AUDIO_IMPORTS_DIR}")
    if not args.transcribe:
        print("\nNote: Use --transcribe flag to transcribe immediately, or")
        print("      run the file watcher to transcribe automatically:")
        print("      python execution/scripts/watch_audio_transcribe.py")


if __name__ == "__main__":
    main()
