"""
Backlog guard for the two-track call-recording folder.

The call folder keeps every past ``<stamp> mic.mp4`` + ``<stamp> system.mp4``
pair beside its sidecars, and the watcher's handled set lived only in memory,
so every daemon start re-transcribed every pair already stored. These tests
drive the production watcher wiring (``_recording_watchers``) against a
temporary HOME and assert which pairs reach ``_handle_recording``.

No real transcription runs: ``_handle_recording`` is patched throughout. All
fixture names and bytes are synthetic.

Run with: pytest execution/daemons/tyto/test_tyto_recordings_backlog.py -v
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_DAEMON_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _DAEMON_DIR.parent.parent.parent
for _p in (str(_REPO_ROOT), str(_DAEMON_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import tyto  # noqa: E402

BACKLOG = ("20260101 0900", "20260101 1400", "20260102 1100")


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A temporary HOME holding only the call folder and Tyto's state dir."""
    fake_home = tmp_path / "home"
    recordings = fake_home / "recordings"
    recordings.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setattr(tyto, "TRANSCRIBE_ENABLED", True)
    monkeypatch.setattr(tyto, "RECORDINGS_DIR", recordings)
    monkeypatch.setattr(
        tyto, "RECORDING_RETRY_STATE",
        fake_home / ".local" / "state" / "ateles" / "tyto-recording-retries.json",
    )
    monkeypatch.setattr(tyto, "NATIVE_RECORDINGS_DIR", None)
    monkeypatch.setattr(tyto, "VOICE_MEMOS_DIR", None)
    return fake_home


def _write_pair(recordings: Path, stamp: str, *, sidecars: bool = True,
                pending_store: bool = False) -> Path:
    system = recordings / f"{stamp} system.mp4"
    system.write_bytes(b"synthetic system track " + stamp.encode())
    (recordings / f"{stamp} mic.mp4").write_bytes(b"synthetic mic track " + stamp.encode())
    if sidecars:
        (recordings / f"{stamp} system.mp4.transcript.txt").write_text("Synthetic.\n")
        (recordings / f"{stamp} system.mp4.stt_raw.json").write_text("{}\n")
    if pending_store:
        (recordings / f"{stamp} system.mp4.pending-store.json").write_text(
            json.dumps({"version": 1, "synthetic": True})
        )
    return system.resolve()


def _watcher(monkeypatch) -> tuple[tyto.RecordingWatcher, list[tuple[Path, Path | None]]]:
    """Construct the production call-folder watcher; record what it transcribes."""
    watchers = tyto._recording_watchers(MagicMock())
    assert len(watchers) == 1
    watcher = watchers[0]
    assert watcher._capture_method == "audio_hijack_system"
    calls: list[tuple[Path, Path | None]] = []

    async def _record(remote_path, mic_path):
        calls.append((remote_path, mic_path))
        return "success", None

    monkeypatch.setattr(watcher, "_handle_recording", _record)
    monkeypatch.setattr(watcher, "_is_settled", lambda *a: True)
    return watcher, calls


def _poll(watcher: tyto.RecordingWatcher, times: int = 1) -> None:
    for _ in range(times):
        asyncio.run(watcher.poll_once())


def _journal() -> dict:
    return json.loads(tyto.RECORDING_RETRY_STATE.read_text())


def test_pre_existing_pairs_are_marked_handled_and_not_transcribed(home, monkeypatch, caplog):
    recordings = tyto.RECORDINGS_DIR
    systems = [_write_pair(recordings, stamp) for stamp in BACKLOG]
    caplog.set_level(logging.INFO)

    watcher, calls = _watcher(monkeypatch)
    # max_files_per_poll=1, so enough polls to drain the folder if unguarded.
    _poll(watcher, times=len(BACKLOG) + 2)

    assert calls == []
    guard_lines = [r.getMessage() for r in caplog.records
                   if "Backlog guard:" in r.getMessage()]
    assert len(guard_lines) == 1
    assert f"{len(BACKLOG)} pre-existing file(s) in {recordings}" in guard_lines[0]
    # Keyed per pair (system track), persisted in the same retry journal.
    journal = _journal()
    assert journal["backlog_seeded_at"]
    assert sorted(journal["handled"]) == sorted(str(p) for p in systems)
    assert journal["pending"] == [] and journal["hard_failed"] == []


