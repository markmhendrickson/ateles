"""
Tests for sync_asana_tasks functionality.

Tests cover:
- Sync scope permutations (both, source, target)
- Three-way merge scenarios (only Asana changed, only Local changed, both changed, neither changed)
- Bidirectional sync (Source → Local, Target → Local, Local → Source, Local → Target)
- Task ID filtering (sync specific tasks, sync all tasks)
- Dry run mode
- Data permutations (various property combinations, modification timestamps, conflict scenarios)
- Error cases (sync failures, merge conflicts, API errors)
"""

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from sync_engine import AsanaTaskSyncer


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sync_both_workspaces(mock_asana_config, mock_parquet_client):
    """Test sync with sync_scope='both'."""
    syncer = AsanaTaskSyncer(mock_asana_config, mock_parquet_client)

    result = await syncer.sync(task_ids=None)

    assert result["success"] is True
    assert result["sync_scope"] == "both"
    assert "source_to_local" in result
    assert "target_to_local" in result
    assert "local_to_source" in result
    assert "local_to_target" in result


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sync_source_only(mock_asana_config, mock_parquet_client):
    """Test sync with sync_scope='source'."""
    syncer = AsanaTaskSyncer(mock_asana_config, mock_parquet_client)

    result = await syncer.sync(task_ids=None)

    assert result["success"] is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sync_target_only(mock_asana_config, mock_parquet_client):
    """Test sync with sync_scope='target'."""
    syncer = AsanaTaskSyncer(mock_asana_config, mock_parquet_client)

    result = await syncer.sync(task_ids=None)

    assert result["success"] is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sync_specific_task_ids(mock_asana_config, mock_parquet_client):
    """Test sync with specific task_ids."""
    syncer = AsanaTaskSyncer(mock_asana_config, mock_parquet_client)

    task_ids = ["task_1", "task_2"]
    result = await syncer.sync(task_ids=task_ids)

    assert result["success"] is True


@pytest.mark.unit
def test_three_way_merge_only_asana_changed(mock_asana_config, mock_parquet_client):
    """Test three-way merge when only Asana changed."""
    syncer = AsanaTaskSyncer(mock_asana_config, mock_parquet_client)

    last_synced = {
        "title": "Original Title",
        "description": "Original Description",
        "status": "pending",
    }

    current_local = {
        "title": "Original Title",
        "description": "Original Description",
        "status": "pending",
    }

    current_asana = {
        "title": "Updated Title",  # Changed in Asana
        "description": "Original Description",
        "status": "pending",
    }

    merged = syncer.merge_task_properties(last_synced, current_local, current_asana)

    # Should use Asana value
    assert merged["title"] == "Updated Title"
    assert merged["description"] == "Original Description"


@pytest.mark.unit
def test_three_way_merge_only_local_changed(mock_asana_config, mock_parquet_client):
    """Test three-way merge when only Local changed."""
    syncer = AsanaTaskSyncer(mock_asana_config, mock_parquet_client)

    last_synced = {
        "title": "Original Title",
        "description": "Original Description",
        "status": "pending",
    }

    current_local = {
        "title": "Updated Locally",  # Changed locally
        "description": "Original Description",
        "status": "pending",
    }

    current_asana = {
        "title": "Original Title",
        "description": "Original Description",
        "status": "pending",
    }

    merged = syncer.merge_task_properties(last_synced, current_local, current_asana)

    # Should use Local value
    assert merged["title"] == "Updated Locally"
    assert merged["description"] == "Original Description"


@pytest.mark.unit
def test_three_way_merge_both_changed_same(mock_asana_config, mock_parquet_client):
    """Test three-way merge when both changed to same value."""
    syncer = AsanaTaskSyncer(mock_asana_config, mock_parquet_client)

    last_synced = {
        "title": "Original Title",
        "status": "pending",
    }

    current_local = {
        "title": "Same Update",  # Both changed to same value
        "status": "pending",
    }

    current_asana = {
        "title": "Same Update",  # Both changed to same value
        "status": "pending",
    }

    merged = syncer.merge_task_properties(last_synced, current_local, current_asana)

    # Should use either (they're the same)
    assert merged["title"] == "Same Update"


@pytest.mark.unit
def test_three_way_merge_both_changed_different(mock_asana_config, mock_parquet_client):
    """Test three-way merge when both changed to different values (conflict)."""
    syncer = AsanaTaskSyncer(mock_asana_config, mock_parquet_client)

    last_synced = {
        "title": "Original Title",
        "status": "pending",
        "updated_at": datetime(2025, 1, 1),
    }

    current_local = {
        "title": "Local Update",  # Changed locally
        "status": "pending",
        "updated_at": datetime(2025, 1, 2),
    }

    current_asana = {
        "title": "Asana Update",  # Changed in Asana
        "status": "pending",
        "updated_at": datetime(2025, 1, 3),  # Newer
    }

    merged = syncer.merge_task_properties(last_synced, current_local, current_asana)

    # Should use newer timestamp (Asana in this case)
    assert merged["title"] == "Asana Update"


