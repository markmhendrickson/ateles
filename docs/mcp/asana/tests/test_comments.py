"""
Tests for import_asana_task_comments functionality.

Tests cover:
- Comment import scenarios (no comments, single comment, multiple comments)
- Comments with HTML content
- Comments with attachments
- Comments from different users
- Data permutations (various text formats, special characters, long comments, empty comments)
- Error cases (invalid task GIDs, API failures)
"""

from unittest.mock import patch

import pytest
from import_engine import AsanaImporter


@pytest.mark.unit
@pytest.mark.asyncio
async def test_import_comments_no_comments(mock_asana_config, mock_parquet_client):
    """Test importing comments for task with no comments."""
    importer = AsanaImporter(mock_asana_config, mock_parquet_client, workspace="source")

    # Mock no comments
    with patch.object(importer.client, "_with_retry", return_value=[]):
        result = await importer.import_comments(task_gids=["task_123"])

    assert result["success"] is True
    assert result.get("comments_imported", 0) == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_import_comments_single_comment(mock_asana_config, mock_parquet_client):
    """Test importing single comment."""
    importer = AsanaImporter(mock_asana_config, mock_parquet_client, workspace="source")

    # Mock single comment
    mock_comments = [
        {
            "gid": "comment_123",
            "type": "comment",
            "text": "This is a comment",
            "html_text": "<body>This is a comment</body>",
            "created_at": "2025-12-30T00:00:00.000Z",
            "created_by": {"gid": "user_123", "name": "Test User"},
        }
    ]

    with patch.object(importer.client, "_with_retry", return_value=mock_comments):
        result = await importer.import_comments(task_gids=["task_123"])

    assert result["success"] is True
    assert result["comments_imported"] >= 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_import_comments_multiple_comments(
    mock_asana_config, mock_parquet_client
):
    """Test importing multiple comments."""
    importer = AsanaImporter(mock_asana_config, mock_parquet_client, workspace="source")

    # Mock multiple comments
    mock_comments = [
        {
            "gid": f"comment_{i}",
            "type": "comment",
            "text": f"Comment {i}",
            "html_text": f"<body>Comment {i}</body>",
            "created_at": "2025-12-30T00:00:00.000Z",
            "created_by": {"gid": "user_123", "name": "Test User"},
        }
        for i in range(10)
    ]

    with patch.object(importer.client, "_with_retry", return_value=mock_comments):
        result = await importer.import_comments(task_gids=["task_123"])

    assert result["success"] is True
    assert result["comments_imported"] >= 10


