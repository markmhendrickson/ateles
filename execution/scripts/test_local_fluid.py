import json
import math
import random
import re
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


@pytest.fixture
def synthetic_ffmpeg(monkeypatch):
    """Hermetic stand-in for ffmpeg, so the tests do not need ffmpeg installed.

    Emulates only the two `-af` filters the module uses on 16-bit 16 kHz stereo PCM: per-channel extraction
    (`pan=mono|c0=cN`) and the equal-weight downmix (`pan=mono|c0=0.5*c0+0.5*c1`). Returns the list of calls.
    """
    from array import array

    monkeypatch.setattr(lf.shutil, "which", lambda name: "/test/ffmpeg")
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        source = Path(argv[argv.index("-i") + 1])
        expression = argv[argv.index("-af") + 1].split("c0=", 1)[1]
        with wave.open(str(source), "rb") as wav:
            assert (wav.getsampwidth(), wav.getframerate(), wav.getnchannels()) == (2, lf.DUAL_MONO_RATE, 2)
            samples = array("h")
            samples.frombytes(wav.readframes(wav.getnframes()))
        if expression in ("c0", "c1"):
            out = array("h", samples[int(expression[1])::2])
        elif expression == "0.5*c0+0.5*c1":
            out = array("h", (
                max(-32768, min(32767, int(round(0.5 * x + 0.5 * y))))
                for x, y in zip(samples[0::2], samples[1::2])
            ))
        else:
            raise AssertionError(f"unexpected filter {expression!r}")
        with wave.open(str(argv[-1]), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(lf.DUAL_MONO_RATE)
            wav.writeframes(out.tobytes())

    monkeypatch.setattr(lf.subprocess, "run", run)
    return calls


def _synthetic_stereo(path, left, right):
    """Write tiny 16 kHz PCM input; no recorded audio is used by these tests."""
    from array import array

    samples = array("h", (value for pair in zip(left, right) for value in pair))
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(lf.DUAL_MONO_RATE)
        wav.writeframes(samples.tobytes())


def _mono_wav(path, samples, rate=None):
    from array import array

    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate or lf.DUAL_MONO_RATE)
        wav.writeframes(array("h", samples).tobytes())
    return path


def _voice_like(length=5 * 16000):
    rng = random.Random(41)
    return [
        int((4000 * math.sin(2 * math.pi * 170 * n / 16000)
             + 1500 * math.sin(2 * math.pi * 430 * n / 16000)
             + rng.randrange(-500, 501)) * (0.5 + 0.5 * math.sin(2 * math.pi * 3 * n / 16000) ** 2))
        for n in range(length)
    ]


def _clip(value):
    return max(-32768, min(32767, value))


def _read_mono(path):
    from array import array

    with wave.open(str(path), "rb") as wav:
        assert (wav.getnchannels(), wav.getframerate()) == (1, lf.DUAL_MONO_RATE)
        samples = array("h")
        samples.frombytes(wav.readframes(wav.getnframes()))
    return list(samples)


def _rms(samples):
    return math.sqrt(sum(v * v for v in samples) / len(samples))


def _fake_model(transcribed, decoded=None):
    def model(cli, args, output):
        if args[0] == "process":
            return {"durationSeconds": 1, "segments": [
                {"startTimeSeconds": 0, "endTimeSeconds": 1, "speakerId": "Speaker_1"}]}
        source = Path(args[1])
        transcribed.append(source.name)
        if decoded is not None:
            decoded.append(_read_mono(source))
        return {"text": "Synthetic", "wordTimings": [{"word": "Synthetic", "startTime": 0.1, "endTime": 0.5}]}

    return model


_COLLAPSED = {"identical", "gain_offset", "two_microphones", "speech_then_silence"}


