"""
Tests for import_asana_task_metadata functionality.

Tests cover:
- Metadata type permutations (custom_fields only, dependencies only, stories only, all types)
- Custom fields scenarios (with/without custom fields, various field types)
- Dependencies scenarios (with/without dependencies, circular dependency handling)
- Stories scenarios (with/without stories, various story types)
- Error cases (invalid task GIDs, API failures)
"""

from unittest.mock import patch

import pytest
from import_engine import AsanaImporter


@pytest.mark.unit
@pytest.mark.asyncio
async def test_import_custom_fields_only(mock_asana_config, mock_parquet_client):
    """Test importing only custom fields."""
    importer = AsanaImporter(mock_asana_config, mock_parquet_client, workspace="source")

    result = await importer.import_metadata(
        task_gids=["task_123"], metadata_types=["custom_fields"]
    )

    assert result["success"] is True
    assert "custom_fields" in result
    # Method returns all metadata types with counts (0 for non-requested types)
    assert "dependencies" in result
    assert "stories" in result
    assert result["custom_fields"] >= 0
    assert result["dependencies"] == 0  # Not requested, so should be 0
    assert result["stories"] == 0  # Not requested, so should be 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_import_dependencies_only(mock_asana_config, mock_parquet_client):
    """Test importing only dependencies."""
    importer = AsanaImporter(mock_asana_config, mock_parquet_client, workspace="source")

    result = await importer.import_metadata(
        task_gids=["task_123"], metadata_types=["dependencies"]
    )

    assert result["success"] is True
    assert "dependencies" in result


@pytest.mark.unit
@pytest.mark.asyncio
async def test_import_stories_only(mock_asana_config, mock_parquet_client):
    """Test importing only stories."""
    importer = AsanaImporter(mock_asana_config, mock_parquet_client, workspace="source")

    result = await importer.import_metadata(
        task_gids=["task_123"], metadata_types=["stories"]
    )

    assert result["success"] is True
    assert "stories" in result


@pytest.mark.unit
@pytest.mark.asyncio
async def test_import_all_metadata_types(mock_asana_config, mock_parquet_client):
    """Test importing all metadata types."""
    importer = AsanaImporter(mock_asana_config, mock_parquet_client, workspace="source")

    result = await importer.import_metadata(
        task_gids=["task_123"],
        metadata_types=["custom_fields", "dependencies", "stories"],
    )

    assert result["success"] is True
    assert "custom_fields" in result
    assert "dependencies" in result
    assert "stories" in result


