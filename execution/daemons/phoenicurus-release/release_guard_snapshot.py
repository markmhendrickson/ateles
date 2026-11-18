"""Digest the existing release checkout's hook and Git configuration surfaces."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path


def _mode(path: Path) -> bytes:
    """Permission bits. Git skips a hook that is not executable, so a mode change
    disables the guard without touching its name or bytes."""
    return b"%o\0" % stat.S_IMODE(path.stat().st_mode)


def _digest_node(path: Path) -> str:
    digest = hashlib.sha256()
    if path.is_symlink():
        digest.update(b"link\0" + os.readlink(path).encode())
        target = path.resolve(strict=True)
        digest.update(_digest_node(target).encode())
    elif path.is_file():
        digest.update(b"file\0" + _mode(path) + path.read_bytes())
    elif path.is_dir():
        digest.update(b"dir\0" + _mode(path))
        for child in sorted(path.iterdir()):
            digest.update(child.name.encode() + b"\0" + _digest_node(child).encode())
    elif path.exists():
        raise OSError(f"unsupported release guard node: {path}")
    else:
        digest.update(b"missing\0")
    return digest.hexdigest()


def _digest_git_entry(path: Path) -> str:
    if path.is_symlink():
        return "link:" + os.readlink(path)
    if path.is_file():
        return "file:" + _mode(path).decode() + hashlib.sha256(path.read_bytes()).hexdigest()
    if path.is_dir():
        return "directory"
    raise OSError("release git directory is unavailable")


def snapshot_release_guards(root: Path) -> dict[str, str]:
    """Capture names, types, links, permission bits, and bytes; unreadable paths fail closed."""
    git_entry = root / ".git"
    if git_entry.is_file() and not git_entry.is_symlink():
        contents = git_entry.read_text().strip()
        if not contents.startswith("gitdir: "):
            raise OSError("release git directory pointer is invalid")
        git_dir = (root / contents.removeprefix("gitdir: ")).resolve(strict=True)
    else:
        git_dir = git_entry.resolve(strict=True)
    if not git_dir.is_dir():
        raise OSError("release git directory is unavailable")
    common_file = git_dir / "commondir"
    common_dir = ((git_dir / common_file.read_text().strip()).resolve(strict=True)
                  if common_file.exists() else git_dir)
    paths = {
        "git config": common_dir / "config",
        "git worktree config": git_dir / "config.worktree",
        "git hooks": common_dir / "hooks",
        ".husky": root / ".husky",
        ".claude/hooks": root / ".claude" / "hooks",
    }
    return {".git": _digest_git_entry(git_entry),
            **{name: _digest_node(path) for name, path in paths.items()}}


def changed_guard_location(root: Path, expected: dict[str, str]) -> str | None:
    if not expected or any(not isinstance(k, str) or not isinstance(v, str)
                           for k, v in expected.items()):
        return "prepare snapshot"
    current = snapshot_release_guards(root)
    return next((name for name in current if current[name] != expected.get(name)), None)
