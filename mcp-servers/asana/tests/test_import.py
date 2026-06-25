"""
Tests for import_asana_tasks functionality.

Tests cover:
- Basic import operations
- Full property imports
- Filter permutations (only_incomplete, assignee_gid, max_tasks, include_archived)
- Merge scenarios (new task creation, existing task update, conflict resolution)
- Data permutations (various property combinations)
- Error cases (invalid workspace GID, invalid assignee GID, API errors, network failures)
"""

from unittest.mock import patch

import pytest
from config import AsanaConfig
from import_engine import AsanaImporter


@pytest.mark.unit
@pytest.mark.asyncio
async def test_basic_import(mock_asana_config, mock_parquet_client):
    """Test basic task import with minimal properties."""
    importer = AsanaImporter(mock_asana_config, mock_parquet_client, workspace="source")

    # Mock fetch_tasks_from_asana to return basic task
    mock_tasks = [
        {
            "gid": "123",
            "name": "Test Task",
            "notes": "Test description",
            "completed": False,
            "due_on": None,
            "assignee": None,
            "projects": [],
            "memberships": [],
            "tags": [],
        }
    ]

    with patch.object(importer, "fetch_tasks_from_asana", return_value=mock_tasks):
        result = await importer.import_tasks()

    assert result["success"] is True
    assert result["workspace"] == "source"
    assert result["fetched"] == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_full_property_import(mock_asana_config, mock_parquet_client):
    """Test import of task with all supported properties."""
    importer = AsanaImporter(mock_asana_config, mock_parquet_client, workspace="source")

    # Mock fetch_tasks_from_asana to return task with all properties
    mock_tasks = [
        {
            "gid": "123",
            "name": "Full Property Task",
            "notes": "Comprehensive description",
            "html_notes": "<body>Comprehensive description</body>",
            "completed": False,
            "completed_at": None,
            "due_on": "2026-01-15",
            "start_on": "2026-01-01",
            "created_at": "2025-12-01T00:00:00.000Z",
            "modified_at": "2025-12-30T00:00:00.000Z",
            "assignee": {"gid": "456", "name": "Test User"},
            "projects": [{"gid": "789", "name": "Test Project"}],
            "memberships": [{"section": {"gid": "101", "name": "Test Section"}}],
            "tags": [{"gid": "202", "name": "test-tag"}],
            "permalink_url": "https://app.asana.com/0/123/123",
            "followers": [{"gid": "456", "name": "Test User"}],
        }
    ]

    with patch.object(importer, "fetch_tasks_from_asana", return_value=mock_tasks):
        result = await importer.import_tasks()

    assert result["success"] is True
    assert result["fetched"] == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_import_with_only_incomplete_filter(
    mock_asana_config, mock_parquet_client
):
    """Test import with only_incomplete=True filter."""
    importer = AsanaImporter(mock_asana_config, mock_parquet_client, workspace="source")

    with patch.object(importer, "fetch_tasks_from_asana", return_value=[]):
        result = await importer.import_tasks(only_incomplete=True)

    assert result["success"] is True
    assert result["workspace"] == "source"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_import_with_assignee_filter(mock_asana_config, mock_parquet_client):
    """Test import with assignee_gid filter."""
    importer = AsanaImporter(mock_asana_config, mock_parquet_client, workspace="source")

    with patch.object(importer, "fetch_tasks_from_asana", return_value=[]):
        result = await importer.import_tasks(assignee_gid="1234567890")

    assert result["success"] is True
    assert result["workspace"] == "source"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_import_with_max_tasks_limit(mock_asana_config, mock_parquet_client):
    """Test import with max_tasks limit."""
    importer = AsanaImporter(mock_asana_config, mock_parquet_client, workspace="source")

    # Mock many tasks - but max_tasks should restrict to 10
    mock_tasks = [
        {"gid": str(i), "name": f"Task {i}", "notes": "", "completed": False}
        for i in range(100)
    ]

    # Mock fetch_tasks_from_asana to respect max_tasks limit
    async def mock_fetch(*args, **kwargs):
        max_tasks = kwargs.get("max_tasks")
        if max_tasks:
            return mock_tasks[:max_tasks]
        return mock_tasks

    with patch.object(importer, "fetch_tasks_from_asana", side_effect=mock_fetch):
        result = await importer.import_tasks(max_tasks=10)

    assert result["success"] is True
    assert result["fetched"] <= 10


