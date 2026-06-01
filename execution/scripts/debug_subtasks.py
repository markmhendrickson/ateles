#!/usr/bin/env python3
"""
Debug script to check why subtasks weren't imported for a specific task.
"""

import sys
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.client import AsanaClientWrapper
from scripts.import_asana_tasks import AsanaConfig


def debug_subtasks(task_gid: str):
    """Debug subtask fetching for a specific task."""
    config = AsanaConfig.from_env()
    client = AsanaClientWrapper(config.source_pat)

    print(f"Fetching subtasks for task GID: {task_gid}")
    print("=" * 60)

    # Try to fetch the task first
    try:
        task_opts = {"opt_fields": "gid,name,completed,subtasks"}
        task = client._with_retry(client.tasks.get_task, task_gid, task_opts)
        print(f"Task: {task.get('name', 'Unknown')}")
        print(f"Completed: {task.get('completed', False)}")
        print(f"Subtasks field: {task.get('subtasks', [])}")
    except Exception as e:
        print(f"Error fetching task: {e}")
        return

    # Try fetching subtasks
    print("\nFetching subtasks via get_subtasks_for_task...")
    opt_fields = [
        "gid",
        "name",
        "notes",
        "html_notes",
        "completed",
        "completed_at",
        "due_on",
        "due_at",
        "start_on",
        "created_at",
        "modified_at",
        "assignee",
        "assignee.gid",
        "assignee.name",
    ]
    opts = {"opt_fields": ",".join(opt_fields)}

    try:
        subtasks = list(
            client._with_retry(client.tasks.get_subtasks_for_task, task_gid, opts)
        )
        print(f"Found {len(subtasks)} subtask(s)")
        for i, subtask in enumerate(subtasks, 1):
            print(f"\n  Subtask {i}:")
            print(f"    GID: {subtask.get('gid')}")
            print(f"    Name: {subtask.get('name')}")
            print(f"    Completed: {subtask.get('completed', False)}")
            print(f"    Created: {subtask.get('created_at')}")
            print(f"    Modified: {subtask.get('modified_at')}")
    except Exception as e:
        print(f"Error fetching subtasks: {e}")
        import traceback

        traceback.print_exc()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python debug_subtasks.py <task_gid>")
        print("Example: python debug_subtasks.py 1208198398045190")
        sys.exit(1)

    task_gid = sys.argv[1]
    debug_subtasks(task_gid)
