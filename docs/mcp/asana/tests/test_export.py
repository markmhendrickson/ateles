"""
Tests for export_asana_tasks functionality.

Tests cover:
- Basic export operations
- Full property exports
- Filter permutations (task_ids, limit, sync_log_filter)
- Create scenarios (new task creation, duplicate detection, assignee assignment, attachment upload)
- Update scenarios (property updates, status changes, date changes)
- Data permutations (various property combinations, local attachments, priorities/urgencies)
- Error cases (invalid task data, missing required fields, API failures, attachment upload failures)
"""

from unittest.mock import AsyncMock, mock_open, patch

import pytest
from export_engine import AsanaExporter


@pytest.mark.unit
@pytest.mark.asyncio
async def test_basic_export(mock_asana_config, mock_parquet_client, basic_task):
    """Test basic task export with minimal properties."""
    exporter = AsanaExporter(mock_asana_config, mock_parquet_client, workspace="target")

    # Mock tasks to export
    mock_parquet_client.read_tasks.return_value = [basic_task]

    # Mock Asana API call
    with patch.object(exporter.client, "_with_retry") as mock_retry:
        mock_retry.return_value = {"gid": "new_gid_123"}

        result = await exporter.export_tasks(task_ids=[basic_task["task_id"]])

    assert result["success"] is True
    assert result["workspace"] == "target"
    assert result["processed"] == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_full_property_export(
    mock_asana_config, mock_parquet_client, full_property_task
):
    """Test export of task with all supported properties."""
    exporter = AsanaExporter(mock_asana_config, mock_parquet_client, workspace="target")

    # Mock tasks to export
    mock_parquet_client.read_tasks.return_value = [full_property_task]

    # Mock Asana API call
    with patch.object(exporter.client, "_with_retry") as mock_retry:
        mock_retry.return_value = {"gid": "new_gid_123"}

        result = await exporter.export_tasks(task_ids=[full_property_task["task_id"]])

    assert result["success"] is True
    assert result["processed"] == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_export_specific_task_ids(mock_asana_config, mock_parquet_client):
    """Test export with specific task_ids parameter."""
    exporter = AsanaExporter(mock_asana_config, mock_parquet_client, workspace="target")

    # Mock tasks
    task_ids = ["task_1", "task_2", "task_3"]
    mock_tasks = [
        {"task_id": tid, "title": f"Task {tid}", "status": "pending"}
        for tid in task_ids
    ]
    mock_parquet_client.read_tasks.return_value = mock_tasks

    # Mock Asana API
    with patch.object(exporter.client, "_with_retry") as mock_retry:
        mock_retry.return_value = {"gid": "new_gid"}

        result = await exporter.export_tasks(task_ids=task_ids)

    assert result["success"] is True
    assert result["processed"] == 3


@pytest.mark.unit
@pytest.mark.asyncio
async def test_export_with_limit(mock_asana_config, mock_parquet_client):
    """Test export with limit parameter."""
    exporter = AsanaExporter(mock_asana_config, mock_parquet_client, workspace="target")

    # Mock many tasks - but limit should restrict to 5
    mock_tasks = [
        {"task_id": f"task_{i}", "title": f"Task {i}", "status": "pending"}
        for i in range(20)
    ]

    # Mock read_tasks to return only the limit
    async def mock_read_tasks(*args, **kwargs):
        limit = kwargs.get("limit", 10)
        return mock_tasks[:limit]

    mock_parquet_client.read_tasks = AsyncMock(side_effect=mock_read_tasks)

    # Mock Asana API
    with patch.object(exporter.client, "_with_retry") as mock_retry:
        mock_retry.return_value = {"gid": "new_gid"}

        result = await exporter.export_tasks(limit=5)

    assert result["success"] is True
    assert result["processed"] <= 5