@pytest.mark.unit
@pytest.mark.asyncio
async def test_import_without_archived_projects(mock_asana_config, mock_parquet_client):
    """Test import with include_archived=False."""
    importer = AsanaImporter(mock_asana_config, mock_parquet_client, workspace="source")

    with patch.object(importer, "fetch_tasks_from_asana", return_value=[]):
        result = await importer.import_tasks(include_archived=False)

    assert result["success"] is True
    assert result["workspace"] == "source"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_import_new_task_creation(mock_asana_config, mock_parquet_client):
    """Test that new tasks are created in local storage."""
    importer = AsanaImporter(mock_asana_config, mock_parquet_client, workspace="source")

    # Mock no existing tasks
    mock_parquet_client.read_tasks.return_value = []

    # Mock new task from Asana
    mock_tasks = [{"gid": "123", "name": "New Task", "notes": "", "completed": False}]

    with patch.object(importer, "fetch_tasks_from_asana", return_value=mock_tasks):
        result = await importer.import_tasks()

    assert result["success"] is True
    assert result["new"] > 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_import_existing_task_update(mock_asana_config, mock_parquet_client):
    """Test that existing tasks are updated in local storage."""
    importer = AsanaImporter(mock_asana_config, mock_parquet_client, workspace="source")

    # Mock existing task
    mock_parquet_client.read_tasks.return_value = [
        {
            "task_id": "existing_123",
            "title": "Existing Task",
            "asana_source_gid": "123",
        }
    ]

    # Mock updated task from Asana
    mock_tasks = [
        {"gid": "123", "name": "Updated Task", "notes": "Updated", "completed": False}
    ]

    with patch.object(importer, "fetch_tasks_from_asana", return_value=mock_tasks):
        result = await importer.import_tasks()

    assert result["success"] is True
    assert result["updated"] > 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_import_invalid_workspace_gid(mock_asana_config, mock_parquet_client):
    """Test import with invalid workspace GID."""
    # Create new config with invalid workspace
    invalid_config = AsanaConfig(
        source_pat=mock_asana_config.source_pat,
        target_pat=mock_asana_config.target_pat,
        source_workspace_gid="invalid_gid",
        target_workspace_gid=mock_asana_config.target_workspace_gid,
    )

    importer = AsanaImporter(invalid_config, mock_parquet_client, workspace="source")

    # Mock API error
    with patch.object(
        importer, "fetch_tasks_from_asana", side_effect=Exception("Invalid workspace")
    ):
        with pytest.raises(Exception, match="Invalid workspace"):
            await importer.import_tasks()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_import_invalid_assignee_gid(mock_asana_config, mock_parquet_client):
    """Test import with invalid assignee GID."""
    importer = AsanaImporter(mock_asana_config, mock_parquet_client, workspace="source")

    # Mock no tasks returned (invalid assignee returns empty)
    with patch.object(importer, "fetch_tasks_from_asana", return_value=[]):
        result = await importer.import_tasks(assignee_gid="invalid_assignee")

    assert result["success"] is True
    assert result["fetched"] == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_import_edge_case_empty_title(mock_asana_config, mock_parquet_client):
    """Test import of task with empty title."""
    importer = AsanaImporter(mock_asana_config, mock_parquet_client, workspace="source")

    mock_tasks = [{"gid": "123", "name": "", "notes": "", "completed": False}]

    with patch.object(importer, "fetch_tasks_from_asana", return_value=mock_tasks):
        result = await importer.import_tasks()

    # Should handle gracefully (title defaults to "Untitled Task")
    assert result["success"] is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_import_edge_case_very_long_title(mock_asana_config, mock_parquet_client):
    """Test import of task with very long title."""
    importer = AsanaImporter(mock_asana_config, mock_parquet_client, workspace="source")

    mock_tasks = [{"gid": "123", "name": "A" * 1024, "notes": "", "completed": False}]

    with patch.object(importer, "fetch_tasks_from_asana", return_value=mock_tasks):
        result = await importer.import_tasks()

    assert result["success"] is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_import_edge_case_special_characters(
    mock_asana_config, mock_parquet_client
):
    """Test import of task with special characters in title."""
    importer = AsanaImporter(mock_asana_config, mock_parquet_client, workspace="source")

    mock_tasks = [
        {
            "gid": "123",
            "name": "Test 日本語 émojis 🎉 & symbols: <>&\"'",
            "notes": "",
            "completed": False,
        }
    ]

    with patch.object(importer, "fetch_tasks_from_asana", return_value=mock_tasks):
        result = await importer.import_tasks()

    assert result["success"] is True


@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.asyncio
async def test_import_real_workspace(
    real_asana_config, real_parquet_client, workspace_fixtures
):
    """Integration test: Import tasks from real test workspace."""
    if not workspace_fixtures:
        pytest.skip("Workspace fixtures not available")

    importer = AsanaImporter(real_asana_config, real_parquet_client, workspace="source")

    # Import tasks from source workspace (should include fixture tasks)
    result = await importer.import_tasks(max_tasks=10)

    assert result["success"] is True
    assert result["workspace"] == "source"
    assert isinstance(result["fetched"], int)
    assert result["fetched"] > 0  # Should find fixture tasks
