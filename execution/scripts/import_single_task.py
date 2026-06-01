#!/usr/bin/env python3
"""
Import a single task by GID and verify subtasks are imported.
"""

import sys
from pathlib import Path

# Add execution directory to path (where scripts are located)
EXECUTION_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(EXECUTION_DIR))

from datetime import datetime

import pandas as pd

from scripts.client import AsanaClientWrapper
from scripts.config import AsanaConfig
from scripts.import_asana_task_comments import (
    download_description_attachments,
    html_to_local_text,
    rewrite_html_with_local_attachments,
)
from scripts.import_asana_tasks import AsanaDirectImporter


def import_single_task(task_gid: str):
    """Import a single task by GID and verify subtasks."""
    config = AsanaConfig.from_env()
    client = AsanaClientWrapper(config.source_pat)

    print(f"Importing task GID: {task_gid}")
    print("=" * 60)

    # Fetch the task
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
    ]

    opts = {"opt_fields": ",".join(opt_fields)}

    try:
        task_data = client._with_retry(client.tasks.get_task, task_gid, opts)
        print(f"Task: {task_data.get('name', 'Unknown')}")
        print(f"Completed: {task_data.get('completed', False)}")
    except Exception as e:
        print(f"Error fetching task: {e}")
        import traceback

        traceback.print_exc()
        return

    # Create importer instance
    importer = AsanaDirectImporter(config, recalculate=False)

    # Normalize the task
    normalized = importer.normalize_task(task_data)
    task_id = normalized["task_id"]
    print(f"\nNormalized task_id: {task_id}")

    # Download description attachments and rewrite HTML
    description_attachment_map = download_description_attachments(
        client, task_gid, normalized.get("description_html_remote")
    )
    normalized["description_html"] = rewrite_html_with_local_attachments(
        normalized.get("description_html_remote"), description_attachment_map
    )
    if normalized.get("description_html"):
        normalized["description"] = html_to_local_text(normalized["description_html"])

    # Fetch subtasks
    print("\nFetching subtasks...")
    try:
        all_subtasks = importer.fetch_all_subtasks_recursive(
            task_gid, parent_task_id=task_gid
        )
        print(f"Found {len(all_subtasks)} subtask(s)")

        if all_subtasks:
            print("\nSubtasks:")
            for i, (subtask_data, parent_id) in enumerate(all_subtasks, 1):
                subtask_gid = subtask_data.get("gid")
                subtask_name = subtask_data.get("name", "Unknown")
                print(
                    f"  {i}. {subtask_name} (GID: {subtask_gid}, parent: {parent_id})"
                )
        else:
            print("No subtasks found")
    except Exception as e:
        print(f"Error fetching subtasks: {e}")
        import traceback

        traceback.print_exc()
        return

    # Normalize subtasks
    normalized_tasks = [normalized]
    subtask_gids = []

    for subtask_data, parent_id in all_subtasks:
        subtask_normalized = importer.normalize_task(
            subtask_data, parent_task_id=parent_id
        )
        subtask_gid = subtask_normalized["task_id"]
        subtask_gids.append(subtask_gid)

        # Download description attachments and rewrite HTML for subtask
        subtask_attachment_map = download_description_attachments(
            client, subtask_gid, subtask_normalized.get("description_html_remote")
        )
        subtask_normalized["description_html"] = rewrite_html_with_local_attachments(
            subtask_normalized.get("description_html_remote"), subtask_attachment_map
        )
        if subtask_normalized.get("description_html"):
            subtask_normalized["description"] = html_to_local_text(
                subtask_normalized["description_html"]
            )
        normalized_tasks.append(subtask_normalized)

    # Load existing tasks
    from scripts.config import get_data_dir

    data_dir = get_data_dir()
    tasks_file = data_dir / "tasks" / "tasks.parquet"
    snapshots_dir = data_dir / "snapshots"
    snapshots_dir.mkdir(parents=True, exist_ok=True)

    # Create snapshot
    if tasks_file.exists():
        timestamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
        snapshot_file = snapshots_dir / f"tasks-{timestamp}.parquet"
        pd.read_parquet(tasks_file).to_parquet(snapshot_file, index=False)
        print(f"\nCreated snapshot: {snapshot_file.name}")

    if tasks_file.exists():
        existing_df = pd.read_parquet(tasks_file)
    else:
        existing_df = pd.DataFrame()

    # Merge tasks
    fetched_task_ids = {t["task_id"] for t in normalized_tasks}

    if not existing_df.empty:
        existing_asana_mask = (
            existing_df["asana_source_gid"].notna()
            if "asana_source_gid" in existing_df.columns
            else pd.Series(False, index=existing_df.index)
        )
        non_asana_df = existing_df[~existing_asana_mask]
        existing_asana_df = existing_df[existing_asana_mask]

        existing_asana_dict = {}
        existing_by_gid = {}
        if not existing_asana_df.empty:
            for _, row in existing_asana_df.iterrows():
                task_id_val = str(row["task_id"])
                existing_asana_dict[task_id_val] = row.to_dict()
                gid_val = str(row.get("asana_source_gid", ""))
                if gid_val:
                    existing_by_gid[gid_val] = row.to_dict()
    else:
        non_asana_df = pd.DataFrame()
        existing_asana_dict = {}
        existing_by_gid = {}

    merged_tasks = []
    tasks_new = 0
    tasks_updated = 0

    for new_task in normalized_tasks:
        task_id = new_task["task_id"]
        asana_gid = new_task.get("asana_source_gid")

        if task_id in existing_asana_dict:
            existing_task = existing_asana_dict[task_id]
            merged_task = importer.merge_task_fields(existing_task, new_task)
            merged_tasks.append(merged_task)
            tasks_updated += 1
        elif asana_gid and asana_gid in existing_by_gid:
            existing_task = existing_by_gid[asana_gid]
            existing_task["task_id"] = task_id
            merged_task = importer.merge_task_fields(existing_task, new_task)
            merged_tasks.append(merged_task)
            tasks_updated += 1
        else:
            merged_tasks.append(new_task)
            tasks_new += 1

    # Preserve existing tasks
    for task_id, existing_task in existing_asana_dict.items():
        if task_id not in fetched_task_ids:
            merged_tasks.append(existing_task)

    # Combine with non-Asana tasks
    if not non_asana_df.empty:
        for _, row in non_asana_df.iterrows():
            merged_tasks.append(row.to_dict())

    # Save
    new_df = pd.DataFrame(merged_tasks)
    new_df.to_parquet(tasks_file, index=False)

    print("\n=== Import Complete ===")
    print(f"Tasks updated: {tasks_updated}")
    print(f"Tasks added: {tasks_new}")
    print(f"Total tasks: {len(new_df)}")
    print(f"Subtasks imported: {len(subtask_gids)}")

    # Verify subtasks
    print("\n=== Verification ===")
    final_df = pd.read_parquet(tasks_file)
    subtasks = final_df[final_df["parent_task_id"] == task_id]
    print(f"Tasks with parent_task_id = {task_id}: {len(subtasks)}")
    if len(subtasks) > 0:
        print("\nImported subtasks:")
        for _, subtask in subtasks.iterrows():
            print(f"  - {subtask['title']} (task_id: {subtask['task_id']})")
    else:
        print("No subtasks found with parent_task_id set")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python import_single_task.py <task_gid>")
        print("Example: python import_single_task.py 1208198398045190")
        sys.exit(1)

    task_gid = sys.argv[1]
    import_single_task(task_gid)