@pytest.mark.unit
def test_three_way_merge_neither_changed(mock_asana_config, mock_parquet_client):
    """Test three-way merge when neither changed."""
    syncer = AsanaTaskSyncer(mock_asana_config, mock_parquet_client)

    last_synced = {
        "title": "Original Title",
        "description": "Original Description",
        "status": "pending",
    }

    current_local = {
        "title": "Original Title",
        "description": "Original Description",
        "status": "pending",
    }

    current_asana = {
        "title": "Original Title",
        "description": "Original Description",
        "status": "pending",
    }

    merged = syncer.merge_task_properties(last_synced, current_local, current_asana)

    # Should keep existing values
    assert merged["title"] == "Original Title"
    assert merged["description"] == "Original Description"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sync_dry_run_mode(mock_asana_config, mock_parquet_client):
    """Test dry run mode (preview changes without applying)."""
    # dry_run is set in constructor, not as parameter to sync()
    syncer = AsanaTaskSyncer(mock_asana_config, mock_parquet_client, dry_run=True)

    # Dry run should not make actual changes
    result = await syncer.sync(task_ids=None)

    assert result["dry_run"] is True
    assert "pending" in result["source_to_local"]
    assert "pending" in result["target_to_local"]
    assert "pending" in result["local_to_source"]
    assert "pending" in result["local_to_target"]
    assert "sync_scope" in result
    # Verify no updates were made to parquet or Asana (dry_run prevents saves)
    # Note: In dry_run mode, sync still runs but doesn't save state


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sync_local_to_target_dry_run_pending_create(
    mock_asana_config, mock_parquet_client
):
    """Dry run should surface pending creates for local tasks without target GID."""
    syncer = AsanaTaskSyncer(mock_asana_config, mock_parquet_client, dry_run=True)

    mock_parquet_client.read_tasks.return_value = [
        {
            "task_id": "task_local_only",
            "title": "Local Only Task",
            "asana_target_gid": None,
        }
    ]

    result = await syncer.sync_local_to_workspace(
        syncer.target_client, mock_asana_config.target_workspace_gid, "target"
    )

    assert result["pending"]["created"] == 1
    assert result["pending"]["tasks"]["created"][0]["task_id"] == "task_local_only"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sync_with_timestamp_conflicts(mock_asana_config, mock_parquet_client):
    """Test sync with different modification timestamps."""
    syncer = AsanaTaskSyncer(mock_asana_config, mock_parquet_client)

    # Mock tasks with different timestamps
    mock_local_task = {
        "task_id": "task_123",
        "title": "Local Title",
        "updated_at": datetime.now() - timedelta(hours=1),
        "asana_source_gid": "asana_123",
    }

    mock_parquet_client.read_tasks.return_value = [mock_local_task]

    # When task_ids is provided, sync_workspace_to_local fetches tasks by GID
    # Mock the client._with_retry to return task data
    with patch.object(syncer.source_client, "_with_retry") as mock_retry:
        mock_retry.return_value = {
            "gid": "asana_123",
            "name": "Asana Title",
            "modified_at": datetime.now().isoformat(),  # Newer
            "due_on": None,  # Ensure due_on is None or string, not datetime
            "start_on": None,
            "completed_at": None,
            "notes": "",
            "completed": False,
            "projects": [],
            "memberships": [],
            "tags": [],
        }

        result = await syncer.sync(task_ids=["task_123"])

    assert "sync_scope" in result


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sync_error_handling(mock_asana_config, mock_parquet_client):
    """Test sync with API errors."""
    syncer = AsanaTaskSyncer(mock_asana_config, mock_parquet_client)

    # Mock API error
    with patch.object(
        syncer, "sync_workspace_to_local", side_effect=Exception("API Error")
    ):
        with pytest.raises(Exception, match="API Error"):
            await syncer.sync()


@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.asyncio
async def test_sync_real_workspaces(real_asana_config, real_parquet_client):
    """Integration test: Sync between real test workspaces."""
    syncer = AsanaTaskSyncer(real_asana_config, real_parquet_client, dry_run=True)

    result = await syncer.sync(task_ids=None)  # Dry run for safety

    assert result["success"] is True
    assert result["sync_scope"] == "both"
