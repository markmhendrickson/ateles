"""Tests for Tyto's transcription routing decision.

The daemon asks the AUDIO whether diarization is warranted, not the API key:
a mic-pair or a multi-channel file means more than one speaker is plausible;
a mono single file is a voice memo and goes to local Whisper.

tyto.py loads .env and heavy daemon runtime deps at import time, so these tests
extract just the two routing functions from the module source and exec them in
an isolated namespace. That keeps the test hermetic — no operator environment,
no network, no daemon runtime — while still exercising the real code.

Fixtures are SYNTHETIC audio generated with ffmpeg at test time. No operator
recording is ever read here.

A missing ffmpeg SKIPS locally but FAILS when ATELES_REQUIRE_AUDIO_FIXTURES=1,
which CI sets — see lib/audio_fixture_support.py. Tyto's _audio_channel_count
is ffprobe-only, so without it there is no routing behaviour left to test and a
skip would report green for a subject that never ran (ateles#866).
"""

import ast
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lib.audio_fixture_support import (  # noqa: E402
    make_synthetic_audio,
    require_ffmpeg,
)

_TYTO_PY = Path(__file__).resolve().parent / "tyto.py"
_ROUTING_FUNCS = ("_audio_channel_count", "_should_diarize")


def _load_routing_namespace() -> dict:
    """Exec only the routing functions from tyto.py in a clean namespace."""
    source = _TYTO_PY.read_text()
    tree = ast.parse(source)
    wanted = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in _ROUTING_FUNCS
    ]
    missing = set(_ROUTING_FUNCS) - {n.name for n in wanted}
    assert not missing, f"tyto.py no longer defines: {sorted(missing)}"

    module = ast.Module(body=wanted, type_ignores=[])
    ns: dict = {
        "os": os,
        "shutil": shutil,
        "subprocess": subprocess,
        "Path": Path,
        "__builtins__": __builtins__,
    }
    exec(compile(module, str(_TYTO_PY), "exec"), ns)  # noqa: S102
    return ns


@pytest.fixture(scope="module")
def routing():
    return _load_routing_namespace()


@pytest.fixture
def clean_env(monkeypatch):
    for var in (
        "ELEVENLABS_API_KEY",
        "RECORD_MEETING_DIARIZE",
        "TRANSCRIBE_ENGINE_LEGACY_ROUTING",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test-key-not-a-real-credential")
    return monkeypatch


def _make_audio(path: Path, channels: int) -> Path:
    """Synthetic fixture, or fail/skip per ATELES_REQUIRE_AUDIO_FIXTURES.

    Unlike the previous bool-returning version, this never hands the caller a
    "could not build it" value to turn into a skip — the missing-tool decision
    is made once, in require_ffmpeg(), and CI makes it a failure.
    """
    require_ffmpeg()
    return make_synthetic_audio(path, channels)


def test_mono_single_file_does_not_diarize(routing, tmp_path, clean_env):
    """A mono voice memo goes to local transcription, not ElevenLabs."""
    audio = _make_audio(tmp_path / "memo.wav", 1)
    assert routing["_should_diarize"](audio, None) is False


def test_stereo_file_diarizes(routing, tmp_path, clean_env):
    """A multi-channel meeting capture keeps the diarization path."""
    audio = _make_audio(tmp_path / "meeting.wav", 2)
    assert routing["_should_diarize"](audio, None) is True


def test_mic_pair_diarizes_even_when_each_track_is_mono(routing, tmp_path, clean_env):
    """Two separate sources are two speakers by construction."""
    remote = _make_audio(tmp_path / "remote.wav", 1)
    mic = _make_audio(tmp_path / "mic.wav", 1)
    assert routing["_should_diarize"](remote, mic) is True


def test_missing_mic_file_is_not_a_pair(routing, tmp_path, clean_env):
    """A mic path that does not exist must not trigger the paid path."""
    remote = _make_audio(tmp_path / "remote.wav", 1)
    assert routing["_should_diarize"](remote, tmp_path / "absent-mic.wav") is False


def test_record_meeting_diarize_zero_still_forces_off(routing, tmp_path, clean_env):
    """The existing kill switch keeps working on multi-channel audio."""
    audio = _make_audio(tmp_path / "meeting.wav", 2)
    clean_env.setenv("RECORD_MEETING_DIARIZE", "0")
    assert routing["_should_diarize"](audio, None) is False


def test_no_elevenlabs_key_never_diarizes(routing, tmp_path, monkeypatch):
    """Without a key the diarization path is not reachable at all."""
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    monkeypatch.delenv("RECORD_MEETING_DIARIZE", raising=False)
    monkeypatch.delenv("TRANSCRIBE_ENGINE_LEGACY_ROUTING", raising=False)
    audio = _make_audio(tmp_path / "meeting.wav", 2)
    assert routing["_should_diarize"](audio, None) is False


def test_legacy_routing_restores_key_presence_behaviour(routing, tmp_path, clean_env):
    """The escape hatch brings back 'diarize whenever the key is set'."""
    audio = _make_audio(tmp_path / "memo.wav", 1)
    clean_env.setenv("TRANSCRIBE_ENGINE_LEGACY_ROUTING", "1")
    assert routing["_should_diarize"](audio, None) is True


def test_channel_count_returns_none_for_unprobeable_file(routing, tmp_path):
    """Unknown channel count is reported as None, never as mono."""
    bogus = tmp_path / "not-audio.wav"
    bogus.write_bytes(b"definitely not audio")
    assert routing["_audio_channel_count"](bogus) is None


def test_channel_count_reads_real_channels(routing, tmp_path):
    mono = _make_audio(tmp_path / "m.wav", 1)
    stereo = _make_audio(tmp_path / "s.wav", 2)
    assert routing["_audio_channel_count"](mono) == 1
    assert routing["_audio_channel_count"](stereo) == 2
