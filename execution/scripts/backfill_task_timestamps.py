#!/usr/bin/env python3
"""
Backfill missing timestamps and project/section data for tasks in tasks.parquet

Fetches timestamp and project/section data directly from Asana API by task GID
for tasks that are missing updated_at values or project/section data (legacy imports).

For tasks that no longer exist in Asana (deleted/archived), uses import_date
as fallback timestamp. Project/section data cannot be recovered for deleted tasks.

Usage:
    python scripts/backfill_task_timestamps.py [--dry-run] [--batch-size N]
    python scripts/backfill_task_timestamps.py --timestamps-only  # Skip projects
"""

import argparse
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.client import AsanaClientWrapper

# Configuration
from scripts.config import DATA_DIR, AsanaConfig

TASKS_FILE = DATA_DIR / "tasks" / "tasks.parquet"
SNAPSHOTS_DIR = DATA_DIR / "snapshots"

# Ensure directories exist
SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)


class TimestampBackfiller:
    """Backfill missing updated_at timestamps for Asana tasks."""

    def __init__(self, config: AsanaConfig, dry_run: bool = False):
        self.config = config
        self.client = AsanaClientWrapper.from_config_source(config)
        self.dry_run = dry_run

    def create_snapshot(self):
        """Create timestamped snapshot before modification."""
        if not TASKS_FILE.exists():
            print("No tasks.parquet to snapshot")
            return

        timestamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
        snapshot_file = SNAPSHOTS_DIR / f"tasks-{timestamp}.parquet"

        df = pd.read_parquet(TASKS_FILE)
        df.to_parquet(snapshot_file, index=False)
        print(f"Created snapshot: {snapshot_file.name}")

    def fetch_task_data(self, task_gid: str) -> dict | None:
        """
        Fetch timestamps and project/section data for a task.

        Returns dict with data or None if task not found/accessible.
        """
        try:
            # Fetch timestamps and project/section info
            opts = {
                "opt_fields": "created_at,modified_at,projects,projects.gid,projects.name,memberships,memberships.project.gid,memberships.project.name,memberships.section.gid,memberships.section.name"
            }
            task_data = self.client._with_retry(
                self.client.tasks.get_task, task_gid, opts
            )

            result = {}

            # Parse timestamps
            created_at_str = task_data.get("created_at")
            modified_at_str = task_data.get("modified_at")

            if created_at_str:
                result["created_at"] = datetime.fromisoformat(created_at_str)
            if modified_at_str:
                result["updated_at"] = datetime.fromisoformat(modified_at_str)

            # Parse project/section info
            projects = task_data.get("projects", [])
            if projects:
                project_ids = [p.get("gid") for p in projects if p.get("gid")]
                project_names = [p.get("name") for p in projects if p.get("name")]

                if project_ids:
                    result["project_ids"] = "|".join(project_ids)
                if project_names:
                    result["project_names"] = "|".join(project_names)

            memberships = task_data.get("memberships", [])
            if memberships:
                section_ids = []
                section_names = []
                for m in memberships:
                    section = m.get("section")
                    if section:
                        if section.get("gid"):
                            section_ids.append(section.get("gid"))
                        if section.get("name"):
                            section_names.append(section.get("name"))

                if section_ids:
                    result["section_ids"] = "|".join(section_ids)
                if section_names:
                    result["section_names"] = "|".join(section_names)

            return result if result else None

        except Exception:
            # Task not found, deleted, or inaccessible
            return None

    def backfill_timestamps(
        self, batch_size: int = 100, include_projects: bool = True
    ) -> dict:
        """
        Backfill missing timestamps and optionally project/section data for tasks.

        Args:
            batch_size: Number of tasks to process before saving progress
            include_projects: Also backfill missing project/section data

        Returns:
            Statistics about the backfill operation
        """
        # Load tasks
        df = pd.read_parquet(TASKS_FILE)

        # Find Asana tasks needing backfill
        asana_mask = df["task_id"].astype(str).str.startswith("asana-", na=False)
        missing_timestamp_mask = df["updated_at"].isna()

        if include_projects:
            missing_projects_mask = df["project_ids"].isna()
            tasks_to_backfill = df[
                asana_mask & (missing_timestamp_mask | missing_projects_mask)
            ].copy()
            backfill_type = "timestamps and project/section data"
        else:
            tasks_to_backfill = df[asana_mask & missing_timestamp_mask].copy()
            backfill_type = "timestamps"

        if len(tasks_to_backfill) == 0:
            print(f"No tasks need {backfill_type} backfill")
            return {
                "total_missing": 0,
                "fetched": 0,
                "not_found": 0,
                "fallback": 0,
                "projects_added": 0,
            }

        print(f"Found {len(tasks_to_backfill)} tasks needing {backfill_type} backfill")

        if self.dry_run:
            print("DRY RUN - would backfill these tasks")
            return {
                "total_missing": len(tasks_to_backfill),
                "fetched": 0,
                "not_found": 0,
                "fallback": 0,
                "projects_added": 0,
            }

        # Create snapshot before modification
        self.create_snapshot()

        stats = {
            "total_missing": len(tasks_to_backfill),
            "fetched": 0,
            "not_found": 0,
            "fallback": 0,
            "projects_added": 0,
        }

        # Process tasks in batches
        updates = []

        for idx, row in tasks_to_backfill.iterrows():
            task_id = row["task_id"]
            task_gid = task_id.replace("asana-", "")

            # Fetch data from Asana
            task_data = self.fetch_task_data(task_gid)

            if task_data:
                # Successfully fetched data
                stats["fetched"] += 1
                update = {
                    "index": idx,
                    "created_at": task_data.get("created_at"),
                    "updated_at": task_data.get("updated_at"),
                    "project_ids": task_data.get("project_ids"),
                    "project_names": task_data.get("project_names"),
                    "section_ids": task_data.get("section_ids"),
                    "section_names": task_data.get("section_names"),
                }

                if include_projects and task_data.get("project_ids"):
                    stats["projects_added"] += 1

                updates.append(update)
                print(f"✓ {task_id}: fetched data")
            else:
                # Task not found - use fallback for timestamps only
                stats["not_found"] += 1

                # Use import_date as fallback timestamp
                import_date = row.get("import_date")
                if pd.notna(import_date):
                    # Convert date to datetime (midnight UTC)
                    fallback_timestamp = pd.Timestamp(import_date, tz="UTC")
                else:
                    # Ultimate fallback - use completed_date or today
                    completed_date = row.get("completed_date")
                    if pd.notna(completed_date):
                        fallback_timestamp = pd.Timestamp(completed_date, tz="UTC")
                    else:
                        fallback_timestamp = pd.Timestamp.now(tz="UTC")

                stats["fallback"] += 1
                updates.append(
                    {
                        "index": idx,
                        "created_at": fallback_timestamp,
                        "updated_at": fallback_timestamp,
                    }
                )
                print(f"⚠ {task_id}: not found, using fallback timestamp")

            # Save progress every batch_size tasks
            if len(updates) > 0 and len(updates) % batch_size == 0:
                self._apply_updates(df, updates)
                df.to_parquet(TASKS_FILE, index=False)
                print(
                    f"Progress saved: {len(updates)}/{len(tasks_to_backfill)} tasks processed"
                )

        # Apply remaining updates
        if updates:
            self._apply_updates(df, updates)
            df.to_parquet(TASKS_FILE, index=False)
            print(f"Final save: {len(updates)} tasks updated")

        return stats

    def _apply_updates(self, df: pd.DataFrame, updates: list):
        """Apply timestamp and project/section updates to dataframe."""
        for update in updates:
            idx = update["index"]
            if update.get("created_at"):
                df.at[idx, "created_at"] = update["created_at"]
            if update.get("updated_at"):
                df.at[idx, "updated_at"] = update["updated_at"]
            if update.get("project_ids"):
                df.at[idx, "project_ids"] = update["project_ids"]
            if update.get("project_names"):
                df.at[idx, "project_names"] = update["project_names"]
            if update.get("section_ids"):
                df.at[idx, "section_ids"] = update["section_ids"]
            if update.get("section_names"):
                df.at[idx, "section_names"] = update["section_names"]


def main():
    parser = argparse.ArgumentParser(
        description="Backfill missing timestamps and project/section data for tasks"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without making changes",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=100,
        help="Save progress every N tasks (default: 100)",
    )
    parser.add_argument(
        "--timestamps-only",
        action="store_true",
        help="Only backfill timestamps, skip project/section data",
    )

    args = parser.parse_args()

    try:
        config = AsanaConfig.from_env()
        backfiller = TimestampBackfiller(config, dry_run=args.dry_run)

        include_projects = not args.timestamps_only
        stats = backfiller.backfill_timestamps(
            batch_size=args.batch_size, include_projects=include_projects
        )

        print("\n=== Backfill Complete ===")
        print(f"Total tasks processed: {stats['total_missing']}")
        print(f"Data fetched from Asana: {stats['fetched']}")
        print(f"Tasks not found (used fallback): {stats['not_found']}")

        if include_projects:
            print(f"Tasks with project/section data added: {stats['projects_added']}")

        if not args.dry_run and stats["total_missing"] > 0:
            if include_projects:
                print(f"\nAll {stats['total_missing']} tasks now have complete data")
            else:
                print(f"\nAll {stats['total_missing']} tasks now have timestamps")

    except Exception as e:
        print(f"Error during backfill: {e}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
