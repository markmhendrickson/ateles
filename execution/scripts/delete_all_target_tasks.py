#!/usr/bin/env python3
"""
Delete all tasks and projects from the target Asana workspace.

Tasks and projects are permanently deleted (moved to trash, recoverable for 30 days).

This script is useful when you want to clear the target workspace before
re-exporting tasks from local parquet.

Usage:
    python execution/scripts/delete_all_target_tasks.py              # Delete all tasks and projects
    python execution/scripts/delete_all_target_tasks.py --dry-run    # Preview what would be deleted
    python execution/scripts/delete_all_target_tasks.py --tasks-only  # Delete only tasks
    python execution/scripts/delete_all_target_tasks.py --projects-only  # Delete only projects
"""

import argparse
import sys
import time
from pathlib import Path

import requests

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.client import AsanaClientWrapper
from scripts.config import AsanaConfig


def get_current_user_gid(client: AsanaClientWrapper) -> str | None:
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


def get_all_tasks_in_workspace(
    client: AsanaClientWrapper, workspace_gid: str, dry_run: bool = False
) -> list[dict]:
    """Get all tasks in the workspace by iterating through projects, user task list, and tasks assigned to current user."""
    all_tasks = []
    task_gids = set()  # Track unique tasks

    opt_fields = ["gid", "name", "completed"]

    # Get current user GID for fetching assigned tasks
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

    # Get tasks assigned to current user (workspace-wide, not just in projects)
    if current_user_gid:
        try:
            print("Fetching tasks assigned to current user...")
            opts = {
                "workspace": workspace_gid,
                "assignee": current_user_gid,
                "opt_fields": ",".join(opt_fields),
            }

            assigned_tasks = list(client._with_retry(client.tasks.get_tasks, opts))

            for task_data in assigned_tasks:
                task_gid = task_data.get("gid")
                if task_gid and task_gid not in task_gids:
                    task_gids.add(task_gid)
                    all_tasks.append(task_data)

            print(f"Found {len(assigned_tasks)} tasks assigned to current user")
        except Exception as e:
            print(f"Warning: Error fetching tasks assigned to current user: {e}")

    # Also get tasks from user task list (My Tasks) - this might fail but try anyway
    try:
        print("Fetching tasks from My Tasks...")
        headers = {"Authorization": f"Bearer {client._pat}"}
        url = "https://app.asana.com/api/1.0/users/me/user_task_lists"
        params = {"workspace": workspace_gid, "opt_fields": "gid,workspace"}
        response = requests.get(url, headers=headers, params=params, timeout=30)
        response.raise_for_status()
        data = response.json().get("data", [])

        user_task_list_gid = None
        for utl in data:
            workspace_data = utl.get("workspace", {})
            if isinstance(workspace_data, dict):
                if workspace_data.get("gid") == workspace_gid:
                    user_task_list_gid = utl.get("gid")
                    break
            elif workspace_data == workspace_gid:
                user_task_list_gid = utl.get("gid")
                break

        if user_task_list_gid:
            opts = {"project": user_task_list_gid, "opt_fields": ",".join(opt_fields)}
            try:
                my_tasks = list(client._with_retry(client.tasks.get_tasks, opts))

                for task_data in my_tasks:
                    task_gid = task_data.get("gid")
                    if task_gid and task_gid not in task_gids:
                        task_gids.add(task_gid)
                        all_tasks.append(task_data)

            except Exception as e:
                print(f"  Warning: Error fetching tasks from My Tasks: {e}")

    except Exception as e:
        print(f"Warning: Error fetching My Tasks: {e}")

    return all_tasks


def delete_task(
    client: AsanaClientWrapper, task_gid: str, dry_run: bool = False
) -> bool:
    """Delete a task by GID using Asana API DELETE endpoint."""
    if dry_run:
        return True

    try:
        # Delete task using DELETE endpoint
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


def get_all_projects_in_workspace(
    client: AsanaClientWrapper, workspace_gid: str
) -> list[dict]:
    """Get all projects in the workspace."""
    try:
        projects_opts = {"workspace": workspace_gid, "archived": False}
        projects = list(client._with_retry(client.projects.get_projects, projects_opts))
        return projects
    except Exception as e:
        print(f"Warning: Error fetching projects: {e}")
        return []


