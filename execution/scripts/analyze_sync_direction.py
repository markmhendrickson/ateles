#!/usr/bin/env python3
"""
Analyze sync direction for specific tasks by comparing local vs remote state.
"""

import sys
from pathlib import Path
from typing import Any

import pandas as pd

EXECUTION = Path(__file__).parent.parent
sys.path.insert(0, str(EXECUTION))

from scripts.client import AsanaClientWrapper
from scripts.config import AsanaConfig


def compare_task_properties(
    local_task: dict, asana_task: dict
) -> dict[str, tuple[str, Any, Any]]:
    """Compare local and Asana task properties.

    Returns: Dict mapping property name to (direction, local_value, asana_value)
    where direction is 'local→remote', 'remote→local', 'match', or 'different'
    """
    differences = {}

    # Title/Name
    local_title = str(local_task.get("title", "") or "").strip()
    asana_name = str(asana_task.get("name", "") or "").strip()
    if local_title != asana_name:
        if local_title:
            differences["name"] = ("local→remote", local_title, asana_name)
        else:
            differences["name"] = ("remote→local", local_title, asana_name)
    else:
        differences["name"] = ("match", local_title, asana_name)

    # Description/Notes
    local_desc = str(local_task.get("description", "") or "").strip()
    local_desc_html = str(local_task.get("description_html", "") or "").strip()
    asana_notes = str(asana_task.get("notes", "") or "").strip()
    asana_html_notes = str(asana_task.get("html_notes", "") or "").strip()

    # Compare notes (prefer HTML if available)
    local_notes = local_desc_html if local_desc_html else local_desc
    asana_notes_final = asana_html_notes if asana_html_notes else asana_notes

    if local_notes != asana_notes_final:
        if local_notes:
            differences["notes"] = (
                "local→remote",
                local_notes[:100] + "..." if len(local_notes) > 100 else local_notes,
                (
                    asana_notes_final[:100] + "..."
                    if len(asana_notes_final) > 100
                    else asana_notes_final
                ),
            )
        else:
            differences["notes"] = (
                "remote→local",
                local_notes,
                (
                    asana_notes_final[:100] + "..."
                    if len(asana_notes_final) > 100
                    else asana_notes_final
                ),
            )
    else:
        differences["notes"] = ("match", "present", "present")

    # Due date
    local_due = local_task.get("due_date")
    asana_due = asana_task.get("due_on")

    if pd.notna(local_due):
        local_due_str = (
            str(local_due)
            if isinstance(local_due, str)
            else (
                local_due.isoformat()
                if hasattr(local_due, "isoformat")
                else str(local_due)
            )
        )
    else:
        local_due_str = None

    if local_due_str != asana_due:
        if local_due_str:
            differences["due_on"] = ("local→remote", local_due_str, asana_due)
        else:
            differences["due_on"] = ("remote→local", local_due_str, asana_due)
    else:
        differences["due_on"] = ("match", local_due_str, asana_due)

    # Assignee
    local_assignee_gid = (
        str(local_task.get("assignee_gid", "") or "")
        if pd.notna(local_task.get("assignee_gid"))
        else None
    )
    asana_assignee = asana_task.get("assignee")
    asana_assignee_gid = (
        str(asana_assignee.get("gid"))
        if isinstance(asana_assignee, dict) and asana_assignee.get("gid")
        else None
    )

    if local_assignee_gid != asana_assignee_gid:
        if local_assignee_gid:
            differences["assignee"] = (
                "local→remote",
                local_assignee_gid,
                asana_assignee_gid,
            )
        else:
            differences["assignee"] = (
                "remote→local",
                local_assignee_gid,
                asana_assignee_gid,
            )
    else:
        differences["assignee"] = ("match", local_assignee_gid, asana_assignee_gid)

    # Status/Completed
    local_status = local_task.get("status", "pending")
    local_completed = local_status == "completed"
    asana_completed = asana_task.get("completed", False)

    if local_completed != asana_completed:
        if local_completed:
            differences["completed"] = (
                "local→remote",
                local_completed,
                asana_completed,
            )
        else:
            differences["completed"] = (
                "remote→local",
                local_completed,
                asana_completed,
            )
    else:
        differences["completed"] = ("match", local_completed, asana_completed)

    # Projects
    local_projects = str(local_task.get("project_names", "") or "").strip()
    asana_projects = asana_task.get("projects", [])
    asana_project_names = [
        p.get("name", "") for p in asana_projects if isinstance(p, dict)
    ]
    asana_projects_str = "|".join(asana_project_names) if asana_project_names else ""

    if local_projects != asana_projects_str:
        if local_projects:
            differences["projects"] = (
                "local→remote",
                local_projects,
                asana_projects_str,
            )
        else:
            differences["projects"] = (
                "remote→local",
                local_projects,
                asana_projects_str,
            )
    else:
        differences["projects"] = ("match", local_projects, asana_projects_str)

    # Sections (from memberships)
    local_sections = str(local_task.get("section_names", "") or "").strip()
    asana_sections = []
    for membership in asana_task.get("memberships", []):
        if isinstance(membership, dict):
            section = membership.get("section", {})
            if isinstance(section, dict) and section.get("name"):
                asana_sections.append(section.get("name"))
    asana_sections_str = "|".join(asana_sections) if asana_sections else ""

    if local_sections != asana_sections_str:
        if local_sections:
            differences["sections"] = (
                "local→remote",
                local_sections,
                asana_sections_str,
            )
        else:
            differences["sections"] = (
                "remote→local",
                local_sections,
                asana_sections_str,
            )
    else:
        differences["sections"] = ("match", local_sections, asana_sections_str)

    # Followers
    local_followers = str(local_task.get("followers_gids", "") or "").strip()
    asana_followers = asana_task.get("followers", [])
    asana_follower_gids = [
        str(f.get("gid", ""))
        for f in asana_followers
        if isinstance(f, dict) and f.get("gid")
    ]
    asana_followers_str = "|".join(asana_follower_gids) if asana_follower_gids else ""

    if local_followers != asana_followers_str:
        if local_followers:
            differences["followers"] = (
                "local→remote",
                local_followers,
                asana_followers_str,
            )
        else:
            differences["followers"] = (
                "remote→local",
                local_followers,
                asana_followers_str,
            )
    else:
        differences["followers"] = ("match", local_followers, asana_followers_str)

    return differences


