"""
Effect tests for Tyto's RecordingWatcher — macOS Voice Memos support.

Covers the two things that had to become per-watcher for memos to work at all:
the file-matching predicate (memo filenames carry no remote/system track name,
so the Audio Hijack predicate rejected every one of them) and the mic-pair
lookup (a memo is always single-file and must never take the two-file merge
path). Plus the backlog guard, which is the requirement that matters most:
the real Voice Memos directory holds hundreds of archived memos and a naive
first poll would fire one transcription and one notification per file.

Every test asserts an observable effect — which paths were handed to
_handle_recording, with what mic argument — never merely "no exception".
No real transcription is ever run: _handle_recording is patched throughout.

All fixture filenames are invented. No real memo names or content appear here.

Run with: pytest execution/daemons/tyto/test_tyto_voice_memos.py -v
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_DAEMON_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _DAEMON_DIR.parent.parent.parent
for _p in (str(_REPO_ROOT), str(_DAEMON_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# tyto.py reads env at import time and pulls in lib.notify; neither performs I/O
# at module load, so a plain import is safe.
import tyto  # noqa: E402

# Ages, in seconds, used to place fixture files either side of the age window.
OLD = 400 * 24 * 3600  # ~13 months — squarely "archive"
FRESH = 30


def _write(dir_path: Path, name: str, *, age_secs: float = 0.0, size: int = 2048) -> Path:
    """Create a fixture file with a controlled mtime."""
    path = dir_path / name
    path.write_bytes(b"\0" * size)
    if age_secs:
        when = time.time() - age_secs
        os.utime(path, (when, when))
    return path


def _make_watcher(watch_dir: Path, **kwargs) -> tyto.RecordingWatcher:
    return tyto.RecordingWatcher(watch_dir, MagicMock(), **kwargs)


def _settled(watcher: tyto.RecordingWatcher) -> None:
    """Pre-seed _seen so files count as settled on the very next poll.

    _is_settled requires an unchanged mtime across two sightings plus
    SETTLE_SECS of age. Recording the current mtime here collapses that to one
    poll without touching the settling logic under test elsewhere.
    """
    for path in watcher._dir.iterdir():
        if path.is_file():
            stat = path.stat()
            watcher._seen[path] = (stat.st_mtime_ns, stat.st_size)


def _poll(watcher: tyto.RecordingWatcher) -> list[tuple[Path, Path | None]]:
    """Run one poll with _handle_recording patched; return its call args."""
    calls: list[tuple[Path, Path | None]] = []

    async def _record(remote_path, mic_path):
        calls.append((remote_path, mic_path))
        return True

    with patch.object(watcher, "_handle_recording", side_effect=_record):
        asyncio.run(watcher.poll_once())
    return calls


# ── Memo files are now picked up ─────────────────────────────────────────────


def test_memo_filename_is_picked_up(tmp_path):
    """A Voice Memos-style name (no 'remote'/'system' token) is transcribed.

    This is the core change: under the old predicate this file was skipped.
    """
    memo = _write(tmp_path, "20260907 193532-AAAA1111.m4a", age_secs=FRESH)
    watcher = _make_watcher(
        tmp_path, capture_method="voice_memo", paired=False,
        extensions={".m4a", ".wav"}, max_age_secs=3600,
    )
    _settled(watcher)

    calls = _poll(watcher)

    assert [c[0] for c in calls] == [memo]


def test_memo_carries_voice_memo_capture_method(tmp_path):
    """The memo watcher stamps a capture_method distinct from both others."""
    watcher = _make_watcher(tmp_path, capture_method="voice_memo", paired=False)
    assert watcher._capture_method == "voice_memo"
    assert watcher._capture_method not in ("audio_hijack_system", "platform_native")


def test_old_predicate_would_have_rejected_the_memo(tmp_path):
    """Documents the blocker: the paired predicate rejects memo filenames.

    Guards against someone 'simplifying' the memo watcher back onto the paired
    predicate, which would silently stop picking memos up again.
    """
    memo = _write(tmp_path, "20260907 193532-AAAA1111.m4a")
    paired = _make_watcher(tmp_path)  # defaults: paired=True

    assert paired._is_remote_file(memo) is False
    assert paired._is_eligible_file(memo) is False


# ── Backlog guard ────────────────────────────────────────────────────────────


def test_backlog_of_old_memos_is_never_transcribed(tmp_path):
    """A pre-populated archive fires zero transcriptions on the first poll.

    The headline requirement: the real directory holds hundreds of files.
    """
    for i in range(25):
        _write(tmp_path, f"2023010{i % 9} 12000{i % 9}-OLD{i:04d}.m4a", age_secs=OLD)

    watcher = _make_watcher(
        tmp_path, capture_method="voice_memo", paired=False,
        extensions={".m4a", ".wav"}, max_age_secs=3600, seed_existing=True,
    )
    _settled(watcher)

    assert _poll(watcher) == []


def test_startup_seeding_marks_existing_files_handled(tmp_path):
    """seed_existing records the whole archive as already-transcribed."""
    for i in range(5):
        _write(tmp_path, f"20230101 12000{i}-OLD{i:04d}.m4a", age_secs=OLD)

    watcher = _make_watcher(
        tmp_path, paired=False, extensions={".m4a"}, seed_existing=True,
    )

    assert len(watcher._transcribed) == 5


def test_seeding_holds_even_when_an_archived_file_is_touched(tmp_path):
    """Seeding is the second half of the guard, independent of mtime.

    An iCloud sync or a Finder copy can refresh an archived memo's mtime,
    which would carry it into the age window. Seeding still holds it back.
    """
    old = _write(tmp_path, "20230101 120000-OLD0001.m4a", age_secs=OLD)
    watcher = _make_watcher(
        tmp_path, paired=False, extensions={".m4a"},
        max_age_secs=3600, seed_existing=True,
    )
    now = time.time()
    os.utime(old, (now, now))  # archive file suddenly looks fresh
    _settled(watcher)

    assert _poll(watcher) == []


def test_age_window_holds_back_a_file_that_appears_later_bearing_an_old_mtime(tmp_path):
    """The age window is the first half, independent of seeding.

    A memo synced down after startup is not covered by seeding, but arrives
    with its original (old) mtime — the window catches it.
    """
    watcher = _make_watcher(
        tmp_path, paired=False, extensions={".m4a"},
        max_age_secs=3600, seed_existing=True,
    )
    _write(tmp_path, "20230101 120000-SYNCED001.m4a", age_secs=OLD)  # after startup
    _settled(watcher)

    assert _poll(watcher) == []


def test_a_new_memo_after_startup_is_still_transcribed(tmp_path):
    """The guard must not be so strict it blocks the feature itself."""
    for i in range(5):
        _write(tmp_path, f"20230101 12000{i}-OLD{i:04d}.m4a", age_secs=OLD)

    watcher = _make_watcher(
        tmp_path, paired=False, extensions={".m4a"},
        max_age_secs=3600, seed_existing=True,
    )
    fresh = _write(tmp_path, "20260908 101500-NEW0001.m4a", age_secs=FRESH)
    _settled(watcher)

    assert [c[0] for c in _poll(watcher)] == [fresh]


def test_multiple_new_memos_after_startup_are_all_transcribed(tmp_path):
    """One poll processes every settled arrival, not only the newest memo."""
    watcher = _make_watcher(
        tmp_path, paired=False, extensions={".m4a", ".wav"},
        max_age_secs=3600, seed_existing=True,
    )
    first = _write(tmp_path, "20260908 101500-NEW0001.m4a", age_secs=FRESH)
    second = _write(tmp_path, "20260908 101501-NEW0002.wav", age_secs=FRESH)
    _settled(watcher)

    assert [call[0] for call in _poll(watcher)] == [first, second]


def test_max_age_zero_disables_the_window_only(tmp_path):
    """max_age_secs=0 means no age limit — the meeting watchers' behavior."""
    old = _write(tmp_path, "20230101 120000-OLD0001.m4a", age_secs=OLD)
    watcher = _make_watcher(tmp_path, paired=False, extensions={".m4a"}, max_age_secs=0)
    _settled(watcher)

    assert [c[0] for c in _poll(watcher)] == [old]


