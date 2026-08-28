"""Effect-level tests for live_transcript_tail.py.

Covers remote-track filtering (mic exclusion), stall/remnant slice decisions,
silence-vs-failure classification (kill-switch contract), and growing-recording
discovery. Mirrors the config-stub + patch.object convention from
test_transcribe_audio.py — execution/scripts/config.py is untracked/gitignored.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPTS_DIR))

if "config" not in sys.modules:
    _stub_config = types.ModuleType("config")
    _stub_config.get_data_dir = lambda: Path(_SCRIPTS_DIR / "_test_data_dir")
    sys.modules["config"] = _stub_config

import live_transcript_tail as lt  # noqa: E402


# --------------------------------------------------------------------------
# Case 1 — is_remote_track()
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "filename,expected",
    [
        ("foo_system.mp4", True),
        ("foo_remote.wav", True),
        ("foo_mic.mp4", False),
        # mic vetoes even when "system" also matches — regression pin
        ("system_mic.mp4", False),
        ("foo_system.txt", False),  # unsupported extension
    ],
)
def test_is_remote_track_filename_filter(filename, expected):
    assert lt.is_remote_track(Path(filename)) is expected


# --------------------------------------------------------------------------
# Case 2 — stall-vs-remnant branch (slice_decision)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "available,stalled,expected",
    [
        (2.0, False, "wait"),  # (a) sub-threshold, still growing
        (0.01, True, "exit_clean"),  # (b) stalled, essentially empty
        (2.0, True, "flush_final"),  # (c) stalled with usable remnant
        (10.0, False, "transcribe"),
        (10.0, True, "transcribe"),
        (0.05, True, "exit_clean"),  # boundary: <= stall_empty
        (0.06, True, "flush_final"),
    ],
)
def test_slice_decision_stall_vs_remnant(available, stalled, expected):
    assert lt.slice_decision(available, stalled) == expected


def test_main_wait_when_subthreshold_and_not_stalled(tmp_path, monkeypatch):
    """(a) available < MIN and not stalled → no record, no ffmpeg/transcribe."""
    recording = tmp_path / "meet_system.mp4"
    recording.write_bytes(b"x")
    out = tmp_path / "out.jsonl"
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    # First sleep: interval. Second sleep would be the next loop — break via
    # KeyboardInterrupt after the continue path by making the second probe
    # return None (recording ended).
    durations = iter([1.0, None])  # available = 1.0 - 0.0 = 1.0 < 5

    with (
        patch.object(lt, "probe_duration", side_effect=lambda _p: next(durations)),
        patch.object(lt.time, "sleep"),
        patch.object(lt.time, "time", return_value=1000.0),
        patch.object(Path, "stat", wraps=recording.stat) as _stat,
        patch.object(lt, "subprocess") as mock_sub,
        patch.object(lt, "transcribe_slice") as mock_tx,
        patch.object(lt, "log") as mock_log,
    ):
        # Fresh mtime so stalled=False (age << interval*2)
        real_stat = recording.stat()

        def _stat_side_effect(*_a, **_k):
            st = MagicMock()
            st.st_mtime = 999.0  # age = 1s << interval*2
            st.st_size = real_stat.st_size
            return st

        with patch.object(Path, "stat", side_effect=_stat_side_effect):
            # --start-at 0 so cursor is known; first loop available=1.0
            rc = lt.main(
                ["--file", str(recording), "--out", str(out), "--interval", "1", "--start-at", "0"]
            )

    assert rc == 0
    assert not out.exists() or out.read_text() == ""
    mock_sub.run.assert_not_called()
    mock_tx.assert_not_called()
    # Second probe None → "could not probe duration"
    assert any("could not probe" in str(c) for c in mock_log.call_args_list)


def test_main_exit_clean_when_stalled_empty(tmp_path, monkeypatch, capsys):
    """(b) sub-threshold + stalled + available≈0 → clean exit, no JSONL line."""
    recording = tmp_path / "meet_system.mp4"
    recording.write_bytes(b"x")
    out = tmp_path / "out.jsonl"
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    with (
        patch.object(lt, "probe_duration", return_value=0.01),
        patch.object(lt.time, "sleep"),
        patch.object(lt.time, "time", return_value=10_000.0),
        patch.object(lt, "subprocess") as mock_sub,
        patch.object(lt, "transcribe_slice") as mock_tx,
        patch.object(lt, "log") as mock_log,
    ):
        def _stat_side_effect(*_a, **_k):
            st = MagicMock()
            st.st_mtime = 0.0  # very old → stalled
            st.st_size = 1
            return st

        with patch.object(Path, "stat", side_effect=_stat_side_effect):
            rc = lt.main(
                ["--file", str(recording), "--out", str(out), "--interval", "1", "--start-at", "0"]
            )

    assert rc == 0
    assert not out.exists() or out.read_text().strip() == ""
    mock_sub.run.assert_not_called()
    mock_tx.assert_not_called()
    assert any(
        "recording appears to have stopped — exiting" in str(c)
        for c in mock_log.call_args_list
    )


def test_main_flush_final_remnant_writes_one_jsonl_line(tmp_path, monkeypatch):
    """(c) stalled with remnant in (0.05, MIN) → one final chunk then exit."""
    recording = tmp_path / "meet_system.mp4"
    recording.write_bytes(b"x")
    out = tmp_path / "out.jsonl"
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    ffmpeg_ok = MagicMock(returncode=0, stderr="", stdout="")

    with (
        patch.object(lt, "probe_duration", return_value=2.0),
        patch.object(lt.time, "sleep"),
        patch.object(lt.time, "time", return_value=10_000.0),
        patch.object(lt.subprocess, "run", return_value=ffmpeg_ok) as mock_run,
        patch.object(lt, "transcribe_slice", return_value=(True, "final words")),
        patch.object(lt, "log") as mock_log,
    ):
        def _stat_side_effect(*_a, **_k):
            st = MagicMock()
            st.st_mtime = 0.0
            st.st_size = 1
            return st

        with patch.object(Path, "stat", side_effect=_stat_side_effect):
            rc = lt.main(
                ["--file", str(recording), "--out", str(out), "--interval", "1", "--start-at", "0"]
            )

    assert rc == 0
    lines = [ln for ln in out.read_text().splitlines() if ln.strip()]
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["ok"] is True
    assert record["text"] == "final words"
    mock_run.assert_called_once()
    assert any("flushing final" in str(c) for c in mock_log.call_args_list)
    assert any("final slice written" in str(c) for c in mock_log.call_args_list)


# --------------------------------------------------------------------------
# Case 3 — silence vs failure classification
# --------------------------------------------------------------------------


def test_apply_transcription_success_resets_failures():
    record = {"ok": True}
    # Prior streak of 2, then success → 0
    assert lt.apply_transcription_result(record, True, "some text", 2) == 0
    assert record["text"] == "some text"
    assert record["ok"] is True
    assert "silence" not in record
    assert "error" not in record


def test_apply_transcription_silence_leaves_failures_unchanged():
    record = {"ok": False}
    # Primed to 3; silence must stay at 3 (neither +1 nor reset to 0)
    assert (
        lt.apply_transcription_result(record, False, lt.SILENCE_SENTINEL, 3) == 3
    )
    assert record["ok"] is True
    assert record["text"] == ""
    assert record["silence"] is True
    assert "error" not in record


def test_apply_transcription_real_error_increments_failures():
    record = {"ok": False}
    assert (
        lt.apply_transcription_result(record, False, "some real error string", 2)
        == 3
    )
    assert record["ok"] is False
    assert record["error"] == "some real error string"
    assert "silence" not in record


def test_silence_does_not_count_toward_kill_switch_streak():
    """Silence neither increments nor resets; real failures still accumulate across it.

    After 4 failures streak is 4; silence leaves it at 4; one more real failure
    reaches 5. (Silence does not *add* a count — that is the SKILL.md contract —
    but also does not break the streak, matching apply_transcription_result.)
    """
    streak = 0
    for ok, payload in [
        (False, "e1"),
        (False, "e2"),
        (False, "e3"),
        (False, "e4"),
    ]:
        streak = lt.apply_transcription_result({"ok": ok}, ok, payload, streak)
    assert streak == 4
    streak = lt.apply_transcription_result(
        {"ok": False}, False, lt.SILENCE_SENTINEL, streak
    )
    assert streak == 4  # unchanged — not 0, not 5
    streak = lt.apply_transcription_result({"ok": False}, False, "e5", streak)
    assert streak == 5


def test_five_real_failures_hit_kill_threshold():
    streak = 0
    for _ in range(5):
        record = {"ok": False}
        streak = lt.apply_transcription_result(record, False, "err", streak)
    assert streak == 5


def test_main_kill_switch_fires_on_five_consecutive_failures(tmp_path, monkeypatch):
    recording = tmp_path / "meet_system.mp4"
    recording.write_bytes(b"x")
    out = tmp_path / "out.jsonl"
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    # Each loop: available = 10 → normal transcribe path; always fail.
    # After 5 failures, main breaks.
    ffmpeg_ok = MagicMock(returncode=0, stderr="", stdout="")
    cursor_durations = [10.0 + i * 10.0 for i in range(6)]  # growing cursor room

    with (
        patch.object(lt, "probe_duration", side_effect=cursor_durations),
        patch.object(lt.time, "sleep"),
        patch.object(lt.subprocess, "run", return_value=ffmpeg_ok),
        patch.object(lt, "transcribe_slice", return_value=(False, "boom")),
        patch.object(lt, "log") as mock_log,
    ):
        rc = lt.main(
            ["--file", str(recording), "--out", str(out), "--interval", "1", "--start-at", "0"]
        )

    assert rc == 0
    lines = [json.loads(ln) for ln in out.read_text().splitlines() if ln.strip()]
    assert len(lines) == 5
    assert all(r["ok"] is False for r in lines)
    assert any(
        "5 consecutive failures — stopping" in str(c) for c in mock_log.call_args_list
    )


def test_main_silence_leaves_streak_unchanged_mid_run(tmp_path, monkeypatch):
    """4 failures + silence: kill has not fired yet; silence line is ok+silence.

    A following 5th real failure *would* trip the switch (silence does not reset).
    Here we stop after the silence chunk to pin the mid-run contract.
    """
    recording = tmp_path / "meet_system.mp4"
    recording.write_bytes(b"x")
    out = tmp_path / "out.jsonl"
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    ffmpeg_ok = MagicMock(returncode=0, stderr="", stdout="")
    results = [
        (False, "e1"),
        (False, "e2"),
        (False, "e3"),
        (False, "e4"),
        (False, lt.SILENCE_SENTINEL),
    ]
    # 5 chunks then probe None to exit without a 5th real failure
    durations = [10.0 * (i + 1) for i in range(5)] + [None]

    with (
        patch.object(lt, "probe_duration", side_effect=durations),
        patch.object(lt.time, "sleep"),
        patch.object(lt.subprocess, "run", return_value=ffmpeg_ok),
        patch.object(lt, "transcribe_slice", side_effect=results),
        patch.object(lt, "log") as mock_log,
    ):
        rc = lt.main(
            ["--file", str(recording), "--out", str(out), "--interval", "1", "--start-at", "0"]
        )

    assert rc == 0
    lines = [json.loads(ln) for ln in out.read_text().splitlines() if ln.strip()]
    assert len(lines) == 5
    silence_line = lines[4]
    assert silence_line["ok"] is True
    assert silence_line["silence"] is True
    assert silence_line["text"] == ""
    assert not any(
        "5 consecutive failures" in str(c) for c in mock_log.call_args_list
    )


def test_success_after_failures_clears_streak_before_kill(tmp_path, monkeypatch):
    """failure, failure, success, then three more failures — kill does not fire at 3 post-reset."""
    recording = tmp_path / "meet_system.mp4"
    recording.write_bytes(b"x")
    out = tmp_path / "out.jsonl"
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    ffmpeg_ok = MagicMock(returncode=0, stderr="", stdout="")
    results = [
        (False, "e1"),
        (False, "e2"),
        (True, "some text"),
        (False, "e3"),
        (False, "e4"),
        (False, "e5"),
    ]
    durations = [10.0 * (i + 1) for i in range(6)] + [None]

    with (
        patch.object(lt, "probe_duration", side_effect=durations),
        patch.object(lt.time, "sleep"),
        patch.object(lt.subprocess, "run", return_value=ffmpeg_ok),
        patch.object(lt, "transcribe_slice", side_effect=results),
        patch.object(lt, "log") as mock_log,
    ):
        rc = lt.main(
            ["--file", str(recording), "--out", str(out), "--interval", "1", "--start-at", "0"]
        )

    assert rc == 0
    lines = [json.loads(ln) for ln in out.read_text().splitlines() if ln.strip()]
    assert lines[2]["ok"] is True and lines[2]["text"] == "some text"
    # After success, only 3 failures — kill switch must NOT fire
    assert not any(
        "5 consecutive failures" in str(c) for c in mock_log.call_args_list
    )


# --------------------------------------------------------------------------
# Case 4 — find_growing_recording()
# --------------------------------------------------------------------------


def _stat_with_growing_size(target: Path, *, before: int = 10, after: int = 20):
    """Path.stat patch: real mtime; size grows between the 2nd and 3rd call on target.

    find_growing_recording calls target.stat() three times for a single candidate:
    sort key, size_before, size_after. Grow only on the third call.
    """
    original_stat = Path.stat
    counts: dict[str, int] = {}

    def sized_stat(self, *a, **k):
        st = original_stat(self)
        mock = MagicMock()
        mock.st_mtime = st.st_mtime
        key = str(self.resolve())
        if self.resolve() == target.resolve():
            counts[key] = counts.get(key, 0) + 1
            mock.st_size = before if counts[key] < 3 else after
        else:
            mock.st_size = st.st_size
        return mock

    return sized_stat


def test_find_growing_recording_returns_growing_file(tmp_path):
    f = tmp_path / "call_system.mp4"
    f.write_bytes(b"aa")

    with (
        patch.object(lt.time, "sleep"),
        patch.object(Path, "stat", _stat_with_growing_size(f)),
    ):
        got = lt.find_growing_recording(tmp_path, settle_probe=0.0)
    assert got == f


def test_find_growing_recording_not_growing_returns_none(tmp_path):
    f = tmp_path / "call_system.mp4"
    f.write_bytes(b"aa")
    # before == after → not growing
    with (
        patch.object(lt.time, "sleep"),
        patch.object(Path, "stat", _stat_with_growing_size(f, before=10, after=10)),
        patch.object(lt, "log") as mock_log,
    ):
        got = lt.find_growing_recording(tmp_path, settle_probe=0.0)
    assert got is None
    assert any("not growing" in str(c) for c in mock_log.call_args_list)


def test_find_growing_recording_empty_dir_returns_none(tmp_path):
    assert lt.find_growing_recording(tmp_path, settle_probe=0.0) is None


def test_find_growing_recording_missing_dir_returns_none(tmp_path):
    missing = tmp_path / "nope"
    with patch.object(lt, "log") as mock_log:
        got = lt.find_growing_recording(missing, settle_probe=0.0)
    assert got is None
    assert any("watch dir does not exist" in str(c) for c in mock_log.call_args_list)


def test_find_growing_recording_picks_newest_mtime(tmp_path):
    import os
    import time as _time

    older = tmp_path / "old_system.mp4"
    newer = tmp_path / "new_system.mp4"
    older.write_bytes(b"aa")
    newer.write_bytes(b"aa")

    older_t = _time.time() - 100
    newer_t = _time.time()
    os.utime(older, (older_t, older_t))
    os.utime(newer, (newer_t, newer_t))

    with (
        patch.object(lt.time, "sleep"),
        patch.object(Path, "stat", _stat_with_growing_size(newer, before=10, after=50)),
    ):
        got = lt.find_growing_recording(tmp_path, settle_probe=0.0)
    assert got == newer
