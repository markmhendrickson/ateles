"""Tests for lib/daemon_runtime/logging_setup.py.

The incident these guard against: a daemon logging one line per failed retry
wrote 276 GB in a single file and filled a 926 GB disk. The two properties that
matter are that bytes on disk stay bounded under a storm, and that the storm
does not scroll away the history preceding it.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from lib.daemon_runtime import logging_setup


@pytest.fixture(autouse=True)
def _isolate_log_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Redirect log output to a temp dir and reset logging between tests."""
    monkeypatch.setattr(logging_setup, "LOG_DIR", tmp_path)
    yield tmp_path
    for handler in logging.root.handlers[:]:
        handler.close()
        logging.root.removeHandler(handler)
    for name in list(logging.Logger.manager.loggerDict):
        lg = logging.getLogger(name)
        for filt in getattr(lg, "filters", [])[:]:
            lg.removeFilter(filt)


def _read_all(tmp_path: Path, name: str) -> str:
    """Concatenate the live log and every rotated backup."""
    parts = []
    for path in sorted(tmp_path.glob(f"{name}.log*")):
        parts.append(path.read_text())
    return "\n".join(parts)


def test_writes_to_named_log_file(tmp_path: Path):
    log = logging_setup.configure_daemon_logging("testdaemon")
    log.info("hello")
    assert (tmp_path / "testdaemon.log").exists()
    assert "hello" in (tmp_path / "testdaemon.log").read_text()


def test_format_includes_daemon_name_and_level(tmp_path: Path):
    log = logging_setup.configure_daemon_logging("testdaemon")
    log.warning("something odd")
    contents = (tmp_path / "testdaemon.log").read_text()
    assert "[testdaemon]" in contents
    assert "WARNING" in contents
    assert "something odd" in contents


def test_rotation_bounds_total_bytes_under_a_storm(tmp_path: Path):
    """The core regression: unbounded retry logging must not grow without limit."""
    max_bytes = 2048
    backup_count = 2
    log = logging_setup.configure_daemon_logging(
        "stormy",
        max_bytes=max_bytes,
        backup_count=backup_count,
        suppress_repeats=False,  # exercise rotation alone
    )

    # Distinct messages so suppression cannot be what bounds the size.
    for i in range(5000):
        log.warning(f"getUpdates network error: attempt {i}")

    total = sum(p.stat().st_size for p in tmp_path.glob("stormy.log*"))
    ceiling = max_bytes * (backup_count + 1)
    # Rotation checks size before each write, so the last record may overshoot.
    assert total <= ceiling * 1.5, f"{total} bytes exceeds bounded ceiling {ceiling}"
    assert total < 100_000, "log grew far beyond the configured ceiling"


def test_repeat_suppression_collapses_identical_messages(tmp_path: Path):
    log = logging_setup.configure_daemon_logging("repeaty")

    for _ in range(1000):
        log.warning("getUpdates network error: DNS failure")

    lines = [ln for ln in (tmp_path / "repeaty.log").read_text().splitlines() if ln]
    # Powers of two below 1000: 1,2,4,...,512 → 10 lines.
    assert len(lines) <= 12, f"expected ~10 lines from exponential backoff, got {len(lines)}"
    assert len(lines) >= 2


def test_suppression_preserves_history_above_the_storm(tmp_path: Path):
    """A storm must not scroll away what happened before it."""
    log = logging_setup.configure_daemon_logging("history")

    log.info("started processing batch 42")
    for _ in range(5000):
        log.warning("getUpdates network error: DNS failure")

    contents = _read_all(tmp_path, "history")
    assert "started processing batch 42" in contents


def test_distinct_messages_are_not_collapsed(tmp_path: Path):
    log = logging_setup.configure_daemon_logging("distinct")

    log.warning("error alpha")
    log.warning("error beta")
    log.warning("error alpha")

    contents = (tmp_path / "distinct.log").read_text()
    assert contents.count("error alpha") == 2
    assert contents.count("error beta") == 1


def test_suppressed_count_is_reported_when_run_breaks(tmp_path: Path):
    log = logging_setup.configure_daemon_logging("counted")

    for _ in range(100):
        log.warning("repeated failure")
    log.info("recovered")

    contents = (tmp_path / "counted.log").read_text()
    assert "repeated 100" in contents, "suppressed occurrences must be counted, not lost"


def test_alternating_errors_remain_visible(tmp_path: Path):
    """Two interleaved failures must not flatten into one suppressed stream."""
    log = logging_setup.configure_daemon_logging("alternating")

    for _ in range(50):
        log.warning("timeout error")
        log.warning("dns error")

    contents = (tmp_path / "alternating.log").read_text()
    assert "timeout error" in contents
    assert "dns error" in contents


def test_env_vars_override_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ATELES_LOG_MAX_BYTES", "4096")
    monkeypatch.setenv("ATELES_LOG_BACKUP_COUNT", "1")

    logging_setup.configure_daemon_logging("configured")
    handler = logging.root.handlers[0]
    assert handler.maxBytes == 4096
    assert handler.backupCount == 1


def test_records_survive_a_message_that_cannot_render(tmp_path: Path):
    """The filter must never be why a log line is lost."""
    log = logging_setup.configure_daemon_logging("broken")

    class Unrenderable:
        def __str__(self) -> str:
            raise ValueError("cannot render")

    log.warning("%s", Unrenderable())  # must not raise
    log.warning("still logging")

    assert "still logging" in (tmp_path / "broken.log").read_text()


def test_also_stdout_does_not_double_annotate(capsys, tmp_path: Path):
    """A filter shared by two handlers must annotate once, not once per handler."""
    log = logging_setup.configure_daemon_logging("dual", also_stdout=True)

    for _ in range(4):
        log.warning("same error")

    file_text = (tmp_path / "dual.log").read_text()
    assert file_text.count("[occurrence 4]") == 1
    assert "[occurrence 4] [occurrence" not in file_text

    captured = capsys.readouterr().out
    assert "same error" in captured
