#!/usr/bin/env python3
"""Export a single task by title to Asana target workspace."""

import sys
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent.resolve()))

import requests

from scripts.client import AsanaClientWrapper
from scripts.config import AsanaConfig
from scripts.export_asana_tasks import (
    TASKS_FILE,
    ProjectManager,
    add_followers_to_task,
    add_tags_to_task,
    fetch_and_post_comments,
    fetch_and_upload_attachments,
    get_assignee_gid,
    set_custom_fields_on_task,
    snapshot_tasks,
)


def export_task_by_title(title_pattern: str) -> None:
    """Export a single task matching the title pattern."""
    # Find the task
    df = pd.read_parquet(TASKS_FILE)
    matching = df[df["title"].str.contains(title_pattern, case=False, na=False)]

    if matching.empty:
        print(f"No task found matching '{title_pattern}'")
        return

    if len(matching) > 1:
        print(f"Multiple tasks found matching '{title_pattern}':")
        for idx, row in matching.iterrows():
            print(f"  - {row['title']}")
        print("Using the first one.")

    task = matching.iloc[0]
    task_idx = task.name

    print(f"Exporting task: {task['title']}")
    print(f"Task ID: {task['task_id']}")
    print(f"Asana Source GID: {task.get('asana_source_gid', 'None')}")
    print(f"Asana Target GID: {task.get('asana_target_gid', 'None')}")
    print()

    # Create a single-row dataframe with just this task
    candidates = df.loc[[task_idx]]

    # Set up clients
    config = AsanaConfig.from_env()
    target_client = AsanaClientWrapper.from_config_target(config)
    source_client = AsanaClientWrapper.from_config_source(config)
    assignee_gid = get_assignee_gid(target_client, config)

    # Snapshot
    snapshot_tasks(df)

    # Set up project manager
    project_mgr = ProjectManager(target_client, config.target_workspace_gid)

    # Post the task (using the same logic as post_tasks)
    created = []
    today = date.today()

    for idx, row in candidates.iterrows():
        title = row["title"]

        # Use HTML description if available
        description_html = row.get("description_html")
        description = row.get("description") or ""
        notes = (
            description_html
            if description_html and pd.notna(description_html)
            else (description or row.get("notes") or "")
        )

        # Handle dates
        due_on = row.get("due_date")
        start_on = row.get("start_date")

        # Determine assignee
        source_gid = row.get("asana_source_gid")
        if source_gid:
            try:
                src_opts = {"opt_fields": "assignee,assignee.gid"}
                src_task = source_client._with_retry(
                    source_client.tasks.get_task,
                    source_gid,
                    src_opts,
                )
                src_assignee_gid = (
                    src_task.get("assignee", {}).get("gid")
                    if src_task.get("assignee")
                    else None
                )
                if src_assignee_gid == assignee_gid:
                    assignee = assignee_gid
                else:
                    assignee = None
            except Exception:
                assignee = None
        else:
            assignee = assignee_gid if row.get("assignee_gid") else None

        # Check if task already exists
        task_id_val = str(row.get("task_id", ""))
        existing_target_gid = None

        # Check for existing target GID in multiple ways:
        # 1. asana_target_gid field (primary check)
        asana_target_gid = row.get("asana_target_gid")
        if pd.notna(asana_target_gid) and asana_target_gid:
            existing_target_gid = str(asana_target_gid)
        # 2. task_id if import_source_file indicates it was exported
        elif str(row.get("import_source_file")) == "asana-post" and task_id_val:
            existing_target_gid = task_id_val
        # 3. Verify task_id is a valid target workspace GID (if it looks like an Asana GID)
        else:
            # If task_id looks like an Asana GID, verify it exists in target workspace
            if (
                task_id_val and len(task_id_val) > 10
            ):  # Asana GIDs are typically long numbers
                try:
                    # Quick check: try to fetch the task from target workspace
                    verify_opts = {"opt_fields": "gid"}
                    target_client._with_retry(
                        target_client.tasks.get_task, task_id_val, verify_opts
                    )
                    # If we get here, task exists in target workspace
                    existing_target_gid = task_id_val
                except Exception:
                    # Task doesn't exist in target workspace, will create new
                    existing_target_gid = None

        # Get source GID for attachments
        source_task_gid = None
        asana_source_gid_val = row.get("asana_source_gid")
        if pd.notna(asana_source_gid_val) and asana_source_gid_val:
            source_task_gid = str(asana_source_gid_val)
        else:
            original_task_id = row.get("task_id", "")
            if original_task_id and original_task_id != existing_target_gid:
                source_task_gid = original_task_id

        # Get project/section info
        project_names_str = row.get("project_names")
        section_names_str = row.get("section_names")
        my_tasks_section_names_str = row.get("my_tasks_section_names")

        # Build task body
        task_data = {
            "name": title,
            "workspace": config.target_workspace_gid,
        }

        if description_html and pd.notna(description_html):
            task_data["html_notes"] = description_html
        else:
            task_data["notes"] = notes

        if due_on and pd.notna(due_on):
            if hasattr(due_on, "isoformat"):
                task_data["due_on"] = due_on.isoformat()
            else:
                task_data["due_on"] = str(due_on)

        if start_on and pd.notna(start_on):
            if hasattr(start_on, "isoformat"):
                task_data["start_on"] = start_on.isoformat()
            else:
                task_data["start_on"] = str(start_on)

        if assignee:
            task_data["assignee"] = assignee

        # Handle project memberships
        memberships = []
        if pd.notna(project_names_str) and project_names_str:
            project_names = project_names_str.split("|")
            section_names = (
                section_names_str.split("|")
                if pd.notna(section_names_str) and section_names_str
                else []
            )

            for i, project_name in enumerate(project_names):
                project_gid = project_mgr.get_or_create_project(project_name)
                if project_gid:
                    section_name = section_names[i] if i < len(section_names) else None
                    if section_name and section_name not in [
                        "(no section)",
                        "None",
                        "",
                    ]:
                        section_gid = project_mgr.get_or_create_section(
                            project_gid, section_name
                        )
                        if section_gid:
                            memberships.append(
                                {"project": project_gid, "section": section_gid}
                            )
                        else:
                            memberships.append({"project": project_gid})
                    else:
                        memberships.append({"project": project_gid})

        if memberships:
            task_data["memberships"] = memberships

        body = {"data": task_data}
        opts = {"opt_fields": "gid,name"}

        if existing_target_gid:
            # Update existing task
            target_client._with_retry(
                target_client.tasks.update_task,
                task_gid=existing_target_gid,
                body=body,
                opts=opts,
            )
            target_task_gid = existing_target_gid
            action = "Updated"
        else:
            # Create new task
            created_task = target_client._with_retry(
                target_client.tasks.create_task,
                body,
                opts,
            )
            target_task_gid = created_task.get("gid")
            action = "Created"

        print(f"{action} task {target_task_gid}: {title}")

        # Add to project sections if needed
        if (
            pd.notna(project_names_str)
            and project_names_str
            and pd.notna(section_names_str)
            and section_names_str
        ):
            project_names = project_names_str.split("|")
            section_names = section_names_str.split("|")

            for i, project_name in enumerate(project_names):
                if i < len(section_names):
                    section_name = section_names[i]
                    if section_name and section_name not in [
                        "(no section)",
                        "None",
                        "",
                    ]:
                        project_gid = project_mgr.get_or_create_project(project_name)
                        if project_gid:
                            section_gid = project_mgr.get_or_create_section(
                                project_gid, section_name
                            )
                            if section_gid:
                                try:
                                    upload_url = f"https://app.asana.com/api/1.0/sections/{section_gid}/addTask"
                                    upload_headers = {
                                        "Authorization": f"Bearer {target_client._pat}"
                                    }
                                    upload_data = {"data": {"task": target_task_gid}}
                                    response = requests.post(
                                        upload_url,
                                        headers=upload_headers,
                                        json=upload_data,
                                        timeout=30,
                                    )
                                    response.raise_for_status()
                                    print(
                                        f"    Added to section: {section_name} in {project_name}"
                                    )
                                except Exception as e:
                                    if "already" not in str(e).lower():
                                        print(
                                            f"    Warning: Could not add to section '{section_name}' in project '{project_name}': {e}"
                                        )

        # Add to My Tasks sections
        if pd.notna(my_tasks_section_names_str) and my_tasks_section_names_str:
            mytasks_sections = my_tasks_section_names_str.split("|")
            for section_name in mytasks_sections:
                section_gid = project_mgr.get_or_create_mytasks_section(
                    config.target_workspace_gid, section_name
                )
                if section_gid:
                    try:
                        upload_url = f"https://app.asana.com/api/1.0/sections/{section_gid}/addTask"
                        upload_headers = {
                            "Authorization": f"Bearer {target_client._pat}"
                        }
                        upload_data = {"data": {"task": target_task_gid}}
                        response = requests.post(
                            upload_url,
                            headers=upload_headers,
                            json=upload_data,
                            timeout=30,
                        )
                        response.raise_for_status()
                        print(f"    Added to My Tasks section: {section_name}")
                    except Exception as e:
                        print(
                            f"    Warning: Could not add to My Tasks section '{section_name}': {e}"
                        )

        # Add tags, followers, custom fields
        if source_task_gid:
            try:
                src_tag_opts = {"opt_fields": "tags,tags.name"}
                src_task_with_tags = source_client._with_retry(
                    source_client.tasks.get_task, source_task_gid, src_tag_opts
                )
                source_tags = src_task_with_tags.get("tags", [])
                if source_tags:
                    tag_names = [
                        tag.get("name") for tag in source_tags if tag.get("name")
                    ]
                    if tag_names:
                        add_tags_to_task(
                            target_client,
                            target_task_gid,
                            config.target_workspace_gid,
                            tag_names,
                        )
            except Exception as e:
                print(f"    Warning: Could not fetch/add tags: {e}")

        # Add followers
        followers_gids_str = row.get("followers_gids")
        if pd.notna(followers_gids_str) and followers_gids_str:
            follower_gids = [
                gid.strip() for gid in str(followers_gids_str).split("|") if gid.strip()
            ]
            if follower_gids:
                add_followers_to_task(target_client, target_task_gid, follower_gids)

        # Set custom fields
        local_task_id = str(row.get("task_id", ""))
        set_custom_fields_on_task(
            target_client, target_task_gid, local_task_id, config.target_workspace_gid
        )

        # Upload attachments
        if source_task_gid:
            attachment_count = fetch_and_upload_attachments(
                source_client, target_client, source_task_gid, target_task_gid
            )
            if attachment_count > 0:
                print(f"    Uploaded {attachment_count} attachment(s)")
            else:
                print("    No attachments found or uploaded")

            # Post comments
            comment_count = fetch_and_post_comments(
                source_client, target_client, source_task_gid, target_task_gid
            )
            if comment_count > 0:
                print(f"    Posted {comment_count} comment(s)")

        # Update local task
        is_new = not existing_target_gid
        created.append((idx, target_task_gid, is_new))

    # Update local tasks with target Asana ids
    if created:
        now_ts = pd.Timestamp.now(tz="UTC")
        for idx, asana_gid, is_new in created:
            df.loc[idx, "asana_target_gid"] = asana_gid
            df.loc[idx, "import_source_file"] = "asana-post"
            df.loc[idx, "updated_at"] = now_ts
            if is_new:
                df.loc[idx, "import_date"] = today

        # Save updated tasks
        df.to_parquet(TASKS_FILE, index=False)
        print("\nUpdated local task data with target GID")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python export_single_task.py <title_pattern>")
        sys.exit(1)

    title_pattern = sys.argv[1]
    export_task_by_title(title_pattern)
