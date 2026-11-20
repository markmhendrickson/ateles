import json
import math
import random
import wave
from pathlib import Path
from unittest.mock import patch

import local_fluid as lf
import local_whisper as lw
import pytest


@pytest.fixture(autouse=True)
def _isolated_compute_placement(monkeypatch):
    monkeypatch.delenv("TRANSCRIBE_FLUID_COMPUTE_UNITS", raising=False)


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
    assert calls[0][1:8] == ["process", str(audio), "--mode", "offline", "--compute-units", "cpu-only", "--output"]
    assert "--language" in calls[1] and "es" in calls[1]


def test_zero_exit_without_output_is_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(
        lf.subprocess,
        "run",
        lambda *a, **kw: type("Result", (), {"returncode": 0, "stderr": ""})(),
    )
    with pytest.raises(lf.LocalFluidUnavailable, match="failed"):
        lf._run_json("fluid", ["process"], tmp_path / "missing.json")


def test_overlapping_channels_keep_whole_utterances():
    events = [
        {"start": 0, "end": 0.4, "speaker": "System/A", "text": "I"},
        {"start": 0.1, "end": 0.5, "speaker": "Mic/B", "text": "Thanks"},
        {"start": 0.5, "end": 0.9, "speaker": "System/A", "text": "agree"},
        {"start": 0.6, "end": 1, "speaker": "Mic/B", "text": "everyone"},
    ]
    assert lf.format_events(events) == (
        "[0.00–0.90] [System/A]\nI agree\n\n[0.10–1.00] [Mic/B]\nThanks everyone"
    )
    assert events[0]["text"] == "I"


def test_speech_without_asr_words_is_failure():
    with pytest.raises(lf.LocalFluidUnavailable, match="speech.*no words"):
        lf.align_words(
            {"text": "", "wordTimings": []},
            {
                "segments": [
                    {"startTimeSeconds": 0, "endTimeSeconds": 1, "speakerId": "A"}
                ]
            },
        )


def test_native_multichannel_keeps_both_channel_identities(tmp_path, monkeypatch):
    audio = tmp_path / "stereo.wav"
    audio.write_bytes(b"synthetic")
    monkeypatch.setattr(lf, "resolve_fluid_cli", lambda: "/test/fluid")
    monkeypatch.setattr(lf.shutil, "which", lambda _: "/test/ffmpeg")
    commands = []

    def extract(argv, **kwargs):
        commands.append(argv)
        Path(argv[-1]).write_bytes(b"channel")

    def model(cli, argv, output):
        assert Path(argv[1]).is_file()
        if argv[0] == "process":
            return {
                "durationSeconds": 2,
                "segments": [
                    {"startTimeSeconds": 0, "endTimeSeconds": 2, "speakerId": "S1"}
                ],
            }
        return {
            "text": "Hi",
            "wordTimings": [{"word": "Hi", "startTime": 0.1, "endTime": 0.5}],
        }

    monkeypatch.setattr(lf.subprocess, "run", extract)
    monkeypatch.setattr(lf, "_run_json", model)
    result = lf.transcribe_fluid(audio, channel_count=2)
    assert [command[command.index("-af") + 1] for command in commands] == [
        "pan=mono|c0=c0",
        "pan=mono|c0=c1",
    ]
    assert [word["speaker"] for word in result["raw_response"]["aligned_words"]] == [
        "Channel_0/S1",
        "Channel_1/S1",
    ]
    assert [track["channel"] for track in result["raw_response"]["tracks"]] == [
        "Channel_0",
        "Channel_1",
    ]
    assert "[Channel_0/S1]" in result["transcription_text"]
    assert "[Channel_1/S1]" in result["transcription_text"]
    assert not Path(commands[0][-1]).exists()


