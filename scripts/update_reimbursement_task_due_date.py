#!/usr/bin/env python3
"""One-off: set GentleHome reimbursement follow-up task due date to one week from 2026-01-26."""

import os
from datetime import datetime
from pathlib import Path

import pandas as pd

DATA_DIR = Path(os.environ.get("DATA_DIR", ""))
if not DATA_DIR:
    # Fallback from known snapshot path
    DATA_DIR = (
        Path.home() / "Library/Mobile Documents/com~apple~CloudDocs/Documents/data"
    )
TASKS_PATH = DATA_DIR / "tasks" / "tasks.parquet"
SNAPSHOTS_DIR = DATA_DIR / "snapshots"


def main():
    if not TASKS_PATH.exists():
        print(f"Tasks file not found: {TASKS_PATH}")
        return 1

    # Snapshot first
    SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    snapshot_path = SNAPSHOTS_DIR / f"tasks-{ts}.parquet"
    df = pd.read_parquet(TASKS_PATH)
    df.to_parquet(snapshot_path, index=False)
    print(f"Snapshot: {snapshot_path}")

    # Find and update the task
    mask = (
        df["title"].astype(str).str.contains("GentleHome order reimbursement", na=False)
    ) & (df["created_date"].astype(str) == "2026-01-26")
    if not mask.any():
        print("Task not found.")
        return 1
    n = mask.sum()
    # Preserve column dtypes: parquet may have date columns
    df.loc[mask, "due_date"] = pd.to_datetime("2026-02-02").date()
    if "updated_date" in df.columns:
        df.loc[mask, "updated_date"] = pd.to_datetime("2026-01-26").date()
    # Ensure date columns are consistent for parquet write
    for col in ("due_date", "created_date", "updated_date"):
        if col in df.columns and df[col].dtype == object:
            df[col] = pd.to_datetime(df[col], errors="coerce")
    df.to_parquet(TASKS_PATH, index=False)
    print(f"Updated due_date to 2026-02-02 for {n} task(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