# ── Sidecars and extensions ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "name",
    [
        "20260907 193532-AAAA1111.waveform",
        "20260907 193532-AAAA1111.composition",
        "CloudRecordings.db",
        "CloudRecordings.db-wal",
        "CloudRecordings.db-shm",
        "CloudRecordings_ckAssets",
    ],
)
def test_sidecar_files_are_never_treated_as_recordings(tmp_path, name):
    memo = _write(tmp_path, "20260907 193532-AAAA1111.m4a", age_secs=FRESH)
    _write(tmp_path, name, age_secs=FRESH)

    watcher = _make_watcher(
        tmp_path, paired=False, extensions={".m4a", ".wav"}, max_age_secs=3600,
    )
    _settled(watcher)

    assert [c[0] for c in _poll(watcher)] == [memo]


def test_sidecar_directories_are_skipped(tmp_path):
    """CaptureRecovery / Capture are directories, not files."""
    (tmp_path / "CaptureRecovery").mkdir()
    (tmp_path / "Capture").mkdir()
    memo = _write(tmp_path, "20260907 193532-AAAA1111.m4a", age_secs=FRESH)

    watcher = _make_watcher(
        tmp_path, paired=False, extensions={".m4a"}, max_age_secs=3600,
    )
    _settled(watcher)

    assert [c[0] for c in _poll(watcher)] == [memo]


