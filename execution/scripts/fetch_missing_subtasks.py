#!/usr/bin/env python3
"""
Fetch and import the 38 missing subtasks identified by diagnose_missing_tasks.py.

This script:
1. Identifies subtasks that are in Asana but not in parquet
2. Fetches each subtask individually
3. Normalizes and adds them to tasks.parquet
"""

import sys
from pathlib import Path

import pandas as pd

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.client import AsanaClientWrapper
from scripts.config import AsanaConfig
from scripts.tally_asana_workspace import get_current_user_gid


def get_all_tasks_simple(client: AsanaClientWrapper, workspace_gid: str) -> set[str]:
    """Get all task GIDs from Asana using the same method as diagnose script."""
    import requests

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


def get_missing_subtask_gids(
    client: AsanaClientWrapper, workspace_gid: str
) -> list[str]:
    """Get list of subtask GIDs that are in Asana but not in parquet.

    Uses same logic as diagnose_missing_tasks.py: compares main tasks from Asana
    with main tasks in parquet to find all missing tasks, then filters for subtasks.
    """
    # Load parquet
    from scripts.config import get_data_dir

    df = pd.read_parquet(get_data_dir() / "tasks" / "tasks.parquet")
    from_source = df[
        (df["asana_source_gid"].notna()) & (df["asana_workspace"] == workspace_gid)
    ]
    # Compare main tasks only (like diagnostic script does)
    main_tasks_parquet = set(
        from_source[from_source["parent_task_id"].isna()]["asana_source_gid"].astype(
            str
        )
    )
    all_tasks_parquet = set(from_source["asana_source_gid"].astype(str))

    # Fetch current tasks from Asana (returns main tasks from projects/My Tasks)
    print("Fetching current tasks from Asana API...")
    asana_task_gids = get_all_tasks_simple(client, workspace_gid)

    # Find missing tasks (in Asana but not in parquet as main tasks)
    missing_in_parquet = asana_task_gids - main_tasks_parquet

    # Filter to only subtasks (check if they have a parent AND are not already in parquet)
    missing_subtasks = []
    for task_gid in missing_in_parquet:
        # Skip if already in parquet (as a subtask)
        if task_gid in all_tasks_parquet:
            continue

        # Check if it's a subtask in Asana
        try:
            opts = {"opt_fields": "gid,name,parent,parent.gid"}
            task = client._with_retry(client.tasks.get_task, task_gid, opts)
            parent = task.get("parent")
            if parent and parent.get("gid"):
                missing_subtasks.append(task_gid)
        except Exception as e:
            print(f"Warning: Could not check parent for {task_gid}: {e}")
            # Skip if we can't check

    return missing_subtasks


