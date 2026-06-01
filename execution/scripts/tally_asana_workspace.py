#!/usr/bin/env python3
"""
Scan source Asana workspace and tally all tasks and projects.

Counts:
- All projects (archived and non-archived)
- All tasks (within projects and standalone)
"""

import sys
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


def get_all_projects(client: AsanaClientWrapper, workspace_gid: str) -> list[dict]:
    """Get all projects in the workspace (archived and non-archived)."""
    all_projects = []

    try:
        # Get non-archived projects
        print("Fetching non-archived projects...")
        projects_opts = {"workspace": workspace_gid, "archived": False}
        projects = list(client._with_retry(client.projects.get_projects, projects_opts))
        all_projects.extend(projects)
        print(f"Found {len(projects)} non-archived project(s)")

        # Get archived projects
        print("Fetching archived projects...")
        projects_opts = {"workspace": workspace_gid, "archived": True}
        archived_projects = list(
            client._with_retry(client.projects.get_projects, projects_opts)
        )
        all_projects.extend(archived_projects)
        print(f"Found {len(archived_projects)} archived project(s)")

    except Exception as e:
        print(f"Warning: Error fetching projects: {e}")

    return all_projects


def fetch_subtasks_recursive(
    client: AsanaClientWrapper,
    task_gid: str,
    all_subtasks: list[dict],
    seen_gids: set[str],
) -> None:
    """Recursively fetch all subtasks for a given task."""
    opt_fields = ["gid", "name", "completed", "parent", "parent.gid"]
    opts = {"opt_fields": ",".join(opt_fields)}

    try:
        subtasks = list(
            client._with_retry(client.tasks.get_subtasks_for_task, task_gid, opts)
        )

        for subtask_data in subtasks:
            subtask_gid = subtask_data.get("gid")
            if not subtask_gid or subtask_gid in seen_gids:
                continue

            seen_gids.add(subtask_gid)
            all_subtasks.append(subtask_data)

            # Recursively fetch subtasks of this subtask
            fetch_subtasks_recursive(client, subtask_gid, all_subtasks, seen_gids)
    except Exception:
        # If task has no subtasks or error occurs, return
        pass


def get_all_tasks(client: AsanaClientWrapper, workspace_gid: str) -> list[dict]:
    """Get all tasks in the workspace by iterating through projects, user task list, and tasks assigned to current user.

    Prevents duplicate counting by:
    - Tracking unique tasks by GID
    - Excluding tasks from assigned-to-user query that are already in projects
    - Excluding tasks from My Tasks that are already counted

    Also recursively fetches all subtasks to match import script behavior.
    """
    all_tasks = []
    task_gids = set()  # Track unique tasks

    # Need project/membership info to detect if task is in projects
    # Also need parent info to identify subtasks
    opt_fields = [
        "gid",
        "name",
        "completed",
        "projects",
        "memberships",
        "parent",
        "parent.gid",
    ]

    # Get current user GID for fetching assigned tasks
    current_user_gid = get_current_user_gid(client)

    # Get tasks from all projects (both archived and non-archived for complete tally)
    tasks_from_projects = 0
    tasks_from_archived = 0
    try:
        print("Fetching non-archived projects for task collection...")
        projects_opts = {"workspace": workspace_gid, "archived": False}
        projects = list(client._with_retry(client.projects.get_projects, projects_opts))
        print(f"Scanning {len(projects)} non-archived project(s) for tasks...")

        for idx, project in enumerate(projects, 1):
            project_gid = project.get("gid")
            project_name = project.get("name", "")

            opts = {"project": project_gid, "opt_fields": ",".join(opt_fields)}

            try:
                project_tasks = list(client._with_retry(client.tasks.get_tasks, opts))

                for task_data in project_tasks:
                    task_gid = task_data.get("gid")
                    if task_gid and task_gid not in task_gids:
                        task_gids.add(task_gid)
                        all_tasks.append(task_data)
                        tasks_from_projects += 1

            except Exception as e:
                print(
                    f"  Warning: Error fetching tasks from project '{project_name}': {e}"
                )
                continue

        print(f"Found {tasks_from_projects} unique task(s) from non-archived projects")

        # Also fetch tasks from archived projects
        print("Fetching archived projects for task collection...")
        archived_projects_opts = {"workspace": workspace_gid, "archived": True}
        archived_projects = list(
            client._with_retry(client.projects.get_projects, archived_projects_opts)
        )
        print(f"Scanning {len(archived_projects)} archived project(s) for tasks...")

        for idx, project in enumerate(archived_projects, 1):
            project_gid = project.get("gid")
            project_name = project.get("name", "")

            opts = {"project": project_gid, "opt_fields": ",".join(opt_fields)}

            try:
                project_tasks = list(client._with_retry(client.tasks.get_tasks, opts))

                for task_data in project_tasks:
                    task_gid = task_data.get("gid")
                    if task_gid and task_gid not in task_gids:
                        task_gids.add(task_gid)
                        all_tasks.append(task_data)
                        tasks_from_archived += 1

            except Exception as e:
                print(
                    f"  Warning: Error fetching tasks from archived project '{project_name}': {e}"
                )
                continue

        print(f"Found {tasks_from_archived} unique task(s) from archived projects")
        print(f"Total from projects: {tasks_from_projects + tasks_from_archived}")

    except Exception as e:
        print(f"Warning: Error fetching projects: {e}")

    # Get tasks assigned to current user that aren't in projects (standalone tasks only)
    standalone_tasks = 0
    if current_user_gid:
        try:
            print("Fetching tasks assigned to current user (standalone only)...")
            opts = {
                "workspace": workspace_gid,
                "assignee": current_user_gid,
                "opt_fields": ",".join(opt_fields),
            }

            assigned_tasks = list(client._with_retry(client.tasks.get_tasks, opts))

            for task_data in assigned_tasks:
                task_gid = task_data.get("gid")
                if task_gid in task_gids:
                    continue  # Skip duplicates (already fetched from projects)

                # Only include tasks that aren't in any projects
                # Check both 'projects' array and 'memberships' for project associations
                task_projects = task_data.get("projects", [])
                memberships = task_data.get("memberships", [])
                has_projects = bool(task_projects) or any(
                    m.get("project", {}).get("gid")
                    for m in memberships
                    if m.get("project")
                )

                if has_projects:
                    continue  # Skip tasks that are in projects

                task_gids.add(task_gid)
                all_tasks.append(task_data)
                standalone_tasks += 1

            print(
                f"Found {len(assigned_tasks)} assigned task(s) total, {standalone_tasks} standalone (not in projects)"
            )
        except Exception as e:
            print(f"Warning: Error fetching tasks assigned to current user: {e}")

    # Also get tasks from user task list (My Tasks) - exclude duplicates
    my_tasks_count = 0
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
                    if task_gid not in task_gids:
                        task_gids.add(task_gid)
                        all_tasks.append(task_data)
                        my_tasks_count += 1

                print(
                    f"Found {len(my_tasks)} task(s) in My Tasks, {my_tasks_count} new unique task(s)"
                )

            except Exception as e:
                print(f"  Warning: Error fetching tasks from My Tasks: {e}")

    except Exception as e:
        print(f"Warning: Error fetching My Tasks: {e}")

    # Now recursively fetch all subtasks for all main tasks
    print("Fetching all subtasks recursively...")
    all_subtasks = []
    subtask_gids = set()

    # Process each main task to fetch its subtasks
    main_task_gids = [
        t.get("gid")
        for t in all_tasks
        if t.get("gid") and not (t.get("parent") and t.get("parent", {}).get("gid"))
    ]

    for idx, main_task_gid in enumerate(main_task_gids, 1):
        if idx % 100 == 0:
            print(f"  Processing main task {idx}/{len(main_task_gids)} for subtasks...")
        fetch_subtasks_recursive(client, main_task_gid, all_subtasks, subtask_gids)

    # Add subtasks to all_tasks (avoid duplicates)
    for subtask in all_subtasks:
        subtask_gid = subtask.get("gid")
        if subtask_gid and subtask_gid not in task_gids:
            task_gids.add(subtask_gid)
            all_tasks.append(subtask)

    print(f"Found {len(all_subtasks)} total subtasks (recursive)")

    return all_tasks