def test_qta_excluded_by_default_included_when_opted_in(tmp_path):
    qta = _write(tmp_path, "20230101 120000-QTA00001.qta", age_secs=FRESH)

    off = _make_watcher(tmp_path, paired=False, extensions={".m4a", ".wav"})
    assert off._is_eligible_file(qta) is False

    on = _make_watcher(tmp_path, paired=False, extensions={".m4a", ".wav", ".qta"})
    assert on._is_eligible_file(qta) is True


# ── Memos never take the two-file merge path ─────────────────────────────────


def test_memo_is_never_paired_with_a_mic_file(tmp_path):
    """mic_path is always None for an unpaired watcher.

    Even with a file literally named 'mic' sitting in the directory — the
    merge path must not be reachable for a single-speaker memo.
    """
    memo = _write(tmp_path, "20260907 193532-AAAA1111.m4a", age_secs=FRESH)
    _write(tmp_path, "20260907 193532 mic.m4a", age_secs=FRESH)

    watcher = _make_watcher(
        tmp_path, paired=False, extensions={".m4a"}, max_age_secs=3600,
    )
    _settled(watcher)
    calls = _poll(watcher)

    assert all(mic is None for _, mic in calls)
    assert memo in [c[0] for c in calls]


def test_unpaired_watcher_never_calls_find_mic_pair(tmp_path):
    _write(tmp_path, "20260907 193532-AAAA1111.m4a", age_secs=FRESH)
    watcher = _make_watcher(
        tmp_path, paired=False, extensions={".m4a"}, max_age_secs=3600,
    )
    _settled(watcher)

    with patch.object(watcher, "_find_mic_pair") as find_pair:
        _poll(watcher)

    find_pair.assert_not_called()


# ── Regression: the meeting-recording watchers must be unchanged ─────────────


def test_meeting_watcher_defaults_are_unchanged(tmp_path):
    """Constructed the old way, the watcher behaves exactly as before."""
    watcher = _make_watcher(tmp_path)

    assert watcher._paired is True
    assert watcher._extensions == {e.lower() for e in watcher.RECORDING_EXTENSIONS}
    assert watcher._max_age_secs == 0
    assert watcher._capture_method == "audio_hijack_system"
    assert watcher._transcribed == set()  # no seeding by default


def test_meeting_watcher_still_merges_the_mic_pair(tmp_path):
    remote = _write(tmp_path, "20260907 1935 remote.mp4", age_secs=FRESH)
    mic = _write(tmp_path, "20260907 1935 mic.mp4", age_secs=FRESH)

    watcher = _make_watcher(tmp_path)
    _settled(watcher)

    assert _poll(watcher) == [(remote, mic)]


def test_meeting_watcher_still_ignores_the_mic_track_alone(tmp_path):
    _write(tmp_path, "20260907 1935 mic.mp4", age_secs=FRESH)
    watcher = _make_watcher(tmp_path)
    _settled(watcher)

    assert _poll(watcher) == []


def test_meeting_watcher_still_handles_the_system_track_name(tmp_path):
    system = _write(tmp_path, "20260907 1935 system.m4a", age_secs=FRESH)
    watcher = _make_watcher(tmp_path)
    _settled(watcher)

    assert [c[0] for c in _poll(watcher)] == [system]


def test_meeting_watcher_transcribes_an_old_recording(tmp_path):
    """No age window is applied to the meeting watchers — an old file still runs.

    A recording left settling across a daemon restart must not be dropped.
    """
    old = _write(tmp_path, "20230101 1200 remote.mp4", age_secs=OLD)
    watcher = _make_watcher(tmp_path)
    _settled(watcher)

    assert [c[0] for c in _poll(watcher)] == [old]