def delete_project(
    client: AsanaClientWrapper, project_gid: str, dry_run: bool = False
) -> bool:
    """Delete a project by GID using Asana API DELETE endpoint."""
    if dry_run:
        return True

    try:
        # Delete project using DELETE endpoint
        headers = {"Authorization": f"Bearer {client._pat}"}
        url = f"https://app.asana.com/api/1.0/projects/{project_gid}"
        response = requests.delete(url, headers=headers, timeout=30)

        if response.status_code == 200:
            return True
        elif response.status_code == 404:
            # Project already deleted
            return True
        else:
            error_detail = (
                response.text if response.text else f"HTTP {response.status_code}"
            )
            print(
                f"  Warning: Failed to delete project {project_gid}: {error_detail[:200]}"
            )
            return False

    except Exception as e:
        print(f"  Warning: Error deleting project {project_gid}: {e}")
        return False


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Delete all tasks and projects from target Asana workspace"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview what would be deleted without deleting",
    )
    parser.add_argument(
        "--tasks-only", action="store_true", help="Delete only tasks, not projects"
    )
    parser.add_argument(
        "--projects-only", action="store_true", help="Delete only projects, not tasks"
    )
    args = parser.parse_args()

    config = AsanaConfig.from_env()
    target_client = AsanaClientWrapper.from_config_target(config)

    delete_tasks = not args.projects_only
    delete_projects = not args.tasks_only

    print(f"Target workspace: {config.target_workspace_gid}")
    if args.dry_run:
        print("DRY RUN MODE - Nothing will be deleted")
    print()

    # Get all tasks if needed
    all_tasks = []
    if delete_tasks:
        print("Fetching tasks...")
        all_tasks = get_all_tasks_in_workspace(
            target_client, config.target_workspace_gid, dry_run=args.dry_run
        )
        print(f"Found {len(all_tasks)} task(s)")

    # Get all projects if needed
    all_projects = []
    if delete_projects:
        print("Fetching projects...")
        all_projects = get_all_projects_in_workspace(
            target_client, config.target_workspace_gid
        )
        print(f"Found {len(all_projects)} project(s)")

    if not all_tasks and not all_projects:
        print("No tasks or projects found in target workspace.")
        return

    if args.dry_run:
        if all_tasks:
            print("\nTasks that would be deleted:")
            for task in all_tasks[:20]:  # Show first 20
                task_name = task.get("name", "Unknown")
                completed = task.get("completed", False)
                status = "completed" if completed else "incomplete"
                print(f"  - {task.get('gid')}: {task_name} ({status})")
            if len(all_tasks) > 20:
                print(f"  ... and {len(all_tasks) - 20} more tasks")

        if all_projects:
            print("\nProjects that would be deleted:")
            for project in all_projects:
                project_name = project.get("name", "Unknown")
                print(f"  - {project.get('gid')}: {project_name}")
        return

    # Confirm deletion
    len(all_tasks) + len(all_projects)
    items_desc = []
    if all_tasks:
        items_desc.append(f"{len(all_tasks)} task(s)")
    if all_projects:
        items_desc.append(f"{len(all_projects)} project(s)")

    print(
        f"\nWARNING: This will DELETE {' and '.join(items_desc)} from the target workspace."
    )
    print("Items will be moved to trash (recoverable for 30 days).")
    response = input("Type 'yes' to confirm: ")
    if response.lower() != "yes":
        print("Cancelled.")
        return

    # Delete tasks first
    if delete_tasks and all_tasks:
        print(f"\nDeleting {len(all_tasks)} task(s)...")
        deleted_count = 0
        failed_count = 0

        for idx, task in enumerate(all_tasks, 1):
            task_gid = task.get("gid")
            task_name = task.get("name", "Unknown")

            print(f"Deleting task {idx}/{len(all_tasks)}: {task_name} ({task_gid})")

            if delete_task(target_client, task_gid, dry_run=False):
                deleted_count += 1
            else:
                failed_count += 1

            # Rate limiting - small delay between deletions
            if idx < len(all_tasks):
                time.sleep(0.5)

        print("Tasks completed:")
        print(f"  Deleted: {deleted_count}")
        print(f"  Failed: {failed_count}")

    # Delete projects
    if delete_projects and all_projects:
        print(f"\nDeleting {len(all_projects)} project(s)...")
        deleted_count = 0
        failed_count = 0

        for idx, project in enumerate(all_projects, 1):
            project_gid = project.get("gid")
            project_name = project.get("name", "Unknown")

            print(
                f"Deleting project {idx}/{len(all_projects)}: {project_name} ({project_gid})"
            )

            if delete_project(target_client, project_gid, dry_run=False):
                deleted_count += 1
            else:
                failed_count += 1

            # Rate limiting - small delay between deletions
            if idx < len(all_projects):
                time.sleep(0.5)

        print("Projects completed:")
        print(f"  Deleted: {deleted_count}")
        print(f"  Failed: {failed_count}")

    print("\nAll operations completed.")


if __name__ == "__main__":
    main()
