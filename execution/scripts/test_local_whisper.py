"""Effect-level tests for local_whisper.py — the default (local) STT backend.

The contracts worth protecting here are all about what must NOT happen silently:
a misconfigured free backend must never become a paid one, and near-silent audio
must never become invented speech.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import local_whisper as lw  # noqa: E402


# --- Backend precedence -----------------------------------------------------


def test_local_is_the_default_with_no_env_at_all():
    assert lw.resolve_backend(env={}) == lw.BACKEND_LOCAL


def test_local_is_the_default_even_when_both_paid_keys_are_present():
    """The regression that matters: a present key must not select a paid path.

    Before this change RECORD_MEETING_DIARIZE defaulted to "1", so merely having
    an ElevenLabs key in .env routed every voice memo through a metered service.
    """
    env = {"ELEVENLABS_API_KEY": "k", "OPENAI_API_KEY": "k"}
    assert lw.resolve_backend(env=env) == lw.BACKEND_LOCAL


def test_explicit_argument_wins_over_everything():
    env = {"TRANSCRIBE_BACKEND": "local", "ELEVENLABS_API_KEY": "k"}
    assert lw.resolve_backend(explicit="openai", env=env) == lw.BACKEND_OPENAI


def test_env_var_selects_a_backend():
    assert (
        lw.resolve_backend(env={"TRANSCRIBE_BACKEND": "openai"}) == lw.BACKEND_OPENAI
    )


def test_env_var_is_case_and_space_insensitive():
    assert (
        lw.resolve_backend(env={"TRANSCRIBE_BACKEND": "  OpenAI "})
        == lw.BACKEND_OPENAI
    )


@pytest.mark.parametrize("bad", ["whisper", "eleven", "gpt-4o-transcribe", "local2"])
def test_unknown_backend_raises_rather_than_guessing(bad):
    with pytest.raises(ValueError, match="backend"):
        lw.resolve_backend(explicit=bad, env={})
    with pytest.raises(ValueError, match="backend"):
        lw.resolve_backend(env={"TRANSCRIBE_BACKEND": bad})


def test_diarization_request_selects_elevenlabs():
    """Diarization is the one thing local cannot do, so it outranks the default."""
    env = {"ELEVENLABS_API_KEY": "k"}
    assert (
        lw.resolve_backend(use_diarization=True, env=env) == lw.BACKEND_ELEVENLABS
    )


def test_record_meeting_diarize_1_selects_elevenlabs_when_key_present():
    env = {"ELEVENLABS_API_KEY": "k", "RECORD_MEETING_DIARIZE": "1"}
    assert lw.resolve_backend(env=env) == lw.BACKEND_ELEVENLABS


def test_record_meeting_diarize_1_without_a_key_stays_local():
    """No key means ElevenLabs cannot run; that is not a reason to pick OpenAI."""
    env = {"RECORD_MEETING_DIARIZE": "1"}
    assert lw.resolve_backend(env=env) == lw.BACKEND_LOCAL


def test_diarization_explicitly_declined_stays_local_not_openai():
    """--no-diarize used to mean "OpenAI". It now means "the free default"."""
    env = {"ELEVENLABS_API_KEY": "k", "RECORD_MEETING_DIARIZE": "1"}
    assert lw.resolve_backend(use_diarization=False, env=env) == lw.BACKEND_LOCAL


def test_openai_is_never_selected_implicitly():
    """Exhaustive: no combination of keys/diarization flags yields the metered path."""
    for el in ("", "k"):
        for oa in ("", "k"):
            for diar in (None, True, False):
                for rmd in (None, "0", "1"):
                    env = {"ELEVENLABS_API_KEY": el, "OPENAI_API_KEY": oa}
                    if rmd is not None:
                        env["RECORD_MEETING_DIARIZE"] = rmd
                    got = lw.resolve_backend(use_diarization=diar, env=env)
                    assert got != lw.BACKEND_OPENAI, (el, oa, diar, rmd, got)


# --- Model resolution -------------------------------------------------------


def test_model_env_override_is_used_when_the_file_exists(tmp_path):
    model = tmp_path / "ggml-custom.bin"
    model.write_bytes(b"x")
    assert lw.resolve_model_path(env={lw.MODEL_ENV_VAR: str(model)}) == model


def test_missing_model_override_raises_and_names_the_path(tmp_path):
    missing = tmp_path / "nope.bin"
    with pytest.raises(lw.LocalWhisperUnavailable) as exc:
        lw.resolve_model_path(env={lw.MODEL_ENV_VAR: str(missing)})
    assert str(missing) in str(exc.value)


def test_missing_model_never_suggests_a_paid_fallback(tmp_path, monkeypatch):
    """The planted negative, as a test: loud failure, and no paid escape hatch."""
    monkeypatch.setattr(lw, "MODEL_SEARCH_DIRS", (tmp_path / "empty",))
    with pytest.raises(lw.LocalWhisperUnavailable) as exc:
        lw.resolve_model_path(env={})
    msg = str(exc.value)
    # Names the expected path, and how to get the model.
    assert "ggml-large-v3-turbo.bin" in msg
    assert "curl" in msg
    assert str(tmp_path / "empty") in msg
    # And is explicit that it will not start billing instead.
    assert "NOT falling back" in msg


def test_model_search_takes_the_first_directory_that_has_it(tmp_path, monkeypatch):
    first, second = tmp_path / "a", tmp_path / "b"
    for d in (first, second):
        d.mkdir()
        (d / lw.DEFAULT_MODEL_NAME).write_bytes(b"x")
    monkeypatch.setattr(lw, "MODEL_SEARCH_DIRS", (first, second))
    assert lw.resolve_model_path(env={}) == first / lw.DEFAULT_MODEL_NAME


# --- Binary resolution ------------------------------------------------------


def test_missing_whisper_cli_names_the_install_command(monkeypatch):
    monkeypatch.setattr(lw.shutil, "which", lambda _c: None)
    with pytest.raises(lw.LocalWhisperUnavailable) as exc:
        lw.resolve_whisper_cli(env={})
    assert "brew install whisper-cpp" in str(exc.value)


def test_missing_ffmpeg_names_the_install_command_and_why_it_is_needed(monkeypatch):
    monkeypatch.setattr(lw.shutil, "which", lambda _c: None)
    with pytest.raises(lw.LocalWhisperUnavailable) as exc:
        lw.resolve_ffmpeg(env={})
    msg = str(exc.value)
    assert "brew install ffmpeg" in msg
    # ffmpeg is not optional: whisper-cli cannot read the .m4a inputs at all.
    assert ".m4a" in msg


# --- JSON parsing -----------------------------------------------------------


def _write_json(tmp_path: Path, segments, language=None) -> Path:
    payload = {"transcription": [{"text": t} for t in segments]}
    if language:
        payload["result"] = {"language": language}
    p = tmp_path / "out.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


def test_segments_are_joined_and_language_is_read(tmp_path):
    p = _write_json(tmp_path, [" Hello there.", " Second part."], language="en")
    text, lang = lw._parse_whisper_json(p)
    assert text == "Hello there. Second part."
    assert lang == "en"


def test_leading_subtitle_dash_is_stripped(tmp_path):
    """whisper.cpp emits caption-style speaker dashes; the speaker did not say one.

    Observed on 2 of the 23 memos in the 2026-09-12 batch.
    """
    p = _write_json(tmp_path, ["- 30 by eight failure."])
    text, _ = lw._parse_whisper_json(p)
    assert text == "30 by eight failure."


def test_a_genuine_leading_hyphen_is_preserved(tmp_path):
    """Only "dash space" is a caption artifact. "-5 degrees" is content."""
    p = _write_json(tmp_path, ["-5 degrees overnight."])
    text, _ = lw._parse_whisper_json(p)
    assert text == "-5 degrees overnight."


def test_mid_text_dashes_are_untouched(tmp_path):
    p = _write_json(tmp_path, ["Set a high-level goal - then revise it."])
    text, _ = lw._parse_whisper_json(p)
    assert text == "Set a high-level goal - then revise it."


def test_empty_segments_are_dropped(tmp_path):
    p = _write_json(tmp_path, ["  ", "Real words.", ""])
    text, _ = lw._parse_whisper_json(p)
    assert text == "Real words."


# --- Silence / no-speech handling ------------------------------------------


def _stub_pipeline(monkeypatch, tmp_path, *, rms, model_text="."):
    """Stub out binaries so the gate logic can be tested without whisper-cli."""
    model = tmp_path / lw.DEFAULT_MODEL_NAME
    model.write_bytes(b"x")
    monkeypatch.setattr(lw, "resolve_whisper_cli", lambda env=None: "/bin/true")
    monkeypatch.setattr(lw, "resolve_ffmpeg", lambda env=None: "/bin/true")
    monkeypatch.setattr(lw, "resolve_model_path", lambda env=None: model)
    monkeypatch.setattr(lw, "measure_sustained_rms_db", lambda *_a, **_k: rms)

    def fake_convert(audio_path, dest_dir, **_kw):
        wav = Path(dest_dir) / "x.16k.wav"
        wav.write_bytes(b"RIFF")
        return wav

    monkeypatch.setattr(lw, "convert_to_wav16k_mono", fake_convert)

    def fake_run(cmd, **_kw):
        # Emulate whisper-cli writing <output-file>.json
        out_stem = cmd[cmd.index("--output-file") + 1]
        Path(f"{out_stem}.json").write_text(
            json.dumps({"transcription": [{"text": model_text}]}), encoding="utf-8"
        )

        class R:
            returncode = 0
            stderr = ""

        return R()

    monkeypatch.setattr(lw.subprocess, "run", fake_run)


def test_below_threshold_audio_is_never_sent_to_the_model(monkeypatch, tmp_path):
    """The level gate: silence is marked, not transcribed — Whisper would invent."""
    _stub_pipeline(monkeypatch, tmp_path, rms=-70.0)
    called = []
    monkeypatch.setattr(
        lw.subprocess, "run", lambda *a, **k: called.append(a) or pytest.fail("ran")
    )
    audio = tmp_path / "quiet.m4a"
    audio.write_bytes(b"x")

    result = lw.transcribe_local(audio)

    assert result["silence"] is True
    assert result["transcription_text"].startswith("[NO SPEECH DETECTED")
    assert "-70.0" in result["transcription_text"]
    assert called == []


def test_a_bare_period_result_is_recorded_as_no_speech_not_as_text(
    monkeypatch, tmp_path
):
    """The real planted negative.

    The operator's accidental 0.75s capture measures -31.5 dB p95 — ABOVE the
    -50 dB gate and inside their own verified speech range, so no level
    threshold can catch it. whisper-cli returns "." for it. The result-side
    screen must turn that into an explicit marker, never store "." as content.
    """
    _stub_pipeline(monkeypatch, tmp_path, rms=-31.5, model_text=".")
    audio = tmp_path / "accident.m4a"
    audio.write_bytes(b"x")

    result = lw.transcribe_local(audio)

    assert result["silence"] is True
    assert result["transcription_text"].startswith("[NO SPEECH DETECTED")
    assert result["filtered_reason"] == "no_speech_content"
    # The raw output is preserved in the marker so the call stays auditable.
    assert "'.'" in result["transcription_text"]


def test_real_speech_above_the_gate_is_returned_verbatim(monkeypatch, tmp_path):
    _stub_pipeline(
        monkeypatch, tmp_path, rms=-30.0, model_text="The circuit breaker broke again."
    )
    audio = tmp_path / "memo.m4a"
    audio.write_bytes(b"x")

    result = lw.transcribe_local(audio)

    assert result["silence"] is False
    assert result["transcription_text"] == "The circuit breaker broke again."
    assert result["backend"] == lw.BACKEND_LOCAL
    assert result["rms_db"] == -30.0


def test_a_failed_rms_measurement_transcribes_anyway(monkeypatch, tmp_path):
    """A broken measurement must never silently discard real audio."""
    _stub_pipeline(monkeypatch, tmp_path, rms=None, model_text="Real content here.")
    audio = tmp_path / "memo.m4a"
    audio.write_bytes(b"x")

    result = lw.transcribe_local(audio)

    assert result["silence"] is False
    assert result["transcription_text"] == "Real content here."
    assert result["rms_db"] is None


def test_a_missing_file_raises_before_any_binary_is_resolved(tmp_path):
    with pytest.raises(FileNotFoundError):
        lw.transcribe_local(tmp_path / "absent.m4a")
