#!/usr/bin/env python3
"""
Diagnose why there are fewer tasks in parquet than the tally script reports.

Compares:
- Current tasks from Asana API (using same method as tally script)
- Tasks in parquet file
- Identifies missing tasks and their characteristics
"""

import sys
from pathlib import Path

import pandas as pd

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import requests

from scripts.client import AsanaClientWrapper
from scripts.config import AsanaConfig
from scripts.tally_asana_workspace import get_current_user_gid


def get_all_tasks_simple(client: AsanaClientWrapper, workspace_gid: str) -> set[str]:
    """Get all task GIDs from Asana using the same method as tally script."""
    all_task_gids = set()

    opt_fields = ["gid", "name", "completed", "projects", "memberships"]
    current_user_gid = get_current_user_gid(client)

    # Get tasks from all projects
    try:
        # Non-archived projects
        projects_opts = {"workspace": workspace_gid, "archived": False}
        projects = list(client._with_retry(client.projects.get_projects, projects_opts))

        for project in projects:
            project_gid = project.get("gid")
            opts = {"project": project_gid, "opt_fields": ",".join(opt_fields)}
            try:
                project_tasks = list(client._with_retry(client.tasks.get_tasks, opts))
                for task_data in project_tasks:
                    task_gid = task_data.get("gid")
                    if task_gid:
                        all_task_gids.add(task_gid)
            except Exception as e:
                print(f"  Warning: Error fetching tasks from project: {e}")
                continue

        # Archived projects
        archived_projects_opts = {"workspace": workspace_gid, "archived": True}
        archived_projects = list(
            client._with_retry(client.projects.get_projects, archived_projects_opts)
        )

        for project in archived_projects:
            project_gid = project.get("gid")
            opts = {"project": project_gid, "opt_fields": ",".join(opt_fields)}
            try:
                project_tasks = list(client._with_retry(client.tasks.get_tasks, opts))
                for task_data in project_tasks:
                    task_gid = task_data.get("gid")
                    if task_gid:
                        all_task_gids.add(task_gid)
            except Exception as e:
                print(f"  Warning: Error fetching tasks from archived project: {e}")
                continue

    except Exception as e:
        print(f"Warning: Error fetching projects: {e}")

    # Get tasks assigned to current user (standalone only)
    if current_user_gid:
        try:
            opts = {
                "workspace": workspace_gid,
                "assignee": current_user_gid,
                "opt_fields": ",".join(opt_fields),
            }

            assigned_tasks = list(client._with_retry(client.tasks.get_tasks, opts))

            for task_data in assigned_tasks:
                task_gid = task_data.get("gid")
                if task_gid in all_task_gids:
                    continue

                # Only include standalone tasks
                task_projects = task_data.get("projects", [])
                memberships = task_data.get("memberships", [])
                has_projects = bool(task_projects) or any(
                    m.get("project", {}).get("gid")
                    for m in memberships
                    if m.get("project")
                )

                if not has_projects:
                    all_task_gids.add(task_gid)
        except Exception as e:
            print(f"Warning: Error fetching assigned tasks: {e}")

    # Get tasks from My Tasks
    try:
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
                    if task_gid:
                        all_task_gids.add(task_gid)
            except Exception as e:
                print(f"  Warning: Error fetching My Tasks: {e}")

    except Exception as e:
        print(f"Warning: Error fetching My Tasks: {e}")

    return all_task_gids


def get_task_details(
    client: AsanaClientWrapper, task_gids: list[str]
) -> dict[str, dict]:
    """Get detailed info for specific tasks."""
    task_details = {}

    for task_gid in task_gids:
        try:
            opts = {"opt_fields": "gid,name,completed,parent,projects,memberships"}
            task = client._with_retry(client.tasks.get_task, task_gid, opts)
            task_details[task_gid] = {
                "name": task.get("name", ""),
                "completed": task.get("completed", False),
                "parent": (
                    task.get("parent", {}).get("gid") if task.get("parent") else None
                ),
                "projects": [p.get("gid") for p in task.get("projects", [])],
            }
        except Exception as e:
            task_details[task_gid] = {"error": str(e)}

    return task_details