def test_fluid_failure_identifies_stage_and_retains_gpu_failure_code(tmp_path, monkeypatch):
    failed = type("Result", (), {"returncode": 1, "stderr":
        "MTLCommandBufferErrorDomain Code=2 UCommandBufferCallbackErrorTimeout\n" + "profiling " * 500})()
    monkeypatch.setattr(lf.subprocess, "run", lambda *a, **kw: failed)
    with pytest.raises(lf.LocalFluidUnavailable) as caught:
        lf._run_json("fluid", ["process", "synthetic.wav", "--mode", "offline", "--output"], tmp_path / "missing.json")
    error = str(caught.value)
    assert "stage=process" in error
    assert "UCommandBufferCallbackErrorTimeout" in error
    assert len(error) < 2600


def test_fluid_asr_timeout_identifies_stage_without_cloud_retry(tmp_path, monkeypatch):
    calls = []
    def timeout(argv, **kwargs):
        calls.append(argv)
        raise lf.subprocess.TimeoutExpired(argv, 1)
    monkeypatch.setattr(lf.subprocess, "run", timeout)
    with pytest.raises(lf.LocalFluidUnavailable, match="stage=transcribe.*timed out"):
        lf._run_json("fluid", ["transcribe", "synthetic.wav"], tmp_path / "missing.json")
    assert len(calls) == 1


@pytest.mark.parametrize("units", ["cpu-only", "ane", "all"])
def test_explicit_fluid_compute_units_are_forwarded(tmp_path, monkeypatch, units):
    audio = tmp_path / "fixture.wav"
    audio.write_bytes(b"fixture")
    monkeypatch.setenv("TRANSCRIBE_FLUID_COMPUTE_UNITS", units)
    monkeypatch.setattr(lf, "resolve_fluid_cli", lambda: "/fixture/fluid")
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        payload = {"segments": [], "durationSeconds": 0} if argv[1] == "process" else {"wordTimings": []}
        Path(argv[-1]).write_text(json.dumps(payload))
        return type("Result", (), {"returncode": 0, "stderr": ""})()
    monkeypatch.setattr(lf.subprocess, "run", run)
    lf.transcribe_fluid(audio)
    index = calls[0].index("--compute-units")
    assert calls[0][index + 1] == units
    assert calls[1][1] == "transcribe"


def test_invalid_fluid_compute_units_fail_before_inference(tmp_path, monkeypatch):
    audio = tmp_path / "fixture.wav"
    audio.write_bytes(b"fixture")
    monkeypatch.setenv("TRANSCRIBE_FLUID_COMPUTE_UNITS", "unknown")
    monkeypatch.setattr(lf, "resolve_fluid_cli", lambda: "/fixture/fluid")
    with patch.object(lf.subprocess, "run") as run:
        with pytest.raises(lf.LocalFluidUnavailable, match="COMPUTE_UNITS"):
            lf.transcribe_fluid(audio)
    run.assert_not_called()


def _synthetic_stereo(path, left, right):
    """Write tiny 16 kHz PCM input; no recorded audio is used by these tests."""
    from array import array

    samples = array("h", (value for pair in zip(left, right) for value in pair))
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(lf.DUAL_MONO_RATE)
        wav.writeframes(samples.tobytes())


def _voice_like(length=16000):
    rng = random.Random(41)
    return [
        int((4000 * math.sin(2 * math.pi * 170 * n / 16000)
             + 1500 * math.sin(2 * math.pi * 430 * n / 16000)
             + rng.randrange(-500, 501)) * (0.5 + 0.5 * math.sin(2 * math.pi * 3 * n / 16000) ** 2))
        for n in range(length)
    ]