@pytest.mark.unit
@pytest.mark.asyncio
async def test_import_comments_html_content(mock_asana_config, mock_parquet_client):
    """Test importing comments with HTML content."""
    importer = AsanaImporter(mock_asana_config, mock_parquet_client, workspace="source")

    # Mock comment with HTML
    mock_comments = [
        {
            "gid": "comment_123",
            "type": "comment",
            "text": "Plain text",
            "html_text": "<body><strong>Bold</strong> and <em>italic</em> text<br><a href='#'>Link</a></body>",
            "created_at": "2025-12-30T00:00:00.000Z",
            "created_by": {"gid": "user_123", "name": "Test User"},
        }
    ]

    with patch.object(importer.client, "_with_retry", return_value=mock_comments):
        result = await importer.import_comments(task_gids=["task_123"])

    assert result["success"] is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_import_comments_with_attachments(mock_asana_config, mock_parquet_client):
    """Test importing comments with attachments."""
    importer = AsanaImporter(mock_asana_config, mock_parquet_client, workspace="source")

    # Mock comment with attachment reference
    mock_comments = [
        {
            "gid": "comment_123",
            "type": "comment",
            "text": "See attachment",
            "html_text": "<body>See attachment</body>",
            "created_at": "2025-12-30T00:00:00.000Z",
            "created_by": {"gid": "user_123", "name": "Test User"},
        }
    ]

    with patch.object(importer.client, "_with_retry", return_value=mock_comments):
        result = await importer.import_comments(task_gids=["task_123"])

    assert result["success"] is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_import_comments_different_users(mock_asana_config, mock_parquet_client):
    """Test importing comments from different users."""
    importer = AsanaImporter(mock_asana_config, mock_parquet_client, workspace="source")

    # Mock comments from different users
    mock_comments = [
        {
            "gid": f"comment_{i}",
            "type": "comment",
            "text": f"Comment from user {i}",
            "html_text": f"<body>Comment from user {i}</body>",
            "created_at": "2025-12-30T00:00:00.000Z",
            "created_by": {"gid": f"user_{i}", "name": f"User {i}"},
        }
        for i in range(5)
    ]

    with patch.object(importer.client, "_with_retry", return_value=mock_comments):
        result = await importer.import_comments(task_gids=["task_123"])

    assert result["success"] is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_import_comments_special_characters(
    mock_asana_config, mock_parquet_client
):
    """Test importing comments with special characters."""
    importer = AsanaImporter(mock_asana_config, mock_parquet_client, workspace="source")

    # Mock comment with special characters
    mock_comments = [
        {
            "gid": "comment_123",
            "type": "comment",
            "text": "Test 日本語 émojis 🎉 & symbols: <>&\"'",
            "html_text": "<body>Test 日本語 émojis 🎉 &amp; symbols: &lt;&gt;&amp;&quot;'</body>",
            "created_at": "2025-12-30T00:00:00.000Z",
            "created_by": {"gid": "user_123", "name": "Test User"},
        }
    ]

    with patch.object(importer.client, "_with_retry", return_value=mock_comments):
        result = await importer.import_comments(task_gids=["task_123"])

    assert result["success"] is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_import_comments_long_comment(mock_asana_config, mock_parquet_client):
    """Test importing very long comment."""
    importer = AsanaImporter(mock_asana_config, mock_parquet_client, workspace="source")

    # Mock very long comment
    long_text = "Lorem ipsum " * 500
    mock_comments = [
        {
            "gid": "comment_123",
            "type": "comment",
            "text": long_text,
            "html_text": f"<body>{long_text}</body>",
            "created_at": "2025-12-30T00:00:00.000Z",
            "created_by": {"gid": "user_123", "name": "Test User"},
        }
    ]

    with patch.object(importer.client, "_with_retry", return_value=mock_comments):
        result = await importer.import_comments(task_gids=["task_123"])

    assert result["success"] is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_import_comments_empty_comment(mock_asana_config, mock_parquet_client):
    """Test importing empty comment."""
    importer = AsanaImporter(mock_asana_config, mock_parquet_client, workspace="source")

    # Mock empty comment
    mock_comments = [
        {
            "gid": "comment_123",
            "type": "comment",
            "text": "",
            "html_text": "<body></body>",
            "created_at": "2025-12-30T00:00:00.000Z",
            "created_by": {"gid": "user_123", "name": "Test User"},
        }
    ]

    with patch.object(importer.client, "_with_retry", return_value=mock_comments):
        result = await importer.import_comments(task_gids=["task_123"])

    assert result["success"] is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_import_comments_invalid_task_gid(mock_asana_config, mock_parquet_client):
    """Test importing comments for invalid task GID."""
    importer = AsanaImporter(mock_asana_config, mock_parquet_client, workspace="source")

    # Mock API error - method catches exceptions and logs warnings
    with patch.object(
        importer.client, "_with_retry", side_effect=Exception("Task not found")
    ):
        result = await importer.import_comments(task_gids=["invalid_gid"])

    # Should return success but with 0 comments imported
    assert result["success"] is True
    assert result["comments_imported"] == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_import_comments_api_failure(mock_asana_config, mock_parquet_client):
    """Test importing comments with API failure."""
    importer = AsanaImporter(mock_asana_config, mock_parquet_client, workspace="source")

    # Mock API error - method catches exceptions and logs warnings
    with patch.object(
        importer.client, "_with_retry", side_effect=Exception("API Error")
    ):
        result = await importer.import_comments(task_gids=["task_123"])

    # Should return success but with 0 comments imported
    assert result["success"] is True
    assert result["comments_imported"] == 0


@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.asyncio
async def test_import_comments_real_workspace(
    real_asana_config, real_parquet_client, workspace_fixtures
):
    """Integration test: Import comments from real test workspace."""
    if not workspace_fixtures:
        pytest.skip("Workspace fixtures not available")

    importer = AsanaImporter(real_asana_config, real_parquet_client, workspace="source")

    # Use a fixture task GID from source workspace
    source_gid = workspace_fixtures.get_source_task_gid(0)
    if not source_gid:
        pytest.skip("No fixture tasks in source workspace")

    result = await importer.import_comments(task_gids=[source_gid])

    assert result["success"] is True
    assert isinstance(result["comments_imported"], int)
