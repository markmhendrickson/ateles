#!/usr/bin/env python3
"""
Re-export attachments and comments for tasks already exported to target workspace.

Since previously exported tasks had their task_id updated to target GID,
we need to match them back to source tasks by title and then fetch
attachments/comments from source and add to target.

Usage:
    python scripts/reexport_task_attachments_comments.py [--dry-run] [--limit N]
"""

import argparse
import sys
from pathlib import Path

import pandas as pd

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.client import AsanaClientWrapper
from scripts.config import DATA_DIR, AsanaConfig
from scripts.export_asana_tasks import (
    fetch_and_post_comments,
    fetch_and_upload_attachments,
)

TASKS_FILE = DATA_DIR / "tasks" / "tasks.parquet"


def find_source_task_by_title(
    source_client: AsanaClientWrapper, workspace_gid: str, title: str
) -> str | None:
    """
    Find a task in source workspace by exact title match.

    Returns task GID if found, None otherwise.
    """
    try:
        # Search for tasks with matching name
        # Note: Asana API doesn't have great search, so we'll search projects
        opts = {"workspace": workspace_gid, "archived": False}
        projects = list(
            source_client._with_retry(source_client.projects.get_projects, opts)
        )

        for project in projects:
            project_gid = project.get("gid")
            try:
                # Get tasks from project
                task_opts = {"project": project_gid, "opt_fields": "gid,name"}
                tasks = list(
                    source_client._with_retry(source_client.tasks.get_tasks, task_opts)
                )

                for task in tasks:
                    if task.get("name") == title:
                        return task.get("gid")
            except Exception:
                continue

        return None
    except Exception as e:
        print(f"  Warning: Error searching for source task '{title}': {e}")
        return None


def reexport_attachments_comments(
    source_client: AsanaClientWrapper,
    target_client: AsanaClientWrapper,
    source_workspace_gid: str,
    target_workspace_gid: str,
    dry_run: bool = False,
    limit: int | None = None,
) -> dict:
    """
    Re-export attachments and comments for previously exported tasks.

    Returns statistics about the re-export operation.
    """
    # Load tasks
    df = pd.read_parquet(TASKS_FILE)

    # Find tasks that were exported to target
    exported_tasks = df[df["import_source_file"] == "asana-post"].copy()

    if limit:
        exported_tasks = exported_tasks.head(limit)

    if exported_tasks.empty:
        print("No exported tasks found to process")
        return {
            "total": 0,
            "processed": 0,
            "attachments_added": 0,
            "comments_added": 0,
            "not_found": 0,
        }

    print(f"Found {len(exported_tasks)} exported tasks to process")

    if dry_run:
        print("DRY RUN - would re-export attachments/comments")
        return {
            "total": len(exported_tasks),
            "processed": 0,
            "attachments_added": 0,
            "comments_added": 0,
            "not_found": 0,
        }

    stats = {
        "total": len(exported_tasks),
        "processed": 0,
        "attachments_added": 0,
        "comments_added": 0,
        "not_found": 0,
    }

    for idx, row in exported_tasks.iterrows():
        target_task_gid = row["task_id"].replace("asana-", "")
        title = row["title"]

        print(f"\nProcessing: {title}")
        print(f"  Target task GID: {target_task_gid}")

        # Find matching source task
        source_task_gid = find_source_task_by_title(
            source_client, source_workspace_gid, title
        )

        if not source_task_gid:
            print("  ⚠ Could not find matching source task")
            stats["not_found"] += 1
            continue

        print(f"  Found source task GID: {source_task_gid}")

        # Fetch and upload attachments
        attachment_count = fetch_and_upload_attachments(
            source_client, target_client, source_task_gid, target_task_gid
        )
        stats["attachments_added"] += attachment_count

        # Fetch and post comments
        comment_count = fetch_and_post_comments(
            source_client, target_client, source_task_gid, target_task_gid
        )
        stats["comments_added"] += comment_count

        stats["processed"] += 1

        if attachment_count == 0 and comment_count == 0:
            print("  ✓ No attachments or comments to add")
        else:
            print(
                f"  ✓ Added {attachment_count} attachment(s) and {comment_count} comment(s)"
            )

    return stats


def main():
    parser = argparse.ArgumentParser(
        description="Re-export attachments and comments for previously exported tasks"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without making changes",
    )
    parser.add_argument(
        "--limit", type=int, help="Limit number of tasks to process (for testing)"
    )

    args = parser.parse_args()

    try:
        config = AsanaConfig.from_env()
        source_client = AsanaClientWrapper.from_config_source(config)
        target_client = AsanaClientWrapper.from_config_target(config)

        stats = reexport_attachments_comments(
            source_client,
            target_client,
            config.source_workspace_gid,
            config.target_workspace_gid,
            dry_run=args.dry_run,
            limit=args.limit,
        )

        print("\n=== Re-export Complete ===")
        print(f"Total tasks found: {stats['total']}")
        print(f"Tasks processed: {stats['processed']}")
        print(f"Source tasks not found: {stats['not_found']}")
        print(f"Attachments added: {stats['attachments_added']}")
        print(f"Comments added: {stats['comments_added']}")

    except Exception as e:
        print(f"Error during re-export: {e}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
