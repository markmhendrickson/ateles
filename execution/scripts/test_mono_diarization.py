"""Single-channel transcripts are diarized locally with FluidAudio, and the
transcript header states the method and outcome (operator rule, 12 Nov 2026).
"""
import json
import stat
from pathlib import Path
from unittest.mock import patch

import local_fluid as lf
import local_whisper as lw
import pytest
import transcribe_audio as ta


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in ("TRANSCRIBE_BACKEND", "RECORD_MEETING_DIARIZE",
                 "TRANSCRIBE_FLUID_COMPUTE_UNITS"):
        monkeypatch.delenv(name, raising=False)


def _fake_cli(tmp_path, body):
    cli = tmp_path / "fluidaudiocli"
    cli.write_text("#!/bin/sh\n" + body)
    cli.chmod(cli.stat().st_mode | stat.S_IXUSR)
    return cli


_OK_CLI = """
out=""; prev=""
for a in "$@"; do if [ "$prev" = "--output" ]; then out="$a"; fi; prev="$a"; done
cat > "$out" <<'JSON'
{"speakerCount": 2, "segments": [
 {"speakerId": "S1", "startTimeSeconds": 0.0, "endTimeSeconds": 5.0, "embedding": [0.1, 0.2]},
 {"speakerId": "S2", "startTimeSeconds": 5.0, "endTimeSeconds": 9.0, "embedding": [0.3, 0.4]}]}
JSON
"""


def test_diarize_mono_runs_offline_cli_and_drops_embeddings(tmp_path, monkeypatch):
    cli = _fake_cli(tmp_path, _OK_CLI)
    monkeypatch.setenv("TRANSCRIBE_FLUID_CLI", str(cli))
    seen = {}
    real_run = lf.subprocess.run

    def spy(command, **kwargs):
        seen["command"] = command
        return real_run(command, **kwargs)

    monkeypatch.setattr(lf.subprocess, "run", spy)
    wav = tmp_path / "memo.16k.wav"
    wav.write_bytes(b"synthetic")
    result = lf.diarize_mono(wav)
    assert seen["command"][1:7] == [
        "process", str(wav), "--mode", "offline", "--compute-units", "cpu-only"]
    assert result["status"] == "ok" and result["speaker_count"] == 2
    assert result["method"] == "FluidAudio offline"
    assert "embedding" not in json.dumps(result)
    assert result["segments"][1] == {"start": 5.0, "end": 9.0, "speaker": "S2"}


def test_diarize_mono_failure_is_recorded_not_raised(tmp_path, monkeypatch):
    cli = _fake_cli(tmp_path, "echo 'metal command buffer timeout' >&2\nexit 3\n")
    monkeypatch.setenv("TRANSCRIBE_FLUID_CLI", str(cli))
    wav = tmp_path / "memo.16k.wav"
    wav.write_bytes(b"synthetic")
    result = lf.diarize_mono(wav)
    assert result["status"] == "failed"
    assert "metal command buffer timeout" in result["reason"]
    assert "FAILED" in lf.diarization_header(result)


def test_diarize_mono_missing_cli_is_a_recorded_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("TRANSCRIBE_FLUID_CLI", str(tmp_path / "absent"))
    result = lf.diarize_mono(tmp_path / "memo.wav")
    assert result["status"] == "failed" and "unavailable" in result["reason"]


def test_header_names_method_and_speaker_count():
    one = {"status": "ok", "method": "FluidAudio offline", "cli_build": "abc123",
           "compute_units": "cpu-only", "speaker_count": 1, "segments": []}
    assert lf.diarization_header(one) == (
        "[Diarization: FluidAudio offline (cli build abc123, cpu-only) - 1 speaker]")
    assert "2 speakers" in lf.diarization_header({**one, "speaker_count": 2})
    assert "no speech segments detected" in lf.diarization_header({**one, "speaker_count": 0})


def test_segments_are_attributed_by_greatest_overlap():
    diar = {"segments": [{"start": 0, "end": 5, "speaker": "S1"},
                         {"start": 5, "end": 9, "speaker": "S2"}]}
    turns = lf.attribute_segments([
        {"start": 0.0, "end": 2.0, "text": "One."},
        {"start": 2.0, "end": 4.5, "text": "Two."},
        {"start": 4.6, "end": 8.0, "text": "Three."},
        {"start": 20.0, "end": 21.0, "text": "Outside."},
    ], diar)
    assert [(t["speaker"], t["text"]) for t in turns] == [
        ("S1", "One. Two."), ("S2", "Three."), ("Unassigned", "Outside.")]


def _local_result(**extra):
    return {"transcription_text": "Synthetic memo.", "language": "en", "rms_db": -30.0,
            "silence": False, "transcription_model": "ggml-test.bin", **extra}


def _mono_audio(tmp_path):
    audio = tmp_path / "memo.wav"
    audio.write_bytes(b"synthetic")
    return audio


def test_mono_transcript_calls_fluidaudio_and_states_method_in_header(tmp_path):
    audio = _mono_audio(tmp_path)
    diar = {"status": "ok", "method": "FluidAudio offline", "cli_build": "abc123",
            "compute_units": "cpu-only", "speaker_count": 1, "segments": []}
    with patch.object(ta, "get_audio_channel_count", return_value=1), \
         patch.object(ta, "get_audio_duration", return_value=1.0), \
         patch.object(ta, "transcribe_local", return_value=_local_result(diarization=diar)) as run:
        result = ta.transcribe_audio_file(audio)
    assert run.call_args.kwargs["diarize"] is True
    assert result["transcription_text"].startswith(
        "[Diarization: FluidAudio offline (cli build abc123, cpu-only) - 1 speaker]\n\n")
    assert result["transcription_text"].endswith("Synthetic memo.")
    assert result["diarization"]["speaker_count"] == 1
    ta._validate_transcription_result(result, paired=False)


