#!/usr/bin/env python3
"""Create comprehensive test tasks with all supported data types for export testing."""

import sys
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import requests

from scripts.client import AsanaClientWrapper
from scripts.config import AsanaConfig, get_data_dir

DATA_DIR = get_data_dir()
TASKS_FILE = DATA_DIR / "tasks" / "tasks.parquet"
CUSTOM_FIELDS_FILE = DATA_DIR / "task_custom_fields" / "task_custom_fields.parquet"
DEPENDENCIES_FILE = DATA_DIR / "task_dependencies" / "task_dependencies.parquet"
SNAPSHOTS_DIR = DATA_DIR / "snapshots"


def generate_id() -> str:
    """Generate a 16-character ID."""
    return str(uuid.uuid4())[:16]


def create_snapshot():
    """Create snapshot before modifying."""
    if TASKS_FILE.exists():
        df = pd.read_parquet(TASKS_FILE)
        filename = TASKS_FILE.stem
        timestamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
        SNAPSHOTS_DIR.mkdir(exist_ok=True)
        df.to_parquet(SNAPSHOTS_DIR / f"{filename}-{timestamp}.parquet", index=False)
        print(f"Created snapshot: {SNAPSHOTS_DIR / f'{filename}-{timestamp}.parquet'}")


def get_assignee_gid() -> str:
    """Get current user's GID from target workspace."""
    config = AsanaConfig.from_env()
    target_client = AsanaClientWrapper.from_config_target(config)

    headers = {"Authorization": f"Bearer {target_client._pat}"}
    url = "https://app.asana.com/api/1.0/users/me"
    params = {"opt_fields": "gid"}
    response = requests.get(url, headers=headers, params=params, timeout=30)
    response.raise_for_status()
    return response.json().get("data", {}).get("gid")