@pytest.mark.parametrize("variant", [
    "identical", "gain_offset", "two_microphones", "speech_then_silence",
    "different", "anti_phase", "one_silent", "one_second_on_one_channel", "independent_stretch",
    "too_little_speech", "silent", "short", "near_zero",
])
def test_dual_mono_audio_decision_and_transcription_effect(tmp_path, monkeypatch, synthetic_ffmpeg, variant):
    left = _voice_like(16000 if variant == "short" else 5 * 16000)
    rng = random.Random(57)
    right = left.copy()
    if variant == "gain_offset":
        right = [int(0.6 * value) for value in left]
    elif variant == "two_microphones":
        right = [_clip(int(0.6 * value) + rng.randrange(-1500, 1501)) for value in left]
    elif variant == "speech_then_silence":
        left = left + [0] * (10 * 16000)
        right = left.copy()
    elif variant == "different":
        right = [int(4300 * math.sin(2 * math.pi * 330 * n / 16000)) for n in range(len(left))]
    elif variant == "anti_phase":
        right = [-value for value in left]
    elif variant == "one_silent":
        right = [0] * len(left)
    elif variant == "one_second_on_one_channel":
        right = left[:2 * 16000] + [0] * 16000 + left[3 * 16000:]
    elif variant == "independent_stretch":
        right = left[:2 * 16000] + [
            int(4000 * math.sin(2 * math.pi * 330 * n / 16000)) for n in range(2 * 16000)
        ] + left[4 * 16000:]
    elif variant == "too_little_speech":
        left = left[:2 * 16000] + [0] * (3 * 16000)
        right = left.copy()
    elif variant == "silent":
        left = right = [0] * len(left)
    elif variant == "near_zero":
        left = right = [value // 100 for value in left]
    audio = tmp_path / "synthetic-stereo.wav"
    _synthetic_stereo(audio, left, right)
    monkeypatch.setattr(lf, "resolve_fluid_cli", lambda: "synthetic-fluid")
    transcribed, decoded = [], []
    monkeypatch.setattr(lf, "_run_json", _fake_model(transcribed, decoded))
    result = lf.transcribe_fluid(audio, channel_count=2)
    collapsed = variant in _COLLAPSED
    assert len(transcribed) == (1 if collapsed else 2)
    assert len(result["raw_response"]["tracks"]) == len(transcribed)
    if collapsed:
        # ONE transcription of the equal-weight downmix: nothing from either microphone is dropped
        assert transcribed == ["downmix.wav"]
        mixed = decoded[0]
        assert len(mixed) == len(left)
        assert all(abs(m - 0.5 * (x + y)) <= 1 for m, x, y in zip(mixed, left, right))
        assert result["transcription_text"].count("Synthetic") == 1
        header = result["transcription_text"].splitlines()[0]
        assert header.startswith("[Speakers: 1,")
        match = re.search(r"dual-mono, collapsed; correlation=(\d\.\d{6}); independent_share=(\d\.\d{6})\]", header)
        assert match, header
        # both stored measurements are present and are the values that authorised the collapse
        assert float(match.group(1)) >= lf.DUAL_MONO_MIN_CORRELATION
        assert float(match.group(2)) <= lf.DUAL_MONO_MAX_INDEPENDENT_SHARE
        assert "[Speaker_1]" in result["transcription_text"]
        assert "Channel_" not in result["transcription_text"]
    else:
        assert transcribed == ["channel-0.wav", "channel-1.wav"]
        assert not any(call[call.index("-af") + 1] == lf.DUAL_MONO_DOWNMIX_FILTER for call in synthetic_ffmpeg)
        assert "dual-mono, collapsed" not in result["transcription_text"]
        assert "[Channel_0/Speaker_1]" in result["transcription_text"]
        assert "[Channel_1/Speaker_1]" in result["transcription_text"]


@pytest.mark.usefixtures("synthetic_ffmpeg")
def test_gain_offset_pair_keeps_the_level_of_both_microphones(tmp_path, monkeypatch):
    # The two-microphone memo case: the same speech at different gains. The downmix must not cancel it.
    left = _voice_like()
    right = [int(0.6 * value) for value in left]
    audio = tmp_path / "synthetic-gain.wav"
    _synthetic_stereo(audio, left, right)
    monkeypatch.setattr(lf, "resolve_fluid_cli", lambda: "synthetic-fluid")
    transcribed, decoded = [], []
    monkeypatch.setattr(lf, "_run_json", _fake_model(transcribed, decoded))
    result = lf.transcribe_fluid(audio, channel_count=2)
    assert transcribed == ["downmix.wav"] and "dual-mono, collapsed" in result["transcription_text"]
    assert _rms(decoded[0]) > 0.7 * _rms(left)


@pytest.mark.usefixtures("synthetic_ffmpeg")
def test_a_lagged_pair_whose_sum_would_cancel_is_not_collapsed(tmp_path, monkeypatch):
    # A 500 Hz tone delayed by 1 ms is half a period out of phase: at zero lag (what a sum adds) it is
    # perfectly anti-correlated, so no downmix is made and both channels are kept.
    left = [int(6000 * math.sin(2 * math.pi * 500 * n / 16000)) for n in range(5 * 16000)]
    right = [0] * 16 + left[:-16]
    audio = tmp_path / "synthetic-lagged.wav"
    _synthetic_stereo(audio, left, right)
    monkeypatch.setattr(lf, "resolve_fluid_cli", lambda: "synthetic-fluid")
    transcribed = []
    monkeypatch.setattr(lf, "_run_json", _fake_model(transcribed))
    result = lf.transcribe_fluid(audio, channel_count=2)
    assert transcribed == ["channel-0.wav", "channel-1.wav"]
    assert "dual-mono" not in result["transcription_text"]


def test_a_downmix_that_cannot_be_made_keeps_both_channels(tmp_path, monkeypatch, synthetic_ffmpeg):
    left = _voice_like()
    audio = tmp_path / "synthetic-identical.wav"
    _synthetic_stereo(audio, left, left)
    monkeypatch.setattr(lf, "resolve_fluid_cli", lambda: "synthetic-fluid")
    extract = lf.subprocess.run

    def failing(argv, **kwargs):
        if argv[argv.index("-af") + 1] == lf.DUAL_MONO_DOWNMIX_FILTER:
            raise lf.subprocess.CalledProcessError(1, argv)
        return extract(argv, **kwargs)

    monkeypatch.setattr(lf.subprocess, "run", failing)
    transcribed = []
    monkeypatch.setattr(lf, "_run_json", _fake_model(transcribed))
    result = lf.transcribe_fluid(audio, channel_count=2)
    assert transcribed == ["channel-0.wav", "channel-1.wav"]
    assert "dual-mono" not in result["transcription_text"]
    assert "[Channel_0/Speaker_1]" in result["transcription_text"]


@pytest.mark.usefixtures("synthetic_ffmpeg")
def test_silent_identical_channels_keep_the_exact_headerless_silent_result(tmp_path, monkeypatch):
    # a collapsed clip with no words must stay headerless too: the result validator requires the exact silent text
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


@pytest.mark.usefixtures("synthetic_ffmpeg")
def test_collapsed_header_with_words_but_no_speaker_segments_uses_the_zero_speaker_wording(tmp_path, monkeypatch):
    left = _voice_like()
    audio = tmp_path / "synthetic-identical-no-segments.wav"
    _synthetic_stereo(audio, left, left)
    monkeypatch.setattr(lf, "resolve_fluid_cli", lambda: "synthetic-fluid")
    monkeypatch.setattr(lf, "_run_json", lambda cli, args, output: (
        {"durationSeconds": 1, "segments": []}
        if args[0] == "process"
        else {"text": "Synthetic", "wordTimings": [{"word": "Synthetic", "startTime": 0.1, "endTime": 0.5}]}))
    result = lf.transcribe_fluid(audio, channel_count=2)
    header = result["transcription_text"].splitlines()[0]
    assert header.startswith("[Speakers: no separate speakers found, detected locally by "), header
    assert "dual-mono, collapsed; correlation=" in header and "independent_share=" in header
    assert "[Speakers: 0," not in result["transcription_text"]
    # the same wording the shared helper gives every other zero-speaker outcome
    assert lf.diarization_header({"status": "ok", "speaker_count": 0}).startswith(
        "[Speakers: no separate speakers found, detected locally by ")


def test_dual_mono_similarity_is_inconclusive_for_malformed_unequal_short_and_silent(tmp_path):
    left = _voice_like()
    good_a = _mono_wav(tmp_path / "a.wav", left)
    good_b = _mono_wav(tmp_path / "b.wav", left)
    similarity = lf._dual_mono_similarity(good_a, good_b)  # the control: measurable, and identical
    assert similarity is not None and similarity[0] > 0.999999 and similarity[1] == 0.0
    broken = tmp_path / "broken.wav"
    broken.write_bytes(b"not a wav file")
    assert lf._dual_mono_similarity(good_a, broken) is None
    stereo = tmp_path / "stereo.wav"
    _synthetic_stereo(stereo, left, left)
    assert lf._dual_mono_similarity(good_a, stereo) is None
    slow = _mono_wav(tmp_path / "slow.wav", left, rate=8000)
    assert lf._dual_mono_similarity(good_a, slow) is None
    unequal = _mono_wav(tmp_path / "unequal.wav", left[:-100])
    assert lf._dual_mono_similarity(good_a, unequal) is None
    short = _mono_wav(tmp_path / "short.wav", left[:15999])
    assert lf._dual_mono_similarity(short, short) is None
    silent = _mono_wav(tmp_path / "silent.wav", [0] * len(left))
    assert lf._dual_mono_similarity(silent, silent) is None
    # none of these can authorise a collapse
    for pair in ((good_a, broken), (good_a, stereo), (good_a, slow), (good_a, unequal), (short, short), (silent, silent)):
        assert not lf._is_dual_mono(lf._dual_mono_similarity(*pair))


def test_silence_is_skipped_but_a_second_on_only_one_channel_is_not(tmp_path):
    speech = _voice_like()
    padded = speech + [0] * (10 * 16000)
    a, b = _mono_wav(tmp_path / "a.wav", padded), _mono_wav(tmp_path / "b.wav", padded)
    assert lf._is_dual_mono(lf._dual_mono_similarity(a, b))  # shared silence is nothing to compare
    gap = speech[:2 * 16000] + [0] * 16000 + speech[3 * 16000:]
    c = _mono_wav(tmp_path / "c.wav", gap)
    one_sided = lf._dual_mono_similarity(_mono_wav(tmp_path / "d.wav", speech), c)
    # one second of five heard on only one channel is independent audio: about a fifth of the energy
    assert one_sided is not None and lf.DUAL_MONO_MAX_INDEPENDENT_SHARE < one_sided[1] < 0.3
    assert not lf._is_dual_mono(one_sided)


def test_segment_starts_cover_the_whole_clip_including_the_end():
    size = lf.DUAL_MONO_SEGMENT_SAMPLES
    assert lf._segment_starts(size - 1) == []
    assert lf._segment_starts(5 * size) == [0, size, 2 * size, 3 * size, 4 * size]
    assert lf._segment_starts(5 * size + 5000) == [0, size, 2 * size, 3 * size, 4 * size, 4 * size + 5000]
    frames = 745 * size  # a long memo: bounded work, still spread over the whole clip
    starts = lf._segment_starts(frames)
    assert len(starts) <= lf.DUAL_MONO_MAX_SEGMENTS + 1
    assert starts[0] == 0 and starts[-1] == frames - size
    assert starts == sorted(set(starts)) and all(start + size <= frames for start in starts)
    assert max(b - a for a, b in zip(starts, starts[1:])) <= 3 * size  # no long unmeasured stretch


def _long_pair(tmp_path, independent_seconds):
    """400 seconds of shared voice-like audio whose last `independent_seconds` differ on the right channel."""
    one_second = _voice_like(lf.DUAL_MONO_SEGMENT_SAMPLES)
    other = [int(3000 * math.sin(2 * math.pi * 330 * n / 16000)) for n in range(lf.DUAL_MONO_SEGMENT_SAMPLES)]
    seconds = 400
    left = one_second * seconds
    right = one_second * (seconds - independent_seconds) + other * independent_seconds
    return _mono_wav(tmp_path / "a.wav", left), _mono_wav(tmp_path / "b.wav", right)


def test_a_long_clip_is_sampled_in_bounded_work_and_independent_audio_late_in_it_blocks_the_collapse(
    tmp_path, monkeypatch
):
    a, b = _long_pair(tmp_path, independent_seconds=80)  # the last fifth of the clip
    measured = []
    real = lf._read_segments
    monkeypatch.setattr(lf, "_read_segments", lambda path, starts: (measured.append(len(starts)), real(path, starts))[1])
    similarity = lf._dual_mono_similarity(a, b)
    assert max(measured) <= lf.DUAL_MONO_MAX_SEGMENTS + 1  # a 400 s clip is not read in full
    assert similarity is not None
    assert similarity[0] >= lf.DUAL_MONO_MIN_CORRELATION  # the whole-clip correlation alone would collapse it
    assert similarity[1] > lf.DUAL_MONO_MAX_INDEPENDENT_SHARE  # the sampled late stretch does not
    assert not lf._is_dual_mono(similarity)


def test_the_final_second_is_measured_but_one_stray_second_does_not_block_a_collapse(tmp_path):
    a, b = _long_pair(tmp_path, independent_seconds=1)
    similarity = lf._dual_mono_similarity(a, b)
    assert similarity is not None
    assert 0.0 < similarity[1] < lf.DUAL_MONO_MAX_INDEPENDENT_SHARE  # the very end was sampled and counted
    assert lf._is_dual_mono(similarity)  # the downmix keeps that second too, so nothing is lost


def test_dual_mono_threshold_boundaries():
    correlation = lf.DUAL_MONO_MIN_CORRELATION
    share = lf.DUAL_MONO_MAX_INDEPENDENT_SHARE
    assert lf._is_dual_mono((correlation, share))
    assert lf._is_dual_mono((correlation + 0.000001, share - 0.000001))
    assert not lf._is_dual_mono((correlation - 0.000001, share))
    assert not lf._is_dual_mono((correlation, share + 0.000001))
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
