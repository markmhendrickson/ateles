"""
Pytest configuration and fixtures for Asana MCP tests.

Provides shared fixtures for test workspace configuration, AsanaConfig,
ParquetMCPClient, test data cleanup, and task generators.
"""

import asyncio
import os
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from dotenv import load_dotenv

# Load .env file from repo root if it exists
# Path: mcp/asana/tests/conftest.py
# To repo root: ../../../../ (4 levels up: tests -> asana -> mcp -> ateles)
repo_root = Path(__file__).parent.parent.parent.parent
env_file = repo_root / ".env"
if env_file.exists():
    load_dotenv(env_file, override=False)  # Don't override existing env vars

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from config import AsanaConfig
from export_engine import AsanaExporter
from import_engine import AsanaImporter
from parquet_client import ParquetMCPClient

from tests.fixtures.test_tasks import (
    generate_all_permutations,
    generate_basic_task,
    generate_full_property_task,
)
from tests.fixtures.test_workspaces import (
    TestWorkspaceConfig,
    get_test_workspace_config,
)
from tests.fixtures.workspace_fixtures import WorkspaceFixtures


# Test configuration
@pytest.fixture(scope="session")
def test_workspace_config() -> TestWorkspaceConfig:
    """Get test workspace configuration."""
    return get_test_workspace_config()


# Mock AsanaClientWrapper at module level to avoid real API initialization
# But skip for integration tests which need real clients
@pytest.fixture(autouse=True, scope="function")
def mock_asana_client_wrapper(request):
    """Automatically mock AsanaClientWrapper for all unit tests."""
    # Skip mocking for integration tests - check marker first
    # This must be checked BEFORE any imports that might trigger client initialization
    marker = None
    try:
        marker = request.node.get_closest_marker("integration")
    except (AttributeError, TypeError):
        pass

    # Check function name pattern as fallback (integration tests have 'real_' in name)
    test_name = getattr(request.node, "name", "")
    is_integration = (
        marker is not None or "real_workspace" in test_name or "real_" in test_name
    )

    if is_integration:
        # Don't mock for integration tests - they need real clients
        # Ensure asana module is properly imported and intact
        import asana

        # Force reload if corrupted
        if not hasattr(asana, "Configuration"):
            import importlib

            importlib.reload(asana)
            if not hasattr(asana, "Configuration"):
                raise RuntimeError(
                    f"asana module corrupted! Test: {test_name}, asana module: {asana}, dir: {[x for x in dir(asana) if 'Config' in x]}"
                )
        yield None
        return

    with (
        patch("import_engine.AsanaClientWrapper") as mock_import,
        patch("export_engine.AsanaClientWrapper") as mock_export,
        patch("sync_engine.AsanaClientWrapper") as mock_sync,
    ):
        # Create mock client instances
        mock_client_instance = MagicMock()
        mock_import.from_config_source.return_value = mock_client_instance
        mock_import.from_config_target.return_value = mock_client_instance
        mock_export.from_config_source.return_value = mock_client_instance
        mock_export.from_config_target.return_value = mock_client_instance
        mock_sync.from_config_source.return_value = mock_client_instance
        mock_sync.from_config_target.return_value = mock_client_instance

        yield {
            "import": mock_import,
            "export": mock_export,
            "sync": mock_sync,
            "client": mock_client_instance,
        }


@pytest.fixture(scope="session")
def integration_tests_enabled(test_workspace_config: TestWorkspaceConfig) -> bool:
    """Check if integration tests are enabled."""
    return test_workspace_config.is_configured()


# AsanaConfig fixtures
@pytest.fixture
def mock_asana_config(test_workspace_config: TestWorkspaceConfig) -> AsanaConfig:
    """Create mock AsanaConfig for unit tests."""
    # Create a real AsanaConfig instance with test values
    # This avoids issues with mocking when real objects are needed
    return AsanaConfig(
        source_pat=test_workspace_config.source_pat or "test_source_pat",
        target_pat=test_workspace_config.target_pat or "test_target_pat",
        source_workspace_gid=test_workspace_config.source_workspace_gid
        or "test_source_gid",
        target_workspace_gid=test_workspace_config.target_workspace_gid
        or "test_target_gid",
        fallback_assignee_email=None,
    )


@pytest.fixture
def real_asana_config(test_workspace_config: TestWorkspaceConfig) -> AsanaConfig:
    """Create real AsanaConfig for integration tests."""
    if not test_workspace_config.is_configured():
        pytest.skip("Test workspaces not configured")

    # Set environment variables
    for key, value in test_workspace_config.to_env_dict().items():
        os.environ[key] = value

    return AsanaConfig.from_env()


@pytest.fixture(scope="session")
def session_real_asana_config() -> AsanaConfig:
    """Create real AsanaConfig for session-scoped integration tests."""
    test_workspace_config = get_test_workspace_config()
    if not test_workspace_config.is_configured():
        pytest.skip("Test workspaces not configured")

    # Set environment variables
    for key, value in test_workspace_config.to_env_dict().items():
        os.environ[key] = value

    return AsanaConfig.from_env()


