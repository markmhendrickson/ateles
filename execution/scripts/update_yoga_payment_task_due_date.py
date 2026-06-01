#!/usr/bin/env python3
"""
Update the yoga payment task so its due date is always the day after the next yoga class.

The payment is always for "Yoga with Manel" (private class). Only "Yoga with Manel"
events are used when finding the next class; other yoga events are ignored.

This script:
1. Finds the next "Yoga with Manel" class (from events.parquet or from --next-yoga-date)
2. Sets the yoga payment task's due_date to (yoga_class_date + 1 day)
3. Updates task notes

Run after completing the yoga payment task, or on a schedule, so the task always
reflects the next payment due date.

Usage:
    # Use next yoga from events.parquet (requires calendar events imported)
    python execution/scripts/update_yoga_payment_task_due_date.py

    # Or pass the next "Yoga with Manel" class date (e.g. from Google Calendar MCP)
    python execution/scripts/update_yoga_payment_task_due_date.py --next-yoga-date 2026-02-05
"""

import argparse
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.config import DATA_DIR

TASKS_FILE = DATA_DIR / "tasks" / "tasks.parquet"
EVENTS_FILE = DATA_DIR / "events" / "events.parquet"
SNAPSHOTS_DIR = DATA_DIR / "snapshots"

# Task identifier: Asana GID for the recurring yoga payment task
YOGA_PAYMENT_TASK_ID = "1212594992082004"
YOGA_TITLE_PATTERNS = ("yoga", "Pay for private yoga")


# Payment is always for "Yoga with Manel"; only events containing "manel" count.
YOGA_MANEL_PATTERN = "manel"


def find_next_yoga_from_events() -> date | None:
    """Find the next 'Yoga with Manel' class date from events.parquet. Returns None if not found."""
    if not EVENTS_FILE.exists():
        return None
    try:
        df = pd.read_parquet(EVENTS_FILE)
        if df.empty:
            return None
        # Prefer 'name' (import script), fallback to 'summary' (common calendar field)
        name_col = (
            "name"
            if "name" in df.columns
            else "summary"
            if "summary" in df.columns
            else None
        )
        if name_col is None:
            return None
        today = date.today()
        # Normalize start to date for comparison
        if "start_date" in df.columns:
            start_col = "start_date"
        elif "start" in df.columns:
            start_col = "start"
        else:
            return None

        # Only "Yoga with Manel" events (summary/name contains "manel")
        name_lower = df[name_col].astype(str).str.lower()
        mask = name_lower.str.contains("yoga", na=False) & name_lower.str.contains(
            YOGA_MANEL_PATTERN, na=False
        )
        future = df[mask].copy()
        if future.empty:
            return None
        future["_date"] = pd.to_datetime(future[start_col]).dt.date
        future = future[future["_date"] > today]
        if future.empty:
            return None
        next_date = future["_date"].min()
        return next_date if isinstance(next_date, date) else next_date.date()
    except Exception as e:
        print(f"Error reading events: {e}", file=sys.stderr)
        return None


def update_yoga_payment_task(next_yoga_date: date) -> bool:
    """Set the yoga payment task's due_date to (next_yoga_date + 1 day). Returns True if updated."""
    due_date = next_yoga_date + timedelta(days=1)
    today = date.today()

    if not TASKS_FILE.exists():
        print("Tasks file not found.", file=sys.stderr)
        return False

    df = pd.read_parquet(TASKS_FILE)
    # Find task by ID first, then by title
    by_id = df["task_id"].astype(str).str.strip() == str(YOGA_PAYMENT_TASK_ID).strip()
    by_title = df["title"].astype(str).str.lower().str.contains("yoga", na=False) & df[
        "title"
    ].astype(str).str.lower().str.contains("payment|pay", na=False)
    mask = by_id if by_id.any() else by_title
    if not mask.any():
        print("Yoga payment task not found in tasks.parquet.", file=sys.stderr)
        return False

    # Snapshot before update
    SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    snap_path = SNAPSHOTS_DIR / f"tasks-{ts}.parquet"
    df.to_parquet(snap_path, index=False)
    print(f"Snapshot: {snap_path}")

    idx = df.loc[mask].index[0]
    row = df.loc[idx]
    prev_due = row.get("due_date")
    if hasattr(prev_due, "date"):
        prev_due = prev_due.date() if prev_due else None
    elif isinstance(prev_due, str):
        prev_due = prev_due[:10] if prev_due else None

    # Update fields
    df.at[idx, "due_date"] = due_date
    df.at[idx, "updated_at"] = datetime.now()
    if "updated_date" in df.columns:
        df.at[idx, "updated_date"] = today
    # Append note about next yoga so it's clear why due_date is set
    note = f"Due date set to day after next yoga class ({next_yoga_date}). Payment: €60 BTC to bc1q7ce96cl9zmtwhgl9stsfvsv6fj8zdtrvta9raf."
    existing_notes = str(row.get("notes") or "").strip()
    if existing_notes and "next yoga class" not in existing_notes:
        df.at[idx, "notes"] = f"{existing_notes}\n\n{note}"
    else:
        df.at[idx, "notes"] = note
    # Urgency: today vs this_week
    df.to_parquet(TASKS_FILE, index=False)
    print(
        f"Updated yoga payment task: due_date {prev_due} -> {due_date} (day after yoga on {next_yoga_date})."
    )
    return True


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Set yoga payment task due date to day after next yoga class."
    )
    parser.add_argument(
        "--next-yoga-date",
        type=str,
        metavar="YYYY-MM-DD",
        help="Next yoga class date (from calendar). If omitted, uses events.parquet.",
    )
    args = parser.parse_args()

    next_yoga_date: date | None = None
    if args.next_yoga_date:
        try:
            next_yoga_date = datetime.strptime(
                args.next_yoga_date.strip(), "%Y-%m-%d"
            ).date()
        except ValueError:
            print(
                f"Invalid --next-yoga-date: {args.next_yoga_date!r}. Use YYYY-MM-DD.",
                file=sys.stderr,
            )
            return 1
    else:
        next_yoga_date = find_next_yoga_from_events()

    if not next_yoga_date:
        print(
            "Could not determine next yoga class date.",
            file=sys.stderr,
        )
        print(
            "Either import Google Calendar events to DATA_DIR/events/events.parquet, or run with:\n  --next-yoga-date YYYY-MM-DD",
            file=sys.stderr,
        )
        return 1

    if not update_yoga_payment_task(next_yoga_date):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
