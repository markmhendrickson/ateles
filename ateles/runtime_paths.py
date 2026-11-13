"""Portable runtime paths shared by standalone Ateles scripts."""

from __future__ import annotations

import os
import platform
import stat as _stat
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


# macOS marks a file whose bytes cloud storage has evicted to a placeholder
# with SF_DATALESS (sys/stat.h). The stat module exposes it from Python 3.13.
SF_DATALESS = getattr(_stat, "SF_DATALESS", 0x40000000)


def is_cloud_evicted(path_or_stat) -> bool:
    """True when a file's bytes have been evicted by cloud storage.

    Accepts a path or a stat result (anything with ``st_size``). Detects the
    macOS SF_DATALESS flag, and as a fallback a non-empty file with no
    allocated blocks. ``st_flags`` exists only on macOS/BSD; elsewhere it
    reads as 0 and only the fallback applies. A file that cannot be stat'ed
    is reported as not evicted; the caller's own read surfaces that error.
    """
    st = path_or_stat
    if isinstance(st, (str, bytes, os.PathLike)):
        try:
            st = os.stat(path_or_stat)
        except OSError:
            return False
    if getattr(st, "st_flags", 0) & SF_DATALESS:
        return True
    blocks = getattr(st, "st_blocks", None)
    return blocks == 0 and st.st_size > 0
