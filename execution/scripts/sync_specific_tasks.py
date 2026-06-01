#!/usr/bin/env python3
"""
Sync specific tasks and report what properties were changed.
"""

import sys
from pathlib import Path
from typing import Any

import pandas as pd

EXECUTION = Path(__file__).parent.parent
sys.path.insert(0, str(EXECUTION))

from scripts.config import AsanaConfig
from scripts.sync_asana_tasks import AsanaTaskSyncer


def sync_specific_tasks(
    task_gids: list[str], workspace_name: str = "target"
) -> dict[str, Any]:
    """Sync specific tasks by their target GIDs and report changes."""

    config = AsanaConfig.from_env()
    syncer = AsanaTaskSyncer(config, dry_run=False, sync_scope=workspace_name)

    # Load tasks
    from scripts.config import get_data_dir

    tasks_file = get_data_dir() / "tasks" / "tasks.parquet"
    if not tasks_file.exists():
        print(f"Error: Tasks file not found: {tasks_file}")
        return {}

    df = pd.read_parquet(tasks_file)

    # Filter to tasks with matching target GIDs
    gid_col = "asana_target_gid" if workspace_name == "target" else "asana_source_gid"
    tasks_to_sync = df[df[gid_col].astype(str).isin(task_gids)]

    if tasks_to_sync.empty:
        print(f"No tasks found with GIDs: {task_gids}")
        return {}

    print(f"Found {len(tasks_to_sync)} tasks to sync\n")

    client = (
        syncer.target_client if workspace_name == "target" else syncer.source_client
    )
    workspace_gid = (
        syncer.config.target_workspace_gid
        if workspace_name == "target"
        else syncer.config.source_workspace_gid
    )

    all_changes = {}

    for idx, row in tasks_to_sync.iterrows():
        task_gid = str(row[gid_col])
        title = row.get("title", "N/A")

        print(f"Syncing: {title} ({task_gid})")

        # Update task
        success, changes = syncer.update_asana_task(
            client, task_gid, row.to_dict(), workspace_gid, workspace_name
        )

        if success:
            all_changes[task_gid] = {"title": title, "changes": changes}
            print("  ✓ Synced successfully")
            if changes:
                print(f"  Changed properties: {', '.join(changes.keys())}")

            # Update sync_log and sync_datetime
            if not syncer.dry_run and "sync_log" in df.columns:
                df.loc[idx, "sync_log"] = "synced"
                df.loc[idx, "sync_datetime"] = pd.Timestamp.now(tz="UTC")
        else:
            print("  ✗ Sync failed")
        print()

    # Save updated tasks
    if not syncer.dry_run:
        # Create snapshot
        from datetime import datetime

        snapshots_dir = get_data_dir() / "snapshots"
        snapshots_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
        snapshot_file = snapshots_dir / f"tasks-{timestamp}.parquet"
        pd.read_parquet(tasks_file).to_parquet(snapshot_file, index=False)

        df.to_parquet(tasks_file, index=False)
        print(f"Saved snapshot: {snapshot_file}")

    return all_changes


if __name__ == "__main__":
    # Get the 5 most recently exported tasks
    from scripts.config import get_data_dir

    tasks_file = get_data_dir() / "tasks" / "tasks.parquet"
    df = pd.read_parquet(tasks_file)

    recent_exports = (
        df[df["sync_log"] == "exported_success"]
        .sort_values("sync_datetime", ascending=False)
        .head(5)
    )
    target_gids = recent_exports["asana_target_gid"].dropna().astype(str).tolist()

    print("=" * 80)
    print("Syncing 5 Recently Exported Tasks")
    print("=" * 80)
    print("\nTasks to sync:")
    for _, task in recent_exports.iterrows():
        print(f"  - {task.get('title')} ({task.get('asana_target_gid')})")
    print()

    changes = sync_specific_tasks(target_gids, workspace_name="target")

    print("\n" + "=" * 80)
    print("SYNC RESULTS SUMMARY")
    print("=" * 80)

    for gid, info in changes.items():
        print(f"\n{info['title']} ({gid})")
        print(f"  Properties changed: {', '.join(info['changes'].keys())}")
        for prop, value in info["changes"].items():
            if prop != "notes":  # Don't print full notes content
                print(f"    {prop}: {value}")