@pytest.mark.unit
@pytest.mark.asyncio
async def test_import_custom_fields_various_types(
    mock_asana_config, mock_parquet_client
):
    """Test importing custom fields of various types."""
    importer = AsanaImporter(mock_asana_config, mock_parquet_client, workspace="source")

    # Mock task with various custom field types
    mock_task = {
        "gid": "task_123",
        "custom_fields": [
            {"gid": "cf_1", "name": "Text Field", "type": "text", "text_value": "Test"},
            {
                "gid": "cf_2",
                "name": "Number Field",
                "type": "number",
                "number_value": 42,
            },
            {
                "gid": "cf_3",
                "name": "Enum Field",
                "type": "enum",
                "enum_value": {"gid": "ev_1", "name": "Option 1"},
            },
            {
                "gid": "cf_4",
                "name": "Date Field",
                "type": "date",
                "date_value": {"date": "2026-01-01"},
            },
        ],
    }

    with patch.object(importer.client, "_with_retry", return_value=mock_task):
        result = await importer.import_metadata(
            task_gids=["task_123"], metadata_types=["custom_fields"]
        )

    assert result["success"] is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_import_task_without_custom_fields(
    mock_asana_config, mock_parquet_client
):
    """Test importing metadata for task without custom fields."""
    importer = AsanaImporter(mock_asana_config, mock_parquet_client, workspace="source")

    # Mock task with no custom fields
    mock_task = {"gid": "task_123", "custom_fields": []}

    with patch.object(importer.client, "_with_retry", return_value=mock_task):
        result = await importer.import_metadata(
            task_gids=["task_123"], metadata_types=["custom_fields"]
        )

    assert result["success"] is True
    assert result["custom_fields"] == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_import_dependencies(mock_asana_config, mock_parquet_client):
    """Test importing task dependencies."""
    importer = AsanaImporter(mock_asana_config, mock_parquet_client, workspace="source")

    # Mock task with dependencies
    mock_task = {
        "gid": "task_123",
        "dependencies": [
            {"gid": "dep_1"},
            {"gid": "dep_2"},
        ],
    }

    with patch.object(importer.client, "_with_retry", return_value=mock_task):
        result = await importer.import_metadata(
            task_gids=["task_123"], metadata_types=["dependencies"]
        )

    assert result["success"] is True
    assert result["dependencies"] >= 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_import_task_without_dependencies(mock_asana_config, mock_parquet_client):
    """Test importing metadata for task without dependencies."""
    importer = AsanaImporter(mock_asana_config, mock_parquet_client, workspace="source")

    # Mock task with no dependencies
    mock_task = {"gid": "task_123", "dependencies": []}

    with patch.object(importer.client, "_with_retry", return_value=mock_task):
        result = await importer.import_metadata(
            task_gids=["task_123"], metadata_types=["dependencies"]
        )

    assert result["success"] is True
    assert result["dependencies"] == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_import_stories(mock_asana_config, mock_parquet_client):
    """Test importing task stories."""
    importer = AsanaImporter(mock_asana_config, mock_parquet_client, workspace="source")

    # Mock stories
    mock_stories = [
        {
            "gid": "story_1",
            "type": "comment",
            "text": "Story 1",
            "created_at": "2025-12-30T00:00:00.000Z",
        },
        {
            "gid": "story_2",
            "type": "system",
            "text": "Task completed",
            "created_at": "2025-12-30T01:00:00.000Z",
        },
    ]

    with patch.object(importer.client, "_with_retry", return_value=mock_stories):
        result = await importer.import_metadata(
            task_gids=["task_123"], metadata_types=["stories"]
        )

    assert result["success"] is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_import_task_without_stories(mock_asana_config, mock_parquet_client):
    """Test importing metadata for task without stories."""
    importer = AsanaImporter(mock_asana_config, mock_parquet_client, workspace="source")

    # Mock no stories
    with patch.object(importer.client, "_with_retry", return_value=[]):
        result = await importer.import_metadata(
            task_gids=["task_123"], metadata_types=["stories"]
        )

    assert result["success"] is True
    assert result["stories"] == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_import_metadata_invalid_task_gid(mock_asana_config, mock_parquet_client):
    """Test importing metadata for invalid task GID."""
    importer = AsanaImporter(mock_asana_config, mock_parquet_client, workspace="source")

    # Mock API error - method catches exceptions and logs warnings
    with patch.object(
        importer.client, "_with_retry", side_effect=Exception("Task not found")
    ):
        result = await importer.import_metadata(
            task_gids=["invalid_gid"], metadata_types=["custom_fields"]
        )

    # Should return success but with 0 metadata imported
    assert result["success"] is True
    assert result["custom_fields"] == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_import_metadata_api_failure(mock_asana_config, mock_parquet_client):
    """Test importing metadata with API failure."""
    importer = AsanaImporter(mock_asana_config, mock_parquet_client, workspace="source")

    # Mock API error - method catches exceptions and logs warnings
    with patch.object(
        importer.client, "_with_retry", side_effect=Exception("API Error")
    ):
        result = await importer.import_metadata(
            task_gids=["task_123"], metadata_types=["custom_fields"]
        )

    # Should return success but with 0 metadata imported
    assert result["success"] is True
    assert result["custom_fields"] == 0


@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.asyncio
async def test_import_metadata_real_workspace(
    real_asana_config, real_parquet_client, workspace_fixtures
):
    """Integration test: Import metadata from real test workspace."""
    if not workspace_fixtures:
        pytest.skip("Workspace fixtures not available")

    importer = AsanaImporter(real_asana_config, real_parquet_client, workspace="source")

    # Use a fixture task GID from source workspace
    source_gid = workspace_fixtures.get_source_task_gid(0)
    if not source_gid:
        pytest.skip("No fixture tasks in source workspace")

    result = await importer.import_metadata(
        task_gids=[source_gid],
        metadata_types=["custom_fields", "dependencies", "stories"],
    )

    assert result["success"] is True
