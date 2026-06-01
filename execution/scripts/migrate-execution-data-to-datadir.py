#!/usr/bin/env python3
"""
Migrate data from execution/data into $DATA_DIR

Handles:
- Broken symlinks (remove)
- Snapshots (merge to $DATA_DIR/snapshots)
- Attachments (merge to $DATA_DIR/attachments)
- Empty directories (clean up)
"""

import os
import shutil
from pathlib import Path

from dotenv import load_dotenv

# Load environment variables
PROJECT_ROOT = Path(__file__).parent.parent.parent
load_dotenv(PROJECT_ROOT / ".env")

DATA_DIR = os.getenv("DATA_DIR")
if not DATA_DIR:
    raise RuntimeError(
        "DATA_DIR environment variable is not set. "
        "Please set DATA_DIR in your .env file or as an environment variable."
    )
DATA_DIR = Path(DATA_DIR)

EXECUTION_DATA = PROJECT_ROOT / "execution" / "data"


def move_directory(src: Path, dst: Path, merge: bool = False):
    """Move a directory, merging if merge=True and destination exists."""
    if not src.exists():
        print(f"  ⚠ Source does not exist: {src}")
        return

    if dst.exists() and merge:
        # Merge contents
        print(f"  Merging {src} -> {dst}")
        for item in src.iterdir():
            src_item = src / item.name
            dst_item = dst / item.name

            if dst_item.exists() and dst_item.is_dir():
                # Recursively merge directories
                move_directory(src_item, dst_item, merge=True)
            else:
                # Move file or directory
                shutil.move(str(src_item), str(dst_item))
                print(f"    Moved {item.name}")
    else:
        # Move entire directory
        print(f"  Moving {src} -> {dst}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))


def remove_broken_symlinks(directory: Path):
    """Remove broken symlinks in a directory."""
    if not directory.exists():
        return

    for item in directory.iterdir():
        if item.is_symlink():
            target = item.resolve()
            if not target.exists():
                print(f"  Removing broken symlink: {item}")
                item.unlink()


def main():
    print(f"Migrating data from execution/data to $DATA_DIR: {DATA_DIR}")
    print()

    if not DATA_DIR.exists():
        raise RuntimeError(f"DATA_DIR does not exist: {DATA_DIR}")

    if not EXECUTION_DATA.exists():
        print(f"  ⚠ execution/data does not exist: {EXECUTION_DATA}")
        return

    # Remove broken symlinks
    print("Checking for broken symlinks:")
    remove_broken_symlinks(EXECUTION_DATA / "tasks")

    # Migrate snapshots
    snapshots_src = EXECUTION_DATA / "snapshots"
    snapshots_dst = DATA_DIR / "snapshots"
    if snapshots_src.exists() and any(snapshots_src.iterdir()):
        print(f"\nMigrating snapshots from {snapshots_src}:")
        move_directory(snapshots_src, snapshots_dst, merge=True)
        print("  ✓ Migrated snapshots")

    # Migrate attachments
    attachments_src = EXECUTION_DATA / "attachments"
    attachments_dst = DATA_DIR / "attachments"
    if attachments_src.exists() and any(attachments_src.iterdir()):
        print(f"\nMigrating attachments from {attachments_src}:")
        move_directory(attachments_src, attachments_dst, merge=True)
        print("  ✓ Migrated attachments")

    # Migrate logs (if any)
    logs_src = EXECUTION_DATA / "logs"
    logs_dst = DATA_DIR / "logs"
    if logs_src.exists() and any(logs_src.iterdir()):
        print(f"\nMigrating logs from {logs_src}:")
        move_directory(logs_src, logs_dst, merge=True)
        print("  ✓ Migrated logs")

    # Migrate imports (if any)
    imports_src = EXECUTION_DATA / "imports"
    imports_dst = DATA_DIR / "imports"
    if imports_src.exists() and any(imports_src.iterdir()):
        print(f"\nMigrating imports from {imports_src}:")
        move_directory(imports_src, imports_dst, merge=True)
        print("  ✓ Migrated imports")

    # Clean up empty directories
    print("\nCleaning up empty directories:")
    for subdir in [
        "tasks",
        "snapshots",
        "attachments",
        "logs",
        "imports",
        "daily-triage",
    ]:
        subdir_path = EXECUTION_DATA / subdir
        if subdir_path.exists() and not any(subdir_path.iterdir()):
            subdir_path.rmdir()
            print(f"  Removed empty {subdir_path}")

    # Remove execution/data if empty
    if EXECUTION_DATA.exists() and not any(EXECUTION_DATA.iterdir()):
        EXECUTION_DATA.rmdir()
        print(f"  Removed empty {EXECUTION_DATA}")

    print("\n✓ Migration complete")


if __name__ == "__main__":
    main()
