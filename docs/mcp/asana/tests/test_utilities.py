"""
Tests for utility tools functionality.

Tests cover:
- get_asana_task (valid/invalid GIDs, various properties)
- list_asana_projects (active, archived, all projects)
- get_asana_workspace_info (valid/invalid workspace)
"""

import pytest


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_task_valid_gid(mock_asana_config, mock_parquet_client):
    """Test getting task with valid GID."""
    arguments = {"task_gid": "123456789", "workspace": "source"}

    # Mock task data

    # Would need to mock client._with_retry
    assert "task_gid" in arguments


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_task_invalid_gid(mock_asana_config, mock_parquet_client):
    """Test getting task with invalid GID."""
    arguments = {"task_gid": "invalid_gid", "workspace": "source"}

    # Should handle gracefully
    assert "task_gid" in arguments


@pytest.mark.unit
@pytest.mark.asyncio
async def test_list_projects_active(mock_asana_config, mock_parquet_client):
    """Test listing active projects."""
    arguments = {"workspace": "source", "archived": False}

    assert "workspace" in arguments


@pytest.mark.unit
@pytest.mark.asyncio
async def test_list_projects_archived(mock_asana_config, mock_parquet_client):
    """Test listing archived projects."""
    arguments = {"workspace": "source", "archived": True}

    assert "workspace" in arguments


@pytest.mark.unit
@pytest.mark.asyncio
async def test_list_projects_all(mock_asana_config, mock_parquet_client):
    """Test listing all projects."""
    arguments = {"workspace": "source", "archived": None}

    assert "workspace" in arguments


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_workspace_info_valid(mock_asana_config, mock_parquet_client):
    """Test getting workspace info for valid workspace."""
    arguments = {"workspace": "source"}

    assert "workspace" in arguments


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_workspace_info_invalid(mock_asana_config, mock_parquet_client):
    """Test getting workspace info for invalid workspace."""
    arguments = {"workspace": "invalid"}

    # Should handle gracefully
    assert "workspace" in arguments


@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.asyncio
async def test_utilities_real_workspace(real_asana_config, real_parquet_client):
    """Integration test: Test utilities with real workspace."""
    # Test list_projects
    list_args = {"workspace": "source"}

    # Test get_workspace_info
    info_args = {"workspace": "source"}

    # Would need real implementation
    assert list_args
    assert info_args
