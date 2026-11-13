"""Short-lived, cross-process cache for the session-side agent_policy read.

Every interactive turn runs the rule-delivery hooks, and each of them reads
the whole `agent_policy` corpus through
`policy_skill_renderer.fetch_active_policy_rows`. Several hooks fire on the
same event (and the same hook can be wired at both user and project level),
so one turn used to issue several identical full-corpus reads against the
hosted Neotoma. This module lets those reads share one result for a short
window.

Stdlib-only: the hooks that import it cannot import third-party packages.

What it stores, and where
-------------------------
One JSON file per request, named by a SHA-256 of the base URL and the request
body. The bearer token is never part of the key, the file name or the file
content. The file holds the already-unwrapped live policy rows (the same list
`fetch_active_policy_rows` returns) plus the time they were fetched.

Location: ``$ATELES_POLICY_CACHE_DIR`` when set, otherwise
``$XDG_CACHE_HOME/ateles/policy`` (``~/.cache/ateles/policy`` by default).
The leaf directory is created 0700 and must be owned by the current user
with no group or other permission bits; each intermediate directory this
module walks into must be owned by the current user, must not be a symbolic
link and must not be group- or other-writable. Entry files are created 0600
through a temporary file opened with ``O_CREAT|O_EXCL|O_NOFOLLOW``, fsynced
and renamed into place. A read refuses an entry that is a symbolic link, not
a regular file, not owned by the current user, has any group/other mode bit,
has more than one hard link, or is larger than ``MAX_ENTRY_BYTES``.

Failure posture
---------------
Every cache problem (unusable directory, refused or corrupt entry, expired
entry, a lock that cannot be taken, a failed write) falls through to the live
fetch, so a broken cache costs exactly what having no cache cost. Errors from
the live fetch itself are never caught here: the caller's fail-open handling
sees the same exception it saw before this module existed.

Single flight
-------------
Hooks for the same turn start at the same moment, so on an expired entry
they would all miss together. A per-entry ``flock`` lets the first process
fetch while the others wait (up to ``LOCK_WAIT_S``) and then read the entry
it wrote. A waiter that times out fetches live.

Configuration
-------------
``ATELES_POLICY_CACHE_TTL_S`` sets the lifetime in seconds (default 120,
capped at ``MAX_TTL_S``). ``0``, a negative value or an unparseable value
disables the cache entirely: no directory is created and nothing is read or
written.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import secrets
import stat
import time
from pathlib import Path
from typing import Callable

try:  # Unix only; without it the cache still works, just without single flight.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

TTL_ENV = "ATELES_POLICY_CACHE_TTL_S"
DIR_ENV = "ATELES_POLICY_CACHE_DIR"
DEFAULT_TTL_S = 120.0
MAX_TTL_S = 600.0
MAX_ENTRY_BYTES = 16 * 1024 * 1024
LOCK_WAIT_S = 20.0
_LOCK_POLL_S = 0.05
# Bumped when the entry format changes, so an old entry is a miss, not a
# misread.
FORMAT_VERSION = 1

_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)


class CacheRefused(Exception):
    """The cache location or an entry failed a safety check."""


def ttl_seconds() -> float:
    """The configured lifetime, or 0.0 when caching is disabled."""
    raw = os.environ.get(TTL_ENV)
    if raw is None or raw.strip() == "":
        return DEFAULT_TTL_S
    try:
        value = float(raw)
    except ValueError:
        return 0.0
    if not math.isfinite(value) or value <= 0:
        return 0.0
    return min(value, MAX_TTL_S)


def cache_key(base_url: str, body: dict) -> str:
    """SHA-256 over the base URL and the request body only. Never the token."""
    material = json.dumps(
        {"v": FORMAT_VERSION, "base_url": str(base_url).rstrip("/"), "body": body},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _cache_location() -> tuple[Path, list[str]]:
    """(trusted base directory, components to walk into below it)."""
    override = os.environ.get(DIR_ENV, "").strip()
    if override:
        path = Path(override).expanduser()
        if not path.is_absolute():
            raise CacheRefused("cache dir override is not absolute")
        return path.parent, [path.name]
    xdg = os.environ.get("XDG_CACHE_HOME", "").strip()
    base = Path(xdg) if xdg and Path(xdg).is_absolute() else Path.home() / ".cache"
    return base, ["ateles", "policy"]


def _check_dir(fd: int, *, leaf: bool) -> None:
    st = os.fstat(fd)
    if not stat.S_ISDIR(st.st_mode):
        raise CacheRefused("cache path is not a directory")
    if st.st_uid != os.getuid():
        raise CacheRefused("cache directory is owned by another user")
    forbidden = 0o077 if leaf else 0o022
    if st.st_mode & forbidden:
        raise CacheRefused("cache directory permissions are too open")


def _open_cache_dir() -> tuple[int, Path]:
    """Open (creating as needed) the cache directory; return (fd, path).

    Each component below the trusted base is created 0700 and opened with
    ``O_NOFOLLOW`` relative to its parent's descriptor, so a symbolic link
    anywhere in the walked part of the path is refused rather than followed.
    """
    base, parts = _cache_location()
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(str(base), os.O_RDONLY | _O_DIRECTORY | _O_CLOEXEC)
    path = base
    try:
        for i, name in enumerate(parts):
            if not name or name in (".", "..") or "/" in name:
                raise CacheRefused("bad cache path component")
            try:
                os.mkdir(name, 0o700, dir_fd=fd)
            except FileExistsError:
                pass
            try:
                child = os.open(
                    name, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC, dir_fd=fd
                )
            except OSError as exc:  # ELOOP / ENOTDIR on a symlink or a file
                raise CacheRefused(f"cache path component refused: {type(exc).__name__}") from None
            os.close(fd)
            fd = child
            path = path / name
            _check_dir(fd, leaf=(i == len(parts) - 1))
        return fd, path
    except BaseException:
        os.close(fd)
        raise


def _read_entry(dir_fd: int, key: str, ttl: float, now: float) -> list[dict] | None:
    """The cached rows, or None on a miss. Raises CacheRefused/OSError/ValueError
    on an unsafe or unreadable entry (the caller treats those as a miss)."""
    name = f"{key}.json"
    try:
        fd = os.open(name, os.O_RDONLY | _O_NOFOLLOW | _O_CLOEXEC, dir_fd=dir_fd)
    except FileNotFoundError:
        return None
    except OSError as exc:  # ELOOP: the entry is a symbolic link
        raise CacheRefused(f"cache entry refused: {type(exc).__name__}") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise CacheRefused("cache entry is not a regular file")
        if st.st_uid != os.getuid():
            raise CacheRefused("cache entry is owned by another user")
        if st.st_mode & 0o077:
            raise CacheRefused("cache entry permissions are too open")
        if st.st_nlink != 1:
            raise CacheRefused("cache entry has extra hard links")
        if st.st_size > MAX_ENTRY_BYTES:
            raise CacheRefused("cache entry is too large")
        chunks = []
        remaining = MAX_ENTRY_BYTES + 1
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 1 << 20))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
    finally:
        os.close(fd)
    if len(raw) > MAX_ENTRY_BYTES:
        raise CacheRefused("cache entry is too large")
    entry = json.loads(raw.decode("utf-8"))
    if not isinstance(entry, dict) or entry.get("v") != FORMAT_VERSION or entry.get("key") != key:
        raise ValueError("cache entry has the wrong shape")
    fetched_at = entry.get("fetched_at")
    if isinstance(fetched_at, bool) or not isinstance(fetched_at, (int, float)):
        raise ValueError("cache entry has no fetch time")
    age = now - float(fetched_at)
    if age < 0 or age >= ttl:
        return None  # expired, or stamped in the future: a miss, not an error
    rows = entry.get("rows")
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        raise ValueError("cache entry rows have the wrong shape")
    return rows


def _write_entry(dir_fd: int, dir_path: Path, key: str, rows: list[dict], now: float) -> None:
    payload = json.dumps(
        {"v": FORMAT_VERSION, "key": key, "fetched_at": now, "rows": rows},
        separators=(",", ":"),
    ).encode("utf-8")
    if len(payload) > MAX_ENTRY_BYTES:
        return  # would be refused on read; not worth writing
    tmp = f".{key}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    fd = os.open(
        tmp,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _O_CLOEXEC,
        0o600,
        dir_fd=dir_fd,
    )
    try:
        try:
            os.fchmod(fd, 0o600)  # independent of the process umask
            view = memoryview(payload)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        # os.replace has no dir_fd form on every platform; the directory was
        # verified (owner, mode, no symlink) through dir_fd just before.
        os.replace(str(dir_path / tmp), str(dir_path / f"{key}.json"))
    except BaseException:
        try:
            os.unlink(tmp, dir_fd=dir_fd)
        except OSError:
            pass
        raise


class _Lock:
    """Exclusive flock on ``<key>.lock`` in the cache dir, with a bounded wait.
    ``acquired`` is False when it could not be taken in time (or at all)."""

    def __init__(self, dir_fd: int, key: str, wait_s: float) -> None:
        self.fd: int | None = None
        self.acquired = False
        if fcntl is None:
            return
        try:
            fd = os.open(
                f"{key}.lock",
                os.O_RDWR | os.O_CREAT | _O_NOFOLLOW | _O_CLOEXEC,
                0o600,
                dir_fd=dir_fd,
            )
        except OSError:
            return
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
            os.close(fd)
            return
        self.fd = fd
        deadline = time.monotonic() + wait_s
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.acquired = True
                return
            except OSError:
                if time.monotonic() >= deadline:
                    return
                time.sleep(_LOCK_POLL_S)

    def release(self) -> None:
        if self.fd is None:
            return
        try:
            if self.acquired and fcntl is not None:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
        finally:
            os.close(self.fd)
            self.fd = None


def _try_read(dir_fd: int, key: str, ttl: float) -> list[dict] | None:
    try:
        return _read_entry(dir_fd, key, ttl, time.time())
    except Exception as exc:  # noqa: BLE001 — any bad entry is a miss
        log.debug("policy cache entry unusable (%s); fetching live", type(exc).__name__)
        return None


def cached_fetch(base_url: str, body: dict, fetch: Callable[[], list[dict]]) -> list[dict]:
    """Return a fresh cached result for (base_url, body), or call ``fetch``
    and cache what it returns.

    Exceptions raised by ``fetch`` propagate unchanged and nothing is cached
    for them. Every cache-side failure falls through to ``fetch``.
    """
    ttl = ttl_seconds()
    if ttl <= 0:
        return fetch()
    try:
        key = cache_key(base_url, body)
        dir_fd, dir_path = _open_cache_dir()
    except Exception as exc:  # noqa: BLE001 — unusable cache: fetch live
        log.debug("policy cache unavailable (%s); fetching live", type(exc).__name__)
        return fetch()
    lock = None
    try:
        rows = _try_read(dir_fd, key, ttl)
        if rows is not None:
            return rows
        try:
            lock = _Lock(dir_fd, key, LOCK_WAIT_S)
        except Exception:  # noqa: BLE001
            lock = None
        if lock is not None and lock.acquired:
            # Another process may have filled the entry while we waited.
            rows = _try_read(dir_fd, key, ttl)
            if rows is not None:
                return rows
        rows = fetch()
        try:
            _write_entry(dir_fd, dir_path, key, rows, time.time())
        except Exception as exc:  # noqa: BLE001 — a failed write only loses the cache
            log.debug("policy cache write failed (%s)", type(exc).__name__)
        return rows
    finally:
        if lock is not None:
            try:
                lock.release()
            except Exception:  # noqa: BLE001
                pass
        os.close(dir_fd)
