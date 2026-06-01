#!/usr/bin/env python3
"""
Migrate data from repo root data/ to $DATA_DIR (iCloud on macOS, or configured location).

This script safely migrates data from PROJECT_ROOT/data to the configured DATA_DIR,
merging directories and files where necessary.

Usage:
    python3 migrate_repo_data_to_datadir.py [source_path]

    If source_path is provided, migrates from that path instead of PROJECT_ROOT/data.
"""

import argparse
import shutil
from pathlib import Path

from dotenv import load_dotenv

# Load environment variables
PROJECT_ROOT = Path(__file__).parent.parent.parent
load_dotenv(PROJECT_ROOT / ".env")

# Import DATA_DIR from config (respects env var or defaults)
import sys

sys.path.insert(0, str(PROJECT_ROOT / "execution" / "scripts"))
from config import DATA_DIR


def merge_directories(src: Path, dst: Path):
    """Merge source directory into destination, moving files and merging subdirectories."""
    if not src.exists():
        return

    dst.mkdir(parents=True, exist_ok=True)

    for item in src.iterdir():
        src_item = src / item.name
        dst_item = dst / item.name

        if src_item.is_dir():
            if dst_item.exists() and dst_item.is_dir():
                # Both are directories, recursively merge
                merge_directories(src_item, dst_item)
            else:
                # Destination doesn't exist or is a file, move the directory
                shutil.move(str(src_item), str(dst_item))
                print(f"    Moved directory: {item.name}/")
        else:
            # It's a file
            if dst_item.exists():
                # File exists in destination - check if we should overwrite
                if src_item.stat().st_mtime > dst_item.stat().st_mtime:
                    # Source is newer, backup and replace
                    backup = dst_item.with_suffix(dst_item.suffix + ".backup")
                    shutil.copy2(str(dst_item), str(backup))
                    shutil.move(str(src_item), str(dst_item))
                    print(f"    Updated file: {item.name} (backed up old version)")
                else:
                    # Destination is newer or same, keep it and remove source
                    src_item.unlink()
                    print(f"    Kept existing: {item.name} (removed source)")
            else:
                # Destination doesn't exist, move the file
                shutil.move(str(src_item), str(dst_item))
                print(f"    Moved file: {item.name}")


def main():
    parser = argparse.ArgumentParser(
        description="Migrate data to $DATA_DIR from specified source or repo root"
    )
    parser.add_argument(
        "source_path",
        nargs="?",
        help="Source directory to migrate from (default: PROJECT_ROOT/data)",
    )
    args = parser.parse_args()

    # Determine source path
    if args.source_path:
        REPO_DATA = Path(args.source_path).expanduser().resolve()
        print("Migrating data from specified source to $DATA_DIR")
    else:
        REPO_DATA = PROJECT_ROOT / "data"
        print("Migrating data from repo root to $DATA_DIR")

    print(f"Source: {REPO_DATA}")
    print(f"Destination: {DATA_DIR}")
    print()

    if not REPO_DATA.exists():
        print(f"✓ Source directory does not exist: {REPO_DATA}")
        print("  Nothing to migrate")
        return

    if not DATA_DIR.exists():
        print(f"Creating DATA_DIR: {DATA_DIR}")
        DATA_DIR.mkdir(parents=True, exist_ok=True)

    # Process each subdirectory in source data
    for item in REPO_DATA.iterdir():
        if item.name.startswith("."):
            continue  # Skip hidden files

        src_dir = REPO_DATA / item.name
        dst_dir = DATA_DIR / item.name

        if src_dir.is_dir():
            print(f"Processing: {item.name}/")
            merge_directories(src_dir, dst_dir)

            # Remove source directory if empty
            if src_dir.exists() and not any(src_dir.iterdir()):
                src_dir.rmdir()
                print("  ✓ Removed empty source directory")
        else:
            # It's a file, move it
            if dst_dir.exists():
                if src_dir.stat().st_mtime > dst_dir.stat().st_mtime:
                    backup = dst_dir.with_suffix(dst_dir.suffix + ".backup")
                    shutil.copy2(str(dst_dir), str(backup))
                    shutil.move(str(src_dir), str(dst_dir))
                    print(f"  ✓ Updated file: {item.name} (backed up old version)")
                else:
                    src_dir.unlink()
                    print(f"  ✓ Kept existing: {item.name} (removed source)")
            else:
                shutil.move(str(src_dir), str(dst_dir))
                print(f"  ✓ Moved file: {item.name}")

    # Remove source data directory if empty (only if it's not the external path)
    if REPO_DATA.exists() and not any(REPO_DATA.iterdir()):
        if REPO_DATA == PROJECT_ROOT / "data":
            REPO_DATA.rmdir()
            print("\n✓ Removed empty repo data directory")
        else:
            print(f"\n✓ Source directory is now empty: {REPO_DATA}")
            print("  (Not removing external source directory)")

    print("\n✓ Migration complete!")
    print(f"\nData is now at: {DATA_DIR}")
    print("Update your .env file or set DATA_DIR environment variable if needed.")


if __name__ == "__main__":
    main()