@pytest.mark.parametrize("variant", ["identical", "offset_noise", "different", "extra_voice", "silent", "one_silent", "short", "near_zero"])
def test_dual_mono_audio_decision_and_transcription_effect(tmp_path, monkeypatch, variant):
    left = _voice_like(16000 if variant != "short" else 80)
    rng = random.Random(57)
    right = left.copy()
    if variant == "offset_noise":
        right = [0] * 16 + [max(-32768, min(32767, value + rng.randrange(-80, 81))) for value in left[:-16]]
    elif variant == "different":
        right = [int(4300 * math.sin(2 * math.pi * 330 * n / 16000)) for n in range(len(left))]
    elif variant == "extra_voice":
        right = [
            max(-32768, min(32767, value + int(2500 * math.sin(2 * math.pi * 330 * n / 16000))))
            if 6000 <= n < 9000 else value
            for n, value in enumerate(left)
        ]
    elif variant == "silent":
        left = right = [0] * len(left)
    elif variant == "one_silent":
        right = [0] * len(left)
    elif variant == "near_zero":
        left = right = [value // 100 for value in left]
    audio = tmp_path / "synthetic-stereo.wav"
    _synthetic_stereo(audio, left, right)
    monkeypatch.setattr(lf, "resolve_fluid_cli", lambda: "synthetic-fluid")
    paths = []
    decoded = []

    def model(cli, args, output):
        source = Path(args[1])
        assert source.is_file()
        if args[0] == "process":
            return {"durationSeconds": 1, "segments": [
                {"startTimeSeconds": 0, "endTimeSeconds": 1, "speakerId": "Speaker_1"}
            ]}
        paths.append(source)
        with wave.open(str(source), "rb") as wav:
            decoded.append((wav.getnchannels(), wav.getframerate(), wav.readframes(1)))
        return {"text": "Synthetic", "wordTimings": [
            {"word": "Synthetic", "startTime": 0.1, "endTime": 0.5}
        ]}

    monkeypatch.setattr(lf, "_run_json", model)
    result = lf.transcribe_fluid(audio, channel_count=2)
    collapsed = variant in {"identical", "offset_noise"}
    assert len(paths) == (1 if collapsed else 2)
    assert len(result["raw_response"]["tracks"]) == len(paths)
    if collapsed:
        # one REAL channel is kept (never an average): the louder one
        assert paths[0].name in {"channel-0.wav", "channel-1.wav"}
        assert decoded[0][0:2] == (1, 16000)
        first = int.from_bytes(decoded[0][2], "little", signed=True)
        assert min(abs(first - left[0]), abs(first - right[0])) <= 1
        assert result["transcription_text"].count("Synthetic") == 1
        assert result["transcription_text"].splitlines()[0].startswith("[Speakers: 1,")
        assert "dual-mono, collapsed; correlation=" in result["transcription_text"]
        assert "[Speaker_1]" in result["transcription_text"]
        assert "Channel_" not in result["transcription_text"]
    else:
        assert [path.name for path in paths] == ["channel-0.wav", "channel-1.wav"]
        assert "dual-mono, collapsed" not in result["transcription_text"]
        assert "[Channel_0/Speaker_1]" in result["transcription_text"]
        assert "[Channel_1/Speaker_1]" in result["transcription_text"]


def _rms_of(path):
    from array import array

    with wave.open(str(path), "rb") as wav:
        samples = array("h")
        samples.frombytes(wav.readframes(wav.getnframes()))
    return math.sqrt(sum(v * v for v in samples) / len(samples))


def test_lagged_dual_mono_keeps_full_level_and_is_never_averaged(tmp_path, monkeypatch):
    # A 500 Hz tone delayed by 1 ms is half a period out of phase: summing the channels would cancel it.
    left = [int(6000 * math.sin(2 * math.pi * 500 * n / 16000)) for n in range(16000)]
    right = [0] * 16 + left[:-16]
    audio = tmp_path / "synthetic-lagged.wav"
    _synthetic_stereo(audio, left, right)
    monkeypatch.setattr(lf, "resolve_fluid_cli", lambda: "synthetic-fluid")
    levels = []

    def model(cli, args, output):
        if args[0] == "process":
            return {"durationSeconds": 1, "segments": [
                {"startTimeSeconds": 0, "endTimeSeconds": 1, "speakerId": "Speaker_1"}]}
        levels.append(_rms_of(Path(args[1])))
        return {"text": "Synthetic", "wordTimings": [{"word": "Synthetic", "startTime": 0.1, "endTime": 0.5}]}

    monkeypatch.setattr(lf, "_run_json", model)
    result = lf.transcribe_fluid(audio, channel_count=2)
    assert len(levels) == 1 and "dual-mono, collapsed" in result["transcription_text"]
    assert levels[0] > 0.9 * math.sqrt(sum(v * v for v in left) / len(left))


def test_silent_identical_channels_keep_the_exact_headerless_silent_result(tmp_path, monkeypatch):
    # silent channels are inconclusive for the collapse; a collapsed clip with no words must stay headerless too
    left = _voice_like()
    audio = tmp_path / "synthetic-identical.wav"
    _synthetic_stereo(audio, left, left)
    monkeypatch.setattr(lf, "resolve_fluid_cli", lambda: "synthetic-fluid")
    monkeypatch.setattr(lf, "_run_json", lambda cli, args, output: (
        {"durationSeconds": 1, "segments": []}
        if args[0] == "process" else {"wordTimings": []}))
    result = lf.transcribe_fluid(audio, channel_count=2)
    assert result["silence"] and result["transcription_text"] == lf.format_events([])
    assert "dual-mono" not in result["transcription_text"]


def test_dual_mono_similarity_is_inconclusive_for_malformed_unequal_and_over_limit(tmp_path, monkeypatch):
    left = _voice_like()
    good_a, good_b = tmp_path / "a.wav", tmp_path / "b.wav"
    for path in (good_a, good_b):
        with wave.open(str(path), "wb") as wav:
            from array import array

            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(lf.DUAL_MONO_RATE)
            wav.writeframes(array("h", left).tobytes())
    assert lf._dual_mono_similarity(good_a, good_b) is not None  # the control: measurable and identical
    # malformed: not a WAV at all, and a WAV with the wrong format (stereo)
    broken = tmp_path / "broken.wav"
    broken.write_bytes(b"not a wav file")
    assert lf._dual_mono_similarity(good_a, broken) is None
    stereo = tmp_path / "stereo.wav"
    _synthetic_stereo(stereo, left, left)
    assert lf._dual_mono_similarity(good_a, stereo) is None
    # unequal lengths
    short = tmp_path / "short.wav"
    with wave.open(str(short), "wb") as wav:
        from array import array

        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(lf.DUAL_MONO_RATE)
        wav.writeframes(array("h", left[:-100]).tobytes())
    assert lf._dual_mono_similarity(good_a, short) is None
    # over the memory bound: shrink the bound below this clip's length
    monkeypatch.setattr(lf, "DUAL_MONO_MAX_MEASUREMENT_SECONDS", 0.5)
    assert lf._dual_mono_similarity(good_a, good_b) is None
    # none of these can authorise a collapse
    assert not lf._is_dual_mono(lf._dual_mono_similarity(good_a, broken))


def test_dual_mono_threshold_boundaries():
    correlation = lf.DUAL_MONO_MIN_CORRELATION
    difference = lf.DUAL_MONO_MAX_DIFFERENCE_RATIO
    assert lf._is_dual_mono((correlation, difference))
    assert lf._is_dual_mono((correlation + 0.000001, difference - 0.000001))
    assert not lf._is_dual_mono((correlation - 0.000001, difference))
    assert not lf._is_dual_mono((correlation, difference + 0.000001))
    assert not lf._is_dual_mono(None)


def test_mono_skips_dual_mono_measurement(tmp_path, monkeypatch):
    audio = tmp_path / "synthetic.wav"
    audio.write_bytes(b"synthetic")
    monkeypatch.setattr(lf, "resolve_fluid_cli", lambda: "synthetic-fluid")
    monkeypatch.setattr(lf, "_dual_mono_similarity", lambda *args: pytest.fail("mono measured"))
    monkeypatch.setattr(lf, "_run_json", lambda cli, args, output: (
        {"durationSeconds": 1, "segments": []} if args[0] == "process"
        else {"wordTimings": []}
    ))
    result = lf.transcribe_fluid(audio)
    assert result["silence"]
