"""Round 4 (scope-cut): numbers are speech, discards are disclosed, ffprobe is found next to ffmpeg."""
import stat
from pathlib import Path
from unittest.mock import patch

import local_fluid as lf
import local_whisper as lw
import pytest
import transcribe_audio as ta
import two_track_merge as m

try:
    import chunked_whisper
    import two_track_transcribe as t3
except ImportError:  # pragma: no cover
    chunked_whisper = t3 = None


def seg(start, end, text):
    return {"start": start, "end": end, "text": text}


# --- 1. digits are speech; every discard is disclosed -------------------------------

def test_the_operators_numeric_answers_are_kept_in_a_real_merge():
    tracks = {
        m.SYSTEM: [seg(0, 3, "How much was the deposit and which year did the contract start?")],
        m.MIC: [seg(4, 5, "$300."), seg(6, 7, "2025."), seg(8, 9, "2,500"), seg(10, 11, "1, 2, 3.")],
    }
    result = m.merge_tracks(tracks)
    for answer in ("$300.", "2025.", "2,500", "1, 2, 3."):
        assert answer in result.text
    assert result.stats["segments_discarded"] == 0


def test_a_bare_tag_is_dropped_but_marked_and_counted_with_track_and_span():
    tracks = {m.SYSTEM: [seg(0, 3, "Can you hear me?")],
              m.MIC: [seg(4, 6, "(inaudible)"), seg(6.5, 8, "[BLANK_AUDIO]"), seg(20, 22, "..."), seg(30, 32, "Yes I can.")]}
    result = m.merge_tracks(tracks)
    header = result.text.splitlines()[0]
    assert "3 non-speech segments dropped (Mic 0:04\u20130:08, Mic 0:20\u20130:22)" in header
    assert "[0:04\u20130:08] [Mic]\n[NON-SPEECH TAG DROPPED" in result.text
    assert "Yes I can." in result.text
    assert result.stats["discarded_segments"][0]["text"] == "(inaudible)"


@pytest.fixture
def tracks_on_disk(tmp_path, monkeypatch):
    mic, system = tmp_path / "mic.wav", tmp_path / "system.wav"
    mic.write_bytes(b"mic")
    system.write_bytes(b"system")
    monkeypatch.setattr(ta, "get_audio_duration", lambda path: 16.0)
    monkeypatch.setattr(lw, "resolve_ffmpeg", lambda *a, **k: "ffmpeg")
    monkeypatch.setattr(lf, "diarize_mono", lambda wav: {"status": "failed", "category": "cli not found"})
    monkeypatch.setattr(ta, "_plausible_languages", lambda language: ("en", "es", "ca"), raising=False)
    monkeypatch.setattr(t3, "to_wav16", lambda ffmpeg, src, dest: dest)
    monkeypatch.setattr(t3, "frame_levels", lambda ffmpeg, wav: [-30.0] * 160)
    monkeypatch.setattr(t3, "mix_for_diarization", lambda ffmpeg, s, mm, dest: dest)
    return mic, system


def _answers(wav16, **kwargs):
    if wav16.stem == "system":
        return {"segments": [seg(0, 3, "How much was the deposit and which year did the contract start?")], "chunks": []}
    return {"segments": [seg(4, 5, "$300."), seg(6, 7, "2025."), seg(8, 9, "(inaudible)")], "chunks": []}


def _run(tracks_on_disk, track_fn):
    mic, system = tracks_on_disk
    with patch.object(chunked_whisper, "transcribe_track", side_effect=track_fn), \
            patch.object(lw, "resolve_whisper_cli", return_value="whisper-cli"), \
            patch.object(ta, "_paired_model_path", return_value=Path("/m/ggml-medium.bin")):
        return ta.transcribe_two_files(mic, system)


def test_answers_survive_the_whole_route_and_the_validator_requires_the_discard_disclosure(tracks_on_disk):
    result = _run(tracks_on_disk, _answers)
    text = result["transcription_text"]
    assert "$300." in text and "2025." in text and "1 non-speech segment dropped (Mic 0:08\u20130:09)" in text.splitlines()[0]
    ta._validate_transcription_result(result, paired=True)
    for needle, replacement in (("[NON-SPEECH TAG DROPPED", "[..."), ("non-speech segment dropped", "fine")):
        with pytest.raises(ta.IncompleteTranscriptionResult):
            ta._validate_transcription_result(dict(result, transcription_text=text.replace(needle, replacement)), paired=True)
    merge = dict(result["raw_response"]["segment_merge"], segments_discarded=0, discarded_segments=[])
    with pytest.raises(ta.IncompleteTranscriptionResult):
        ta._validate_transcription_result(
            dict(result, raw_response=dict(result["raw_response"], segment_merge=merge)), paired=True)


# --- 4. ffprobe next to the configured ffmpeg; missing ffprobe is transient ------------

def _fake_tool(folder: Path, name: str, body: str) -> Path:
    tool = folder / name
    tool.write_text("#!/bin/sh\n" + body)
    tool.chmod(tool.stat().st_mode | stat.S_IXUSR)
    return tool


def test_ffprobe_is_found_next_to_the_configured_ffmpeg_when_path_has_none(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    ffmpeg = _fake_tool(bin_dir, "ffmpeg", "exit 0\n")
    sibling = _fake_tool(bin_dir, "ffprobe", "echo 42.5\n")
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("TRANSCRIBE_FFMPEG", str(ffmpeg))
    monkeypatch.setenv("PATH", str(empty))
    clip = tmp_path / "call.m4a"
    clip.write_bytes(b"not really audio")
    assert ta._resolve_ffprobe() == str(sibling)
    assert ta.get_audio_duration(clip) == pytest.approx(42.5)


def test_a_missing_ffprobe_is_a_clear_transient_error_never_a_permanent_hold(tmp_path, monkeypatch):
    empty = tmp_path / "empty"
    empty.mkdir()
    ffmpeg = _fake_tool(tmp_path, "ffmpeg", "exit 0\n")           # no ffprobe beside it
    monkeypatch.setenv("TRANSCRIBE_FFMPEG", str(ffmpeg))
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.delenv("TRANSCRIBE_PAIRED_ASR", raising=False)
    mic, system = tmp_path / "mic.m4a", tmp_path / "system.m4a"
    mic.write_bytes(b"x")
    system.write_bytes(b"y")
    monkeypatch.setattr(lw, "resolve_ffmpeg", lambda *a, **k: str(ffmpeg))
    with patch.object(lw, "resolve_whisper_cli", side_effect=AssertionError("must fail before any work")):
        with pytest.raises(RuntimeError) as failed:
            ta.transcribe_two_files(mic, system)
    message = str(failed.value)
    assert "ffprobe not found" in message and "Tyto will retry" in message
    assert "ERR_" not in message                     # no permanent code: Tyto retries and alerts


def test_the_refusal_prints_the_variable_as_a_prefix_with_a_value_and_stays_under_the_cut():
    with pytest.raises(RuntimeError) as refused:
        ta._check_whisper_time_budget(3 * 3600.0)
    message = str(refused.value)
    assert "TRANSCRIBE_PAIRED_ASR=parakeet" in message
    import re
    assert re.search(r"TYTO_TRANSCRIBE_PROCESS_TIMEOUT_SECS=\d+ as a command prefix", message)
    assert "in Tyto" not in message and len("Error transcribing audio: ") + len(message) < 300
