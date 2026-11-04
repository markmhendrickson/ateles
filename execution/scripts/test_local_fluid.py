import json
from pathlib import Path
from unittest.mock import patch

import local_fluid as lf
import local_whisper as lw
import pytest


def test_diarization_never_selects_cloud_from_key_presence():
    for env in ({}, {"ELEVENLABS_API_KEY": "synthetic", "RECORD_MEETING_DIARIZE": "1"}):
        assert lw.resolve_backend(use_diarization=True, env=env) == "fluid"
        assert lw.resolve_backend(channel_count=2, env=env) == "fluid"


def test_missing_cli_fails_without_running_any_transcriber(monkeypatch):
    monkeypatch.setattr(lf.shutil, "which", lambda _: None)
    with patch.object(lf.subprocess, "run") as run:
        with pytest.raises(lf.LocalFluidUnavailable, match="No cloud fallback"):
            lf.transcribe_fluid(Path("synthetic.wav"))
    run.assert_not_called()


def test_overlap_and_unassigned_words_are_preserved():
    diar = {
        "segments": [
            {"startTimeSeconds": 0, "endTimeSeconds": 2, "speakerId": "A"},
            {"startTimeSeconds": 1, "endTimeSeconds": 3, "speakerId": "B"},
        ]
    }
    asr = {
        "wordTimings": [
            {"word": "First", "startTime": 0.2, "endTime": 0.8},
            {"word": "Overlap", "startTime": 1.2, "endTime": 1.8},
            {"word": "Outside", "startTime": 4, "endTime": 5},
        ]
    }
    events = lf.align_words(asr, diar)
    assert [e["speaker"] for e in events] == ["A", "A + B", "Unassigned"]
    assert [e["text"] for e in events] == ["First", "Overlap", "Outside"]


def test_invalid_timestamps_and_untimed_asr_fail_closed():
    with pytest.raises(lf.LocalFluidUnavailable, match="word timestamps"):
        lf.align_words({"text": "Untimed", "wordTimings": []}, {"segments": []})
    with pytest.raises(lf.LocalFluidUnavailable, match="timestamp"):
        lf.align_words(
            {"wordTimings": [{"word": "Bad", "startTime": float("nan"), "endTime": 2}]},
            {"segments": []},
        )


def test_cli_commands_keep_audio_local_and_preserve_raw_results(tmp_path, monkeypatch):
    audio = tmp_path / "synthetic.wav"
    audio.write_bytes(b"synthetic")
    monkeypatch.setattr(lf, "resolve_fluid_cli", lambda: "/test/fluidaudiocli")
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv[1] == "process":
            value = {
                "durationSeconds": 2,
                "segments": [
                    {
                        "startTimeSeconds": 0,
                        "endTimeSeconds": 2,
                        "speakerId": "Speaker_1",
                    }
                ],
            }
        else:
            value = {
                "text": "Hola",
                "wordTimings": [{"word": "Hola", "startTime": 0.1, "endTime": 0.6}],
            }
        Path(argv[-1]).write_text(json.dumps(value))

        class Result:
            returncode, stderr = 0, ""

        return Result()

    monkeypatch.setattr(lf.subprocess, "run", fake_run)
    result = lf.transcribe_fluid(audio, language="es")
    assert result["backend"] == "fluid"
    assert "[Speaker_1]" in result["transcription_text"]
    assert "Hola" in result["transcription_text"]
    assert result["raw_response"]["tracks"][0]["asr"]["text"] == "Hola"
    assert calls[0][1:6] == ["process", str(audio), "--mode", "offline", "--output"]
    assert "--language" in calls[1] and "es" in calls[1]


def test_zero_exit_without_output_is_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(
        lf.subprocess,
        "run",
        lambda *a, **kw: type("Result", (), {"returncode": 0, "stderr": ""})(),
    )
    with pytest.raises(lf.LocalFluidUnavailable, match="failed"):
        lf._run_json("fluid", ["process"], tmp_path / "missing.json")