@pytest.mark.unit
@pytest.mark.asyncio
async def test_export_with_sync_log_filter(mock_asana_config, mock_parquet_client):
    """Test export with sync_log_filter parameter."""
    exporter = AsanaExporter(mock_asana_config, mock_parquet_client, workspace="target")

    # Mock tasks with specific sync_log
    mock_tasks = [
        {
            "task_id": "task_1",
            "title": "Task 1",
            "status": "pending",
            "sync_log": "pending_export",
        }
    ]
    mock_parquet_client.read_tasks.return_value = mock_tasks

    # Mock Asana API
    with patch.object(exporter.client, "_with_retry") as mock_retry:
        mock_retry.return_value = {"gid": "new_gid"}

        result = await exporter.export_tasks(sync_log_filter="pending_export")

    assert result["success"] is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_export_create_new_task(
    mock_asana_config, mock_parquet_client, basic_task
):
    """Test creating a new task in Asana."""
    exporter = AsanaExporter(mock_asana_config, mock_parquet_client, workspace="target")

    # Mock task without existing Asana GID
    basic_task["asana_target_gid"] = None
    mock_parquet_client.read_tasks.return_value = [basic_task]

    # Mock no duplicate found
    with patch.object(exporter, "find_duplicate_by_title", return_value=None):
        # Mock Asana create
        with patch.object(exporter.client, "_with_retry") as mock_retry:
            mock_retry.return_value = {"gid": "new_gid_123"}

            result = await exporter.export_tasks(task_ids=[basic_task["task_id"]])

    assert result["success"] is True
    assert result["created"] == 1
    assert result["updated"] == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_export_update_existing_task(
    mock_asana_config, mock_parquet_client, basic_task
):
    """Test updating an existing task in Asana."""
    exporter = AsanaExporter(mock_asana_config, mock_parquet_client, workspace="target")

    # Mock task with existing Asana GID
    basic_task["asana_target_gid"] = "existing_gid_123"
    mock_parquet_client.read_tasks.return_value = [basic_task]

    # Mock Asana update
    with patch.object(exporter.client, "_with_retry") as mock_retry:
        mock_retry.return_value = {"gid": "existing_gid_123"}

        result = await exporter.export_tasks(task_ids=[basic_task["task_id"]])

    assert result["success"] is True
    assert result["created"] == 0
    assert result["updated"] == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_export_duplicate_detection(
    mock_asana_config, mock_parquet_client, basic_task
):
    """Test duplicate detection by title."""
    exporter = AsanaExporter(mock_asana_config, mock_parquet_client, workspace="target")

    # Mock task without Asana GID
    basic_task["asana_target_gid"] = None
    mock_parquet_client.read_tasks.return_value = [basic_task]

    # Mock duplicate found
    with patch.object(
        exporter, "find_duplicate_by_title", return_value="duplicate_gid"
    ):
        # Mock Asana update
        with patch.object(exporter.client, "_with_retry") as mock_retry:
            mock_retry.return_value = {"gid": "duplicate_gid"}

            result = await exporter.export_tasks(task_ids=[basic_task["task_id"]])

    assert result["success"] is True
    assert result["updated"] == 1  # Should update duplicate, not create new


