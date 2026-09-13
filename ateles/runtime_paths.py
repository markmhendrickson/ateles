"""Portable runtime paths shared by standalone Ateles scripts."""

from __future__ import annotations

import os
import platform
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def get_data_dir() -> Path:
    """Resolve the operator data root from config, with platform-safe defaults."""
    configured = os.environ.get("DATA_DIR", "").strip()
    if configured:
        return Path(configured).expanduser()
    if platform.system() == "Darwin":
        return (
            Path.home()
            / "Library"
            / "Mobile Documents"
            / "com~apple~CloudDocs"
            / "Documents"
            / "data"
        )
    return PROJECT_ROOT / "data"
