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
import os
import sys
import time
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


def _age(*paths: Path, secs: float = 30 * 86400) -> None:
    old = time.time() - secs
    for path in paths:
        os.utime(path, (old, old))


def _write_pair(recordings: Path, stamp: str, *, sidecars: bool = True,
                pending_store: bool = False, fresh: bool = False) -> Path:
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
    if not fresh:
        _age(*recordings.glob(f"{stamp} *"))
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
    # A leftover sidecar from a store that already landed (it is never removed).
    _write_pair(recordings, "20251231 1000", pending_store=True)
    caplog.set_level(logging.INFO)

    watcher, calls = _watcher(monkeypatch)
    # max_files_per_poll=1, so enough polls to drain the folder if unguarded.
    _poll(watcher, times=len(BACKLOG) + 2)

    assert calls == []
    guard_lines = [r.getMessage() for r in caplog.records
                   if "Backlog guard:" in r.getMessage()]
    assert len(guard_lines) == 1
    line = guard_lines[0]
    assert f"{len(BACKLOG) + 1} pre-existing pair(s) in {recordings}" in line
    assert "1 pair(s) keep a .pending-store.json sidecar" in line
    assert "does not mean the store is unfinished" in line
    assert "file(s)" not in line
    # Keyed per pair (system track), persisted in the same retry journal.
    journal = _journal()
    assert journal["version"] == 3
    assert journal["backlog_seeded_at"]
    assert sorted(journal["handled"]) == sorted(
        str(p) for p in [*systems, (recordings / "20251231 1000 system.mp4").resolve()]
    )
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


def _v2_journal(**extra) -> None:
    tyto.RECORDING_RETRY_STATE.parent.mkdir(parents=True, exist_ok=True)
    tyto.RECORDING_RETRY_STATE.write_text(json.dumps(
        {"version": 2, "pending": [], "hard_failed": [], **extra}
    ))


def test_fresh_pair_at_first_v3_start_from_v2_journal_is_transcribed(home, monkeypatch):
    recordings = tyto.RECORDINGS_DIR
    for stamp in BACKLOG:
        _write_pair(recordings, stamp)
    _v2_journal()
    # A call being recorded now, a fresh pair that already has a transcript
    # (still possibly new), and a pair that landed while the daemon was down
    # (old mtime, never processed, so no transcript sidecar).
    recording_now = _write_pair(recordings, "20260401 1000", sidecars=False, fresh=True)
    fresh_done = _write_pair(recordings, "20260401 1100", fresh=True)
    landed_down = _write_pair(recordings, "20260301 0900", sidecars=False)

    watcher, calls = _watcher(monkeypatch)
    _poll(watcher, times=len(BACKLOG) + 6)

    assert sorted(c[0] for c in calls) == sorted([recording_now, fresh_done, landed_down])


def test_pair_with_only_pending_store_and_no_journal_entry_is_transcribed(home, monkeypatch):
    recordings = tyto.RECORDINGS_DIR
    for stamp in BACKLOG:
        _write_pair(recordings, stamp)
    only_sidecar = _write_pair(recordings, "20251230 1700", sidecars=False, pending_store=True)
    _v2_journal()

    watcher, calls = _watcher(monkeypatch)
    _poll(watcher, times=len(BACKLOG) + 3)

    assert [c[0] for c in calls] == [only_sidecar]


def test_unreadable_folder_at_first_start_never_swallows_a_later_pair(home, monkeypatch):
    recordings = tyto.RECORDINGS_DIR
    recordings.rmdir()
    first, first_calls = _watcher(monkeypatch)
    _poll(first, times=2)
    assert first_calls == [] and not tyto.RECORDING_RETRY_STATE.exists()

    # The folder appears with its backlog plus one genuinely new pair.
    recordings.mkdir()
    for stamp in BACKLOG:
        _write_pair(recordings, stamp)
    new = _write_pair(recordings, "20260301 1000", sidecars=False)
    _poll(first, times=len(BACKLOG) + 4)
    assert [c[0] for c in first_calls] == [new]
    assert _journal()["backlog_seeded_at"] is not None

    # Restart after another pair landed while the daemon was down.
    while_down = _write_pair(recordings, "20260302 0800", sidecars=False)
    second, second_calls = _watcher(monkeypatch)
    _poll(second, times=4)
    assert [c[0] for c in second_calls] == [while_down]


