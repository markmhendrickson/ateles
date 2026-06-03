"""
Configuration management for Asana MCP Server.

Supports multiple authentication methods:
1. Environment variables (highest priority)
2. Config file (~/.config/asana-mcp/.env)
3. 1Password integration (optional, backward compatibility)
"""

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Config directory (portable)
CONFIG_DIR = Path.home() / ".config" / "asana-mcp"
CONFIG_DIR.mkdir(parents=True, exist_ok=True)
ENV_FILE = CONFIG_DIR / ".env"

# Try to load from config directory
if ENV_FILE.exists():
    load_dotenv(ENV_FILE)

# Optional: Try to import 1Password credential utility if available
HAS_CREDENTIALS_MODULE = False
try:
    # Try importing from common locations (for backward compatibility)
    import sys

    server_dir = Path(__file__).parent
    possible_paths = [
        server_dir.parent.parent.parent,  # execution/mcp-servers/asana -> execution -> personal
        server_dir.parent.parent,  # mcp-servers/asana -> mcp-servers -> execution
    ]

    for parent_path in possible_paths:
        credentials_path = parent_path / "execution" / "scripts" / "credentials.py"
        if credentials_path.exists():
            sys.path.insert(0, str(parent_path))
            try:
                from execution.scripts.credentials import get_credential

                HAS_CREDENTIALS_MODULE = True
                break
            except ImportError:
                continue
except Exception:
    pass


@dataclass
class AsanaConfig:
    """
    Configuration for connecting to Asana source and target workspaces.

    Values are primarily sourced from environment variables or a .env file:
    - ASANA_SOURCE_PAT
    - ASANA_TARGET_PAT
    - SOURCE_WORKSPACE_GID
    - TARGET_WORKSPACE_GID
    """

    source_pat: str
    target_pat: str
    source_workspace_gid: str
    target_workspace_gid: str
    fallback_assignee_email: str | None = None
    allow_overwrite: bool = False

    @classmethod
    def from_env(cls) -> "AsanaConfig":
        """Load configuration from environment variables or config file."""
        # First try environment variables
        source_pat = os.getenv("ASANA_SOURCE_PAT")
        target_pat = os.getenv("ASANA_TARGET_PAT") or source_pat
        source_ws = os.getenv("SOURCE_WORKSPACE_GID")
        target_ws = os.getenv("TARGET_WORKSPACE_GID")

        # If not found in env, try config file
        if not source_pat and ENV_FILE.exists():
            load_dotenv(ENV_FILE)
            source_pat = os.getenv("ASANA_SOURCE_PAT")
            target_pat = os.getenv("ASANA_TARGET_PAT") or source_pat
            source_ws = os.getenv("SOURCE_WORKSPACE_GID")
            target_ws = os.getenv("TARGET_WORKSPACE_GID")

        # If still not found, try 1Password (optional, backward compatibility)
        if not source_pat and HAS_CREDENTIALS_MODULE:
            try:
                source_pat = get_credential("Asana", field="source_pat")
                target_pat = get_credential("Asana", field="target_pat") or source_pat
                source_ws = get_credential("Asana", field="source_workspace_gid")
                target_ws = get_credential("Asana", field="target_workspace_gid")
            except Exception:
                pass

        missing = [
            name
            for name, value in [
                ("ASANA_SOURCE_PAT", source_pat),
                ("SOURCE_WORKSPACE_GID", source_ws),
                ("TARGET_WORKSPACE_GID", target_ws),
            ]
            if not value
        ]
        if missing:
            raise RuntimeError(
                f"Missing required configuration values: {', '.join(missing)}. "
                "Set them in the environment, a .env file, or 1Password."
            )

        fallback_assignee_email = os.getenv("FALLBACK_ASSIGNEE_EMAIL") or None
        allow_overwrite = os.getenv("ALLOW_OVERWRITE", "false").lower() in {
            "1",
            "true",
            "yes",
        }

        return cls(
            source_pat=source_pat,
            target_pat=target_pat,
            source_workspace_gid=source_ws,
            target_workspace_gid=target_ws,
            fallback_assignee_email=fallback_assignee_email,
            allow_overwrite=allow_overwrite,
        )
