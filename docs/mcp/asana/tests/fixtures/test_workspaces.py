"""
Test workspace setup and teardown utilities.

Provides fixtures for managing test workspace configuration and cleanup.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env file from repo root if it exists
# Path: mcp/asana/tests/fixtures/test_workspaces.py
# To repo root: ../../../../.. (5 levels up: fixtures -> tests -> asana -> mcp -> ateles)
_current_file = Path(__file__).resolve()
repo_root = _current_file.parent.parent.parent.parent.parent
env_file = repo_root / ".env"
if env_file.exists():
    load_dotenv(env_file, override=False)  # Don't override existing env vars
# Note: If .env is not found in repo root, configure environment variables
# directly or use ~/.config/asana-mcp/.env instead


class TestWorkspaceConfig:
    """Configuration for test workspaces."""

    def __init__(
        self,
        source_pat: str | None = None,
        target_pat: str | None = None,
        source_workspace_gid: str | None = None,
        target_workspace_gid: str | None = None,
    ):
        # Load from environment if not provided
        # First try TEST_ prefixed vars (for dedicated test workspaces)
        # Then fall back to regular vars (for using production workspaces as test workspaces)
        self.source_pat = (
            source_pat
            or os.getenv("TEST_ASANA_SOURCE_PAT")
            or os.getenv("ASANA_SOURCE_PAT")
        )
        self.target_pat = (
            target_pat
            or os.getenv("TEST_ASANA_TARGET_PAT")
            or os.getenv("ASANA_TARGET_PAT")
            or os.getenv("ASANA_SOURCE_PAT")  # Fallback to source if target not set
        )
        self.source_workspace_gid = (
            source_workspace_gid
            or os.getenv("TEST_SOURCE_WORKSPACE_GID")
            or os.getenv("SOURCE_WORKSPACE_GID")
        )
        self.target_workspace_gid = (
            target_workspace_gid
            or os.getenv("TEST_TARGET_WORKSPACE_GID")
            or os.getenv("TARGET_WORKSPACE_GID")
        )

    def is_configured(self) -> bool:
        """Check if test workspaces are configured."""
        return all(
            [
                self.source_pat,
                self.target_pat,
                self.source_workspace_gid,
                self.target_workspace_gid,
            ]
        )

    def to_env_dict(self) -> dict[str, str]:
        """Convert to environment variables dict."""
        return {
            "ASANA_SOURCE_PAT": self.source_pat or "",
            "ASANA_TARGET_PAT": self.target_pat or "",
            "SOURCE_WORKSPACE_GID": self.source_workspace_gid or "",
            "TARGET_WORKSPACE_GID": self.target_workspace_gid or "",
        }


def get_test_workspace_config() -> TestWorkspaceConfig:
    """Get test workspace configuration."""
    return TestWorkspaceConfig()


def skip_if_no_test_workspaces():
    """Decorator to skip tests if test workspaces are not configured."""
    import pytest

    config = get_test_workspace_config()
    return pytest.mark.skipif(
        not config.is_configured(),
        reason="Test workspaces not configured. Set TEST_ASANA_SOURCE_PAT (or ASANA_SOURCE_PAT), "
        "TEST_ASANA_TARGET_PAT (or ASANA_TARGET_PAT), TEST_SOURCE_WORKSPACE_GID (or SOURCE_WORKSPACE_GID), "
        "and TEST_TARGET_WORKSPACE_GID (or TARGET_WORKSPACE_GID) environment variables.",
    )
