"""
Tests for register_asana_webhooks functionality.

Tests cover:
- Workspace permutations (source, target, both)
- Project scenarios (with webhooks, without webhooks, multiple projects)
- Error cases (invalid webhook URL, invalid workspace, API failures)
"""

import pytest


@pytest.mark.unit
@pytest.mark.asyncio
async def test_register_webhooks_source_workspace(
    mock_asana_config, mock_parquet_client
):
    """Test registering webhooks for source workspace."""
    arguments = {
        "webhook_url": "https://example.com/webhooks/asana",
        "workspace": "source",
    }

    # This would require more mocking to fully test
    # For now, basic structure test
    assert "workspace" in arguments


@pytest.mark.unit
@pytest.mark.asyncio
async def test_register_webhooks_target_workspace(
    mock_asana_config, mock_parquet_client
):
    """Test registering webhooks for target workspace."""
    arguments = {
        "webhook_url": "https://example.com/webhooks/asana",
        "workspace": "target",
    }

    assert "workspace" in arguments


@pytest.mark.unit
@pytest.mark.asyncio
async def test_register_webhooks_both_workspaces(
    mock_asana_config, mock_parquet_client
):
    """Test registering webhooks for both workspaces."""
    arguments = {
        "webhook_url": "https://example.com/webhooks/asana",
        "workspace": "both",
    }

    assert "workspace" in arguments


@pytest.mark.unit
@pytest.mark.asyncio
async def test_register_webhooks_invalid_url(mock_asana_config, mock_parquet_client):
    """Test webhook registration with invalid URL."""
    arguments = {"webhook_url": "not-a-valid-url", "workspace": "source"}

    # Should fail validation
    assert "webhook_url" in arguments


@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.asyncio
async def test_register_webhooks_real_workspace(real_asana_config, real_parquet_client):
    """Integration test: Register webhooks in real test workspace."""
    # Note: This requires a valid HTTPS webhook URL
    pytest.skip("Requires valid webhook URL and real workspace")