def test_meeting_watcher_falls_back_to_remote_only_without_a_mic_file(tmp_path):
    remote = _write(tmp_path, "20260907 1935 remote.mp4", age_secs=FRESH)
    watcher = _make_watcher(tmp_path)
    _settled(watcher)

    assert _poll(watcher) == [(remote, None)]


# ── Misc watcher invariants ──────────────────────────────────────────────────


def test_missing_directory_is_tolerated(tmp_path):
    """An absent Voice Memos dir warns and retries rather than crashing."""
    missing = tmp_path / "does-not-exist"
    watcher = _make_watcher(missing, paired=False, extensions={".m4a"}, seed_existing=True)

    assert _poll(watcher) == []


def test_empty_directory_is_a_quiet_success(tmp_path):
    """An existing empty watch directory produces no work and no error."""
    watcher = _make_watcher(
        tmp_path, paired=False, extensions={".m4a", ".wav"},
        max_age_secs=3600, seed_existing=True,
    )

    assert _poll(watcher) == []


def test_a_memo_is_transcribed_only_once(tmp_path):
    memo = _write(tmp_path, "20260908 101500-NEW0001.m4a", age_secs=FRESH)
    watcher = _make_watcher(
        tmp_path, paired=False, extensions={".m4a"}, max_age_secs=3600,
    )
    _settled(watcher)

    first = _poll(watcher)
    second = _poll(watcher)

    assert [c[0] for c in first] == [memo]
    assert second == []


def test_failed_memo_is_retried_and_cleared_only_after_success(tmp_path):
    """A backend failure stays eligible instead of becoming handled."""
    memo_dir = tmp_path / "memos"
    memo_dir.mkdir()
    state_path = tmp_path / "retry.json"
    watcher = _make_watcher(
        memo_dir,
        capture_method="voice_memo",
        paired=False,
        extensions={".m4a"},
        max_age_secs=3600,
        seed_existing=True,
        retry_state_path=state_path,
        retry_secs=0,
    )
    memo = _write(memo_dir, "20260908 101500-NEW0001.m4a", age_secs=FRESH)
    _settled(watcher)
    outcomes = iter((False, True))
    calls = []

    async def _record(remote_path, mic_path):
        calls.append((remote_path, mic_path))
        return next(outcomes)

    with patch.object(watcher, "_handle_recording", side_effect=_record):
        asyncio.run(watcher.poll_once())
        assert memo not in watcher._transcribed
        assert memo in watcher._pending_retries
        assert json.loads(state_path.read_text())["pending"][0]["path"] == str(memo)

        asyncio.run(watcher.poll_once())

    assert calls == [(memo, None), (memo, None)]
    assert memo in watcher._transcribed
    assert watcher._pending_retries == {}
    assert json.loads(state_path.read_text())["pending"] == []


def test_startup_seeding_preserves_a_failed_new_memo_retry(tmp_path):
    """Restarting cannot absorb a previously failed arrival into the backlog."""
    memo_dir = tmp_path / "memos"
    memo_dir.mkdir()
    state_path = tmp_path / "retry.json"
    first = _make_watcher(
        memo_dir,
        capture_method="voice_memo",
        paired=False,
        extensions={".m4a"},
        max_age_secs=3600,
        seed_existing=True,
        retry_state_path=state_path,
        retry_secs=0,
    )
    memo = _write(memo_dir, "20260908 101500-NEW0001.m4a", age_secs=FRESH)
    _settled(first)

    async def _fail(_remote_path, _mic_path):
        return False

    with patch.object(first, "_handle_recording", side_effect=_fail):
        asyncio.run(first.poll_once())

    restarted = _make_watcher(
        memo_dir,
        capture_method="voice_memo",
        paired=False,
        extensions={".m4a"},
        max_age_secs=3600,
        seed_existing=True,
        retry_state_path=state_path,
        retry_secs=0,
    )
    _settled(restarted)

    assert memo not in restarted._transcribed
    assert memo in restarted._pending_retries
    assert [call[0] for call in _poll(restarted)] == [memo]
    assert json.loads(state_path.read_text())["pending"] == []


