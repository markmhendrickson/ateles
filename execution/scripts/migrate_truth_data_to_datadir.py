#!/usr/bin/env python3
"""
Migrate data from truth/data and truth/truth/data into $DATA_DIR

Moves all data files from the old truth/data directories into the
centralized $DATA_DIR location and removes the old directories.
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

TRUTH_DATA = PROJECT_ROOT / "truth" / "data"
TRUTH_TRUTH_DATA = PROJECT_ROOT / "truth" / "truth" / "data"


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


def main():
    print(f"Migrating data to $DATA_DIR: {DATA_DIR}")
    print()

    if not DATA_DIR.exists():
        raise RuntimeError(f"DATA_DIR does not exist: {DATA_DIR}")

    # Move from truth/data
    if TRUTH_DATA.exists():
        print(f"Processing {TRUTH_DATA}:")

        # Move imports/audio
        if (TRUTH_DATA / "imports" / "audio").exists():
            move_directory(
                TRUTH_DATA / "imports" / "audio",
                DATA_DIR / "imports" / "audio",
                merge=True,
            )

        # Merge logs
        if (TRUTH_DATA / "logs").exists():
            move_directory(TRUTH_DATA / "logs", DATA_DIR / "logs", merge=True)

        # Move/merge orders
        orders_src = TRUTH_DATA / "orders"
        orders_dst = DATA_DIR / "orders"
        if orders_src.exists():
            if orders_dst.exists():
                # Merge parquet files
                src_file = orders_src / "orders.parquet"
                dst_file = orders_dst / "orders.parquet"
                if src_file.exists() and dst_file.exists():
                    merge_parquet_files(src_file, dst_file)
                elif src_file.exists():
                    orders_dst.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(src_file), str(dst_file))
                    print("  ✓ Moved orders.parquet")
                orders_src.rmdir() if not any(orders_src.iterdir()) else None
            else:
                move_directory(orders_src, orders_dst, merge=False)
                print("  ✓ Moved orders")

        # Merge snapshots
        if (TRUTH_DATA / "snapshots").exists():
            move_directory(TRUTH_DATA / "snapshots", DATA_DIR / "snapshots", merge=True)

        # Move/merge transcriptions
        trans_src = TRUTH_DATA / "transcriptions"
        trans_dst = DATA_DIR / "transcriptions"
        if trans_src.exists():
            if trans_dst.exists():
                # Merge parquet files
                src_file = trans_src / "transcriptions.parquet"
                dst_file = trans_dst / "transcriptions.parquet"
                if src_file.exists() and dst_file.exists():
                    merge_parquet_files(src_file, dst_file)
                elif src_file.exists():
                    trans_dst.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(src_file), str(dst_file))
                    print("  ✓ Moved transcriptions.parquet")
                trans_src.rmdir() if not any(trans_src.iterdir()) else None
            else:
                move_directory(trans_src, trans_dst, merge=False)
                print("  ✓ Moved transcriptions")

        # Remove empty imports directory if it exists
        imports_dir = TRUTH_DATA / "imports"
        if imports_dir.exists() and not any(imports_dir.iterdir()):
            imports_dir.rmdir()
            print(f"  Removed empty {imports_dir}")

        # Remove truth/data if empty
        if TRUTH_DATA.exists() and not any(TRUTH_DATA.iterdir()):
            TRUTH_DATA.rmdir()
            print(f"  Removed empty {TRUTH_DATA}")

    # Move from truth/truth/data
    if TRUTH_TRUTH_DATA.exists():
        print(f"\nProcessing {TRUTH_TRUTH_DATA}:")

        # Move/merge tasks
        tasks_src = TRUTH_TRUTH_DATA / "tasks"
        tasks_dst = DATA_DIR / "tasks"
        if tasks_src.exists():
            if tasks_dst.exists():
                # Merge parquet files
                src_file = tasks_src / "tasks.parquet"
                dst_file = tasks_dst / "tasks.parquet"
                if src_file.exists() and dst_file.exists():
                    merge_parquet_files(src_file, dst_file)
                elif src_file.exists():
                    tasks_dst.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(src_file), str(dst_file))
                    print("  ✓ Moved tasks.parquet")
                tasks_src.rmdir() if not any(tasks_src.iterdir()) else None
            else:
                move_directory(tasks_src, tasks_dst, merge=False)
                print("  ✓ Moved tasks")

        # Merge snapshots
        if (TRUTH_TRUTH_DATA / "snapshots").exists():
            move_directory(
                TRUTH_TRUTH_DATA / "snapshots", DATA_DIR / "snapshots", merge=True
            )

        # Remove truth/truth/data if empty
        if TRUTH_TRUTH_DATA.exists() and not any(TRUTH_TRUTH_DATA.iterdir()):
            TRUTH_TRUTH_DATA.rmdir()
            print(f"  Removed empty {TRUTH_TRUTH_DATA}")

        # Remove truth/truth if empty
        truth_truth = TRUTH_TRUTH_DATA.parent
        if truth_truth.exists() and not any(truth_truth.iterdir()):
            truth_truth.rmdir()
            print(f"  Removed empty {truth_truth}")

    print("\n✓ Migration complete")


if __name__ == "__main__":
    main()
