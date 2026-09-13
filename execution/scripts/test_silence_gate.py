"""Tests for the shared silence gate extracted from live_transcript_tail.py.

The point of the extraction is that the streaming and batch paths measure
identically. These tests pin the calibrated statistic so a future edit cannot
quietly swap the p95 for a mean — which is the specific change that would
reintroduce the fabrication-on-silence failure.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import silence_gate as sg  # noqa: E402


def test_threshold_default_is_the_calibrated_minus_50_db():
    assert sg.DEFAULT_SILENCE_THRESHOLD_DB == -50.0


def test_percentile_is_p95_not_the_median():
    assert sg.RMS_PERCENTILE == 0.95


def test_parse_ignores_infinite_values():
    stderr = "RMS_level=-inf\nRMS_level=-42.5\nRMS_level=-inf\n"
    assert sg.parse_rms_levels(stderr) == [-42.5]


def test_parse_returns_empty_on_no_matches():
    assert sg.parse_rms_levels("") == []
    assert sg.parse_rms_levels("nothing here") == []


def test_p95_ignores_a_pausing_speakers_quiet_windows():
    """The median FAILS on this input; the p95 is what makes it speech.

    A speaker who pauses between sentences leaves most windows near the noise
    floor. Measured on 39 labelled chunks: medians of -75 to -82 dB were
    indistinguishable from true silence.
    """
    values = [-80.0] * 19 + [-30.0]
    assert sg.sustained_rms_db(values) == -30.0
    # And the statistic that would have failed:
    assert sorted(values)[len(values) // 2] < -50.0


def test_p95_is_not_the_peak_so_a_click_does_not_count_as_speech():
    """Transient clicks push silent windows to -22 dB; the peak FAILS on this."""
    # With enough windows the single click is correctly outvoted by the p95.
    many = [-80.0] * 99 + [-22.0]
    assert sg.sustained_rms_db(many) == -80.0
    assert max(many) == -22.0  # the peak would have called this speech


def test_empty_measurement_returns_none_meaning_transcribe_anyway():
    assert sg.sustained_rms_db([]) is None


def test_measurement_failure_returns_none_rather_than_raising(monkeypatch):
    def boom(*_a, **_k):
        raise OSError("ffmpeg gone")

    monkeypatch.setattr(sg.subprocess, "run", boom)
    logged: list[str] = []
    assert (
        sg.measure_sustained_rms_db(Path("x.wav"), log=logged.append) is None
    )
    assert logged and "transcribing anyway" in logged[0]


def test_live_transcript_tail_reexports_the_same_objects():
    """Both paths must share one threshold, not two that can drift apart."""
    import live_transcript_tail as lt

    assert lt.DEFAULT_SILENCE_THRESHOLD_DB == sg.DEFAULT_SILENCE_THRESHOLD_DB
    assert lt.RMS_PERCENTILE == sg.RMS_PERCENTILE
    assert lt.sustained_rms_db is sg.sustained_rms_db
    assert lt.parse_rms_levels is sg.parse_rms_levels
