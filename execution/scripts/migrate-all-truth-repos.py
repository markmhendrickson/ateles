#!/usr/bin/env python3
"""
Migrate data from all truth repositories (truth and truth-layer) into $DATA_DIR

Handles absolute paths to external truth repositories:
- /Users/markmhendrickson/Projects/truth
- /Users/markmhendrickson/Projects/truth-layer
"""

import os
import shutil
from pathlib import Path

import pandas as pd
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

# Absolute paths to truth repositories
TRUTH_REPO = Path("/Users/markmhendrickson/Projects/truth")
TRUTH_LAYER_REPO = Path("/Users/markmhendrickson/Projects/truth-layer")

TRUTH_DATA = TRUTH_REPO / "data"
TRUTH_TRUTH_DATA = TRUTH_REPO / "truth" / "data"
TRUTH_LAYER_DATA = TRUTH_LAYER_REPO / "truth-layer" / "data"


def merge_parquet_files(src: Path, dst: Path):
    """Merge two parquet files, combining dataframes and removing duplicates."""
    try:
        src_df = pd.read_parquet(src)
        dst_df = pd.read_parquet(dst)

        # Combine dataframes
        combined_df = pd.concat([dst_df, src_df], ignore_index=True)

        # Remove duplicates if there's an ID column (common patterns)
        id_cols = [
            col for col in combined_df.columns if "id" in col.lower() or col == "id"
        ]
        if id_cols:
            combined_df = combined_df.drop_duplicates(subset=id_cols, keep="last")
        else:
            # Remove exact duplicates
            combined_df = combined_df.drop_duplicates(keep="last")

        # Write merged file
        combined_df.to_parquet(dst, index=False)
        print(
            f"    Merged {src.name} ({len(src_df)} + {len(dst_df)} = {len(combined_df)} rows)"
        )
        src.unlink()
        return True
    except Exception as e:
        print(f"    ⚠ Error merging {src.name}: {e}")
        return False


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

            # Handle parquet file merging
            if (
                src_item.is_file()
                and src_item.suffix == ".parquet"
                and dst_item.exists()
            ):
                merge_parquet_files(src_item, dst_item)
            elif dst_item.exists() and dst_item.is_dir():
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


def migrate_transcriptions(src: Path, dst: Path):
    """Migrate transcriptions.parquet file, merging if destination exists."""
    src_file = src / "transcriptions.parquet"
    dst_file = dst / "transcriptions.parquet"

    if not src_file.exists():
        print(f"  ⚠ No transcriptions.parquet found in {src}")
        return

    if dst_file.exists():
        print(f"  Merging transcriptions from {src} -> {dst}")
        merge_parquet_files(src_file, dst_file)
    else:
        print(f"  Moving transcriptions from {src} -> {dst}")
        dst.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src_file), str(dst_file))
        print("    ✓ Moved transcriptions.parquet")

    # Remove source directory if empty
    if src.exists() and not any(src.iterdir()):
        src.rmdir()
        print(f"    Removed empty {src}")


def migrate_snapshots(src: Path, dst: Path):
    """Migrate snapshot files, merging directories."""
    if src.exists():
        move_directory(src, dst, merge=True)
        print(f"  ✓ Migrated snapshots from {src}")


def migrate_imports(src: Path, dst: Path):
    """Migrate imports directory, merging contents."""
    if src.exists():
        move_directory(src, dst, merge=True)
        print(f"  ✓ Migrated imports from {src}")


def migrate_logs(src: Path, dst: Path):
    """Migrate logs directory, merging contents."""
    if src.exists():
        move_directory(src, dst, merge=True)
        print(f"  ✓ Migrated logs from {src}")


def main():
    print(f"Migrating data to $DATA_DIR: {DATA_DIR}")
    print()

    if not DATA_DIR.exists():
        raise RuntimeError(f"DATA_DIR does not exist: {DATA_DIR}")

    # Process truth/data
    if TRUTH_DATA.exists():
        print(f"Processing {TRUTH_DATA}:")
        migrate_transcriptions(
            TRUTH_DATA / "transcriptions", DATA_DIR / "transcriptions"
        )
        migrate_snapshots(TRUTH_DATA / "snapshots", DATA_DIR / "snapshots")
        migrate_imports(TRUTH_DATA / "imports", DATA_DIR / "imports")
        migrate_logs(TRUTH_DATA / "logs", DATA_DIR / "logs")

        # Remove empty truth/data if it exists
        if TRUTH_DATA.exists() and not any(TRUTH_DATA.iterdir()):
            TRUTH_DATA.rmdir()
            print(f"  Removed empty {TRUTH_DATA}")

    # Process truth/truth/data
    if TRUTH_TRUTH_DATA.exists():
        print(f"\nProcessing {TRUTH_TRUTH_DATA}:")
        migrate_transcriptions(
            TRUTH_TRUTH_DATA / "transcriptions", DATA_DIR / "transcriptions"
        )
        migrate_snapshots(TRUTH_TRUTH_DATA / "snapshots", DATA_DIR / "snapshots")

        # Remove empty directories
        if TRUTH_TRUTH_DATA.exists() and not any(TRUTH_TRUTH_DATA.iterdir()):
            TRUTH_TRUTH_DATA.rmdir()
            print(f"  Removed empty {TRUTH_TRUTH_DATA}")

        truth_truth = TRUTH_TRUTH_DATA.parent
        if truth_truth.exists() and not any(truth_truth.iterdir()):
            truth_truth.rmdir()
            print(f"  Removed empty {truth_truth}")

    # Process truth-layer/truth-layer/data
    if TRUTH_LAYER_DATA.exists():
        print(f"\nProcessing {TRUTH_LAYER_DATA}:")
        migrate_transcriptions(
            TRUTH_LAYER_DATA / "transcriptions", DATA_DIR / "transcriptions"
        )
        migrate_snapshots(TRUTH_LAYER_DATA / "snapshots", DATA_DIR / "snapshots")
        migrate_imports(TRUTH_LAYER_DATA / "imports", DATA_DIR / "imports")
        migrate_logs(TRUTH_LAYER_DATA / "logs", DATA_DIR / "logs")

        # Remove empty directories
        if TRUTH_LAYER_DATA.exists() and not any(TRUTH_LAYER_DATA.iterdir()):
            TRUTH_LAYER_DATA.rmdir()
            print(f"  Removed empty {TRUTH_LAYER_DATA}")

    print("\n✓ Migration complete")


if __name__ == "__main__":
    main()
