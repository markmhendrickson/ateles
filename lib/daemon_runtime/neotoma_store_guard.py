"""Pacing and transient-failure policy for a pinned daemon's Neotoma stores.

Operator conditions (Neotoma lead, 2026-11-13), applied to piculet's importer
chain after it regained the ability to store:

1. **Pacing.** Stores go out at least ``NEOTOMA_STORE_MIN_INTERVAL_SECONDS``
   apart (default 12), across memos and within one memo. The importer chain is
   several processes (daemon -> importer -> transcriber -> CLI), so the clock is
   a small file guarded by an exclusive lock, held across the store so two
   stores can never overlap either. Read-only lookups are not paced.
2. **Transient failures.** A store answered with 502, 503 or a timeout is
   read back first (it may have landed); only if it did not is it retried
   once, with the same idempotency key, after
   ``NEOTOMA_STORE_RETRY_DELAY_SECONDS`` (default 120). If that fails too the
   memo is deferred: ``StoreDeferred`` -> exit ``EXIT_STORE_DEFERRED``.

Pacing and the retry policy are active only in a pinned run
(``ATELES_PINNED_NEOTOMA_SUB`` set by `pinned_cli_identity`), so other users of
the transcriber (Tyto, interactive runs) behave exactly as before.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import stat
import time
from pathlib import Path
from typing import Callable, TypeVar

from lib.daemon_runtime.pinned_cli_identity import PIN_ENV

T = TypeVar("T")

# EX_TEMPFAIL is 75 (lookup unavailable); 76 says "the store itself could not
# be completed or confirmed; try again later". Neither marks a memo seen.
EXIT_STORE_DEFERRED = 76

DEFAULT_MIN_INTERVAL_SECONDS = 12.0
DEFAULT_RETRY_DELAY_SECONDS = 120.0


class StoreDeferred(RuntimeError):
    """A store did not land and could not be confirmed; defer, do not fail."""


def active() -> bool:
    return bool(os.environ.get(PIN_ENV, "").strip())


def _float_env(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, "").strip())
    except ValueError:
        return default
    return value if value >= 0 else default


def min_interval() -> float:
    return _float_env("NEOTOMA_STORE_MIN_INTERVAL_SECONDS", DEFAULT_MIN_INTERVAL_SECONDS)


def retry_delay() -> float:
    return _float_env("NEOTOMA_STORE_RETRY_DELAY_SECONDS", DEFAULT_RETRY_DELAY_SECONDS)


def _pace_file() -> Path:
    explicit = os.environ.get("NEOTOMA_STORE_PACE_FILE", "").strip()
    if explicit:
        return Path(explicit)
    return Path.home() / ".ateles-work" / "neotoma_store_pace.json"


def _trusted_dir(path: Path) -> None:
    """Owner-only directory that is not a symlink and is ours, else StoreDeferred."""
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid():
        raise StoreDeferred("the pacing state directory is not trustworthy; deferring")
    if stat.S_IMODE(info.st_mode) & 0o077:
        path.chmod(0o700)


def _open_nofollow(path: Path, flags: int) -> int:
    """Open owner-only without following a symlink; refuse on any failure."""
    try:
        return os.open(path, flags | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), 0o600)
    except OSError as exc:
        raise StoreDeferred("the pacing state file is not trustworthy; deferring") from exc


def _read_stamp(path: Path) -> float:
    try:
        fd = _open_nofollow(path, os.O_RDONLY)
    except StoreDeferred:
        if not path.exists() and not path.is_symlink():
            return 0.0
        raise
    try:
        with os.fdopen(fd) as handle:
            return float(json.loads(handle.read()).get("last", 0))
    except Exception:
        return 0.0


def _write_stamp(path: Path, value: float) -> None:
    """Atomic: write a private temp file in the same directory, then rename."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = _open_nofollow(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
    with os.fdopen(fd, "w") as handle:
        handle.write(json.dumps({"last": value}))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def paced(
    call: Callable[[], T],
    *,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Run one Neotoma store ``call``, no sooner than the interval after the last.

    The stamp is written when the store starts and again when it ends, so the
    gap is measured from the END of the previous store (a slow store does not
    shorten the next wait) and a crash mid-store still leaves a start stamp.
    State lives in an owner-only directory; files are 0600, opened without
    following symlinks, and the stamp is replaced atomically. Not pinned: runs
    the call immediately.
    """
    if not active():
        return call()
    path = _pace_file()
    _trusted_dir(path.parent)
    lock = path.with_suffix(path.suffix + ".lock")
    fd = _open_nofollow(lock, os.O_RDWR | os.O_CREAT)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        wait = _read_stamp(path) + min_interval() - clock()
        if wait > 0:
            sleep(wait)
        _write_stamp(path, clock())
        try:
            return call()
        finally:
            _write_stamp(path, clock())
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


_TRANSIENT_RE = re.compile(
    r"(?:\bhttp|\bstatus|\bcode|\berror)[\s:=/]*50[234]\b"
    r"|\b50[234]\s+(?:bad gateway|service unavailable|gateway)"
    r"|bad gateway|service unavailable|gateway time-?out"
    r"|\betimedout\b|\beconnreset\b|\beconnrefused\b|socket hang up"
    r"|\b(?:request|connect|connection|operation|store|write|read|socket|headers)\s+(?:timed out|timeout)\b"
    r"|\btimed out after\b|\bUND_ERR_\w*TIMEOUT\b|\b(?:type)?error:\s*fetch failed\b"
    r"|\bconnection (?:reset|refused)\b",
    re.IGNORECASE,
)


def is_transient_failure(text: str) -> bool:
    """True when a failed store's output names a 502/503/504, a refused or reset
    connection, or a real timeout. A bare word "timeout" elsewhere does not count."""
    return bool(_TRANSIENT_RE.search(text or ""))
