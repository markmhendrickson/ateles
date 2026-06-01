#!/usr/bin/env python3
"""
One-off migration: strip legacy 'asana-' prefixes from task_id and
ensure asana_source_gid is populated with the literal Asana GID.

This is an explicit maintenance script run under user instruction.
It:
- Creates a timestamped snapshot of tasks.parquet
- Updates any row where task_id starts with 'asana-':
  * task_id := task_id.replace('asana-', '')
  * if asana_source_gid is null/empty, set it to the stripped GID
"""

import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
from scripts.config import DATA_DIR

TASKS_DIR = DATA_DIR / "tasks"
TASKS_FILE = TASKS_DIR / "tasks.parquet"
SNAPSHOTS_DIR = DATA_DIR / "snapshots"


def main() -> None:
    if not TASKS_FILE.exists():
        print("tasks.parquet not found, nothing to migrate")
        return

    SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)

    # Snapshot before modification
    timestamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    snapshot_file = (
        SNAPSHOTS_DIR / f"tasks-{timestamp}-before-strip-asana-prefix.parquet"
    )
    df_orig = pd.read_parquet(TASKS_FILE)
    df_orig.to_parquet(snapshot_file, index=False)
    print(f"Created snapshot: {snapshot_file}")

    df = df_orig.copy()

    if "task_id" not in df.columns:
        print("No task_id column found, aborting")
        return

    # Ensure asana_source_gid column exists
    if "asana_source_gid" not in df.columns:
        df["asana_source_gid"] = None

    mask = df["task_id"].astype(str).str.startswith("asana-", na=False)
    if not mask.any():
        print("No task_ids with 'asana-' prefix found, nothing to migrate")
        return

    # Strip prefix and backfill asana_source_gid when missing
    stripped_gids = df.loc[mask, "task_id"].astype(str).str.replace("asana-", "", n=1)
    df.loc[mask, "task_id"] = stripped_gids

    # Only set asana_source_gid where currently null/empty
    asana_gid_empty = df["asana_source_gid"].isna() | (
        df["asana_source_gid"].astype(str).str.strip() == ""
    )
    to_fill = mask & asana_gid_empty
    df.loc[to_fill, "asana_source_gid"] = df.loc[to_fill, "task_id"]

    df.to_parquet(TASKS_FILE, index=False)

    print(f"Migrated {int(mask.sum())} task_id values; wrote updated tasks.parquet")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"Migration failed: {e}", file=sys.stderr)
        sys.exit(1)
