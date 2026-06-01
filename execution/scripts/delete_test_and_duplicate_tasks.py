#!/usr/bin/env python3
"""
Delete test tasks and duplicate tasks from Asana target workspace.

Test tasks: Tasks with titles starting with "[TEST EXPORT]"
Duplicates: Tasks with identical titles (keeps first, deletes rest)
"""

import sys
import time
from collections import defaultdict
from pathlib import Path

import requests

# Add execution to path
EXECUTION_LAYER = Path(__file__).parent.parent
sys.path.insert(0, str(EXECUTION_LAYER))

from scripts.client import AsanaClientWrapper
from scripts.config import AsanaConfig


def get_current_user_gid(client: AsanaClientWrapper):
    """Get the current user's GID."""
    try:
        headers = {"Authorization": f"Bearer {client._pat}"}
        url = "https://app.asana.com/api/1.0/users/me"
        params = {"opt_fields": "gid,name,email"}
        response = requests.get(url, headers=headers, params=params, timeout=30)
        response.raise_for_status()
        me = response.json().get("data", {})
        return me.get("gid")
    except Exception as e:
        print(f"Warning: Could not get current user GID: {e}")
        return None


def get_all_tasks_in_workspace(client: AsanaClientWrapper, workspace_gid: str):
    """Get all tasks from target workspace."""
    all_tasks = []
    task_gids = set()

    opt_fields = ["gid", "name", "completed"]

    # Get current user GID
    current_user_gid = get_current_user_gid(client)

    # Get tasks from all projects
    try:
        print("Fetching projects...")
        projects_opts = {"workspace": workspace_gid, "archived": False}
        projects = list(client._with_retry(client.projects.get_projects, projects_opts))
        print(f"Found {len(projects)} projects")

        for idx, project in enumerate(projects, 1):
            project_gid = project.get("gid")
            project_name = project.get("name", "")
            print(f"Fetching tasks from project {idx}/{len(projects)}: {project_name}")

            opts = {"project": project_gid, "opt_fields": ",".join(opt_fields)}

            try:
                project_tasks = list(client._with_retry(client.tasks.get_tasks, opts))

                for task_data in project_tasks:
                    task_gid = task_data.get("gid")
                    if task_gid and task_gid not in task_gids:
                        task_gids.add(task_gid)
                        all_tasks.append(task_data)

            except Exception as e:
                print(
                    f"  Warning: Error fetching tasks from project '{project_name}': {e}"
                )
                continue

    except Exception as e:
        print(f"Warning: Error fetching projects: {e}")

    # Get tasks assigned to current user (using assignee + workspace)
    if current_user_gid:
        try:
            print("Fetching tasks assigned to you in workspace...")
            assignee_opts = {
                "assignee": current_user_gid,
                "workspace": workspace_gid,
                "opt_fields": ",".join(opt_fields),
            }
            assignee_tasks = list(
                client._with_retry(client.tasks.get_tasks, assignee_opts)
            )

            for task_data in assignee_tasks:
                task_gid = task_data.get("gid")
                if task_gid and task_gid not in task_gids:
                    task_gids.add(task_gid)
                    all_tasks.append(task_data)

            print(f"Found {len(assignee_tasks)} tasks assigned to you")
        except Exception as e:
            print(f"Warning: Error fetching tasks assigned to you: {e}")

    return all_tasks


def delete_task(client: AsanaClientWrapper, task_gid: str) -> bool:
    """Delete a task by GID using Asana API DELETE endpoint."""
    try:
        headers = {"Authorization": f"Bearer {client._pat}"}
        url = f"https://app.asana.com/api/1.0/tasks/{task_gid}"
        response = requests.delete(url, headers=headers, timeout=30)

        if response.status_code == 200:
            return True
        elif response.status_code == 404:
            # Task already deleted
            return True
        else:
            error_detail = (
                response.text if response.text else f"HTTP {response.status_code}"
            )
            print(f"  Warning: Failed to delete task {task_gid}: {error_detail[:200]}")
            return False

    except Exception as e:
        print(f"  Warning: Error deleting task {task_gid}: {e}")
        return False


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Delete test tasks and duplicates from Asana"
    )
    parser.add_argument("--yes", action="store_true", help="Skip confirmation prompt")
    args = parser.parse_args()

    print("=" * 80)
    print("Delete Test Tasks and Duplicates from Asana Target Workspace")
    print("=" * 80)

    config = AsanaConfig.from_env()
    target_client = AsanaClientWrapper.from_config_target(config)

    # Get all tasks
    print("\nFetching all tasks from target workspace...")
    all_tasks = get_all_tasks_in_workspace(target_client, config.target_workspace_gid)
    print(f"\nTotal tasks found: {len(all_tasks)}")

    # Identify test tasks
    test_tasks = [t for t in all_tasks if t.get("name", "").startswith("[TEST EXPORT]")]
    print(f"\nTest tasks found: {len(test_tasks)}")
    for task in test_tasks:
        print(f"  - {task.get('name')} ({task.get('gid')})")

    # Identify duplicate tasks (same title)
    title_to_tasks = defaultdict(list)
    for task in all_tasks:
        title = task.get("name", "").strip()
        if title:  # Only consider tasks with titles
            title_to_tasks[title].append(task)

    duplicate_tasks = []
    for title, tasks in title_to_tasks.items():
        if len(tasks) > 1:
            # Keep the first one, mark the rest as duplicates
            duplicate_tasks.extend(tasks[1:])

    print(f"\nDuplicate tasks found: {len(duplicate_tasks)}")
    for task in duplicate_tasks[:10]:  # Show first 10
        print(f"  - {task.get('name')} ({task.get('gid')})")
    if len(duplicate_tasks) > 10:
        print(f"  ... and {len(duplicate_tasks) - 10} more")

    # Combine tasks to delete (test tasks + duplicates, avoiding duplicates)
    tasks_to_delete = {}
    for task in test_tasks + duplicate_tasks:
        task_gid = task.get("gid")
        if task_gid:
            tasks_to_delete[task_gid] = task

    print(f"\nTotal unique tasks to delete: {len(tasks_to_delete)}")
    print(f"  - Test tasks: {len(test_tasks)}")
    print(f"  - Duplicate tasks: {len(duplicate_tasks)}")
    print(
        f"  - Overlap (test tasks that are also duplicates): {len(test_tasks) + len(duplicate_tasks) - len(tasks_to_delete)}"
    )

    # Confirm deletion
    if not args.yes:
        print("\n" + "=" * 80)
        response = input(
            f"Delete {len(tasks_to_delete)} task(s) from Asana? (yes/no): "
        )
        if response.lower() != "yes":
            print("Cancelled.")
            return
    else:
        print(f"\nProceeding with deletion of {len(tasks_to_delete)} task(s)...")

    # Delete tasks
    print(f"\nDeleting {len(tasks_to_delete)} task(s)...")
    deleted_count = 0
    failed_count = 0

    for idx, (task_gid, task) in enumerate(tasks_to_delete.items(), 1):
        task_name = task.get("name", "Unknown")
        print(f"Deleting task {idx}/{len(tasks_to_delete)}: {task_name} ({task_gid})")

        if delete_task(target_client, task_gid):
            deleted_count += 1
        else:
            failed_count += 1

        # Rate limiting - small delay between deletions
        if idx < len(tasks_to_delete):
            time.sleep(0.5)

    print("\n" + "=" * 80)
    print("Deletion completed:")
    print(f"  Deleted: {deleted_count}")
    print(f"  Failed: {failed_count}")
    print("=" * 80)


if __name__ == "__main__":
    main()
