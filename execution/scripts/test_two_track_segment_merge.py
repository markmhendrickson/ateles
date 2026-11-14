"""transcribe_two_files merges the two tracks by segment (through the real entry point).

The fixtures are synthetic: word timings and levels are made up, no audio or model
is read. They are written to pass on the segment merge and to fail on the earlier
word-level merge, which labelled every word by diarization overlap.
"""
from unittest.mock import patch

import local_fluid as lf
import local_whisper as lw
import pytest
import transcribe_audio as ta

try:  # absent before the segment merge existed
    import chunked_whisper
    import two_track_transcribe as t3
except ImportError:  # pragma: no cover
    chunked_whisper = t3 = None


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in ("TRANSCRIBE_BACKEND", "RECORD_MEETING_DIARIZE", "TRANSCRIBE_PAIRED_ASR",
                 "TRANSCRIBE_WHISPER_MODEL", "TRANSCRIBE_WHISPER_MODEL_NAME"):
        monkeypatch.delenv(name, raising=False)


def _words(text, start, speakers):
    """One word every 0.4 s; speakers is a list cycled over the words."""
    out = []
    for i, word in enumerate(text.split()):
        out.append({"start": start + 0.4 * i, "end": start + 0.4 * i + 0.3, "text": word,
                    "speaker": speakers[i % len(speakers)], "speaker_candidates": [], "confidence": 0.9})
    return out


# The remote speaker's sentence, with per-word diarization labels that flicker,
# as FluidAudio's do on a short overlap with the operator.
SYSTEM_WORDS = (
    _words("so the plan is to ship on Friday", 0.0, ["S1", "S1", "S1 + S2", "S1", "S1 + S2", "S1", "S1"])
    + _words("and then we review it", 8.0, ["S1"])
)
MIC_WORDS = (
    _words("yeah right", 1.3, ["S1"])                  # the operator's real backchannel, mid-sentence
    + _words("and then we review it", 8.0, ["S1"])    # the remote voice leaking into the mic, 35 dB down
    + _words("okay sounds good", 12.0, ["S1"])
)


def _levels(spans, total=16.0):
    frames = [-90.0] * int(total * 10)
    for start, end, db in spans:
        for i in range(int(start * 10), int(end * 10)):
            frames[i] = db
    return frames


LEVELS = {
    "system": _levels([(0.0, 10.0, -20.0)]),
    "mic": _levels([(1.3, 2.1, -35.0), (8.0, 10.0, -70.0), (12.0, 13.2, -35.0)]),
}
DIARIZATION = {"status": "ok", "speaker_count": 2, "cli_build": "abc12345", "segments": [
    {"start": 0.0, "end": 3.2, "speaker": "A"}, {"start": 8.0, "end": 10.0, "speaker": "A"},
    {"start": 1.3, "end": 2.1, "speaker": "B"}, {"start": 12.0, "end": 13.2, "speaker": "B"},
]}


@pytest.fixture
def tracks(tmp_path, monkeypatch):
    mic, system = tmp_path / "mic.wav", tmp_path / "system.wav"
    mic.write_bytes(b"mic")
    system.write_bytes(b"system")
    monkeypatch.setattr(ta, "get_audio_duration", lambda path: 16.0)
    monkeypatch.setattr(lw, "resolve_ffmpeg", lambda *a, **k: "ffmpeg")
    monkeypatch.setattr(lf, "diarize_mono", lambda wav: DIARIZATION)
    if t3 is not None:
        monkeypatch.setattr(t3, "to_wav16", lambda ffmpeg, src, dest: dest)
        monkeypatch.setattr(t3, "frame_levels", lambda ffmpeg, wav: LEVELS[wav.stem])
        monkeypatch.setattr(t3, "mix_for_diarization", lambda ffmpeg, s, m, dest: dest)
    return mic, system


def _parakeet(path, **kwargs):
    words = SYSTEM_WORDS if path.stem == "system" else MIC_WORDS
    return {"raw_response": {"original": path.name, "aligned_words": words}}