def main() -> None:
    config = AsanaConfig.from_env()
    source_client = AsanaClientWrapper.from_config_source(config)
    source_workspace = config.source_workspace_gid

    print("=" * 60)
    print("DIAGNOSING MISSING TASKS")
    print("=" * 60)
    print()

    # Load parquet
    print("Loading tasks from parquet...")
    from scripts.config import get_data_dir

    df = pd.read_parquet(get_data_dir() / "tasks" / "tasks.parquet")
    from_source = df[
        (df["asana_source_gid"].notna()) & (df["asana_workspace"] == source_workspace)
    ]
    main_tasks_parquet = set(
        from_source[from_source["parent_task_id"].isna()]["asana_source_gid"].astype(
            str
        )
    )
    print(f"Main tasks in parquet: {len(main_tasks_parquet):,}")
    print()

    # Fetch current tasks from Asana
    print("Fetching current tasks from Asana API...")
    print("(This may take a few minutes...)")
    asana_task_gids = get_all_tasks_simple(source_client, source_workspace)
    print(f"Main tasks in Asana (current): {len(asana_task_gids):,}")
    print()

    # Compare
    missing_in_parquet = asana_task_gids - main_tasks_parquet
    extra_in_parquet = main_tasks_parquet - asana_task_gids

    print("=" * 60)
    print("COMPARISON RESULTS")
    print("=" * 60)
    print(f"Tasks in Asana but NOT in parquet: {len(missing_in_parquet):,}")
    print(f"Tasks in parquet but NOT in Asana: {len(extra_in_parquet):,}")
    print()

    # Analyze missing tasks
    if missing_in_parquet:
        print("=" * 60)
        print("MISSING TASKS (in Asana but not in parquet)")
        print("=" * 60)
        print(f"Fetching details for {len(missing_in_parquet)} missing tasks...")
        missing_details = get_task_details(
            source_client, list(missing_in_parquet)[:50]
        )  # Limit to 50 for speed

        subtask_count = 0
        completed_count = 0
        error_count = 0

        for task_gid, details in list(missing_details.items())[:20]:  # Show first 20
            if "error" in details:
                print(f"  {task_gid}: ERROR - {details['error']}")
                error_count += 1
            else:
                is_subtask = details.get("parent") is not None
                is_completed = details.get("completed", False)
                name = details.get("name", "Untitled")[:60]

                if is_subtask:
                    subtask_count += 1
                    print(f"  {task_gid}: {name} [SUBTASK]")
                elif is_completed:
                    completed_count += 1
                    print(f"  {task_gid}: {name} [COMPLETED]")
                else:
                    print(f"  {task_gid}: {name}")

        if len(missing_in_parquet) > 20:
            print(f"  ... and {len(missing_in_parquet) - 20} more")

        print()
        print("Summary of missing tasks:")
        print(f"  - Subtasks: {subtask_count}")
        print(f"  - Completed: {completed_count}")
        print(f"  - Errors fetching: {error_count}")
        print()

    # Analyze extra tasks
    if extra_in_parquet:
        print("=" * 60)
        print("EXTRA TASKS (in parquet but not in Asana)")
        print("=" * 60)
        extra_tasks = from_source[
            from_source["asana_source_gid"].isin(extra_in_parquet)
        ]

        completed_count = 0
        for idx, row in extra_tasks.head(20).iterrows():
            title = row.get("title", "Untitled")[:60]
            status = row.get("status", "unknown")
            if status == "completed":
                completed_count += 1
            print(f"  {row.get('asana_source_gid')}: {title} [{status}]")

        if len(extra_in_parquet) > 20:
            print(f"  ... and {len(extra_in_parquet) - 20} more")

        print()
        print("Summary of extra tasks:")
        print(f"  - Completed: {completed_count}")
        print("  - Likely deleted from Asana but preserved in parquet")
        print()


if __name__ == "__main__":
    main()
