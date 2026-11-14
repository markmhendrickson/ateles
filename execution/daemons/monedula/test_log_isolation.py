"""Importing monedula under pytest must not write to the real daemon log."""

from __future__ import annotations

import logging
from pathlib import Path

from lib.daemon_runtime import logging_setup

REAL_LOG = Path.home() / "Library" / "Logs" / "ateles" / "monedula.log"


def _stat(path: Path):
    return (path.stat().st_size, path.stat().st_mtime_ns) if path.exists() else None


def test_log_dir_is_redirected_away_from_real_path() -> None:
    assert logging_setup.LOG_DIR != REAL_LOG.parent


def test_importing_monedula_does_not_touch_real_log() -> None:
    before = _stat(REAL_LOG)

    import monedula  # noqa: F401  (module-level configure_daemon_logging runs)

    monedula.log.info("log isolation probe")
    for handler in logging.getLogger().handlers:
        handler.flush()

    assert _stat(REAL_LOG) == before
    files = {
        Path(h.baseFilename)
        for h in logging.getLogger().handlers
        if hasattr(h, "baseFilename")
    }
    assert files, "monedula should have a file handler"
    assert REAL_LOG not in files