def test_one_speaker_is_not_split_across_tags_and_the_leak_is_dropped(tracks, monkeypatch):
    monkeypatch.setenv("TRANSCRIBE_PAIRED_ASR", "parakeet")
    mic, system = tracks
    with patch.object(ta, "transcribe_fluid", side_effect=_parakeet):
        result = ta.transcribe_two_files(mic, system)
    text = result["transcription_text"]
    # The remote sentence is whole, under one tag, whatever the per-word labels said.
    assert "[System]\nso the plan is to ship on Friday\n" in text
    assert "S1 + S2" not in text
    # The operator's backchannel is its own whole turn, ordered by start time.
    assert "[Mic]\nyeah right" in text
    assert text.index("so the plan") < text.index("yeah right") < text.index("and then we review it")
    # The remote voice leaking into the quiet mic is not printed twice.
    assert text.count("review it") == 1
    assert text.splitlines()[0].startswith("[Speakers: ") and "1 cross-talk segments dropped" in text.splitlines()[0]


def test_default_route_is_chunked_whisper_with_the_same_merge(tracks):
    seen = []

    def fake_track(wav16, *, asr, language, ffmpeg):
        seen.append(wav16.stem)
        words = SYSTEM_WORDS if wav16.stem == "system" else MIC_WORDS
        # whisper segments: split where the speaker's run has a pause, 8 s gap here
        first = [w for w in words if w["start"] < 6]
        second = [w for w in words if 6 <= w["start"] < 11]
        third = [w for w in words if w["start"] >= 11]
        segs = [{"start": g[0]["start"], "end": g[-1]["end"], "text": " ".join(w["text"] for w in g)}
                for g in (first, second, third) if g]
        return {"segments": segs, "chunks": [{"kind": "chunk", "index": 0, "status": "ok"}]}

    mic, system = tracks
    with patch.object(chunked_whisper, "transcribe_track", side_effect=fake_track), \
            patch.object(lw, "resolve_whisper_cli", return_value="whisper-cli"), \
            patch.object(lw, "resolve_model_path", return_value=__import__("pathlib").Path("/m/ggml-medium.bin")):
        result = ta.transcribe_two_files(mic, system)
    assert seen == ["system", "mic"]
    assert result["backend"] == "fluid" and "ggml-medium.bin" in result["transcription_model"]
    text = result["transcription_text"]
    assert "[System]\nso the plan is to ship on Friday\n" in text and text.count("review it") == 1
    raw = result["raw_response"]
    assert [t["channel"] for t in raw["tracks"]] == ["System", "Mic"]
    assert raw["segment_merge"]["bleed_dropped"] == 1 and raw["aligned_words"] == []
    ta._validate_transcription_result(result, paired=True)


def test_parakeet_route_result_passes_raw_evidence_validation(tracks, monkeypatch):
    monkeypatch.setenv("TRANSCRIBE_PAIRED_ASR", "parakeet")
    mic, system = tracks

    def native(path, **kwargs):
        words = SYSTEM_WORDS if path.stem == "system" else MIC_WORDS
        return {"raw_response": {
            "tracks": [{"channel": None,
                        "diarization": {"segments": [{"startTimeSeconds": 0.0, "endTimeSeconds": 16.0, "speakerId": "S1"}]},
                        "asr": {"wordTimings": [{"startTime": w["start"], "endTime": w["end"], "word": w["text"],
                                                 "confidence": 0.9} for w in words]}}],
            "aligned_words": [{**w, "speaker_candidates": ["S1"], "speaker": "S1"} for w in words]}}

    with patch.object(ta, "transcribe_fluid", side_effect=native):
        result = ta.transcribe_two_files(mic, system)
    ta._validate_transcription_result(result, paired=True)


def test_silence_on_both_tracks_is_the_no_speech_marker(tracks, monkeypatch):
    monkeypatch.setenv("TRANSCRIBE_PAIRED_ASR", "parakeet")
    mic, system = tracks
    with patch.object(ta, "transcribe_fluid", return_value={"raw_response": {"aligned_words": []}}):
        result = ta.transcribe_two_files(mic, system)
    assert result["transcription_text"] == lf.format_events([])


def test_unknown_paired_asr_source_is_refused(tracks, monkeypatch):
    monkeypatch.setenv("TRANSCRIBE_PAIRED_ASR", "elevenlabs")
    mic, system = tracks
    with pytest.raises(ValueError, match="TRANSCRIBE_PAIRED_ASR"):
        ta.transcribe_two_files(mic, system)