def fetch_and_normalize_subtask(
    client: AsanaClientWrapper,
    importer,  # AsanaDirectImporter instance
    subtask_gid: str,
) -> dict:
    """Fetch a single subtask and normalize it."""
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
        "projects",
        "projects.gid",
        "projects.name",
        "projects.color",
        "projects.archived",
        "projects.public",
        "projects.icon",
        "projects.notes",
        "projects.html_notes",
        "projects.due_date",
        "projects.start_on",
        "projects.owner",
        "projects.owner.gid",
        "projects.owner.name",
        "projects.followers",
        "projects.followers.gid",
        "projects.followers.name",
        "projects.members",
        "projects.members.gid",
        "projects.members.name",
        "projects.custom_fields",
        "projects.default_view",
        "memberships",
        "memberships.project.gid",
        "memberships.project.name",
        "memberships.section.gid",
        "memberships.section.name",
        "assignee_section",
        "assignee_section.gid",
        "assignee_section.name",
        "tags",
        "tags.name",
        "permalink_url",
        "followers",
        "followers.gid",
        "followers.name",
        "custom_fields",
        "custom_fields.gid",
        "custom_fields.name",
        "custom_fields.type",
        "custom_fields.text_value",
        "custom_fields.number_value",
        "custom_fields.enum_value",
        "custom_fields.enum_value.name",
        "custom_fields.date_value",
        "custom_fields.people_value",
        "custom_fields.people_value.gid",
        "custom_fields.people_value.name",
        "custom_fields.multi_enum_values",
        "custom_fields.multi_enum_values.name",
        "dependencies",
        "dependencies.predecessor.gid",
        "dependencies.successor.gid",
        "parent",
        "parent.gid",
    ]
    opts = {"opt_fields": ",".join(opt_fields)}

    try:
        task_data = client._with_retry(client.tasks.get_task, subtask_gid, opts)

        # Get parent GID
        parent = task_data.get("parent", {})
        parent_gid = parent.get("gid") if parent else None

        # Normalize the subtask
        normalized = importer.normalize_task(task_data, parent_task_id=parent_gid)

        # Download description attachments and rewrite HTML
        from scripts.import_asana_tasks import (
            download_description_attachments,
            html_to_local_text,
            rewrite_html_with_local_attachments,
        )

        description_attachment_map = download_description_attachments(
            client, subtask_gid, normalized.get("description_html_remote")
        )
        normalized["description_html"] = rewrite_html_with_local_attachments(
            normalized.get("description_html_remote"), description_attachment_map
        )
        if normalized.get("description_html"):
            normalized["description"] = html_to_local_text(
                normalized["description_html"]
            )

        return normalized
    except Exception as e:
        print(f"Error fetching subtask {subtask_gid}: {e}")
        return None


def main() -> None:
    config = AsanaConfig.from_env()
    source_client = AsanaClientWrapper.from_config_source(config)
    source_workspace = config.source_workspace_gid

    # Import the importer class
    from scripts.import_asana_tasks import AsanaDirectImporter

    importer = AsanaDirectImporter(
        config=config,
        only_incomplete=False,
        assignee_gid=None,
        max_tasks=None,
        recalculate=False,
        download_attachments=False,
        include_archived=True,
        task_gid=None,
        resume=False,
        checkpoint_interval=100,
    )

    print("=" * 60)
    print("FETCHING MISSING SUBTASKS")
    print("=" * 60)
    print()

    # Get missing subtask GIDs
    print("Identifying missing subtasks...")
    missing_subtask_gids = get_missing_subtask_gids(source_client, source_workspace)
    print(f"Found {len(missing_subtask_gids)} missing subtasks")
    print()

    if not missing_subtask_gids:
        print("No missing subtasks to fetch.")
        return

    # Create snapshot
    importer.create_parquet_snapshot()

    # Fetch and normalize each subtask
    print("Fetching and normalizing subtasks...")
    normalized_subtasks = []
    failed = []

    for idx, subtask_gid in enumerate(missing_subtask_gids, 1):
        print(
            f"  [{idx}/{len(missing_subtask_gids)}] Fetching subtask {subtask_gid}..."
        )
        normalized = fetch_and_normalize_subtask(source_client, importer, subtask_gid)
        if normalized:
            normalized_subtasks.append(normalized)
            title = normalized.get("title", "Untitled")[:50]
            print(f"    ✓ {title}")
        else:
            failed.append(subtask_gid)
            print("    ✗ Failed to fetch")

    print()
    print(f"Successfully fetched: {len(normalized_subtasks)}")
    print(f"Failed: {len(failed)}")
    if failed:
        print(f"Failed GIDs: {failed}")
    print()

    if not normalized_subtasks:
        print("No subtasks to import.")
        return

    # Merge and save to parquet
    print("Merging with existing tasks...")
    merged_df, stats = importer.merge_and_save_tasks(
        normalized_subtasks, log_individual_tasks=True
    )

    print()
    print("=" * 60)
    print("IMPORT COMPLETE")
    print("=" * 60)
    print(f"Tasks added: {stats['tasks_new']}")
    print(f"Tasks updated: {stats['tasks_updated']}")
    print(f"Tasks skipped: {stats['tasks_skipped']}")
    print(f"Total tasks in parquet: {len(merged_df):,}")
    print()


if __name__ == "__main__":
    main()
