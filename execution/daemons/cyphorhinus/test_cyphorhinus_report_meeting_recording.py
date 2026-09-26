"""Effect tests for cyphorhinus report_meeting_recording sidecar Path (ateles#1029).

Phase 14 writes `<stem>_meeting_processed.md`. Watchers must treat that as
ready, still accept legacy `_meeting_analysis.md` during cutover, and not
notify when the transcript is absent.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

import watch  # noqa: E402


def _wav(tmp_path: Path, stem: str = "20260101 1200 meeting") -> Path:
    wav = tmp_path / f"{stem}.wav"
    wav.write_bytes(b"RIFF")
    return wav


def test_processed_sidecar_notifies_complete(tmp_path, monkeypatch):
    wav = _wav(tmp_path)
    (tmp_path / f"{wav.stem}.txt").write_text("transcript")
    (tmp_path / f"{wav.stem}_meeting_processed.md").write_text("report")
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(watch, "notify", lambda title, message: calls.append((title, message)))

    watch.report_meeting_recording(wav)

    assert len(calls) == 1
    assert "analysis complete" in calls[0][1]


def test_legacy_analysis_sidecar_still_notifies_complete(tmp_path, monkeypatch):
    wav = _wav(tmp_path)
    (tmp_path / f"{wav.stem}.txt").write_text("transcript")
    (tmp_path / f"{wav.stem}_meeting_analysis.md").write_text("legacy report")
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(watch, "notify", lambda title, message: calls.append((title, message)))

    watch.report_meeting_recording(wav)

    assert len(calls) == 1
    assert "analysis complete" in calls[0][1]


def test_transcript_only_notifies_pending(tmp_path, monkeypatch):
    wav = _wav(tmp_path)
    (tmp_path / f"{wav.stem}.txt").write_text("transcript")
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(watch, "notify", lambda title, message: calls.append((title, message)))

    watch.report_meeting_recording(wav)

    assert len(calls) == 1
    assert "analysis pending" in calls[0][1]


def test_no_transcript_skips_notify(tmp_path, monkeypatch):
    wav = _wav(tmp_path)
    (tmp_path / f"{wav.stem}_meeting_processed.md").write_text("report")
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(watch, "notify", lambda title, message: calls.append((title, message)))

    watch.report_meeting_recording(wav)

    assert calls == []