def test_v3_journal_without_seed_marker_is_refused(home, monkeypatch, caplog):
    recordings = tyto.RECORDINGS_DIR
    _write_pair(recordings, BACKLOG[0])
    new = _write_pair(recordings, "20260301 1000", sidecars=False)
    for marker in ({}, {"backlog_seeded_at": None}):
        tyto.RECORDING_RETRY_STATE.parent.mkdir(parents=True, exist_ok=True)
        tyto.RECORDING_RETRY_STATE.write_text(json.dumps(
            {"version": 3, "pending": [], "hard_failed": [], "handled": [], **marker}
        ))
        before = tyto.RECORDING_RETRY_STATE.read_text()
        caplog.clear()
        watcher, calls = _watcher(monkeypatch)
        assert watcher._retry_state_available is False
        _poll(watcher, times=3)
        assert calls == [] and new.exists()
        assert tyto.RECORDING_RETRY_STATE.read_text() == before
        message = " ".join(r.getMessage() for r in caplog.records)
        assert "Call recording retry state is unreadable" in message
        assert "Voice Memo" not in message
        assert "do not edit or delete" in message


def test_explicit_recording_file_recovery_is_journaled_handled(home, monkeypatch):
    recordings = tyto.RECORDINGS_DIR
    _write_pair(recordings, BACKLOG[0])
    watcher, calls = _watcher(monkeypatch)
    _poll(watcher)
    new = _write_pair(recordings, "20260301 1000", sidecars=False)
    monkeypatch.setattr(watcher, "_run_transcription", lambda *a, **k: "ent_synthetic")
    assert watcher.process_selected_recording(new, store_only=False, recover_held=False) == "ent_synthetic"
    assert str(new) in _journal()["handled"]
    assert _journal()["version"] == 3

    restarted, restarted_calls = _watcher(monkeypatch)
    _poll(restarted, times=3)
    assert restarted_calls == []


def test_stale_handled_entry_never_hides_a_journaled_retry_or_hold(home, monkeypatch):
    recordings = tyto.RECORDINGS_DIR
    retry = _write_pair(recordings, "20251230 1700", sidecars=False, pending_store=True)
    held = _write_pair(recordings, "20251229 1300", sidecars=False)
    tyto.RECORDING_RETRY_STATE.parent.mkdir(parents=True)
    tyto.RECORDING_RETRY_STATE.write_text(json.dumps({
        "version": 3,
        "backlog_seeded_at": "2026-01-01T00:00:00+00:00",
        "pending": [{"path": str(retry), "retry_after": 0, "attempts": 1}],
        "hard_failed": [{"path": str(held), "error_code": "capability_denied",
                         "at": "2026-01-01T00:00:00+00:00", "notified": True}],
        "handled": [str(retry), str(held)],
    }))
    watcher, calls = _watcher(monkeypatch)
    _poll(watcher, times=3)
    assert [c[0] for c in calls] == [retry]
    assert [h["path"] for h in _journal()["hard_failed"]] == [str(held)]
    # The hold was already notified: no new alert.
    assert watcher._notifier.send.call_count == 0


def test_guard_with_no_journal_warns_through_the_notifier(home, monkeypatch, caplog):
    recordings = tyto.RECORDINGS_DIR
    for stamp in BACKLOG:
        _write_pair(recordings, stamp)
    _write_pair(recordings, "20260301 1000", sidecars=False)
    caplog.set_level(logging.WARNING)
    watcher, calls = _watcher(monkeypatch)
    assert watcher._notifier.send.call_count == 1
    text = watcher._notifier.send.call_args.args[0]
    assert f"{len(BACKLOG)} pair(s) marked handled, 1 pair(s) left" in text
    assert [r for r in caplog.records if "Backlog guard:" in r.getMessage()
            and r.levelno == logging.WARNING]


def test_guard_start_from_existing_v2_journal_does_not_notify(home, monkeypatch):
    for stamp in BACKLOG:
        _write_pair(tyto.RECORDINGS_DIR, stamp)
    _v2_journal()
    watcher, _calls = _watcher(monkeypatch)
    assert watcher._notifier.send.call_count == 0
