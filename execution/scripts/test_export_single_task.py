#!/usr/bin/env python3
"""
Test exporting a single task to verify section export works.
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd
import requests

from scripts.client import AsanaClientWrapper
from scripts.config import AsanaConfig, get_data_dir

DATA_DIR = get_data_dir()
TASKS_FILE = DATA_DIR / "tasks" / "tasks.parquet"


def test_export_single_task(task_id: str):
    """Test exporting a single task and verify sections are added."""

    # Load tasks
    df = pd.read_parquet(TASKS_FILE)

    # Find the task
    task = df[df["task_id"].astype(str) == str(task_id)]
    if task.empty:
        print(f"Task {task_id} not found")
        return

    row = task.iloc[0]

    print(f"Testing export for task: {row['title']}")
    print(f"  Project: {row.get('project_names', 'None')}")
    print(f"  Section: {row.get('section_names', 'None')}")
    print(f"  My Tasks Section: {row.get('my_tasks_section_names', 'None')}")
    print()

    # Get config and client
    config = AsanaConfig.from_env()
    target_client = AsanaClientWrapper.from_config_target(config)

    # Extract section info (simulating export script logic)
    row.get("project_names")
    row.get("section_names")
    my_tasks_section_names_str = row.get("my_tasks_section_names")

    mytasks_sections = []

    # Handle My Tasks sections (can exist alongside projects or standalone)
    if pd.notna(my_tasks_section_names_str) and my_tasks_section_names_str:
        my_tasks_section_names = my_tasks_section_names_str.split("|")
        mytasks_sections = [
            s
            for s in my_tasks_section_names
            if s and s not in ["(no section)", "None", ""]
        ]
        print(f"Extracted My Tasks sections: {mytasks_sections}")

    if not mytasks_sections:
        print("No My Tasks sections to export")
        return

    # Get current user GID
    headers = {"Authorization": f"Bearer {target_client._pat}"}
    url = "https://app.asana.com/api/1.0/users/me"
    params = {"opt_fields": "gid"}
    response = requests.get(url, headers=headers, params=params, timeout=30)
    response.raise_for_status()
    me = response.json().get("data", {})
    me.get("gid")

    # Get user task list
    url = "https://app.asana.com/api/1.0/users/me/user_task_lists"
    params = {"workspace": config.target_workspace_gid, "opt_fields": "gid,workspace"}
    response = requests.get(url, headers=headers, params=params, timeout=30)
    response.raise_for_status()
    data = response.json().get("data", [])

    user_task_list_gid = None
    for utl in data:
        workspace_data = utl.get("workspace", {})
        if isinstance(workspace_data, dict):
            if workspace_data.get("gid") == config.target_workspace_gid:
                user_task_list_gid = utl.get("gid")
                break

    if not user_task_list_gid:
        print("Could not find user task list")
        return

    print(f"User task list GID: {user_task_list_gid}")
    print()

    # Check if sections exist
    from scripts.export_asana_tasks import ProjectManager

    project_mgr = ProjectManager(target_client, config.target_workspace_gid)

    for section_name in mytasks_sections:
        print(f"Checking section: {section_name}")
        section_gid = project_mgr.get_or_create_mytasks_section(
            config.target_workspace_gid, section_name
        )
        if section_gid:
            print(f"  Section GID: {section_gid}")
        else:
            print("  Could not get/create section")


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        test_export_single_task(sys.argv[1])
    else:
        # Test with the task we found
        test_export_single_task("1206502542214394")
