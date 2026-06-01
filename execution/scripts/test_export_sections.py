#!/usr/bin/env python3
"""
Test script to verify section export for a specific task.
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd

from scripts.client import AsanaClientWrapper
from scripts.config import AsanaConfig, get_data_dir

DATA_DIR = get_data_dir()
TASKS_FILE = DATA_DIR / "tasks" / "tasks.parquet"


def test_section_export():
    """Test section export logic with a real task."""

    # Load tasks
    df = pd.read_parquet(TASKS_FILE)

    # Find a task with my_tasks_section_names that's assigned to user
    config = AsanaConfig.from_env()
    target_client = AsanaClientWrapper.from_config_target(config)

    # Get current user GID
    import requests

    headers = {"Authorization": f"Bearer {target_client._pat}"}
    url = "https://app.asana.com/api/1.0/users/me"
    params = {"opt_fields": "gid,name,email"}
    response = requests.get(url, headers=headers, params=params, timeout=30)
    response.raise_for_status()
    me = response.json().get("data", {})
    assignee_gid = me.get("gid")

    print(f"Current user GID: {assignee_gid}")

    # Find tasks assigned to user with my_tasks_section_names
    test_tasks = df[
        (df["assignee_gid"].astype(str) == str(assignee_gid))
        & (df["my_tasks_section_names"].notna())
        & (df["my_tasks_section_names"] != "")
        & (df["status"].isin(["pending", "in_progress", "blocked"]))
    ].head(3)

    if test_tasks.empty:
        print(
            "No test tasks found. Looking for any task with my_tasks_section_names..."
        )
        test_tasks = df[
            (df["my_tasks_section_names"].notna())
            & (df["my_tasks_section_names"] != "")
        ].head(3)

    if test_tasks.empty:
        print("No tasks with my_tasks_section_names found.")
        return

    print(f"\nFound {len(test_tasks)} test task(s):\n")

    for idx, row in test_tasks.iterrows():
        print(f"Task: {row['title']}")
        print(f"  Task ID: {row['task_id']}")
        print(f"  Project: {row.get('project_names', 'None')}")
        print(f"  Section: {row.get('section_names', 'None')}")
        print(f"  My Tasks Section: {row.get('my_tasks_section_names', 'None')}")
        print(f"  Assignee GID: {row.get('assignee_gid', 'None')}")
        print(f"  Status: {row.get('status', 'None')}")
        print()

        # Test the section extraction logic
        project_names_str = row.get("project_names")
        section_names_str = row.get("section_names")
        my_tasks_section_names_str = row.get("my_tasks_section_names")

        print("  Section extraction test:")
        print(f"    project_names_str: {project_names_str}")
        print(f"    section_names_str: {section_names_str}")
        print(f"    my_tasks_section_names_str: {my_tasks_section_names_str}")

        mytasks_sections = []

        # Simulate the logic from export script
        if pd.notna(my_tasks_section_names_str) and my_tasks_section_names_str:
            my_tasks_section_names = my_tasks_section_names_str.split("|")
            mytasks_sections = [
                s
                for s in my_tasks_section_names
                if s and s not in ["(no section)", "None", ""]
            ]
            print(f"    Extracted My Tasks sections: {mytasks_sections}")
        else:
            print("    No My Tasks sections extracted")

        print()


if __name__ == "__main__":
    test_section_export()
