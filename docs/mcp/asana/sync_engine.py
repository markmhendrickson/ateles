"""
Asana task sync engine with three-way merge and parquet MCP integration.

Bidirectional sync between Asana workspaces and local parquet.
"""

import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

# Add current directory to path for imports
sys.path.insert(0, str(Path(__file__).parent))

from client import AsanaClientWrapper
from config import AsanaConfig
from parquet_client import ParquetMCPClient


class AsanaTaskSyncer:
    """Bidirectional sync between Asana workspaces and local parquet via MCP."""

    MERGE_PROPERTIES = [
        "title",
        "description",
        "description_html",
        "status",
        "due_date",
        "start_date",
        "completed_date",
        "updated_date",
        "created_date",  # Legacy date columns
        "project_names",
        "section_names",
        "assignee_gid",
        "followers_gids",
    ]

    def __init__(
        self,
        config: AsanaConfig,
        parquet_client: ParquetMCPClient,
        sync_scope: str = "both",
        dry_run: bool = False,
    ):
        self.config = config
        self.parquet_client = parquet_client
        self.sync_scope = sync_scope
        self.dry_run = dry_run

        self.source_client = AsanaClientWrapper.from_config_source(config)
        self.target_client = AsanaClientWrapper.from_config_target(config)

        # Cache for last synced state
        self._last_synced_state = {}

    async def sync(self, task_ids: list[str] | None = None) -> dict[str, Any]:
        """
        Perform bidirectional sync.

        Args:
            task_ids: Specific task IDs to sync (if provided, syncs only these tasks)

        Returns: Sync statistics
        """
        stats = {
            "source_to_local": {
                "updated": 0,
                "new": 0,
                "tasks": {"updated": [], "new": []},
            },
            "target_to_local": {
                "updated": 0,
                "new": 0,
                "tasks": {"updated": [], "new": []},
            },
            "local_to_source": {
                "updated": 0,
                "created": 0,
                "tasks": {"updated": [], "created": []},
            },
            "local_to_target": {
                "updated": 0,
                "created": 0,
                "tasks": {"updated": [], "created": []},
            },
            "sync_scope": self.sync_scope,
        }

        try:
            # If task_ids provided, sync both workspaces for those tasks
            if task_ids:
                do_source = True
                do_target = True
            else:
                do_source = self.sync_scope in ("both", "source")
                do_target = self.sync_scope in ("both", "target")

            # Load last synced state
            await self.load_last_synced_state()

            # Sync source workspace ↔ local
            if do_source:
                result = await self.sync_workspace_to_local(
                    self.source_client,
                    self.config.source_workspace_gid,
                    "source",
                    task_ids=task_ids,
                )
                stats["source_to_local"] = result

            # Sync target workspace ↔ local
            if do_target:
                result = await self.sync_workspace_to_local(
                    self.target_client,
                    self.config.target_workspace_gid,
                    "target",
                    task_ids=task_ids,
                )
                stats["target_to_local"] = result

            # Sync local → source workspace
            if do_source:
                result = await self.sync_local_to_workspace(
                    self.source_client,
                    self.config.source_workspace_gid,
                    "source",
                    task_ids=task_ids,
                )
                stats["local_to_source"] = result

            # Sync local → target workspace
            if do_target:
                result = await self.sync_local_to_workspace(
                    self.target_client,
                    self.config.target_workspace_gid,
                    "target",
                    task_ids=task_ids,
                )
                stats["local_to_target"] = result

            # Save last synced state after successful sync
            if not self.dry_run:
                await self.save_last_synced_state()

        except Exception as e:
            print(f"Error during sync: {e}", file=sys.stderr)
            raise

        result = {"success": True, "dry_run": self.dry_run, **stats}

        return result

    async def sync_workspace_to_local(
        self,
        client: AsanaClientWrapper,
        workspace_gid: str,
        workspace_name: str,
        task_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Sync tasks from Asana workspace to local parquet."""
        # If specific task_ids provided, fetch only those tasks
        pending = (
            {"updated": 0, "new": 0, "tasks": {"updated": [], "new": []}}
            if self.dry_run
            else None
        )
        if task_ids:
            # Get local tasks to find their Asana GIDs
            local_tasks = await self.parquet_client.read_tasks(
                filters={"task_id": {"$in": task_ids}}, limit=len(task_ids)
            )

            # Extract Asana GIDs for this workspace
            gid_field = (
                "asana_source_gid" if workspace_name == "source" else "asana_target_gid"
            )
            asana_gids = [t.get(gid_field) for t in local_tasks if t.get(gid_field)]

            if not asana_gids:
                result = {"updated": 0, "new": 0, "tasks": {"updated": [], "new": []}}
                if self.dry_run:
                    result["pending"] = pending
                return result

            # Fetch specific tasks from Asana by GID
            tasks = []
            for gid in asana_gids:
                try:
                    opts = {
                        "opt_fields": "gid,name,notes,html_notes,completed,completed_at,due_on,start_on,created_at,modified_at,assignee,assignee.gid,projects,projects.name,memberships,memberships.section.name,tags,tags.name"
                    }
                    task = client._with_retry(client.tasks.get_task, gid, opts)
                    tasks.append(task)
                except Exception as e:
                    print(f"Warning: Could not fetch task {gid}: {e}", file=sys.stderr)
        else:
            # Fetch modified tasks from Asana
            tasks = await self.fetch_tasks_modified_since(
                client, workspace_gid, workspace_name
            )

        if not tasks:
            result = {"updated": 0, "new": 0, "tasks": {"updated": [], "new": []}}
            if self.dry_run:
                result["pending"] = pending
            return result

        updated_count = 0
        new_count = 0
        updated_tasks = []
        new_tasks = []

        for task_data in tasks:
            normalized = self.normalize_asana_task(task_data, workspace_name)
            task_id = normalized["task_id"]
            task_title = normalized.get("title", "Untitled Task")
            asana_gid = task_data.get("gid")

            # Check if task exists locally
            existing_tasks = await self.parquet_client.read_tasks(
                filters={"task_id": task_id}, limit=1
            )

            if existing_tasks:
                # Three-way merge
                local_task = existing_tasks[0]
                # Normalize date columns in local task (convert date objects to ISO strings)
                local_task = self._normalize_date_columns(local_task)
                last_synced = self._last_synced_state.get(task_id, {})
                # Normalize date columns in last_synced state too
                if last_synced:
                    last_synced = self._normalize_date_columns(last_synced)

                merged = self.merge_task_properties(
                    last_synced=last_synced,
                    current_local=local_task,
                    current_asana=normalized,
                )

                merged = self._normalize_date_columns(merged)
                needs_update = self._needs_update(local_task, merged)

                # Update local task with merged properties
                if needs_update:
                    if not self.dry_run:
                        await self.parquet_client.update_tasks(
                            filters={"task_id": task_id}, updates=merged
                        )
                        updated_count += 1
                        updated_tasks.append(
                            {
                                "task_id": task_id,
                                "title": task_title,
                                "asana_gid": asana_gid,
                                "action": "updated",
                            }
                        )
                    else:
                        pending["updated"] += 1
                        pending["tasks"]["updated"].append(
                            {
                                "task_id": task_id,
                                "title": task_title,
                                "asana_gid": asana_gid,
                                "action": "pending_update",
                            }
                        )
            else:
                # Add new task
                if not self.dry_run:
                    # Normalize date columns before sending to parquet (should already be normalized, but ensure)
                    normalized = self._normalize_date_columns(normalized)
                    await self.parquet_client.add_task(normalized)
                    new_count += 1
                    new_tasks.append(
                        {
                            "task_id": task_id,
                            "title": task_title,
                            "asana_gid": asana_gid,
                            "action": "created",
                        }
                    )
                else:
                    pending["new"] += 1
                    pending["tasks"]["new"].append(
                        {
                            "task_id": task_id,
                            "title": task_title,
                            "asana_gid": asana_gid,
                            "action": "pending_create",
                        }
                    )

        result = {
            "updated": updated_count,
            "new": new_count,
            "tasks": {"updated": updated_tasks, "new": new_tasks},
        }
        if self.dry_run:
            result["pending"] = pending

        return result

    async def sync_local_to_workspace(
        self,
        client: AsanaClientWrapper,
        workspace_gid: str,
        workspace_name: str,
        task_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Sync local tasks to Asana workspace."""
        # Build filters (include tasks without remote GIDs so they can be created)
        filters = {}
        if task_ids:
            filters["task_id"] = {"$in": task_ids}

        local_tasks = await self.parquet_client.read_tasks(filters=filters)

        pending = (
            {"updated": 0, "created": 0, "tasks": {"updated": [], "created": []}}
            if self.dry_run
            else None
        )

        if not local_tasks:
            result = {
                "updated": 0,
                "created": 0,
                "tasks": {"updated": [], "created": []},
            }
            if self.dry_run:
                result["pending"] = pending
            return result

        updated_count = 0
        created_count = 0
        updated_tasks = []
        created_tasks = []

        for task in local_tasks:
            task_id = task["task_id"]
            task_gid = task.get(
                "asana_target_gid" if workspace_name == "target" else "asana_source_gid"
            )

            if task_gid:
                # Task exists - check if we need to update
                try:
                    # Fetch current Asana state
                    opts = {"opt_fields": "modified_at,name,notes,due_on,completed"}
                    asana_task = client._with_retry(
                        client.tasks.get_task, task_gid, opts
                    )

                    # Three-way merge
                    last_synced = self._last_synced_state.get(task_id, {})
                    normalized_asana = self.normalize_asana_task(
                        asana_task, workspace_name
                    )

                    merged = self.merge_task_properties(
                        last_synced=last_synced,
                        current_local=task,
                        current_asana=normalized_asana,
                    )

                    # Check if we need to update Asana
                    needs_update = False
                    for prop in ["title", "description", "due_date", "status"]:
                        if merged.get(prop) != normalized_asana.get(prop):
                            needs_update = True
                            break

                    if needs_update:
                        if not self.dry_run:
                            # Update Asana task
                            update_data = {
                                "name": merged.get("title"),
                                "notes": merged.get("description"),
                                "due_on": (
                                    str(merged.get("due_date"))
                                    if merged.get("due_date")
                                    else None
                                ),
                                "completed": merged.get("status") == "completed",
                            }
                            update_data = {
                                k: v for k, v in update_data.items() if v is not None
                            }

                            client._with_retry(
                                client.tasks.update_task,
                                {"data": update_data},
                                task_gid,
                                {},
                            )
                            updated_count += 1
                            updated_tasks.append(
                                {
                                    "task_id": task_id,
                                    "title": task.get("title", "Untitled Task"),
                                    "asana_gid": task_gid,
                                    "action": "updated",
                                }
                            )
                        else:
                            pending["updated"] += 1
                            pending["tasks"]["updated"].append(
                                {
                                    "task_id": task_id,
                                    "title": task.get("title", "Untitled Task"),
                                    "asana_gid": task_gid,
                                    "action": "pending_update",
                                }
                            )

                except Exception as e:
                    print(
                        f"Warning: Could not sync task {task_gid}: {e}", file=sys.stderr
                    )
            else:
                # Task doesn't exist in this workspace - create it
                if task.get("title"):
                    try:
                        task_data = {
                            "workspace": workspace_gid,
                            "name": task.get("title"),
                            "notes": task.get("description", ""),
                            "due_on": (
                                str(task.get("due_date"))
                                if task.get("due_date")
                                else None
                            ),
                            "completed": task.get("status") == "completed",
                        }
                        task_data = {
                            k: v for k, v in task_data.items() if v is not None
                        }

                        if not self.dry_run:
                            new_task = client._with_retry(
                                client.tasks.create_task, {"data": task_data}, {}
                            )
                            created_count += 1

                            # Update local with new GID
                            new_gid = new_task.get("gid")
                            gid_field = (
                                "asana_target_gid"
                                if workspace_name == "target"
                                else "asana_source_gid"
                            )
                            await self.parquet_client.update_tasks(
                                filters={"task_id": task_id},
                                updates={gid_field: new_gid},
                            )
                            created_tasks.append(
                                {
                                    "task_id": task_id,
                                    "title": task.get("title", "Untitled Task"),
                                    "asana_gid": new_gid,
                                    "action": "created",
                                }
                            )
                        else:
                            pending["created"] += 1
                            pending["tasks"]["created"].append(
                                {
                                    "task_id": task_id,
                                    "title": task.get("title", "Untitled Task"),
                                    "asana_gid": None,
                                    "action": "pending_create",
                                }
                            )

                    except Exception as e:
                        print(
                            f"Warning: Could not create task in {workspace_name}: {e}",
                            file=sys.stderr,
                        )

        result = {
            "updated": updated_count,
            "created": created_count,
            "tasks": {"updated": updated_tasks, "created": created_tasks},
        }
        if self.dry_run:
            result["pending"] = pending

        return result

    async def fetch_tasks_modified_since(
        self,
        client: AsanaClientWrapper,
        workspace_gid: str,
        workspace_name: str,
        since: datetime | None = None,
    ) -> list[dict]:
        """Fetch tasks modified since timestamp."""
        # Asana API requires exactly one of: project, tag, section, user task list, or assignee + workspace
        # We'll use assignee + workspace to get tasks assigned to the current user

        # Get current user GID
        import requests

        headers = {"Authorization": f"Bearer {client._pat}"}
        me_url = "https://app.asana.com/api/1.0/users/me"
        me_response = requests.get(
            me_url, headers=headers, params={"opt_fields": "gid"}, timeout=30
        )

        if me_response.status_code != 200:
            print(
                f"Warning: Could not get current user for {workspace_name} workspace sync",
                file=sys.stderr,
            )
            return []

        user_gid = me_response.json().get("data", {}).get("gid")
        if not user_gid:
            print(
                f"Warning: Could not get user GID for {workspace_name} workspace sync",
                file=sys.stderr,
            )
            return []

        opt_fields = [
            "gid",
            "name",
            "notes",
            "html_notes",
            "completed",
            "completed_at",
            "due_on",
            "start_on",
            "created_at",
            "modified_at",
            "assignee",
            "assignee.gid",
            "projects",
            "projects.name",
            "memberships",
            "memberships.section.name",
            "tags",
            "tags.name",
            "custom_fields",
            "custom_fields.gid",
            "custom_fields.name",
            "custom_fields.type",
            "custom_fields.enum_value",
            "custom_fields.enum_value.name",
        ]

        opts = {
            "assignee": user_gid,
            "workspace": workspace_gid,
            "opt_fields": ",".join(opt_fields),
        }

        # Add completed_since filter if provided
        if since:
            opts["completed_since"] = since.isoformat()
        else:
            # Only fetch incomplete tasks if no timestamp provided
            opts["completed_since"] = "now"

        tasks = list(client._with_retry(client.tasks.get_tasks, opts))
        return tasks

    def normalize_asana_task(self, task_data: dict, workspace_name: str) -> dict:
        """Normalize Asana task data."""
        gid = task_data.get("gid", "")
        title = task_data.get("name", "")
        notes = task_data.get("notes", "") or ""
        html_notes = task_data.get("html_notes")
        completed = task_data.get("completed", False)

        # Parse dates and convert to ISO format strings (parquet MCP expects strings for legacy date columns)
        due_on = task_data.get("due_on")
        due_date = datetime.fromisoformat(due_on).date().isoformat() if due_on else None

        start_on = task_data.get("start_on")
        start_date = (
            datetime.fromisoformat(start_on).date().isoformat() if start_on else None
        )

        completed_at = task_data.get("completed_at")
        completed_date = (
            datetime.fromisoformat(completed_at).date().isoformat()
            if completed_at
            else None
        )

        modified_at_str = task_data.get("modified_at")
        # Convert datetime to date and then to ISO string for updated_date compatibility
        updated_at_dt = (
            datetime.fromisoformat(modified_at_str.replace("Z", "+00:00"))
            if modified_at_str
            else None
        )
        updated_at = updated_at_dt.date().isoformat() if updated_at_dt else None

        # Also set updated_date (legacy column name) for compatibility
        updated_date = updated_at

        # Extract created_at for created_date (legacy column)
        created_at_str = task_data.get("created_at")
        created_at_dt = (
            datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
            if created_at_str
            else None
        )
        created_date = created_at_dt.date().isoformat() if created_at_dt else None

        # Extract project/section info
        projects = task_data.get("projects", [])
        project_names = [p.get("name") for p in projects if p.get("name")]

        memberships = task_data.get("memberships", [])
        section_names = []
        for m in memberships:
            section = m.get("section")
            if section and section.get("name"):
                section_names.append(section.get("name"))

        # Extract assignee
        assignee = task_data.get("assignee") or {}
        assignee_gid = assignee.get("gid") if assignee else None

        # Extract followers
        followers = task_data.get("followers", [])
        follower_gids = [f.get("gid") for f in followers if f.get("gid")]

        # Status
        status = "completed" if completed else "pending"

        # Add workspace GID based on workspace name
        gid_field = (
            "asana_source_gid" if workspace_name == "source" else "asana_target_gid"
        )

        return {
            "task_id": gid,
            "title": title,
            "description": notes,
            "description_html": html_notes,
            "status": status,
            "due_date": due_date,
            "start_date": start_date,
            "completed_date": completed_date,
            "updated_at": updated_at,
            "updated_date": updated_date,  # Legacy column for parquet compatibility
            "created_date": created_date,  # Legacy column for parquet compatibility
            "project_names": "|".join(project_names) if project_names else None,
            "section_names": "|".join(section_names) if section_names else None,
            "assignee_gid": assignee_gid,
            "followers_gids": "|".join(follower_gids) if follower_gids else None,
            gid_field: gid,  # Add workspace-specific GID
        }

    def merge_task_properties(
        self, last_synced: dict, current_local: dict, current_asana: dict
    ) -> dict:
        """
        Three-way merge of task properties.

        Uses last synced state to determine which changes to preserve.
        """
        merged = current_local.copy()

        properties_to_merge = self.MERGE_PROPERTIES

        for prop in properties_to_merge:
            if prop not in current_asana:
                continue

            last_val = last_synced.get(prop) if last_synced else None
            local_val = current_local.get(prop)
            asana_val = current_asana.get(prop)

            # Normalize values for comparison
            def normalize_val(val):
                if pd.isna(val) or val is None:
                    return None
                if isinstance(val, str):
                    return val.strip() if val.strip() else None
                return val

            last_val_norm = normalize_val(last_val)
            local_val_norm = normalize_val(local_val)
            asana_val_norm = normalize_val(asana_val)

            # Three-way merge logic
            if last_val_norm == local_val_norm and last_val_norm != asana_val_norm:
                # Only Asana changed - use Asana
                merged[prop] = asana_val
            elif last_val_norm == asana_val_norm and last_val_norm != local_val_norm:
                # Only Local changed - use Local (keep current)
                pass
            elif last_val_norm != local_val_norm and last_val_norm != asana_val_norm:
                # Both changed - use newer timestamp
                local_updated = current_local.get("updated_at")
                asana_modified = current_asana.get("updated_at")

                if asana_modified and local_updated:
                    # Convert both to datetime for comparison (handle timezone-aware and naive)
                    local_dt = None
                    asana_dt = None

                    if isinstance(local_updated, str):
                        try:
                            # Try parsing with timezone first
                            local_dt = datetime.fromisoformat(
                                local_updated.replace("Z", "+00:00")
                            )
                        except (ValueError, AttributeError):
                            try:
                                # Fallback to naive datetime
                                local_dt = datetime.fromisoformat(local_updated)
                            except (ValueError, AttributeError):
                                local_dt = None
                    elif isinstance(local_updated, datetime):
                        local_dt = local_updated
                    elif isinstance(local_updated, date):
                        local_dt = datetime.combine(local_updated, datetime.min.time())

                    if isinstance(asana_modified, str):
                        try:
                            # Try parsing with timezone first
                            asana_dt = datetime.fromisoformat(
                                asana_modified.replace("Z", "+00:00")
                            )
                        except (ValueError, AttributeError):
                            try:
                                # Fallback to naive datetime
                                asana_dt = datetime.fromisoformat(asana_modified)
                            except (ValueError, AttributeError):
                                asana_dt = None
                    elif isinstance(asana_modified, datetime):
                        asana_dt = asana_modified
                    elif isinstance(asana_modified, date):
                        asana_dt = datetime.combine(asana_modified, datetime.min.time())

                    # Normalize timezones for comparison (make both timezone-aware or both naive)
                    if local_dt and asana_dt:
                        # If one is aware and one is naive, make both naive for comparison
                        if local_dt.tzinfo is not None and asana_dt.tzinfo is None:
                            local_dt = local_dt.replace(tzinfo=None)
                        elif local_dt.tzinfo is None and asana_dt.tzinfo is not None:
                            asana_dt = asana_dt.replace(tzinfo=None)

                    if asana_dt and local_dt and asana_dt > local_dt:
                        merged[prop] = asana_val
                    elif local_dt:
                        # Local is newer or asana_modified is None - keep local
                        pass
                    else:
                        # Both None or local_updated is None - use asana
                        merged[prop] = asana_val
                elif asana_modified:
                    merged[prop] = asana_val

        # Update timestamp to newer value
        local_updated = current_local.get("updated_at")
        asana_modified = current_asana.get("updated_at")

        # Convert both to datetime for comparison
        local_dt = None
        asana_dt = None

        if local_updated:
            if isinstance(local_updated, str):
                try:
                    # Try parsing with timezone first
                    local_dt = datetime.fromisoformat(
                        local_updated.replace("Z", "+00:00")
                    )
                except (ValueError, AttributeError):
                    try:
                        # Fallback to naive datetime
                        local_dt = datetime.fromisoformat(local_updated)
                    except (ValueError, AttributeError):
                        pass
            elif isinstance(local_updated, datetime):
                local_dt = local_updated
            elif isinstance(local_updated, date):
                local_dt = datetime.combine(local_updated, datetime.min.time())

        if asana_modified:
            if isinstance(asana_modified, str):
                try:
                    # Try parsing with timezone first
                    asana_dt = datetime.fromisoformat(
                        asana_modified.replace("Z", "+00:00")
                    )
                except (ValueError, AttributeError):
                    try:
                        # Fallback to naive datetime
                        asana_dt = datetime.fromisoformat(asana_modified)
                    except (ValueError, AttributeError):
                        pass
            elif isinstance(asana_modified, datetime):
                asana_dt = asana_modified
            elif isinstance(asana_modified, date):
                asana_dt = datetime.combine(asana_modified, datetime.min.time())

        # Normalize timezones for comparison (make both timezone-aware or both naive)
        if local_dt and asana_dt:
            # If one is aware and one is naive, make both naive for comparison
            if local_dt.tzinfo is not None and asana_dt.tzinfo is None:
                local_dt = local_dt.replace(tzinfo=None)
            elif local_dt.tzinfo is None and asana_dt.tzinfo is not None:
                asana_dt = asana_dt.replace(tzinfo=None)

        # Set updated_at to newer value (ensure it's an ISO format string for parquet compatibility)
        if asana_dt and local_dt:
            newer_dt = max(asana_dt, local_dt)
            merged["updated_at"] = newer_dt.date().isoformat()
        elif asana_dt:
            merged["updated_at"] = asana_dt.date().isoformat()
        elif local_dt:
            merged["updated_at"] = local_dt.date().isoformat()
        elif asana_modified:
            # Fallback: use asana value if it's already a string
            merged["updated_at"] = (
                asana_modified
                if isinstance(asana_modified, str)
                else str(asana_modified)
            )
        elif local_updated:
            # Fallback: use local value if it's already a string
            merged["updated_at"] = (
                local_updated if isinstance(local_updated, str) else str(local_updated)
            )

        # Ensure updated_date is set (legacy column) - same as updated_at
        if "updated_at" in merged:
            merged["updated_date"] = merged["updated_at"]

        return merged

    def _needs_update(self, current_task: dict, merged_task: dict) -> bool:
        """Check if merged task differs from current task for merge properties."""
        for prop in self.MERGE_PROPERTIES:
            current_val = self._normalize_merge_value(current_task.get(prop))
            merged_val = self._normalize_merge_value(merged_task.get(prop))
            if current_val != merged_val:
                return True
        return False

    @staticmethod
    def _normalize_merge_value(val: Any) -> Any:
        """Normalize values for merge comparison."""
        if pd.isna(val) or val is None:
            return None
        if isinstance(val, str):
            stripped = val.strip()
            return stripped if stripped else None
        return val

    async def load_last_synced_state(self) -> None:
        """Load last synced state from parquet MCP."""
        # Simplified: Load all tasks and store in cache
        # In production, this should be persisted separately
        tasks = await self.parquet_client.read_tasks()

        for task in tasks:
            task_id = task.get("task_id")
            if task_id:
                # Normalize date columns before storing in cache
                normalized_task = self._normalize_date_columns(task)
                self._last_synced_state[task_id] = normalized_task

    async def save_last_synced_state(self) -> None:
        """Save current state as last synced state."""
        # Reload tasks and update cache
        await self.load_last_synced_state()

    def _normalize_date_columns(self, task: dict) -> dict:
        """Normalize date columns in task dict (convert date objects to ISO strings)."""
        normalized = task.copy()
        date_columns = [
            "due_date",
            "start_date",
            "completed_date",
            "updated_date",
            "created_date",
            "updated_at",
        ]

        for col in date_columns:
            if col in normalized and normalized[col] is not None:
                val = normalized[col]
                if isinstance(val, date) and not isinstance(val, datetime):
                    normalized[col] = val.isoformat()
                elif isinstance(val, datetime):
                    normalized[col] = val.date().isoformat()
                elif isinstance(val, pd.Timestamp):
                    normalized[col] = val.date().isoformat()
                # If it's already a string, leave it as is        return normalized