def test_pair_arriving_after_first_poll_is_transcribed(home, monkeypatch):
    recordings = tyto.RECORDINGS_DIR
    for stamp in BACKLOG:
        _write_pair(recordings, stamp)
    watcher, calls = _watcher(monkeypatch)
    _poll(watcher)
    assert calls == []

    new_system = _write_pair(recordings, "20260301 1000", sidecars=False)
    _poll(watcher, times=3)

    assert calls == [(new_system, (recordings / "20260301 1000 mic.mp4").resolve())]
    assert str(new_system) in _journal()["handled"]


def test_restart_does_not_re_mark_or_re_run_anything(home, monkeypatch, caplog):
    recordings = tyto.RECORDINGS_DIR
    for stamp in BACKLOG:
        _write_pair(recordings, stamp)
    first, first_calls = _watcher(monkeypatch)
    _poll(first)
    arrived = _write_pair(recordings, "20260301 1000", sidecars=False)
    _poll(first, times=2)
    assert first_calls == [(arrived, (recordings / "20260301 1000 mic.mp4").resolve())]
    journal_before = _journal()

    caplog.clear()
    caplog.set_level(logging.INFO)
    restarted, restarted_calls = _watcher(monkeypatch)
    _poll(restarted, times=len(BACKLOG) + 3)

    assert restarted_calls == []
    assert not [r for r in caplog.records if "Backlog guard:" in r.getMessage()]
    assert _journal() == journal_before

    # A pair that landed while the daemon was down arrived after the guard
    # first ran, so the restart must not swallow it as backlog.
    while_down = _write_pair(recordings, "20260302 0800", sidecars=False)
    second_restart, second_calls = _watcher(monkeypatch)
    _poll(second_restart, times=3)
    assert second_calls == [(while_down, (recordings / "20260302 0800 mic.mp4").resolve())]
    assert _journal()["backlog_seeded_at"] == journal_before["backlog_seeded_at"]


def test_pending_store_retry_restored_from_journal_is_still_retried(home, monkeypatch, caplog):
    recordings = tyto.RECORDINGS_DIR
    systems = [_write_pair(recordings, stamp) for stamp in BACKLOG]
    retry = _write_pair(recordings, "20251230 1700", pending_store=True)
    # The journal as an earlier daemon left it (v2): this pair's store was
    # deferred, its result kept in the .pending-store.json sidecar.
    tyto.RECORDING_RETRY_STATE.parent.mkdir(parents=True)
    tyto.RECORDING_RETRY_STATE.write_text(json.dumps({
        "version": 2,
        "pending": [{"path": str(retry), "retry_after": 0, "attempts": 1}],
        "hard_failed": [],
    }))
    caplog.set_level(logging.INFO)

    watcher, calls = _watcher(monkeypatch)
    _poll(watcher, times=len(BACKLOG) + 3)

    assert calls == [(retry, (recordings / "20251230 1700 mic.mp4").resolve())]
    guard_lines = [r.getMessage() for r in caplog.records
                   if "Backlog guard:" in r.getMessage()]
    assert len(guard_lines) == 1 and f"{len(BACKLOG)} pre-existing" in guard_lines[0]
    journal = _journal()
    assert journal["pending"] == []
    assert sorted(journal["handled"]) == sorted(str(p) for p in [*systems, retry])


def test_journaled_hold_keeps_its_disposition_not_marked_handled(home, monkeypatch):
    recordings = tyto.RECORDINGS_DIR
    for stamp in BACKLOG:
        _write_pair(recordings, stamp)
    held = _write_pair(recordings, "20251229 1300", pending_store=True)
    tyto.RECORDING_RETRY_STATE.parent.mkdir(parents=True)
    tyto.RECORDING_RETRY_STATE.write_text(json.dumps({
        "version": 2, "pending": [],
        "hard_failed": [{"path": str(held), "error_code": "capability_denied",
                         "at": "2026-01-01T00:00:00+00:00", "notified": True}],
    }))

    watcher, calls = _watcher(monkeypatch)
    _poll(watcher, times=len(BACKLOG) + 2)

    assert calls == []
    journal = _journal()
    assert str(held) not in journal["handled"]
    assert [h["path"] for h in journal["hard_failed"]] == [str(held)]
