"""Chunked per-track whisper: chunking, repeat check, boundary repair, silence gate.

Whisper is faked; the audio is a synthetic tone cut by the real ffmpeg, so the
tests are skipped where ffmpeg is absent.
"""
import math
import shutil
import struct
import wave
from pathlib import Path

import chunked_whisper as cw
import pytest

FFMPEG = shutil.which("ffmpeg")
needs_ffmpeg = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg not installed")


def tone(path: Path, seconds: float, amplitude: float = 0.3) -> Path:
    rate = 16000
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(
            struct.pack("<h", int(amplitude * 32767 * math.sin(2 * math.pi * 220 * i / rate)))
            for i in range(int(seconds * rate))
        ))
    return path


def test_repeat_loop_detection_counts_adjacent_repeats_only():
    assert cw.has_repeat_loop("so " + "thank you " * 5 + "bye")
    assert cw.has_repeat_loop("no no no no no")
    assert not cw.has_repeat_loop("i think so, i think it works, i think we can, i think not")


@needs_ffmpeg
def test_long_track_is_cut_into_two_minute_chunks_each_run_separately(tmp_path):
    track = tone(tmp_path / "t.wav", 250)
    calls = []

    def asr(wav, language, extra):
        calls.append((wav.name, extra))
        return [{"start": 1.0, "end": 3.0, "text": f"words in {wav.stem}"}], "en"

    found = cw.transcribe_track(track, asr=asr, language="en", ffmpeg=FFMPEG)
    chunks = [c for c in found["chunks"] if c["kind"] == "chunk"]
    assert [c["start"] for c in chunks] == [0.0, 120.0, 240.0]
    # three chunks, then one repair window per seam (two seams)
    assert len(calls) == 3 + 2
    assert all(c["status"] == "ok" for c in chunks)
    # chunk text is placed on the shared timeline
    starts = [s["start"] for s in found["segments"]]
    assert any(119 < s < 123 for s in starts) and any(240 < s < 245 for s in starts)


@needs_ffmpeg
def test_a_collapsed_chunk_is_rerun_without_context_and_the_loop_is_recorded(tmp_path):
    track = tone(tmp_path / "t.wav", 60)
    runs = []

    def asr(wav, language, extra):
        runs.append(extra)
        if not extra:  # first pass loops
            return [{"start": 0.0, "end": 30.0, "text": "thanks for watching " * 8}], "en"
        return [{"start": 0.0, "end": 5.0, "text": "the actual content"}], "en"

    found = cw.transcribe_track(track, asr=asr, language="en", ffmpeg=FFMPEG)
    assert runs == [[], cw.RERUN_ARGS]
    info = found["chunks"][0]
    assert info["rerun"] is True and info["loop_after_rerun"] is False
    assert [s["text"] for s in found["segments"]] == ["the actual content"]


@needs_ffmpeg
def test_a_loop_that_survives_the_rerun_is_flagged_for_the_merge_to_collapse(tmp_path):
    track = tone(tmp_path / "t.wav", 60)

    def asr(wav, language, extra):
        return [{"start": 0.0, "end": 30.0, "text": "same thing again " * 8}], "en"

    found = cw.transcribe_track(track, asr=asr, language=None, ffmpeg=FFMPEG)
    assert found["chunks"][0]["loop_after_rerun"] is True


@needs_ffmpeg
def test_a_chunk_below_the_speech_floor_is_not_transcribed(tmp_path):
    track = tone(tmp_path / "t.wav", 30, amplitude=0.0005)

    def asr(wav, language, extra):
        raise AssertionError("whisper must not run on silence")

    found = cw.transcribe_track(track, asr=asr, language="en", ffmpeg=FFMPEG)
    assert found["segments"] == [] and found["chunks"][0]["status"] == "silent"


@needs_ffmpeg
def test_words_cut_at_a_chunk_boundary_are_replaced_by_the_window_transcription(tmp_path):
    track = tone(tmp_path / "t.wav", 150)

    def asr(wav, language, extra):
        if wav.name.startswith("norm0000"):    # chunk 0 ends mid-sentence at the seam
            return [{"start": 116.0, "end": 120.0, "text": "we should ship the"}], "en"
        if wav.name.startswith("norm0001"):    # chunk 1 starts mid-sentence
            return [{"start": 0.0, "end": 3.0, "text": "release on Friday"}], "en"
        # the seam window covers 106-134 s; its own timeline starts at 0
        return [{"start": 10.0, "end": 17.0, "text": "we should ship the release on Friday"}], "en"

    found = cw.transcribe_track(track, asr=asr, language="en", ffmpeg=FFMPEG)
    texts = [s["text"] for s in found["segments"]]
    assert texts == ["we should ship the release on Friday"]
    assert [c for c in found["chunks"] if c["kind"] == "seam"][0]["replaced"] is True
