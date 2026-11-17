"""Lightweight checkpoint replay-denial state: startup validation and markers.

Standard library only, so callers that must not load the Apis daemon (the
operator's resolver CLI, the MCP server's startup check) share the exact
marker naming and validity rules the daemon uses.
"""

from __future__ import annotations

import hashlib
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


MARKER_UNSET_REASON = (
    "APIS_CHECKPOINT_DENIAL_DIR is not configured; durable cross-process "
    "denial fallback is unavailable"
)
MARKER_RELATIVE_REASON = (
    "APIS_CHECKPOINT_DENIAL_DIR must be an absolute shared-state path"
)


def checkpoint_denial_marker_with_reason(
    checkpoint_id: str,
) -> tuple[Path | None, str]:
    """The durable denial marker path for a checkpoint, or None and why not.

    An unset or relative store yields no marker: a relative path would resolve
    against whatever directory the process happens to run in.
    """
    configured = os.environ.get("APIS_CHECKPOINT_DENIAL_DIR", "").strip()
    if not configured:
        return None, MARKER_UNSET_REASON
    root = Path(configured)
    if not root.is_absolute():
        return None, MARKER_RELATIVE_REASON
    digest = hashlib.sha256(checkpoint_id.encode()).hexdigest()
    return root / f"{digest}.denied", ""


def checkpoint_denial_marker(checkpoint_id: str) -> Path | None:
    """The durable denial marker path for a checkpoint, or None if unbound."""
    return checkpoint_denial_marker_with_reason(checkpoint_id)[0]


def valid_checkpoint_denial_marker(marker: Path) -> bool:
    """A marker counts only as a regular file, never a symlink or directory."""
    try:
        mode = os.lstat(marker).st_mode
    except OSError:
        return False
    return stat.S_ISREG(mode) and not stat.S_ISLNK(mode)


def checkpoint_denial_persisted(checkpoint_id: str) -> bool:
    """Whether a valid durable denial marker exists for the checkpoint."""
    marker = checkpoint_denial_marker(checkpoint_id)
    return marker is not None and valid_checkpoint_denial_marker(marker)
