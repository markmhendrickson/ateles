#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime
from pathlib import Path

from parquet_to_neotoma_migration import build_migration_matrix, get_parquet_client

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = REPO_ROOT / "data"
ARCHIVE_ROOT = DATA_DIR / "archive"


def archive_data_type(data_type: str, timestamp: str, execute: bool) -> dict:
    source_dir = DATA_DIR / data_type
    source_file = source_dir / f"{data_type}.parquet"
    target_dir = ARCHIVE_ROOT / f"parquet_cutover_{timestamp}" / data_type
    target_file = target_dir / f"{data_type}.parquet"
    exists = source_file.exists()
    if execute and exists:
        target_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_file, target_file)
    return {
        "data_type": data_type,
        "source_file": str(source_file),
        "archived_file": str(target_file),
        "exists": exists,
        "copied": bool(execute and exists),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Archive Parquet files after Neotoma cutover."
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Copy current parquet files into the archive tree.",
    )
    parser.add_argument(
        "--manifest-path",
        type=Path,
        default=ARCHIVE_ROOT / "latest_cutover_manifest.json",
        help="Where to write the archive manifest.",
    )
    args = parser.parse_args()

    client = get_parquet_client()
    matrix = build_migration_matrix(client)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    manifest = {
        "timestamp": timestamp,
        "mode": "execute" if args.execute else "dry_run",
        "entries": [
            archive_data_type(entry.data_type, timestamp, args.execute)
            for entry in matrix
        ],
    }

    args.manifest_path.parent.mkdir(parents=True, exist_ok=True)
    args.manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"Wrote archive manifest to {args.manifest_path}")
    if not args.execute:
        print("Dry run only. Re-run with --execute to copy parquet files into archive.")


if __name__ == "__main__":
    main()
