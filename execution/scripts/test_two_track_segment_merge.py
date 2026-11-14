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
    monkeypatch.setattr(ta, "_plausible_languages", lambda language: ("en", "es", "ca"), raising=False)
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

    def fake_track(wav16, *, asr, language, ffmpeg, **kwargs):
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


def _screened_track(wav16, **kwargs):
    """A System track whose second chunk the output screen withheld; the Mic is plain."""
    if wav16.stem == "mic":
        segs = [{"start": 130.0, "end": 134.0, "text": "okay sounds good"}]
        return {"segments": segs, "chunks": [{"kind": "chunk", "index": 1, "start": 120.0, "end": 240.0,
                                              "status": "ok", "rms_db": -30.0}]}
    return {"segments": [{"start": 1.0, "end": 5.0, "text": "welcome everyone to the call"}],
            "chunks": [
                {"kind": "chunk", "index": 0, "start": 0.0, "end": 120.0, "status": "ok", "rms_db": -20.0},
                {"kind": "chunk", "index": 1, "start": 120.0, "end": 240.0, "status": "screened: foreign_diacritic",
                 "reason": "foreign_diacritic", "detail": "letters outside en/es/ca: ö",
                 "withheld_text": "the office of Mr Schröder", "withheld_segments": [
                     {"start": 121.0, "end": 126.0, "text": "the office of Mr Schröder"}]},
                {"kind": "chunk", "index": 2, "start": 240.0, "end": 300.0, "status": "silent", "rms_db": -70.0}]}


def _run_with(tracks, monkeypatch, track_fn):
    import pathlib
    mic, system = tracks
    monkeypatch.setattr(t3, "frame_levels", lambda ffmpeg, wav: LEVELS[wav.stem])
    with patch.object(chunked_whisper, "transcribe_track", side_effect=lambda wav16, **k: track_fn(wav16)), \
            patch.object(lw, "resolve_whisper_cli", return_value="whisper-cli"), \
            patch.object(ta, "_paired_model_path", return_value=pathlib.Path("/m/ggml-medium.bin")):
        return ta.transcribe_two_files(mic, system)


def test_a_screened_chunk_is_a_timed_marker_and_a_header_count_never_silence(tracks, monkeypatch):
    result = _run_with(tracks, monkeypatch, _screened_track)
    text = result["transcription_text"]
    header = text.splitlines()[0]
    assert "1 chunks withheld by the output screen (System 2:00–4:00 foreign_diacritic)" in header
    assert "1 chunks skipped as silent" in header
    assert "[2:00–4:00] [System]\n[SPEECH WITHHELD" in text
    assert "foreign_diacritic" in text and "ASR returned no words" not in text
    chunk = result["raw_response"]["tracks"][0]["chunks"][1]
    assert chunk["withheld_text"] == "the office of Mr Schröder"
    ta._validate_transcription_result(result, paired=True)


def test_the_validator_requires_the_disclosure(tracks, monkeypatch):
    result = _run_with(tracks, monkeypatch, _screened_track)
    stripped = dict(result, transcription_text=result["transcription_text"].replace("[SPEECH WITHHELD", "[…"))
    with pytest.raises(ta.IncompleteTranscriptionResult):
        ta._validate_transcription_result(stripped, paired=True)
    merge = dict(result["raw_response"]["segment_merge"], gaps=[])
    forgotten = dict(result, raw_response=dict(result["raw_response"], segment_merge=merge))
    with pytest.raises(ta.IncompleteTranscriptionResult):
        ta._validate_transcription_result(forgotten, paired=True)


def test_a_recording_whose_only_chunk_was_screened_is_not_reported_as_no_words(tracks, monkeypatch):
    def only_screened(wav16):
        if wav16.stem == "mic":
            return {"segments": [], "chunks": []}
        return {"segments": [], "chunks": [{
            "kind": "chunk", "index": 0, "start": 0.0, "end": 60.0, "status": "screened: degenerate_repetition",
            "reason": "degenerate_repetition", "detail": "loop", "withheld_text": "thank you " * 30}]}

    result = _run_with(tracks, monkeypatch, only_screened)
    assert result["transcription_text"] != lf.format_events([])
    assert "[SPEECH WITHHELD" in result["transcription_text"]
    ta._validate_transcription_result(result, paired=True)


def test_plausible_languages_come_from_the_session_language_profile(monkeypatch):
    import session_language

    class Found:
        plausible = ("en", "pt")

    monkeypatch.setattr(session_language, "resolve_session_languages", lambda language, timeout=5.0: Found())
    assert ta._plausible_languages("en") == ("en", "pt")

    def broken(language, timeout=5.0):
        raise OSError("down")

    monkeypatch.setattr(session_language, "resolve_session_languages", broken)
    from hallucination_filter import DEFAULT_PLAUSIBLE_LANGUAGES
    assert ta._plausible_languages("en") == tuple(DEFAULT_PLAUSIBLE_LANGUAGES)


def test_fingerprint_follows_the_medium_model_the_paired_route_uses(tmp_path, monkeypatch):
    model = tmp_path / "ggml-medium.bin"
    model.write_bytes(b"one")
    monkeypatch.setattr(ta, "_paired_model_path", lambda: model)
    before = ta._processing_fingerprint()
    model.write_bytes(b"two two")
    assert ta._processing_fingerprint() != before


def test_a_call_too_long_for_the_deadline_is_refused_before_any_work(tracks, monkeypatch):
    mic, system = tracks
    monkeypatch.setattr(ta, "get_audio_duration", lambda path: 3 * 3600.0)
    with patch.object(lw, "resolve_whisper_cli", side_effect=AssertionError("must refuse before any work")):
        with pytest.raises(RuntimeError, match="TRANSCRIBE_PAIRED_ASR=parakeet"):
            ta.transcribe_two_files(mic, system)


def test_a_long_call_that_fits_only_warns(tracks, monkeypatch, capsys):
    monkeypatch.setattr(ta, "get_audio_duration", lambda path: 100 * 60.0)
    ta._check_whisper_time_budget(100 * 60.0)
    assert "expected to take about" in capsys.readouterr().err
    ta._check_whisper_time_budget(10 * 60.0)
    assert capsys.readouterr().err == ""
    monkeypatch.setenv("TYTO_TRANSCRIBE_PROCESS_TIMEOUT_SECS", "14400")
    ta._check_whisper_time_budget(3 * 3600.0)  # a longer deadline admits it