def create_test_tasks():
    """Create comprehensive test tasks."""
    print("Creating test tasks with all supported data types...")

    # Create snapshot
    create_snapshot()

    # Load existing tasks
    if TASKS_FILE.exists():
        df = pd.read_parquet(TASKS_FILE)
    else:
        # Create empty dataframe with schema
        df = pd.DataFrame()

    # Get assignee GID
    try:
        assignee_gid = get_assignee_gid()
        assignee_name = "Test User"
    except Exception as e:
        print(f"Warning: Could not get assignee GID: {e}")
        assignee_gid = None
        assignee_name = None

    today = date.today()
    now = datetime.now(tz=pd.Timestamp.now().tz)

    # Test Task 1: Parent task with all features
    parent_id = generate_id()
    parent_task = {
        "task_id": parent_id,
        "title": "[TEST EXPORT] Parent Task - All Features",
        "description": "This is a test parent task with all supported features for export testing.",
        "description_html": "<body>This is a test <strong>parent task</strong> with all supported features for export testing.</body>",
        "status": "pending",
        "due_date": today + timedelta(days=7),
        # "start_date": today,  # Requires premium - skip for testing
        "project_names": "Test Project|Another Test Project",
        "section_names": "In Progress|Backlog",
        "my_tasks_section_names": "Today",
        "assignee_gid": assignee_gid,
        "assignee_name": assignee_name,
        "followers_gids": assignee_gid if assignee_gid else "",
        "follower_names": assignee_name if assignee_name else "",
        "created_at": now,
        "updated_at": now,
        "import_date": today,
        "import_source_file": "test_export",
        "asana_source_gid": None,  # Local-only task
        "asana_target_gid": None,
    }

    # Test Task 2: Child task (level 1)
    child1_id = generate_id()
    child1_task = {
        "task_id": child1_id,
        "title": "[TEST EXPORT] Child Task Level 1",
        "description": "First level child task",
        "description_html": "<body>First level <em>child task</em></body>",
        "status": "in_progress",
        "due_date": today + timedelta(days=5),
        # "start_date": today + timedelta(days=1),  # Requires premium
        "project_names": "Test Project",
        "section_names": "In Progress",
        "my_tasks_section_names": "This Week",
        "assignee_gid": assignee_gid,
        "assignee_name": assignee_name,
        "parent_task_id": parent_id,
        "created_at": now,
        "updated_at": now,
        "import_date": today,
        "import_source_file": "test_export",
        "asana_source_gid": None,
        "asana_target_gid": None,
    }

    # Test Task 3: Grandchild task (level 2)
    child2_id = generate_id()
    child2_task = {
        "task_id": child2_id,
        "title": "[TEST EXPORT] Grandchild Task Level 2",
        "description": "Second level child task (grandchild)",
        "description_html": "<body>Second level <u>child task</u> (grandchild)</body>",
        "status": "pending",
        "due_date": today + timedelta(days=10),
        # "start_date": today + timedelta(days=3),  # Requires premium
        "project_names": "",
        "section_names": "",
        "my_tasks_section_names": "Soon",
        "assignee_gid": assignee_gid,
        "assignee_name": assignee_name,
        "parent_task_id": child1_id,
        "created_at": now,
        "updated_at": now,
        "import_date": today,
        "import_source_file": "test_export",
        "asana_source_gid": None,
        "asana_target_gid": None,
    }

    # Test Task 4: Task with dependencies
    dep_task1_id = generate_id()
    dep_task2_id = generate_id()

    dep_task1 = {
        "task_id": dep_task1_id,
        "title": "[TEST EXPORT] Dependency Task 1 (Predecessor)",
        "description": "This task must be completed before Dependency Task 2",
        "status": "pending",
        "due_date": today + timedelta(days=3),
        "assignee_gid": assignee_gid,
        "assignee_name": assignee_name,
        "created_at": now,
        "updated_at": now,
        "import_date": today,
        "import_source_file": "test_export",
        "asana_source_gid": None,
        "asana_target_gid": None,
    }

    dep_task2 = {
        "task_id": dep_task2_id,
        "title": "[TEST EXPORT] Dependency Task 2 (Successor)",
        "description": "This task depends on Dependency Task 1",
        "status": "blocked",
        "due_date": today + timedelta(days=5),
        "assignee_gid": assignee_gid,
        "assignee_name": assignee_name,
        "created_at": now,
        "updated_at": now,
        "import_date": today,
        "import_source_file": "test_export",
        "asana_source_gid": None,
        "asana_target_gid": None,
    }

    # Test Task 5: Task with custom fields (will need to be created separately)
    cf_task_id = generate_id()
    cf_task = {
        "task_id": cf_task_id,
        "title": "[TEST EXPORT] Task with Custom Fields",
        "description": "This task has custom field values",
        "status": "pending",
        "assignee_gid": assignee_gid,
        "assignee_name": assignee_name,
        "created_at": now,
        "updated_at": now,
        "import_date": today,
        "import_source_file": "test_export",
        "asana_source_gid": None,
        "asana_target_gid": None,
    }

    # Add all tasks to dataframe
    new_tasks = [
        parent_task,
        child1_task,
        child2_task,
        dep_task1,
        dep_task2,
        cf_task,
    ]

    # Ensure all required columns exist
    if df.empty:
        df = pd.DataFrame(columns=parent_task.keys())

    # Add new tasks
    for task in new_tasks:
        # Fill missing columns with None
        for col in df.columns:
            if col not in task:
                task[col] = None

    new_df = pd.DataFrame(new_tasks)
    df = pd.concat([df, new_df], ignore_index=True)

    # Save tasks
    TASKS_FILE.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(TASKS_FILE, index=False)
    print(f"Created {len(new_tasks)} test tasks")

    # Create custom fields for cf_task
    if CUSTOM_FIELDS_FILE.exists():
        pd.read_parquet(CUSTOM_FIELDS_FILE)
    else:
        pd.DataFrame()

    # Note: Custom fields require actual Asana custom field GIDs
    # We'll create placeholder entries that will need real GIDs when exporting
    print("\nNote: Custom fields require actual Asana custom field GIDs.")
    print("You'll need to create custom fields in Asana and update the test data.")

    # Create dependencies
    if DEPENDENCIES_FILE.exists():
        dep_df = pd.read_parquet(DEPENDENCIES_FILE)
    else:
        dep_df = pd.DataFrame()

    dependency = {
        "dependency_id": generate_id(),
        "task_id": dep_task2_id,  # Successor task
        "asana_task_gid": None,  # Will be set after export
        "asana_workspace": None,
        "predecessor_task_id": dep_task1_id,
        "predecessor_asana_gid": None,  # Will be set after export
        "successor_task_id": dep_task2_id,
        "successor_asana_gid": None,  # Will be set after export
        "created_at": now,
        "imported_at": today,
        "import_source_file": "test_export",
    }

    if dep_df.empty:
        dep_df = pd.DataFrame(columns=dependency.keys())

    for col in dep_df.columns:
        if col not in dependency:
            dependency[col] = None

    dep_df = pd.concat([dep_df, pd.DataFrame([dependency])], ignore_index=True)

    DEPENDENCIES_FILE.parent.mkdir(parents=True, exist_ok=True)
    dep_df.to_parquet(DEPENDENCIES_FILE, index=False)
    print("Created 1 dependency relationship")

    print("\nTest tasks created:")
    print(f"  1. Parent Task (ID: {parent_id})")
    print(f"  2. Child Task Level 1 (ID: {child1_id}, parent: {parent_id})")
    print(f"  3. Grandchild Task Level 2 (ID: {child2_id}, parent: {child1_id})")
    print(f"  4. Dependency Task 1 (ID: {dep_task1_id})")
    print(f"  5. Dependency Task 2 (ID: {dep_task2_id}, depends on {dep_task1_id})")
    print(f"  6. Custom Fields Task (ID: {cf_task_id})")
    print(
        "\nReady to export. Run: python execution/scripts/export_asana_tasks.py --limit 10"
    )


if __name__ == "__main__":
    create_test_tasks()
