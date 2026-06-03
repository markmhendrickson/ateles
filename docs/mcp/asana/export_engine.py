"""
Asana task export engine with parquet MCP integration.

Exports local tasks to Asana workspace via the parquet MCP server.
"""

import mimetypes
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

# Add current directory to path for imports
sys.path.insert(0, str(Path(__file__).parent))

from client import AsanaClientWrapper
from config import AsanaConfig
from custom_field_manager import CustomFieldManager
from parquet_client import ParquetMCPClient


class AsanaExporter:
    """Export local tasks to Asana workspace via MCP."""

    def __init__(
        self,
        config: AsanaConfig,
        parquet_client: ParquetMCPClient,
        workspace: str = "target",
    ):
        self.config = config
        self.parquet_client = parquet_client
        self.workspace = workspace

        if workspace == "source":
            self.client = AsanaClientWrapper.from_config_source(config)
            self.workspace_gid = config.source_workspace_gid
        else:
            self.client = AsanaClientWrapper.from_config_target(config)
            self.workspace_gid = config.target_workspace_gid

        # Initialize custom field manager for syncing enumerated properties
        self.custom_field_manager = CustomFieldManager(self.client, self.workspace_gid)

    async def export_tasks(
        self,
        task_ids: list[str] | None = None,
        limit: int = 10,
        sync_log_filter: str | None = None,
    ) -> dict[str, Any]:
        """
        Export local tasks to Asana workspace.

        Args:
            task_ids: Specific task IDs to export (if provided, ignores limit and sync_log_filter)
            limit: Maximum number of tasks to export (ignored if task_ids provided)
            sync_log_filter: Filter tasks by sync_log value (ignored if task_ids provided)

        Returns: Statistics about the export
        """
        # If specific task IDs provided, fetch only those tasks
        if task_ids:
            filters = {"task_id": {"$in": task_ids}}
            tasks = await self.parquet_client.read_tasks(
                filters=filters, limit=len(task_ids)
            )
        else:
            # Build filters for task selection
            filters = {"status": {"$in": ["pending", "in_progress", "blocked"]}}

            if sync_log_filter:
                filters["sync_log"] = sync_log_filter

            # Fetch tasks via parquet MCP
            tasks = await self.parquet_client.read_tasks(filters=filters, limit=limit)

        if not tasks:
            return {
                "success": True,
                "workspace": self.workspace,
                "processed": 0,
                "created": 0,
                "updated": 0,
                "failed": 0,
            }

        # Sort by due date (soonest first)
        def sort_key(t):
            due = t.get("due_date")
            due_str = str(due) if due else "9999-12-31"  # nulls last
            return due_str

        sorted_tasks = sorted(tasks, key=sort_key)

        created_count = 0
        updated_count = 0
        failed_count = 0
        created_tasks = []
        updated_tasks = []
        failed_tasks = []

        for task in sorted_tasks:
            try:
                task_id = task["task_id"]
                title = task.get("title", "Untitled Task")

                # Check if task already exists in target workspace
                existing_target_gid = (
                    task.get("asana_target_gid")
                    if self.workspace == "target"
                    else task.get("asana_source_gid")
                )

                # Check for duplicates by title
                if not existing_target_gid:
                    existing_target_gid = await self.find_duplicate_by_title(title)

                # Prepare task data
                task_data = {
                    "name": title,
                    "notes": task.get("description", ""),
                    "due_on": (
                        str(task.get("due_date")) if task.get("due_date") else None
                    ),
                    "completed": task.get("status") == "completed",
                }

                # Remove None values
                task_data = {k: v for k, v in task_data.items() if v is not None}

                if existing_target_gid:
                    # Update existing task
                    # Ensure assignee is set if not already assigned
                    if "assignee" not in task_data:
                        assignee_gid = await self.get_assignee_gid()
                        if assignee_gid:
                            task_data["assignee"] = assignee_gid

                    # Remove None values again after adding assignee
                    task_data = {k: v for k, v in task_data.items() if v is not None}

                    self.client._with_retry(
                        self.client.tasks.update_task,
                        {"data": task_data},
                        existing_target_gid,
                        {},
                    )
                    updated_count += 1
                    updated_tasks.append(
                        {
                            "task_id": task_id,
                            "title": title,
                            "asana_gid": existing_target_gid,
                            "action": "updated",
                        }
                    )

                    # Upload local attachments if any
                    await self.upload_local_attachments(existing_target_gid, task)

                    # Update sync log
                    await self.parquet_client.update_tasks(
                        filters={"task_id": task_id},
                        updates={
                            "sync_log": "exported",
                            "sync_datetime": datetime.now(),
                        },
                    )
                else:
                    # Create new task
                    task_data["workspace"] = self.workspace_gid

                    # Always assign to current user (or fallback_assignee_email if specified)
                    assignee_gid = await self.get_assignee_gid()
                    if assignee_gid:
                        task_data["assignee"] = assignee_gid

                    new_task = self.client._with_retry(
                        self.client.tasks.create_task, {"data": task_data}, {}
                    )
                    created_count += 1

                    new_gid = new_task.get("gid")
                    created_tasks.append(
                        {
                            "task_id": task_id,
                            "title": title,
                            "asana_gid": new_gid,
                            "action": "created",
                        }
                    )

                    # Upload local attachments if any
                    await self.upload_local_attachments(new_gid, task)

                    # Update task with new GID and sync log
                    gid_field = (
                        "asana_target_gid"
                        if self.workspace == "target"
                        else "asana_source_gid"
                    )
                    await self.parquet_client.update_tasks(
                        filters={"task_id": task_id},
                        updates={
                            gid_field: new_gid,
                            "sync_log": "exported",
                            "sync_datetime": datetime.now(),
                        },
                    )

            except Exception as e:
                failed_count += 1
                failed_tasks.append(
                    {
                        "task_id": task.get("task_id"),
                        "title": task.get("title", "Untitled Task"),
                        "error": str(e),
                        "error_type": type(e).__name__,
                        "action": "failed",
                    }
                )
                print(
                    f"Error exporting task {task.get('task_id')}: {e}", file=sys.stderr
                )

                # Update sync log with error
                try:
                    await self.parquet_client.update_tasks(
                        filters={"task_id": task["task_id"]},
                        updates={
                            "sync_log": f"failed:{type(e).__name__}",
                            "sync_datetime": datetime.now(),
                        },
                    )
                except:
                    pass

        return {
            "success": True,
            "workspace": self.workspace,
            "processed": len(sorted_tasks),
            "created": created_count,
            "updated": updated_count,
            "failed": failed_count,
            "tasks": {
                "created": created_tasks,
                "updated": updated_tasks,
                "failed": failed_tasks,
            },
        }

    async def find_duplicate_by_title(self, title: str) -> str | None:
        """Check for existing task with same title in workspace."""
        try:
            headers = {"Authorization": f"Bearer {self.client._pat}"}
            me_url = "https://app.asana.com/api/1.0/users/me"
            me_response = requests.get(
                me_url, headers=headers, params={"opt_fields": "gid"}, timeout=5
            )

            if me_response.status_code == 200:
                user_gid = me_response.json().get("data", {}).get("gid")

                # Search assigned tasks
                search_opts = {
                    "assignee": user_gid,
                    "workspace": self.workspace_gid,
                    "opt_fields": "gid,name",
                    "limit": 100,
                }

                search_tasks = list(
                    self.client._with_retry(self.client.tasks.get_tasks, search_opts)
                )

                # Check for exact title match
                for task in search_tasks:
                    if task.get("name") == title:
                        return task.get("gid")

        except Exception:
            pass

        return None

    async def get_assignee_gid(self) -> str | None:
        """
        Get assignee GID.

        If fallback_assignee_email is set, attempts to look up that user by email.
        Otherwise, uses the current authenticated user ("me").
        Always returns the current user's GID as fallback.
        """
        try:
            headers = {"Authorization": f"Bearer {self.client._pat}"}

            # If email is specified, try to look up user by email
            if self.config.fallback_assignee_email:
                # List users in workspace and find by email
                url = "https://app.asana.com/api/1.0/users"
                params = {"workspace": self.workspace_gid, "opt_fields": "gid,email"}
                response = requests.get(url, headers=headers, params=params, timeout=30)

                if response.status_code == 200:
                    users = response.json().get("data", [])
                    for user in users:
                        if user.get("email") == self.config.fallback_assignee_email:
                            return user.get("gid")

            # Always fallback to current authenticated user
            url = "https://app.asana.com/api/1.0/users/me"
            params = {"opt_fields": "gid"}
            response = requests.get(url, headers=headers, params=params, timeout=30)

            if response.status_code == 200:
                return response.json().get("data", {}).get("gid")
        except Exception as e:
            print(f"Warning: Could not get assignee GID: {e}", file=sys.stderr)

        return None

    async def upload_local_attachments(
        self, asana_task_gid: str, task: dict[str, Any]
    ) -> int:
        """
        Upload local attachments referenced in task description to Asana.

        Looks for patterns like:
        - [attachment: data/attachments/asana_tasks/{task_id}/description/{filename}]
        - [attachment: path/to/file]

        Returns: Number of attachments successfully uploaded
        """
        uploaded_count = 0
        description = task.get("description", "") or task.get("description_html", "")

        if not description:
            return 0

        # Find all attachment references in description
        # Pattern: [attachment: path/to/file]
        attachment_pattern = r"\[attachment:\s*([^\]]+)\]"
        matches = re.findall(attachment_pattern, description, re.IGNORECASE)

        if not matches:
            return 0

        # Try to find attachment files
        task_id = task.get("task_id")
        repo_root = Path(__file__).parent.parent.parent.parent

        # Check DATA_DIR first (where tasks data is stored)
        data_dir = os.getenv("DATA_DIR")

        for attachment_ref in matches:
            attachment_path = attachment_ref.strip()
            local_file = None

            # Try DATA_DIR first (iCloud Drive location)
            if data_dir:
                attachments_dir = (
                    Path(data_dir)
                    / "attachments"
                    / "asana_tasks"
                    / str(task_id)
                    / "description"
                )
                filename = Path(attachment_path).name
                potential_file = attachments_dir / filename
                if potential_file.exists():
                    local_file = potential_file

            # Try relative to repo root
            if not local_file:
                local_file = repo_root / attachment_path
                if not local_file.exists():
                    # Try relative to task_id directory in repo
                    task_attachments_dir = (
                        repo_root
                        / "data"
                        / "attachments"
                        / "asana_tasks"
                        / str(task_id)
                        / "description"
                    )
                    filename = Path(attachment_path).name
                    local_file = task_attachments_dir / filename

            if not local_file.exists() or not local_file.is_file():
                print(
                    f"Warning: Attachment file not found: {attachment_path}",
                    file=sys.stderr,
                )
                continue

            # Check file size (100MB limit)
            file_size = local_file.stat().st_size
            if file_size > 100 * 1024 * 1024:
                print(
                    f"Warning: Attachment too large ({file_size / 1024 / 1024:.1f}MB): {local_file.name}",
                    file=sys.stderr,
                )
                continue

            # Determine MIME type
            mime_type, _ = mimetypes.guess_type(str(local_file))
            if not mime_type:
                mime_type = "application/octet-stream"

            # Upload to Asana
            try:
                with open(local_file, "rb") as f:
                    files = {"file": (local_file.name, f, mime_type)}
                    data = {"name": local_file.name}

                    upload_headers = {"Authorization": f"Bearer {self.client._pat}"}
                    upload_url = f"https://app.asana.com/api/1.0/tasks/{asana_task_gid}/attachments"

                    upload_response = requests.post(
                        upload_url,
                        headers=upload_headers,
                        files=files,
                        data=data,
                        timeout=300,  # Longer timeout for large files
                    )
                    upload_response.raise_for_status()

                    uploaded_count += 1
                    file_size_mb = file_size / 1024 / 1024
                    print(
                        f"✓ Uploaded attachment: {local_file.name} ({file_size_mb:.2f}MB)",
                        file=sys.stderr,
                    )

            except requests.exceptions.HTTPError as e:
                error_detail = e.response.text if e.response else str(e)
                print(
                    f"Warning: HTTP error uploading '{local_file.name}': {e.response.status_code if e.response else 'unknown'}",
                    file=sys.stderr,
                )
                print(f"  Response: {error_detail[:200]}", file=sys.stderr)
            except Exception as e:
                print(
                    f"Warning: Could not upload attachment '{local_file.name}': {e}",
                    file=sys.stderr,
                )

        return uploaded_count
