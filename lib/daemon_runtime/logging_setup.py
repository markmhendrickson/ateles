"""
lib/daemon_runtime/logging_setup.py — bounded, rotating daemon logs.

A daemon that logs one line per failed retry writes at the speed of the failure,
not the speed of the work. On 2026-08-18 ``cyphorhinus.log`` reached **276 GB** —
roughly 30% of a 926 GB disk — and filled it to 100%:

  - ``_tg_get_updates`` is paced by a 50s Telegram long-poll, so the loop is slow
    while the network is up.
  - When DNS resolution failed the request never reached the server. ``urllib``
    raised ``URLError`` in ~2ms, the handler logged one line and returned, and the
    loop immediately re-polled. Consecutive lines landed 2ms apart — ~500/second,
    sustained for the length of the outage.
  - Every one of the 20 daemons under ``execution/daemons/`` configured logging
    with ``basicConfig`` + a plain ``FileHandler``: no rotation, no size ceiling.
    Nine also flushed on every record, so there was no buffer to absorb a burst.

Disk pressure then hid its own cause: ``du`` could not finish while a 276 GB file
was being read, and the daemon looked idle in ``ps`` — an append-only writer
shows low CPU and low memory, which is exactly what a healthy process looks like.

## Posture

Two independent ceilings, because they fail differently.

``RotatingFileHandler`` bounds the bytes on disk. Even a daemon spinning at
maximum rate cannot exceed ``max_bytes * (backup_count + 1)`` — the 276 GB case
becomes 300 MB.

``_RepeatSuppressingFilter`` bounds the *lines*, which rotation alone does not:
without it a storm still rotates useful history out of the backups within
seconds, so the log survives at bounded size but says nothing about what
happened before the storm. The filter collapses consecutive identical messages,
emitting at exponentially sparser intervals (1st, 2nd, 4th, 8th, …) and a
summary count when the repetition breaks. A ten-minute outage costs ~20 lines
instead of ~300,000, and the history above it survives.

Flushing per record is preserved. It costs throughput but means a daemon killed
mid-incident leaves its last line on disk, which is when logs matter most.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from pathlib import Path

# 50 MB × (5 + 1) = 300 MB worst case per daemon.
DEFAULT_MAX_BYTES = 50 * 1024 * 1024
DEFAULT_BACKUP_COUNT = 5

LOG_DIR = Path.home() / "Library" / "Logs" / "ateles"


class _FlushingRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """Rotate on size, and flush every record so a kill -9 loses nothing."""

    def emit(self, record: logging.LogRecord) -> None:
        super().emit(record)
        self.flush()


class _RepeatSuppressingFilter(logging.Filter):
    """Collapse consecutive identical messages to exponentially sparser lines.

    Keyed on (level, rendered message), so a retry storm alternating between two
    distinct errors is still visible as two streams rather than being flattened
    into one. Emits the 1st, 2nd, 4th, 8th, ... occurrence; when the run breaks,
    the next record carries a ``[repeated N×]`` note so the count is never lost.
    """

    def __init__(self, name: str = "") -> None:
        super().__init__(name)
        self._last_key: tuple[int, str] | None = None
        self._run_length = 0
        self._pending_summary = 0

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            key = (record.levelno, record.getMessage())
        except Exception:
            # Never let the filter be the reason a log line is lost.
            return True

        if key == self._last_key:
            self._run_length += 1
            # Powers of two: 1, 2, 4, 8, 16, ...
            if self._run_length & (self._run_length - 1) == 0:
                record.msg = f"{record.getMessage()} [occurrence {self._run_length}]"
                record.args = ()
                return True
            return False

        # Run broken — attach the suppressed count to the next line through.
        if self._run_length > 1:
            self._pending_summary = self._run_length
        self._last_key = key
        self._run_length = 1

        if self._pending_summary:
            record.msg = (
                f"{record.getMessage()} "
                f"[previous message repeated {self._pending_summary}×]"
            )
            record.args = ()
            self._pending_summary = 0
        return True


def configure_daemon_logging(
    name: str,
    *,
    level: int = logging.INFO,
    max_bytes: int | None = None,
    backup_count: int | None = None,
    suppress_repeats: bool = True,
    also_stdout: bool = False,
) -> logging.Logger:
    """Configure bounded rotating file logging for a daemon.

    Args:
        name: Daemon name. Used for the logger, the ``[name]`` log prefix, and
            the ``~/Library/Logs/ateles/<name>.log`` path.
        level: Root log level.
        max_bytes: Bytes before rotation. Defaults to 50 MB; override with
            ``ATELES_LOG_MAX_BYTES``.
        backup_count: Rotated files kept. Defaults to 5; override with
            ``ATELES_LOG_BACKUP_COUNT``.
        suppress_repeats: Collapse consecutive identical messages. Leave on
            unless a daemon genuinely needs every duplicate line.
        also_stdout: Additionally log to stdout. Set this for daemons that had a
            ``StreamHandler`` before migrating — under launchd that stream is
            redirected to a file too, so it is bounded only by the same
            suppression filter, not by rotation.

    Returns:
        The configured logger.
    """
    if max_bytes is None:
        max_bytes = int(os.environ.get("ATELES_LOG_MAX_BYTES", DEFAULT_MAX_BYTES))
    if backup_count is None:
        backup_count = int(
            os.environ.get("ATELES_LOG_BACKUP_COUNT", DEFAULT_BACKUP_COUNT)
        )

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / f"{name}.log"

    formatter = logging.Formatter(f"%(asctime)s [{name}] %(levelname)s %(message)s")

    handler = _FlushingRotatingFileHandler(
        log_file, maxBytes=max_bytes, backupCount=backup_count
    )
    handler.setFormatter(formatter)

    handlers: list[logging.Handler] = [handler]
    if also_stdout:
        stream = logging.StreamHandler(sys.stdout)
        stream.setFormatter(formatter)
        handlers.append(stream)

    logging.basicConfig(level=level, handlers=handlers, force=True)
    logger = logging.getLogger(name)

    if suppress_repeats:
        # Attach to the named logger, not to the handlers and not to root.
        #
        # Handler-level: the filter runs once per handler, so with two handlers
        # each record advances the run-length counter twice and a record
        # suppressed for the first handler still reaches the second — file and
        # stdout end up disagreeing about what happened.
        #
        # Root-level: filters on a logger apply only to records logged directly
        # on it, NOT to records propagating up from children. Daemons log via
        # getLogger(name), so a root filter never fires at all.
        logger.addFilter(_RepeatSuppressingFilter())

    return logger
