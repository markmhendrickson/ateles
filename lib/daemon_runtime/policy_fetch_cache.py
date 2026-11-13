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
One JSON file per request, named by a SHA-256 over the base URL, the request
body and a digest of the bearer token. The token itself is never part of the
key material, the file name or the file content; only a salted SHA-256 of it
goes into the key, so two tokens (two principals the server may scope
differently) against the same URL never share an entry. The file holds the
already-unwrapped live policy rows (the same list `fetch_active_policy_rows`
returns) plus the time they were fetched.

Location: ``$ATELES_POLICY_CACHE_DIR`` when set (it must be absolute),
otherwise ``$XDG_CACHE_HOME/ateles/policy`` (``~/.cache/ateles/policy`` by
default). The leaf directory is created 0700 and must be owned by the current
user with no group or other permission bits; each intermediate directory
this module walks into must be owned by the current user, must not be a
symbolic link and must not be group- or other-writable. On macOS the leaf is
marked excluded from Time Machine (the ``com_apple_backup_excludeItem``
extended attribute, or ``tmutil addexclusion`` when the attribute cannot be
set); when neither works the cache carries on without it.

Entry files are created 0600 through a temporary file opened with
``O_CREAT|O_EXCL|O_NOFOLLOW``, fsynced and renamed into place. A read refuses
an entry that is a symbolic link, not a regular file, not owned by the
current user, has any group/other mode bit, has more than one hard link, or
is larger than ``MAX_ENTRY_BYTES``. Each successful write also deletes any
other entry, failure marker or stale temporary file in the directory that is
older than the lifetime, so expired policy text does not linger on disk.

Failure posture
---------------
Every cache problem (unusable directory, refused or corrupt entry, expired
entry, a lock that cannot be taken, a failed write) falls through to the live
fetch. Errors from the live fetch itself are never caught here: the caller's
fail-open handling sees the same exception it saw before this module existed.

Single flight, and outages
--------------------------
Hooks for the same turn start at the same moment, so on an expired entry
they would all miss together. A per-entry ``flock`` lets the first process
fetch while the others wait (up to ``LOCK_WAIT_S``) and then read the entry
it wrote. When the holder's fetch FAILS it leaves a short failure marker; a
waiter that then takes the lock and finds a marker written after it started
waiting raises ``PolicyFetchRecentlyFailed`` at once instead of making its own
slow fetch against the same outage. Concurrent hooks therefore fail in about
one fetch timeout, not one each in turn. A caller that did not wait on that
failure still tries live, so recovery is not delayed. A waiter that times out
on the lock fetches live.

Configuration
-------------
``ATELES_POLICY_CACHE_TTL_S`` sets the lifetime in seconds (default 120,
capped at ``MAX_TTL_S``). ``0``, a negative value, a non-finite value or an
unparseable value disables the cache entirely: no directory is created and
nothing is read or written.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import hashlib
import json
import logging
import math
import os
import plistlib
import re
import secrets
import stat
import subprocess
import sys
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
# A temporary file younger than this may belong to a writer that is still
# working, so pruning leaves it alone whatever the lifetime is.
_TMP_GRACE_S = 60.0
# Bumped when the entry format or key material changes, so an old entry is a
# miss, not a misread.
FORMAT_VERSION = 2

_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
# Every open is non-blocking: a FIFO planted at a cache path must be refused,
# never wait for a writer and hang the hook.
_O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)

_IS_DARWIN = sys.platform == "darwin"
BACKUP_EXCLUDE_XATTR = "com.apple.metadata:com_apple_backup_excludeItem"
_BACKUP_EXCLUDE_VALUE = plistlib.dumps("com.apple.backupd", fmt=plistlib.FMT_BINARY)

_ENTRY_NAME = re.compile(r"^[0-9a-f]{64}\.(?:json|failed)$")
_TMP_NAME = re.compile(r"^\.[0-9a-f]{64}\.(?:json|failed)\.\d+\.[0-9a-f]{16}\.tmp$")


