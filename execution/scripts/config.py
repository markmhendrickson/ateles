import os
import platform
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Load .env from project root (execution/scripts -> execution -> project root)
PROJECT_ROOT = Path(__file__).parent.parent.parent
load_dotenv(PROJECT_ROOT / ".env")


def get_data_dir() -> Path:
    """
    Get the default data directory path.

    On macOS, defaults to iCloud Documents/data.
    On other platforms, defaults to PROJECT_ROOT/data.
    Can be overridden with DATA_DIR environment variable.
    """
    # Check for environment variable override first
    env_data_dir = os.getenv("DATA_DIR")
    if env_data_dir:
        return Path(env_data_dir).expanduser()

    # Default to iCloud path on macOS
    if platform.system() == "Darwin":
        icloud_path = (
            Path.home()
            / "Library"
            / "Mobile Documents"
            / "com~apple~CloudDocs"
            / "Documents"
            / "data"
        )
        return icloud_path

    # Fallback to project-relative path on other platforms
    return PROJECT_ROOT / "data"


# Default data directory
DATA_DIR = get_data_dir()


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
        source_pat = os.getenv("ASANA_SOURCE_PAT")
        target_pat = os.getenv("ASANA_TARGET_PAT") or source_pat
        source_ws = os.getenv("SOURCE_WORKSPACE_GID")
        target_ws = os.getenv("TARGET_WORKSPACE_GID")

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
                "Set them in the environment or a .env file."
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