# ParquetMCPClient fixtures
@pytest.fixture
def mock_parquet_client() -> ParquetMCPClient:
    """Create mock ParquetMCPClient for unit tests."""
    client = AsyncMock(spec=ParquetMCPClient)

    # Mock common methods
    client.read_tasks = AsyncMock(return_value=[])
    client.add_task = AsyncMock(return_value={"success": True})
    client.update_tasks = AsyncMock(return_value={"success": True, "updated": 0})
    client.upsert_task = AsyncMock(return_value={"success": True, "action": "created"})

    client.read_comments = AsyncMock(return_value=[])
    client.upsert_comment = AsyncMock(return_value={"success": True})

    client.read_custom_fields = AsyncMock(return_value=[])
    client.upsert_custom_field = AsyncMock(return_value={"success": True})

    client.read_dependencies = AsyncMock(return_value=[])
    client.upsert_dependency = AsyncMock(return_value={"success": True})

    client.read_stories = AsyncMock(return_value=[])
    client.upsert_story = AsyncMock(return_value={"success": True})

    client.read_attachments = AsyncMock(return_value=[])
    client.upsert_attachment = AsyncMock(return_value={"success": True})

    return client


@pytest.fixture
def real_parquet_client() -> ParquetMCPClient:
    """Create real ParquetMCPClient for integration tests."""
    # Ensure PARQUET_DATA_DIR is set for tests
    if "PARQUET_DATA_DIR" not in os.environ:
        # Use test data directory
        test_data_dir = Path(__file__).parent / "test_data"
        test_data_dir.mkdir(exist_ok=True)
        os.environ["PARQUET_DATA_DIR"] = str(test_data_dir)

    return ParquetMCPClient()


@pytest.fixture(scope="session")
def session_real_parquet_client() -> ParquetMCPClient:
    """Create real ParquetMCPClient for session-scoped integration tests."""
    # Ensure PARQUET_DATA_DIR is set for tests
    if "PARQUET_DATA_DIR" not in os.environ:
        # Use test data directory
        test_data_dir = Path(__file__).parent / "test_data"
        test_data_dir.mkdir(exist_ok=True)
        os.environ["PARQUET_DATA_DIR"] = str(test_data_dir)

    return ParquetMCPClient()


# Task data fixtures
@pytest.fixture
def basic_task() -> dict[str, Any]:
    """Generate a basic test task."""
    return generate_basic_task()


@pytest.fixture
def full_property_task() -> dict[str, Any]:
    """Generate a task with all properties."""
    return generate_full_property_task()


@pytest.fixture
def all_task_permutations() -> list[dict[str, Any]]:
    """Generate all task permutations for comprehensive testing."""
    return generate_all_permutations()


# Workspace fixtures for integration tests
@pytest.fixture(scope="session")
async def workspace_fixtures(session_real_asana_config, session_real_parquet_client):
    """
    Populate source and target workspaces with test fixtures.

    This fixture runs once per test session and creates test tasks
    in both workspaces covering various property permutations.
    """
    # Only populate if workspaces are configured
    test_workspace_config = get_test_workspace_config()
    if not test_workspace_config.is_configured():
        yield None
        return

    # Create importers/exporters
    source_importer = AsanaImporter(
        session_real_asana_config, session_real_parquet_client, workspace="source"
    )
    target_exporter = AsanaExporter(
        session_real_asana_config, session_real_parquet_client, workspace="target"
    )

    # Create workspace fixtures manager
    fixtures = WorkspaceFixtures(
        source_importer, target_exporter, session_real_parquet_client
    )

    # Populate workspaces
    print("\n[Workspace Fixtures] Populating source workspace...")
    try:
        source_gids = await fixtures.populate_source_workspace()
        print(
            f"[Workspace Fixtures] Created {len(source_gids)} tasks in source workspace"
        )
    except Exception as e:
        print(f"[Workspace Fixtures] Error populating source workspace: {e}")
        source_gids = []

    print("[Workspace Fixtures] Populating target workspace...")
    try:
        target_gids = await fixtures.populate_target_workspace()
        print(
            f"[Workspace Fixtures] Created {len(target_gids)} tasks in target workspace"
        )
    except Exception as e:
        print(f"[Workspace Fixtures] Error populating target workspace: {e}")
        target_gids = []

    yield fixtures

    # Cleanup (optional - can be implemented later)
    # await fixtures.cleanup()


# Cleanup fixtures
@pytest.fixture(autouse=True)
async def cleanup_test_data(request):
    """Cleanup test data after each test."""
    # Setup: nothing to do before test
    yield

    # Teardown: cleanup after test
    # This will be implemented based on specific test needs
    # For now, just ensure we're in a clean state
    pass


# Event loop fixture for async tests
@pytest.fixture(scope="session")
def event_loop():
    """Create event loop for async tests."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


# Test markers
def pytest_configure(config):
    """Register custom markers."""
    config.addinivalue_line("markers", "unit: Unit tests (no external dependencies)")
    config.addinivalue_line(
        "markers", "integration: Integration tests (require test workspaces)"
    )
    config.addinivalue_line(
        "markers", "premium: Premium feature tests (may fail based on plan)"
    )
    config.addinivalue_line("markers", "slow: Slow-running tests")


# Collection hooks
def pytest_collection_modifyitems(config, items):
    """Modify test collection to skip integration tests if not configured."""
    if not get_test_workspace_config().is_configured():
        skip_integration = pytest.mark.skip(reason="Test workspaces not configured")
        for item in items:
            if "integration" in item.keywords:
                item.add_marker(skip_integration)
