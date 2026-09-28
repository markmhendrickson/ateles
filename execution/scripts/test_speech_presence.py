"""Unit tests for speech_presence.py."""

from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPTS_DIR))

from speech_presence import (  # noqa: E402
    SKIPPED_BELOW_THRESHOLD,
    SKIPPED_NO_SPEECH_PROB,
    SKIPPED_VAD_NON_SPEECH,
    apply_no_speech_prob,
    classify_pre_transcription,
)


def test_classify_rms_wins_before_vad():
    assert (
        classify_pre_transcription(
            -56.0, 0.5, silence_threshold_db=-50.0, min_speech_fraction=0.075
        )
        == SKIPPED_BELOW_THRESHOLD
    )


def test_classify_vad_when_rms_clears():
    assert (
        classify_pre_transcription(
            -39.0, 0.05, silence_threshold_db=-50.0, min_speech_fraction=0.075
        )
        == SKIPPED_VAD_NON_SPEECH
    )


def test_classify_fail_open_when_meters_missing():
    assert (
        classify_pre_transcription(
            None, None, silence_threshold_db=-50.0, min_speech_fraction=0.075
        )
        == "transcribe"
    )


def test_apply_no_speech_prob_requires_segments():
    record = {"ok": True, "text": "Heat to 10C"}
    assert apply_no_speech_prob(record, None) is False
    assert record["text"] == "Heat to 10C"


def test_apply_no_speech_prob_suppresses_when_every_segment_is_no_speech():
    record = {"ok": True, "text": "fluent fabrication"}
    assert apply_no_speech_prob(
        record,
        [{"no_speech_prob": 0.91}, {"no_speech_prob": 0.88}],
        max_no_speech_prob=0.6,
    )
    assert record["text"] == ""
    assert record["skipped"] == SKIPPED_NO_SPEECH_PROB
    assert record["skipped"] != SKIPPED_BELOW_THRESHOLD


def test_apply_no_speech_prob_keeps_chunk_if_any_segment_is_speech():
    record = {"ok": True, "text": "real speech"}
    assert (
        apply_no_speech_prob(
            record,
            [{"no_speech_prob": 0.91}, {"no_speech_prob": 0.2}],
            max_no_speech_prob=0.6,
        )
        is False
    )
    assert record["text"] == "real speech"