def test_failed_diarization_keeps_the_transcript_and_says_so(tmp_path):
    audio = _mono_audio(tmp_path)
    diar = {"status": "failed", "method": "FluidAudio offline", "reason": "cli exploded"}
    with patch.object(ta, "get_audio_channel_count", return_value=1), \
         patch.object(ta, "get_audio_duration", return_value=1.0), \
         patch.object(ta, "transcribe_local", return_value=_local_result(diarization=diar)):
        result = ta.transcribe_audio_file(audio)
    assert "FluidAudio offline FAILED - cli exploded" in result["transcription_text"]
    assert result["transcription_text"].endswith("Synthetic memo.")
    ta._validate_transcription_result(result, paired=False)


def test_multi_speaker_mono_is_rendered_as_speaker_turns(tmp_path):
    audio = _mono_audio(tmp_path)
    diar = {"status": "ok", "method": "FluidAudio offline", "cli_build": None,
            "compute_units": "cpu-only", "speaker_count": 2,
            "segments": [{"start": 0, "end": 5, "speaker": "S1"},
                         {"start": 5, "end": 9, "speaker": "S2"}]}
    segments = [{"start": 0.0, "end": 4.0, "text": "Hello."},
                {"start": 5.0, "end": 8.0, "text": "Hi."}]
    with patch.object(ta, "get_audio_channel_count", return_value=1), \
         patch.object(ta, "get_audio_duration", return_value=1.0), \
         patch.object(ta, "transcribe_local",
                      return_value=_local_result(diarization=diar, segments=segments)):
        result = ta.transcribe_audio_file(audio)
    text = result["transcription_text"]
    assert "2 speakers" in text.splitlines()[0]
    assert "[0.00–4.00] [S1]\nHello." in text and "[5.00–8.00] [S2]\nHi." in text


def test_explicit_opt_out_is_unchanged_and_has_no_header(tmp_path):
    audio = _mono_audio(tmp_path)
    with patch.object(ta, "get_audio_channel_count", return_value=1), \
         patch.object(ta, "get_audio_duration", return_value=1.0), \
         patch.object(ta, "transcribe_local", return_value=_local_result()) as run:
        result = ta.transcribe_audio_file(audio, use_diarization=False)
    assert run.call_args.kwargs["diarize"] is False
    assert result["transcription_text"] == "Synthetic memo."
    assert "diarization" not in result


def test_silence_marker_is_not_given_a_header(tmp_path):
    audio = _mono_audio(tmp_path)
    marker = lw.NO_SPEECH_MARKER_LEVEL.format(rms=-70.0, threshold=-50.0)
    with patch.object(ta, "get_audio_channel_count", return_value=1), \
         patch.object(ta, "get_audio_duration", return_value=1.0), \
         patch.object(ta, "transcribe_local",
                      return_value=_local_result(transcription_text=marker, silence=True)):
        result = ta.transcribe_audio_file(audio)
    assert result["transcription_text"] == marker


def test_transcribe_local_diarizes_the_converted_wav_and_reruns_timed_only_for_many_speakers(
        tmp_path, monkeypatch):
    audio = _mono_audio(tmp_path)
    model = tmp_path / "m.bin"
    model.write_bytes(b"m")
    calls = []

    def fake_run(cmd, **_kwargs):
        calls.append(cmd)
        stem = cmd[cmd.index("--output-file") + 1]
        body = {"transcription": [
            {"offsets": {"from": 0, "to": 4000}, "text": " Hello."},
            {"offsets": {"from": 5000, "to": 8000}, "text": " Hi."}]}
        Path(f"{stem}.json").write_text(json.dumps(body))
        return type("P", (), {"returncode": 0, "stderr": ""})()

    def fake_convert(_audio, dest, **_kwargs):
        wav = dest / "x.16k.wav"
        wav.write_bytes(b"w")
        return wav

    monkeypatch.setattr(lw, "resolve_whisper_cli", lambda: "whisper-cli")
    monkeypatch.setattr(lw, "resolve_model_path", lambda: model)
    monkeypatch.setattr(lw, "resolve_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(lw, "convert_to_wav16k_mono", fake_convert)
    monkeypatch.setattr(lw, "measure_sustained_rms_db", lambda *_a, **_k: -30.0)
    monkeypatch.setattr(lw.subprocess, "run", fake_run)
    seen = []
    count = [1]

    def fake_diar(wav):
        seen.append(wav.name)
        return {"status": "ok", "method": "FluidAudio offline",
                "speaker_count": count[0], "segments": []}

    monkeypatch.setattr(lf, "diarize_mono", fake_diar)
    one = lw.transcribe_local(audio, diarize=True)
    assert seen == ["x.16k.wav"] and len(calls) == 1 and "segments" not in one
    assert "--no-timestamps" in calls[0]
    count[0] = 2
    many = lw.transcribe_local(audio, diarize=True)
    assert len(calls) == 3 and "--no-timestamps" not in calls[2]
    assert many["segments"][1] == {"start": 5.0, "end": 8.0, "text": "Hi."}
    plain = lw.transcribe_local(audio)
    assert "diarization" not in plain