def test_failed_memo_obeys_retry_backoff(tmp_path):
    """A failing backend is retried later without being hammered every poll."""
    memo_dir = tmp_path / "memos"
    memo_dir.mkdir()
    state_path = tmp_path / "retry.json"
    watcher = _make_watcher(
        memo_dir,
        capture_method="voice_memo",
        paired=False,
        extensions={".m4a"},
        max_age_secs=3600,
        seed_existing=True,
        retry_state_path=state_path,
        retry_secs=300,
    )
    memo = _write(memo_dir, "20260908 101500-NEW0001.m4a", age_secs=FRESH)
    _settled(watcher)
    calls = []

    async def _fail(remote_path, mic_path):
        calls.append((remote_path, mic_path))
        return False

    with patch.object(watcher, "_handle_recording", side_effect=_fail):
        asyncio.run(watcher.poll_once())
        asyncio.run(watcher.poll_once())

    assert calls == [(memo, None)]
    assert watcher._pending_retries[memo] > time.time()


def test_corrupt_retry_journal_disables_memo_processing_fail_closed(tmp_path):
    """Unreadable retry state must not let startup seeding hide failed work."""
    memo_dir = tmp_path / "memos"
    memo_dir.mkdir()
    memo = _write(memo_dir, "20260908 101500-NEW0001.m4a", age_secs=FRESH)
    state_path = tmp_path / "retry.json"
    state_path.write_text("not valid JSON")

    watcher = _make_watcher(
        memo_dir,
        capture_method="voice_memo",
        paired=False,
        extensions={".m4a"},
        max_age_secs=3600,
        seed_existing=True,
        retry_state_path=state_path,
    )
    _settled(watcher)

    assert watcher._retry_state_available is False
    assert memo not in watcher._transcribed
    assert _poll(watcher) == []


def test_voice_memo_uses_local_backend_even_when_elevenlabs_key_exists(tmp_path, monkeypatch):
    """Single-speaker memos honor the operator's local-STT selection."""
    memo = _write(tmp_path, "20260908 101500-NEW0001.m4a", age_secs=FRESH)
    watcher = _make_watcher(tmp_path, capture_method="voice_memo", paired=False)
    monkeypatch.setenv("ELEVENLABS_API_KEY", "configured-but-not-used")
    completed = MagicMock(
        returncode=0,
        stdout="NEOTOMA_TRANSCRIPTION_ENTITY_ID=ent_test_local\n",
        stderr="",
    )

    with patch.object(tyto.subprocess, "run", return_value=completed) as run:
        assert watcher._run_transcription(memo, None) == "ent_test_local"

    command = run.call_args.args[0]
    assert command[-2:] == ["--backend", "local"]
    assert "--diarize" not in command


def test_single_file_meeting_defers_routing_to_transcribe_script(tmp_path, monkeypatch):
    """Tyto must not reintroduce key-presence routing beside the shared selector."""
    recording = _write(tmp_path, "20260908 101500 system.m4a", age_secs=FRESH)
    watcher = _make_watcher(tmp_path, capture_method="audio_hijack_system")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "configured")
    monkeypatch.delenv("RECORD_MEETING_DIARIZE", raising=False)
    completed = MagicMock(
        returncode=0,
        stdout="NEOTOMA_TRANSCRIPTION_ENTITY_ID=ent_test_auto\n",
        stderr="",
    )

    with patch.object(tyto.subprocess, "run", return_value=completed) as run:
        assert watcher._run_transcription(recording, None) == "ent_test_auto"

    command = run.call_args.args[0]
    assert "--diarize" not in command
    assert "--no-diarize" not in command
    assert "--backend" not in command


def test_zero_byte_memo_is_not_transcribed(tmp_path):
    """A memo still being written has size 0 and must wait."""
    _write(tmp_path, "20260908 101500-NEW0001.m4a", age_secs=FRESH, size=0)
    watcher = _make_watcher(
        tmp_path, paired=False, extensions={".m4a"}, max_age_secs=3600,
    )
    _settled(watcher)

    assert _poll(watcher) == []


def test_memo_must_stop_growing_before_transcription(tmp_path):
    """Stable mtime alone cannot make a still-growing memo look settled."""
    memo = _write(tmp_path, "20260908 101500-NEW0001.m4a", age_secs=FRESH)
    watcher = _make_watcher(
        tmp_path, paired=False, extensions={".m4a"}, max_age_secs=3600,
    )

    # First sighting records the initial (mtime, size) signature.
    assert _poll(watcher) == []
    original = memo.stat()

    # Simulate a writer that appends content while preserving mtime.  A
    # timestamp-only settling check would transcribe on this poll.
    with memo.open("ab") as handle:
        handle.write(b"more audio")
    os.utime(memo, ns=(original.st_atime_ns, original.st_mtime_ns))
    assert _poll(watcher) == []

    # Once the complete signature is unchanged, the memo is eligible.
    assert [call[0] for call in _poll(watcher)] == [memo]
