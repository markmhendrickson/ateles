"""Lightweight startup validation for checkpoint replay-denial state."""

from __future__ import annotations

import os
import stat
import time
from pathlib import Path


def require_checkpoint_denial_store() -> Path:
    """Prove the configured denial store is absolute, durable, and writable."""
    configured = os.environ.get("APIS_CHECKPOINT_DENIAL_DIR", "").strip()
    if not configured:
        raise RuntimeError("APIS_CHECKPOINT_DENIAL_DIR is required")
    root = Path(configured)
    if not root.is_absolute():
        raise RuntimeError("APIS_CHECKPOINT_DENIAL_DIR must be absolute")
    try:
        root.mkdir(parents=True, exist_ok=True)
        root_mode = os.lstat(root).st_mode
        if not stat.S_ISDIR(root_mode) or stat.S_ISLNK(root_mode):
            raise RuntimeError(
                "APIS_CHECKPOINT_DENIAL_DIR must be a real directory, not a symlink"
            )
        probe = root / f".startup-probe-{os.getpid()}-{time.time_ns()}"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(probe, flags, 0o600)
        try:
            os.write(fd, b"probe\n")
            os.fsync(fd)
        finally:
            os.close(fd)
        probe.unlink()
        dir_flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            dir_flags |= os.O_DIRECTORY
        dir_fd = os.open(root, dir_flags)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except RuntimeError:
        raise
    except OSError as exc:
        raise RuntimeError(
            f"APIS_CHECKPOINT_DENIAL_DIR is unusable: {type(exc).__name__}"
        ) from exc
    return root