def main() -> None:
    config = AsanaConfig.from_env()
    source_client = AsanaClientWrapper.from_config_source(config)

    print(f"Source workspace: {config.source_workspace_gid}")
    print()

    # Get all projects
    print("=" * 60)
    print("PROJECTS")
    print("=" * 60)
    all_projects = get_all_projects(source_client, config.source_workspace_gid)

    # Count archived vs non-archived
    archived_count = sum(1 for p in all_projects if p.get("archived", False))
    non_archived_count = len(all_projects) - archived_count

    print()
    print(f"Total projects: {len(all_projects)}")
    print(f"  Non-archived: {non_archived_count}")
    print(f"  Archived: {archived_count}")
    print()

    # Get all tasks
    print("=" * 60)
    print("TASKS")
    print("=" * 60)
    all_tasks = get_all_tasks(source_client, config.source_workspace_gid)

    # Separate main tasks from subtasks
    main_tasks = []
    subtasks = []
    for task in all_tasks:
        parent = task.get("parent")
        if parent and parent.get("gid"):
            subtasks.append(task)
        else:
            main_tasks.append(task)

    # Count completed vs incomplete for main tasks
    main_completed = sum(1 for t in main_tasks if t.get("completed", False))
    main_incomplete = len(main_tasks) - main_completed

    # Count completed vs incomplete for subtasks
    subtask_completed = sum(1 for t in subtasks if t.get("completed", False))
    subtask_incomplete = len(subtasks) - subtask_completed

    # Total counts
    total_completed = main_completed + subtask_completed
    total_incomplete = main_incomplete + subtask_incomplete

    print()
    print(f"Total unique tasks: {len(all_tasks)}")
    print(f"  Main tasks: {len(main_tasks)}")
    print(f"    Incomplete: {main_incomplete}")
    print(f"    Completed: {main_completed}")
    print(f"  Subtasks: {len(subtasks)}")
    print(f"    Incomplete: {subtask_incomplete}")
    print(f"    Completed: {subtask_completed}")
    print()
    print("Overall:")
    print(f"  Incomplete: {total_incomplete}")
    print(f"  Completed: {total_completed}")
    print()

    # Summary
    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"Projects: {len(all_projects)}")
    print(f"  Non-archived: {non_archived_count}")
    print(f"  Archived: {archived_count}")
    print()
    print(f"Tasks (deduplicated): {len(all_tasks)}")
    print(f"  Main tasks: {len(main_tasks)}")
    print(f"    Incomplete: {main_incomplete}")
    print(f"    Completed: {main_completed}")
    print(f"  Subtasks: {len(subtasks)}")
    print(f"    Incomplete: {subtask_incomplete}")
    print(f"    Completed: {subtask_completed}")
    print()


if __name__ == "__main__":
    main()
