"""
Workspace fixtures for integration tests.

Populates source and target Asana workspaces with test tasks covering
various property permutations before integration tests run.
"""

import sys
from typing import Any

import requests
from export_engine import AsanaExporter
from import_engine import AsanaImporter

from tests.fixtures.test_tasks import (
    generate_basic_task,
    generate_full_property_task,
)


class WorkspaceFixtures:
    """Manages test fixtures in Asana workspaces."""

    def __init__(
        self,
        source_importer: AsanaImporter,
        target_exporter: AsanaExporter,
        parquet_client,
    ):
        self.source_importer = source_importer
        self.target_exporter = target_exporter
        self.parquet_client = parquet_client

        # Store created task GIDs for cleanup and test reference
        self.source_task_gids: list[str] = []
        self.target_task_gids: list[str] = []
        self.parquet_task_ids: list[str] = []

    async def cleanup_workspace(self, workspace: str) -> int:
        """
        Delete all tasks from the specified workspace.

        Args:
            workspace: "source" or "target"

        Returns:
            Number of tasks deleted
        """
        if workspace == "source":
            client = self.source_importer.client
            workspace_gid = self.source_importer.workspace_gid
        else:
            client = self.target_exporter.client
            workspace_gid = self.target_exporter.workspace_gid

        # Get current user GID
        headers = {"Authorization": f"Bearer {client._pat}"}
        me_url = "https://app.asana.com/api/1.0/users/me"
        me_response = requests.get(
            me_url, headers=headers, params={"opt_fields": "gid"}, timeout=30
        )

        if me_response.status_code != 200:
            print(
                f"Warning: Could not get current user for {workspace} workspace cleanup",
                file=sys.stderr,
            )
            return 0

        user_gid = me_response.json().get("data", {}).get("gid")
        if not user_gid:
            print(
                f"Warning: Could not get user GID for {workspace} workspace cleanup",
                file=sys.stderr,
            )
            return 0

        # Fetch all tasks assigned to current user in workspace
        tasks_opts = {
            "assignee": user_gid,
            "workspace": workspace_gid,
            "opt_fields": "gid,name",
            "limit": 100,
        }

        try:
            # Asana API returns an iterator that handles pagination automatically
            all_tasks = list(client._with_retry(client.tasks.get_tasks, tasks_opts))
        except Exception as e:
            print(
                f"Warning: Could not fetch tasks for {workspace} workspace cleanup: {e}",
                file=sys.stderr,
            )
            return 0

        # Delete each task
        deleted_count = 0
        for task in all_tasks:
            task_gid = task.get("gid")
            if not task_gid:
                continue

            try:
                client._with_retry(client.tasks.delete_task, task_gid, {})
                deleted_count += 1
            except Exception as e:
                print(
                    f"Warning: Could not delete task {task_gid} from {workspace} workspace: {e}",
                    file=sys.stderr,
                )

        return deleted_count

    async def populate_source_workspace(self) -> list[str]:
        """
        Populate source workspace with test tasks.

        Returns list of created task GIDs.
        """
        # Clean up existing tasks first
        print(
            "[Workspace Fixtures] Cleaning up existing tasks from source workspace..."
        )
        deleted = await self.cleanup_workspace("source")
        if deleted > 0:
            print(
                f"[Workspace Fixtures] Deleted {deleted} existing tasks from source workspace"
            )

        # Generate a curated set of test tasks (not all permutations to avoid too many)
        test_tasks = self._generate_curated_test_tasks()

        # Create source exporter
        source_config = self.source_importer.config
        source_exporter = AsanaExporter(
            source_config, self.parquet_client, workspace="source"
        )

        # Create tasks in source workspace via export
        # First, add them to parquet
        source_task_ids = []
        for task in test_tasks:
            await self.parquet_client.add_task(task)
            source_task_ids.append(task["task_id"])

        # Export to source workspace
        result = await source_exporter.export_tasks(task_ids=source_task_ids)

        if result["success"]:
            # Extract created and updated GIDs from result
            if "tasks" in result:
                created_gids = [
                    task.get("asana_gid")
                    for task in result["tasks"].get("created", [])
                    if task.get("asana_gid")
                ]
                updated_gids = [
                    task.get("asana_gid")
                    for task in result["tasks"].get("updated", [])
                    if task.get("asana_gid")
                ]
                self.source_task_gids = created_gids + updated_gids

        return self.source_task_gids

    async def populate_target_workspace(self) -> list[str]:
        """
        Populate target workspace with test tasks.

        Returns list of created task GIDs.
        """
        # Clean up existing tasks first
        print(
            "[Workspace Fixtures] Cleaning up existing tasks from target workspace..."
        )
        deleted = await self.cleanup_workspace("target")
        if deleted > 0:
            print(
                f"[Workspace Fixtures] Deleted {deleted} existing tasks from target workspace"
            )

        # Generate a curated set of test tasks
        test_tasks = self._generate_curated_test_tasks()

        # Create new task IDs for target (different from source)
        target_tasks = []
        for i, task in enumerate(test_tasks):
            new_task = task.copy()
            new_task["task_id"] = f"target_{task['task_id']}"
            new_task["title"] = f"[Target] {task['title']}"
            # Clear source GID if present
            new_task["asana_source_gid"] = None
            new_task["asana_target_gid"] = None
            target_tasks.append(new_task)

        # Add to parquet
        for task in target_tasks:
            await self.parquet_client.add_task(task)
            self.parquet_task_ids.append(task["task_id"])

        # Export to target workspace
        result = await self.target_exporter.export_tasks(
            task_ids=[t["task_id"] for t in target_tasks]
        )

        if result["success"]:
            # Extract created and updated GIDs from result
            if "tasks" in result:
                created_gids = [
                    task.get("asana_gid")
                    for task in result["tasks"].get("created", [])
                    if task.get("asana_gid")
                ]
                updated_gids = [
                    task.get("asana_gid")
                    for task in result["tasks"].get("updated", [])
                    if task.get("asana_gid")
                ]
                self.target_task_gids = created_gids + updated_gids

        return self.target_task_gids

    async def cleanup(self):
        """Clean up created test tasks from workspaces."""
        # Note: Asana API doesn't have a bulk delete endpoint
        # This would need to delete tasks one by one
        # For now, we'll leave cleanup as manual or implement later
        pass

    def _generate_curated_test_tasks(self) -> list[dict[str, Any]]:
        """
        Generate a curated set of test tasks covering key permutations.

        This is a subset of all permutations to keep fixture population manageable.
        """
        tasks = []

        # Essential test cases
        tasks.append(generate_basic_task(title="[Fixture] Basic Task"))
        tasks.append(generate_full_property_task())

        # Status variants (one of each)
        tasks.append(
            generate_basic_task(title="[Fixture] Pending Task", status="pending")
        )
        tasks.append(
            generate_basic_task(
                title="[Fixture] In Progress Task", status="in_progress"
            )
        )
        tasks.append(
            generate_basic_task(title="[Fixture] Completed Task", status="completed")
        )

        # Date combinations
        from datetime import date, timedelta

        today = date.today()
        tasks.append(
            generate_basic_task(
                title="[Fixture] Task with Due Date", due_date=today + timedelta(days=7)
            )
        )
        tasks.append(
            generate_basic_task(
                title="[Fixture] Task with Start and Due Date",
                start_date=today,
                due_date=today + timedelta(days=7),
            )
        )
        tasks.append(
            generate_basic_task(
                title="[Fixture] Past Due Task", due_date=today - timedelta(days=1)
            )
        )

        # Domain variants
        tasks.append(
            generate_basic_task(title="[Fixture] Finance Domain", domain="finance")
        )
        tasks.append(generate_basic_task(title="[Fixture] Work Domain", domain="work"))

        # Edge cases
        tasks.append(
            generate_basic_task(
                title="[Fixture] Task with Long Description",
                description="This is a test task with a longer description. " * 10,
            )
        )
        tasks.append(
            generate_basic_task(
                title="[Fixture] Task with Special Characters: <>&\"'",
            )
        )

        return tasks

    def get_source_task_gid(self, index: int = 0) -> str | None:
        """Get a source task GID by index."""
        if 0 <= index < len(self.source_task_gids):
            return self.source_task_gids[index]
        return None

    def get_target_task_gid(self, index: int = 0) -> str | None:
        """Get a target task GID by index."""
        if 0 <= index < len(self.target_task_gids):
            return self.target_task_gids[index]
        return None

    def get_all_source_gids(self) -> list[str]:
        """Get all source task GIDs."""
        return self.source_task_gids.copy()

    def get_all_target_gids(self) -> list[str]:
        """Get all target task GIDs."""
        return self.target_task_gids.copy()