class CacheRefused(Exception):
    """The cache location or an entry failed a safety check."""


class PolicyFetchRecentlyFailed(RuntimeError):
    """A concurrent fetch for the same entry failed while this caller waited
    for it; this caller gives up rather than repeat a slow failing fetch."""


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


def _principal_digest(token: str) -> str:
    """Salted SHA-256 of the bearer token, or "" when there is none. Only this
    digest enters the key material; the key is then hashed again, so neither
    the token nor this digest appears in a file name or file."""
    if not token:
        return ""
    return hashlib.sha256(b"ateles-policy-cache-principal\0" + token.encode("utf-8")).hexdigest()


def cache_key(base_url: str, body: dict, token: str = "") -> str:
    """SHA-256 over the base URL, the request body and a digest of the token."""
    material = json.dumps(
        {
            "v": FORMAT_VERSION,
            "base_url": str(base_url).rstrip("/"),
            "body": body,
            "principal": _principal_digest(token),
        },
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


# ---------------------------------------------------------------------------
# Backup exclusion (macOS)
# ---------------------------------------------------------------------------
_libc = None


def _load_libc():
    global _libc
    if _libc is None:
        _libc = ctypes.CDLL(ctypes.util.find_library("c") or None, use_errno=True)
        _libc.fgetxattr.argtypes = [
            ctypes.c_int, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t,
            ctypes.c_uint32, ctypes.c_int,
        ]
        _libc.fgetxattr.restype = ctypes.c_ssize_t
        _libc.fsetxattr.argtypes = [
            ctypes.c_int, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t,
            ctypes.c_uint32, ctypes.c_int,
        ]
        _libc.fsetxattr.restype = ctypes.c_int
    return _libc


def _set_backup_exclusion_xattr(fd: int) -> bool:
    """Set the Time Machine exclusion attribute on the open directory; True
    when it is present afterwards. Raises when the attribute API is
    unavailable."""
    libc = _load_libc()
    name = BACKUP_EXCLUDE_XATTR.encode()
    if libc.fgetxattr(fd, name, None, 0, 0, 0) >= 0:
        return True
    buf = ctypes.create_string_buffer(_BACKUP_EXCLUDE_VALUE, len(_BACKUP_EXCLUDE_VALUE))
    return libc.fsetxattr(fd, name, buf, len(_BACKUP_EXCLUDE_VALUE), 0, 0) == 0


def _exclude_from_backups(fd: int, path: Path, created: bool) -> None:
    """Best effort: mark the cache directory excluded from Time Machine.
    Falls back to `tmutil addexclusion` (only when the directory was just
    created, so the subprocess does not run on every call); any failure is
    ignored."""
    if not _IS_DARWIN:
        return
    try:
        if _set_backup_exclusion_xattr(fd):
            return
    except Exception as exc:  # noqa: BLE001
        log.debug("backup-exclusion attribute unavailable (%s)", type(exc).__name__)
    if not created:
        return
    try:
        subprocess.run(
            ["tmutil", "addexclusion", str(path)],
            capture_output=True, timeout=5, check=False,
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("tmutil addexclusion failed (%s)", type(exc).__name__)


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
    created = False
    try:
        for i, name in enumerate(parts):
            if not name or name in (".", "..") or "/" in name:
                raise CacheRefused("bad cache path component")
            try:
                os.mkdir(name, 0o700, dir_fd=fd)
                created = True
            except FileExistsError:
                created = False
            try:
                child = os.open(
                    name, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_NONBLOCK | _O_CLOEXEC, dir_fd=fd
                )
            except OSError as exc:  # ELOOP / ENOTDIR on a symlink or a file
                raise CacheRefused(f"cache path component refused: {type(exc).__name__}") from None
            os.close(fd)
            fd = child
            path = path / name
            _check_dir(fd, leaf=(i == len(parts) - 1))
        _exclude_from_backups(fd, path, created)
        return fd, path
    except BaseException:
        os.close(fd)
        raise


# ---------------------------------------------------------------------------
# Entries
# ---------------------------------------------------------------------------
def _read_json(dir_fd: int, name: str) -> dict | None:
    """Parse one cache file, or None when it does not exist. Raises
    CacheRefused/OSError/ValueError on an unsafe or unreadable file."""
    try:
        fd = os.open(name, os.O_RDONLY | _O_NOFOLLOW | _O_NONBLOCK | _O_CLOEXEC, dir_fd=dir_fd)
    except FileNotFoundError:
        return None
    except OSError as exc:  # ELOOP: the file is a symbolic link
        raise CacheRefused(f"cache file refused: {type(exc).__name__}") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise CacheRefused("cache file is not a regular file")
        if st.st_uid != os.getuid():
            raise CacheRefused("cache file is owned by another user")
        if st.st_mode & 0o077:
            raise CacheRefused("cache file permissions are too open")
        if st.st_nlink != 1:
            raise CacheRefused("cache file has extra hard links")
        # Bounded read: at most one byte past the cap, so an oversized (or
        # growing) file is refused without being read whole.
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
        raise CacheRefused("cache file is too large")
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("cache file has the wrong shape")
    return data


def _read_entry(dir_fd: int, key: str, ttl: float, now: float) -> tuple[list[dict], float] | None:
    """(cached rows, age in seconds), or None on a miss. Raises on an unsafe
    or malformed entry (the caller treats that as a miss)."""
    entry = _read_json(dir_fd, f"{key}.json")
    if entry is None:
        return None
    if entry.get("v") != FORMAT_VERSION or entry.get("key") != key:
        raise ValueError("cache entry has the wrong version or key")
    fetched_at = entry.get("fetched_at")
    if isinstance(fetched_at, bool) or not isinstance(fetched_at, (int, float)):
        raise ValueError("cache entry has no fetch time")
    age = now - float(fetched_at)
    if age < 0 or age >= ttl:
        return None  # expired, or stamped in the future: a miss, not an error
    rows = entry.get("rows")
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        raise ValueError("cache entry rows have the wrong shape")
    return rows, age


def _atomic_write(dir_fd: int, dir_path: Path, name: str, payload: bytes) -> None:
    tmp = f".{name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
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
        os.replace(str(dir_path / tmp), str(dir_path / name))
    except BaseException:
        try:
            os.unlink(tmp, dir_fd=dir_fd)
        except OSError:
            pass
        raise


def _write_entry(dir_fd: int, dir_path: Path, key: str, rows: list[dict], now: float) -> None:
    payload = json.dumps(
        {"v": FORMAT_VERSION, "key": key, "fetched_at": now, "rows": rows},
        separators=(",", ":"),
    ).encode("utf-8")
    if len(payload) > MAX_ENTRY_BYTES:
        return  # would be refused on read; not worth writing
    _atomic_write(dir_fd, dir_path, f"{key}.json", payload)


def _mark_failure(dir_fd: int, dir_path: Path, key: str, now: float) -> None:
    payload = json.dumps(
        {"v": FORMAT_VERSION, "key": key, "failed_at": now}, separators=(",", ":")
    ).encode("utf-8")
    _atomic_write(dir_fd, dir_path, f"{key}.failed", payload)


def _failed_since(dir_fd: int, key: str, since: float) -> bool:
    """True when a failure marker for `key` was written at or after `since`."""
    try:
        marker = _read_json(dir_fd, f"{key}.failed")
    except Exception:  # noqa: BLE001 — an unusable marker is no marker
        return False
    if not marker or marker.get("v") != FORMAT_VERSION or marker.get("key") != key:
        return False
    failed_at = marker.get("failed_at")
    if isinstance(failed_at, bool) or not isinstance(failed_at, (int, float)):
        return False
    return float(failed_at) >= since


def _prune(dir_fd: int, ttl: float, now: float, keep: str) -> int:
    """Delete entries, failure markers and stale temp files older than the
    lifetime (temp files: older than `_TMP_GRACE_S` too). Returns how many
    were removed. Lock files are never removed: unlinking a file another
    process holds an flock on would let two processes lock different files."""
    removed = 0
    for name in os.listdir(dir_fd):
        if name == keep:
            continue
        is_tmp = bool(_TMP_NAME.match(name))
        if not is_tmp and not _ENTRY_NAME.match(name):
            continue
        try:
            st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except OSError:
            continue
        if not (stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode)):
            continue
        limit = max(ttl, _TMP_GRACE_S) if is_tmp else ttl
        if now - st.st_mtime < limit:
            continue
        try:
            os.unlink(name, dir_fd=dir_fd)
            removed += 1
        except OSError:
            pass
    return removed


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
                os.O_RDWR | os.O_CREAT | _O_NOFOLLOW | _O_NONBLOCK | _O_CLOEXEC,
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


def _try_read(dir_fd: int, key: str, ttl: float) -> tuple[list[dict], float] | None:
    try:
        return _read_entry(dir_fd, key, ttl, time.time())
    except Exception as exc:  # noqa: BLE001 — any bad entry is a miss
        log.debug("policy cache entry unusable (%s); fetching live", type(exc).__name__)
        return None


def cached_fetch_with_age(
    base_url: str,
    body: dict,
    fetch: Callable[[], list[dict]],
    token: str = "",
) -> tuple[list[dict], float]:
    """(rows, age of the read in seconds): a fresh cached result for
    (base_url, body, token), or ``fetch()``'s result with age 0.

    Exceptions raised by ``fetch`` propagate unchanged and nothing is cached
    for them. Every cache-side failure falls through to ``fetch``. Raises
    ``PolicyFetchRecentlyFailed`` only for a caller that waited on the lock
    while another caller's fetch for the same entry failed.
    """
    ttl = ttl_seconds()
    if ttl <= 0:
        return fetch(), 0.0
    try:
        key = cache_key(base_url, body, token)
        dir_fd, dir_path = _open_cache_dir()
    except Exception as exc:  # noqa: BLE001 — unusable cache: fetch live
        log.debug("policy cache unavailable (%s); fetching live", type(exc).__name__)
        return fetch(), 0.0
    lock = None
    try:
        hit = _try_read(dir_fd, key, ttl)
        if hit is not None:
            return hit
        wait_started = time.time()
        try:
            lock = _Lock(dir_fd, key, LOCK_WAIT_S)
        except Exception:  # noqa: BLE001
            lock = None
        if lock is not None and lock.acquired:
            # Another process may have filled the entry while we waited...
            hit = _try_read(dir_fd, key, ttl)
            if hit is not None:
                return hit
            # ...or failed against the same outage: do not repeat it.
            if _failed_since(dir_fd, key, wait_started):
                raise PolicyFetchRecentlyFailed(
                    "a concurrent agent_policy read failed while this one waited"
                )
        try:
            rows = fetch()
        except Exception:
            try:
                _mark_failure(dir_fd, dir_path, key, time.time())
            except Exception:  # noqa: BLE001
                pass
            raise
        now = time.time()
        try:
            _write_entry(dir_fd, dir_path, key, rows, now)
        except Exception as exc:  # noqa: BLE001 — a failed write only loses the cache
            log.debug("policy cache write failed (%s)", type(exc).__name__)
        try:
            os.unlink(f"{key}.failed", dir_fd=dir_fd)
        except OSError:
            pass
        try:
            _prune(dir_fd, ttl, now, keep=f"{key}.json")
        except Exception as exc:  # noqa: BLE001
            log.debug("policy cache prune failed (%s)", type(exc).__name__)
        return rows, 0.0
    finally:
        if lock is not None:
            try:
                lock.release()
            except Exception:  # noqa: BLE001
                pass
        os.close(dir_fd)


def cached_fetch(
    base_url: str, body: dict, fetch: Callable[[], list[dict]], token: str = ""
) -> list[dict]:
    """`cached_fetch_with_age` without the age."""
    return cached_fetch_with_age(base_url, body, fetch, token)[0]
