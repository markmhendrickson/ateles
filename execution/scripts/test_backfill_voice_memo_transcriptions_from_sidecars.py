"""Tests for the sidecar-only voice-memo backfill (ateles#1083).

Every test asserts an observable effect: which files were treated as
candidates, whether whisper was invoked (it must never be — the sidecar text
IS the transcription result), and whether the dry-run count matches what a
real run would store. No network or subprocess calls happen here;
``transcribe_audio.save_transcription`` is patched at the module boundary
this script actually calls through.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

os.environ.setdefault("DATA_DIR", str(_SCRIPTS_DIR / "_test_data_dir"))

import backfill_voice_memo_transcriptions_from_sidecars as bf  # noqa: E402
import transcribe_audio as ta  # noqa: E402


def _write_memo_with_sidecar(dir_path: Path, stem: str, text: str, ext: str = ".m4a") -> Path:
    audio = dir_path / f"{stem}{ext}"
    audio.write_bytes(b"\0" * 128)
    sidecar = audio.with_suffix(audio.suffix + ".transcript.txt")
    sidecar.write_text(text, encoding="utf-8")
    return audio


def test_finds_only_memos_that_have_a_sidecar(tmp_path):
    with_sidecar = _write_memo_with_sidecar(tmp_path, "memo1", "hello there")
    (tmp_path / "memo2.m4a").write_bytes(b"\0" * 128)  # no sidecar — never transcribed

    candidates = bf.find_backfill_candidates([tmp_path])

    assert candidates == [with_sidecar]


def test_non_memo_extension_is_ignored(tmp_path):
    _write_memo_with_sidecar(tmp_path, "memo1", "hello", ext=".mp4")

    candidates = bf.find_backfill_candidates([tmp_path])

    assert candidates == []


def test_dry_run_never_calls_save_transcription(tmp_path, monkeypatch):
    audio = _write_memo_with_sidecar(tmp_path, "memo1", "hello there")
    monkeypatch.setattr(ta, "is_already_transcribed", lambda _p: False)

    with patch.object(ta, "save_transcription") as save:
        status = bf.backfill_one(audio, dry_run=True)

    assert status == "would_store"
    save.assert_not_called()


def test_backfill_never_invokes_whisper(tmp_path, monkeypatch):
    """The sidecar text is the transcription result; STT must not re-run."""
    audio = _write_memo_with_sidecar(tmp_path, "memo1", "hello there")
    monkeypatch.setattr(ta, "is_already_transcribed", lambda _p: False)

    with patch.object(ta, "save_transcription") as save, patch.object(
        ta, "transcribe_audio_file"
    ) as whisper:
        status = bf.backfill_one(audio, dry_run=False)

    assert status == "stored"
    whisper.assert_not_called()
    save.assert_called_once()
    _, kwargs = save.call_args
    stored_result = save.call_args.args[1]
    assert stored_result["transcription_text"] == "hello there"
    assert kwargs.get("observation_source") == "import"
    assert kwargs.get("attach_audio_file") is True


def test_already_existing_transcription_is_skipped_not_restored(tmp_path, monkeypatch):
    """A memo already backfilled (or never lost) is not stored a second time."""
    audio = _write_memo_with_sidecar(tmp_path, "memo1", "hello there")
    monkeypatch.setattr(ta, "is_already_transcribed", lambda _p: True)

    with patch.object(ta, "save_transcription") as save:
        status = bf.backfill_one(audio, dry_run=False)

    assert status == "skipped_existing"
    save.assert_not_called()


def test_save_transcription_failure_is_reported_not_raised(tmp_path, monkeypatch):
    """One bad memo in a backlog must not abort the whole backfill run."""
    audio = _write_memo_with_sidecar(tmp_path, "memo1", "hello there")
    monkeypatch.setattr(ta, "is_already_transcribed", lambda _p: False)

    with patch.object(ta, "save_transcription", side_effect=RuntimeError("boom")):
        status = bf.backfill_one(audio, dry_run=False)

    assert status == "failed"


def test_dry_run_count_matches_a_real_runs_store_count(tmp_path, monkeypatch):
    """The instrument check: --dry-run must predict the real outcome exactly."""
    for i in range(3):
        _write_memo_with_sidecar(tmp_path, f"memo{i}", f"transcript number {i}")
    monkeypatch.setattr(ta, "is_already_transcribed", lambda _p: False)

    candidates = bf.find_backfill_candidates([tmp_path])
    with patch.object(ta, "save_transcription"):
        dry_statuses = [bf.backfill_one(p, dry_run=True) for p in candidates]
        real_statuses = [bf.backfill_one(p, dry_run=False) for p in candidates]

    assert dry_statuses == ["would_store"] * 3
    assert real_statuses == ["stored"] * 3
    assert len(dry_statuses) == len(real_statuses) == len(candidates)


def test_empty_sidecar_is_a_failure_not_a_silent_store(tmp_path, monkeypatch):
    audio = _write_memo_with_sidecar(tmp_path, "memo1", "   ")
    monkeypatch.setattr(ta, "is_already_transcribed", lambda _p: False)

    with patch.object(ta, "save_transcription") as save:
        status = bf.backfill_one(audio, dry_run=False)

    assert status == "failed"
    save.assert_not_called()


def test_main_reports_counts_and_exits_zero(tmp_path, monkeypatch, capsys):
    for i in range(2):
        _write_memo_with_sidecar(tmp_path, f"memo{i}", f"text {i}")
    monkeypatch.setattr(ta, "is_already_transcribed", lambda _p: False)

    with patch.object(ta, "save_transcription"):
        exit_code = bf.main([str(tmp_path)])

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "stored=2" in out


def test_main_rejects_a_missing_directory(tmp_path, capsys):
    missing = tmp_path / "does-not-exist"

    exit_code = bf.main([str(missing)])

    assert exit_code == 1


def test_main_with_no_dirs_and_no_env_is_a_usage_error(monkeypatch, capsys):
    monkeypatch.delenv("TYTO_VOICE_MEMOS_DIR", raising=False)
    monkeypatch.setattr(bf, "_default_scan_dir", lambda: None)

    exit_code = bf.main([])

    assert exit_code == 1