def analyze_sync_direction(
    task_gids: list[str], workspace_name: str = "target"
) -> dict[str, Any]:
    """Analyze sync direction for specific tasks."""

    config = AsanaConfig.from_env()
    client = (
        AsanaClientWrapper.from_config_target(config)
        if workspace_name == "target"
        else AsanaClientWrapper.from_config_source(config)
    )

    # Load local tasks
    from scripts.config import get_data_dir

    tasks_file = get_data_dir() / "tasks" / "tasks.parquet"
    if not tasks_file.exists():
        print(f"Error: Tasks file not found: {tasks_file}")
        return {}

    df = pd.read_parquet(tasks_file)

    # Filter to tasks with matching target GIDs
    gid_col = "asana_target_gid" if workspace_name == "target" else "asana_source_gid"
    tasks_to_analyze = df[df[gid_col].astype(str).isin(task_gids)]

    if tasks_to_analyze.empty:
        print(f"No tasks found with GIDs: {task_gids}")
        return {}

    print(f"Analyzing {len(tasks_to_analyze)} tasks...\n")

    results = {}

    for idx, row in tasks_to_analyze.iterrows():
        task_gid = str(row[gid_col])
        title = row.get("title", "N/A")

        print(f"Fetching Asana task: {title} ({task_gid})...")

        # Fetch full task details from Asana
        opts = {
            "opt_fields": "name,notes,html_notes,due_on,assignee,assignee.gid,assignee.name,completed,projects,projects.name,projects.gid,memberships,memberships.section,memberships.section.name,followers,followers.gid,followers.name,modified_at"
        }

        try:
            asana_task = client._with_retry(
                client.tasks.get_task, task_gid=task_gid, opts=opts
            )

            # Compare properties
            differences = compare_task_properties(row.to_dict(), asana_task)
            results[task_gid] = {
                "title": title,
                "local_task": row.to_dict(),
                "asana_task": asana_task,
                "differences": differences,
            }

        except Exception as e:
            print(f"  Error fetching task {task_gid}: {e}")
            results[task_gid] = {"title": title, "error": str(e)}

    return results


if __name__ == "__main__":
    # Get the 5 most recently synced tasks
    from scripts.config import get_data_dir

    tasks_file = get_data_dir() / "tasks" / "tasks.parquet"
    df = pd.read_parquet(tasks_file)

    # First try to find synced tasks
    synced_tasks = df[df["sync_log"] == "synced"].sort_values(
        "sync_datetime", ascending=False
    )

    if len(synced_tasks) > 0:
        recent_tasks = synced_tasks.head(5)
        print("=" * 80)
        print("SYNC DIRECTION ANALYSIS")
        print("=" * 80)
        print("\nAnalyzing 5 recently synced tasks:")
        for _, task in recent_tasks.iterrows():
            print(f"  - {task.get('title')} ({task.get('asana_target_gid')})")
        print()
        target_gids = recent_tasks["asana_target_gid"].dropna().astype(str).tolist()
    else:
        # Fallback to recently exported tasks
        recent_exports = (
            df[df["sync_log"] == "exported_success"]
            .sort_values("sync_datetime", ascending=False)
            .head(5)
        )
        target_gids = recent_exports["asana_target_gid"].dropna().astype(str).tolist()
        print("=" * 80)
        print("SYNC DIRECTION ANALYSIS")
        print("=" * 80)
        print("\nAnalyzing 5 recently exported tasks (no synced tasks found):")
        for _, task in recent_exports.iterrows():
            print(f"  - {task.get('title')} ({task.get('asana_target_gid')})")
        print()

    results = analyze_sync_direction(target_gids, workspace_name="target")

    print("\n" + "=" * 80)
    print("SYNC DIRECTION RESULTS")
    print("=" * 80)

    for gid, info in results.items():
        if "error" in info:
            print(f"\n{info['title']} ({gid})")
            print(f"  Error: {info['error']}")
            continue

        print(f"\n{info['title']} ({gid})")
        print("-" * 80)

        differences = info["differences"]

        # Group by direction
        local_to_remote = []
        remote_to_local = []
        matches = []

        for prop, (direction, local_val, asana_val) in differences.items():
            if direction == "local→remote":
                local_to_remote.append((prop, local_val, asana_val))
            elif direction == "remote→local":
                remote_to_local.append((prop, local_val, asana_val))
            elif direction == "match":
                matches.append(prop)

        if local_to_remote:
            print("\n  LOCAL → REMOTE (synced to Asana):")
            for prop, local_val, asana_val in local_to_remote:
                print(f"    {prop}:")
                print(f"      Local:  {local_val}")
                print(f"      Asana:  {asana_val}")

        if remote_to_local:
            print("\n  REMOTE → LOCAL (synced from Asana):")
            for prop, local_val, asana_val in remote_to_local:
                print(f"    {prop}:")
                print(f"      Asana:  {asana_val}")
                print(f"      Local:  {local_val}")

        if matches:
            print(f"\n  MATCHED (no sync needed): {', '.join(matches)}")

        if not local_to_remote and not remote_to_local:
            print("\n  No differences found - all properties match")