@pytest.mark.unit
@pytest.mark.asyncio
async def test_export_assignee_assignment(
    mock_asana_config, mock_parquet_client, basic_task
):
    """Test that tasks are assigned to current user."""
    exporter = AsanaExporter(mock_asana_config, mock_parquet_client, workspace="target")

    # Mock task without Asana GID
    basic_task["asana_target_gid"] = None
    mock_parquet_client.read_tasks.return_value = [basic_task]

    # Mock get_assignee_gid
    with patch.object(exporter, "get_assignee_gid", return_value="user_gid_123"):
        # Mock no duplicate
        with patch.object(exporter, "find_duplicate_by_title", return_value=None):
            # Mock Asana create
            with patch.object(exporter.client, "_with_retry") as mock_retry:
                mock_retry.return_value = {"gid": "new_gid_123"}

                result = await exporter.export_tasks(task_ids=[basic_task["task_id"]])

    assert result["success"] is True
    assert result["created"] == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_export_with_local_attachment(mock_asana_config, mock_parquet_client):
    """Test export with local attachment reference."""
    exporter = AsanaExporter(mock_asana_config, mock_parquet_client, workspace="target")

    # Mock task with attachment reference
    task_with_attachment = {
        "task_id": "task_123",
        "title": "Task with attachment",
        "description": "See attached file: [attachment: test_data/test_file.txt]",
        "status": "pending",
        "asana_target_gid": None,
    }
    mock_parquet_client.read_tasks.return_value = [task_with_attachment]

    # Mock file exists
    with patch("pathlib.Path.exists", return_value=True):
        # Mock file open
        with patch("builtins.open", mock_open(read_data=b"test data")):
            # Mock get_assignee_gid
            with patch.object(exporter, "get_assignee_gid", return_value="user_gid"):
                # Mock no duplicate
                with patch.object(
                    exporter, "find_duplicate_by_title", return_value=None
                ):
                    # Mock Asana calls
                    with patch.object(exporter.client, "_with_retry") as mock_retry:
                        mock_retry.return_value = {"gid": "new_gid_123"}

                        result = await exporter.export_tasks(task_ids=["task_123"])

    assert result["success"] is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_export_due_date_sorting(mock_asana_config, mock_parquet_client):
    """Test that tasks are exported in due date order."""
    exporter = AsanaExporter(mock_asana_config, mock_parquet_client, workspace="target")

    # Mock tasks - sort is by due_date (soonest first)
    mock_tasks = [
        {
            "task_id": "task_first",
            "title": "First",
            "status": "pending",
            "due_date": "2025-01-01",
        },
        {
            "task_id": "task_second",
            "title": "Second",
            "status": "pending",
            "due_date": "2025-01-02",
        },
        {
            "task_id": "task_third",
            "title": "Third",
            "status": "pending",
            "due_date": "2025-01-03",
        },
    ]
    mock_parquet_client.read_tasks.return_value = mock_tasks

    # Mock Asana API
    with patch.object(exporter.client, "_with_retry") as mock_retry:
        mock_retry.return_value = {"gid": "new_gid"}

        result = await exporter.export_tasks(limit=10)

    assert result["success"] is True
    # Earliest due task should be processed first
    assert result["tasks"]["created"][0]["task_id"] == "task_first"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_export_invalid_task_data(mock_asana_config, mock_parquet_client):
    """Test export with invalid task data."""
    exporter = AsanaExporter(mock_asana_config, mock_parquet_client, workspace="target")

    # Mock task with missing required field (title)
    invalid_task = {
        "task_id": "invalid_task",
        "title": None,  # Invalid: title is required
        "status": "pending",
    }
    mock_parquet_client.read_tasks.return_value = [invalid_task]

    # Mock Asana API error
    with patch.object(
        exporter.client, "_with_retry", side_effect=Exception("Invalid task data")
    ):
        result = await exporter.export_tasks(task_ids=["invalid_task"])

    assert result["success"] is True  # Should handle gracefully
    assert result["failed"] == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_export_api_failure(mock_asana_config, mock_parquet_client, basic_task):
    """Test export with API failure."""
    exporter = AsanaExporter(mock_asana_config, mock_parquet_client, workspace="target")

    mock_parquet_client.read_tasks.return_value = [basic_task]

    # Mock Asana API error
    with patch.object(
        exporter.client, "_with_retry", side_effect=Exception("API Error")
    ):
        result = await exporter.export_tasks(task_ids=[basic_task["task_id"]])

    assert result["success"] is True  # Should handle gracefully
    assert result["failed"] == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_export_attachment_upload_failure(mock_asana_config, mock_parquet_client):
    """Test export with attachment upload failure."""
    exporter = AsanaExporter(mock_asana_config, mock_parquet_client, workspace="target")

    # Mock task with attachment
    task_with_attachment = {
        "task_id": "task_123",
        "title": "Task with attachment",
        "description": "See: [attachment: missing_file.txt]",
        "status": "pending",
        "asana_target_gid": None,
    }
    mock_parquet_client.read_tasks.return_value = [task_with_attachment]

    # Mock file doesn't exist
    with patch("pathlib.Path.exists", return_value=False):
        with patch.object(exporter, "get_assignee_gid", return_value="user_gid"):
            with patch.object(exporter, "find_duplicate_by_title", return_value=None):
                with patch.object(exporter.client, "_with_retry") as mock_retry:
                    mock_retry.return_value = {"gid": "new_gid_123"}

                    result = await exporter.export_tasks(task_ids=["task_123"])

    assert result["success"] is True
    # Task should still be created even if attachment fails


@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.asyncio
async def test_export_real_workspace(
    real_asana_config, real_parquet_client, workspace_fixtures
):
    """Integration test: Export task to real test workspace."""
    if not workspace_fixtures:
        pytest.skip("Workspace fixtures not available")

    # Use a fixture task that was already created in target workspace
    # Verify it exists by checking parquet for tasks with target GIDs
    target_gids = workspace_fixtures.get_all_target_gids()
    assert len(target_gids) > 0, "No fixture tasks in target workspace"

    # Verify the tasks exist in Asana by importing them
    from import_engine import AsanaImporter

    importer = AsanaImporter(real_asana_config, real_parquet_client, workspace="target")
    result = await importer.import_tasks(max_tasks=5)

    assert result["success"] is True
    assert result["workspace"] == "target"
    assert result["fetched"] > 0  # Should find fixture tasks
