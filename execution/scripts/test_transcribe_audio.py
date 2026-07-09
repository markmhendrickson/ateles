"""Tests for the proper-noun/stutter post-transcription corrector (issue #212)
and its coverage across every code path that returns a stored transcript:
ElevenLabs single-channel/diarized, ElevenLabs multichannel split/merge,
OpenAI Whisper (single-file and chunked), and the mic+remote two-file merge.

Loads transcribe_audio as a module, stubbing the ``config`` import it expects
(execution/scripts has no config.py in this checkout — a pre-existing gap
unrelated to #212) so the module can be imported and exercised directly, the
same pattern test_loxia_review.py uses for loxia_review.
"""

import os
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPTS_DIR))

# transcribe_audio.py does `from scripts.config import get_data_dir` (falling
# back to `from config import get_data_dir`); neither module exists in this
# checkout. Stub a minimal `config` module before import so DATA_DIR resolves
# to a real Path without requiring the parent "personal" monorepo layout.
if "config" not in sys.modules:
    _stub_config = types.ModuleType("config")
    _stub_config.get_data_dir = lambda: Path(_SCRIPTS_DIR / "_test_data_dir")
    sys.modules["config"] = _stub_config

import transcribe_audio as ta  # noqa: E402


# --------------------------------------------------------------------------
# Unit tests: _apply_proper_noun_corrections
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mishear,canonical",
    [
        ("Pitague", "Bottega8"),
        ("Bottega eight", "Bottega8"),
        ("Bottega Aid", "Bottega8"),
        ("Ateli", "Ateles"),
        ("the Automa", "Neotoma"),
    ],
)
def test_named_mishears_corrected(mishear, canonical):
    text = f"We discussed {mishear} in the call."
    corrected = ta._apply_proper_noun_corrections(text)
    assert canonical in corrected
    assert mishear not in corrected


def test_hobbs_mishear_corrected_via_env(monkeypatch):
    # "Hobbes" is intentionally NOT in the shipped default map (real-word/surname
    # false-positive risk) — the issue's Hobbs case is the documented
    # TRANSCRIBE_PROPER_NOUNS extension path, not a built-in default.
    monkeypatch.setenv("TRANSCRIBE_PROPER_NOUNS", "Hobbs=Hobbes")
    corrected = ta._apply_proper_noun_corrections("Ask Hobbes about the schedule.")
    assert "Hobbs" in corrected
    assert "Hobbes" not in corrected


def test_prospect_crm_variant_from_map():
    # Assert against the literal tuple contents so this fails loudly if the
    # map changes, per QA's requirement not to assume the variant string.
    entry = next(
        (canon, variant_re)
        for canon, variant_re in ta._PROPER_NOUN_CORRECTIONS
        if canon == "Prospect CRM"
    )
    canon, variant_re = entry
    import re

    m = re.search(variant_re, "Prospect CRMs", re.IGNORECASE)
    assert m is not None
    corrected = ta._apply_proper_noun_corrections("Log it in Prospect CRMs.")
    assert corrected == "Log it in Prospect CRM."


def test_mixed_multi_name_text_all_corrected():
    text = "Pitague integrates with the Automa, and Ateli's API calls it too."
    corrected = ta._apply_proper_noun_corrections(text)
    assert "Bottega8" in corrected
    assert "Neotoma" in corrected
    assert "Ateles" in corrected
    assert "Pitague" not in corrected
    assert "Automa" not in corrected
    assert "Ateli" not in corrected


# --------------------------------------------------------------------------
# Idempotency / already-correct-input edge cases
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "canonical_text",
    [
        "Bottega8 shipped the release.",
        "Ateles is the swarm.",
        "Neotoma stores the memory.",
        "Prospect CRM tracks the deal.",
    ],
)
def test_already_correct_text_is_byte_identical(canonical_text):
    assert ta._apply_proper_noun_corrections(canonical_text) == canonical_text


def test_hobbs_already_correct_is_byte_identical_with_env(monkeypatch):
    monkeypatch.setenv("TRANSCRIBE_PROPER_NOUNS", "Hobbs=Hobbes")
    text = "Ask Hobbs about the schedule."
    assert ta._apply_proper_noun_corrections(text) == text


def test_case_insensitivity(monkeypatch):
    assert ta._apply_proper_noun_corrections("pitague shipped it.").startswith(
        "Bottega8"
    )
    monkeypatch.setenv("TRANSCRIBE_PROPER_NOUNS", "Hobbs=Hobbes")
    corrected = ta._apply_proper_noun_corrections("HOBBES asked a question.")
    assert corrected.startswith("Hobbs")


def test_word_boundary_no_false_positive_substring_rewrite():
    # "Bottega8" variant regex must not fire inside an unrelated longer token.
    text = "Superbottega8000 is unrelated."
    corrected = ta._apply_proper_noun_corrections(text)
    assert corrected == text


def test_empty_and_whitespace_input_unchanged():
    assert ta._apply_proper_noun_corrections("") == ""
    assert ta._apply_proper_noun_corrections("   ") == "   "


# --------------------------------------------------------------------------
# TRANSCRIBE_PROPER_NOUNS extensibility
# --------------------------------------------------------------------------


def test_new_pair_via_env_no_code_change(monkeypatch):
    monkeypatch.setenv("TRANSCRIBE_PROPER_NOUNS", "Widgetco=Widget Co|Wigetco")
    assert "Widgetco" not in dict(ta._PROPER_NOUN_CORRECTIONS)
    corrected = ta._apply_proper_noun_corrections("We are Wigetco now.")
    assert "Widgetco" in corrected


def test_multi_entry_env_format_parses_both(monkeypatch):
    monkeypatch.setenv(
        "TRANSCRIBE_PROPER_NOUNS", "Widgetco=Widget Co|Wigetco, Acme=Ack Me"
    )
    pairs = ta._extra_proper_nouns()
    canon_names = {c for c, _ in pairs}
    assert canon_names == {"Widgetco", "Acme"}


@pytest.mark.parametrize("raw_value", ["", "NoEqualsSignHere", "   "])
def test_malformed_or_empty_env_falls_back_to_builtin_only(monkeypatch, raw_value):
    monkeypatch.setenv("TRANSCRIBE_PROPER_NOUNS", raw_value)
    assert ta._extra_proper_nouns() == []
    # Built-in corrections still apply, no exception.
    corrected = ta._apply_proper_noun_corrections("Pitague shipped it.")
    assert "Bottega8" in corrected


def test_unset_env_falls_back_to_builtin_only(monkeypatch):
    monkeypatch.delenv("TRANSCRIBE_PROPER_NOUNS", raising=False)
    assert ta._extra_proper_nouns() == []


# --------------------------------------------------------------------------
# Per-surface effect tests — the confirmed coverage gap this PR closes.
# Each asserts the *returned* transcription_text/dict field is corrected,
# not merely that config loaded (fixed_means_behavior_verified_not_contract_accepted).
# --------------------------------------------------------------------------


class _FakeTranscript:
    def __init__(self, text, language="en"):
        self.text = text
        self.language = language


def test_whisper_single_file_returns_corrected_text(tmp_path, monkeypatch):
    audio_path = tmp_path / "call.m4a"
    audio_path.write_bytes(b"fake-audio-bytes")

    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)

    with patch.object(ta, "OpenAI") as mock_openai_cls, patch.object(
        ta, "transcribe_with_retry"
    ) as mock_retry, patch.object(
        ta, "get_audio_duration", return_value=12.0
    ):
        mock_openai_cls.return_value = MagicMock()
        mock_retry.return_value = _FakeTranscript(
            "We discussed Ateli's roadmap with Pitague."
        )

        result = ta.transcribe_audio_file(
            audio_path, language="en", use_diarization=False
        )

    assert "Ateles" in result["transcription_text"]
    assert "Bottega8" in result["transcription_text"]
    assert "Ateli" not in result["transcription_text"]
    assert "Pitague" not in result["transcription_text"]


def test_whisper_chunked_returns_corrected_text(tmp_path, monkeypatch):
    audio_path = tmp_path / "long_call.m4a"
    audio_path.write_bytes(b"fake-audio-bytes")

    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)

    chunk_paths = [tmp_path / "chunk0.m4a", tmp_path / "chunk1.m4a"]
    for c in chunk_paths:
        c.write_bytes(b"fake-chunk-bytes")

    chunk_texts = [
        "First half discusses the Automa integration.",
        "Second half mentions Ateli's API and Pitague's release.",
    ]

    with patch.object(ta, "OpenAI") as mock_openai_cls, patch.object(
        ta, "get_audio_duration", return_value=1200.0
    ), patch.object(
        ta, "split_audio_file", return_value=chunk_paths
    ), patch.object(
        ta,
        "transcribe_with_retry",
        side_effect=[_FakeTranscript(t) for t in chunk_texts],
    ):
        mock_openai_cls.return_value = MagicMock()
        # Force the chunked branch regardless of the fake file's actual size.
        with patch.object(Path, "stat") as mock_stat:
            mock_stat.return_value = types.SimpleNamespace(
                st_size=30 * 1024 * 1024
            )
            result = ta.transcribe_audio_file(
                audio_path, language="en", use_diarization=False
            )

    combined = result["transcription_text"]
    assert "Neotoma" in combined
    assert "Ateles" in combined
    assert "Bottega8" in combined
    assert "Automa" not in combined
    assert "Ateli" not in combined
    assert "Pitague" not in combined


def test_transcribe_two_files_returns_corrected_text(tmp_path, monkeypatch):
    mic_path = tmp_path / "mic.wav"
    remote_path = tmp_path / "remote.wav"
    mic_path.write_bytes(b"fake-mic-bytes")
    remote_path.write_bytes(b"fake-remote-bytes")

    stereo_path = tmp_path / "combined_stereo.wav"
    stereo_path.write_bytes(b"fake-stereo-bytes")

    fake_el_result = {
        "transcription_text": "Neotoma",  # already corrected by the ElevenLabs path
        "language": "en",
        "audio_duration_seconds": 30.0,
        "raw_response": {"transcripts": []},
    }

    with patch.object(
        ta, "_combine_tracks_to_stereo", return_value=(stereo_path, None)
    ), patch.object(
        ta,
        "transcribe_with_elevenlabs_speech_to_text",
        return_value=dict(fake_el_result),
    ) as mock_el, patch.object(
        ta, "get_audio_duration", return_value=30.0
    ):
        result = ta.transcribe_two_files(mic_path, remote_path, language="en")

    # transcribe_two_files delegates entirely to
    # transcribe_with_elevenlabs_speech_to_text, which always returns through
    # an already-corrected path (_parse_elevenlabs_stt_response or the
    # multichannel merge) — assert the merged output carries that correction
    # through untouched, and that the ElevenLabs call actually happened once.
    assert result["transcription_text"] == "Neotoma"
    mock_el.assert_called_once()


def test_transcribe_two_files_fallback_concatenation_corrected(tmp_path):
    # Guards the case where the underlying ElevenLabs call itself already
    # applied correction inside a fallback (non-chronological) concatenation
    # branch of _parse_elevenlabs_stt_response_raw — mishears must not survive
    # even in that fallback shape.
    mic_path = tmp_path / "mic.wav"
    remote_path = tmp_path / "remote.wav"
    mic_path.write_bytes(b"fake-mic-bytes")
    remote_path.write_bytes(b"fake-remote-bytes")
    stereo_path = tmp_path / "combined_stereo.wav"
    stereo_path.write_bytes(b"fake-stereo-bytes")

    body = {
        "transcripts": [
            {"text": "Pitague shipped it.", "language_code": "en", "words": []},
            {"text": "Ask Hobbes.", "language_code": "en", "words": []},
        ]
    }

    with patch.object(
        ta, "_combine_tracks_to_stereo", return_value=(stereo_path, None)
    ), patch.object(ta, "get_audio_duration", return_value=30.0), patch.object(
        ta, "_post_elevenlabs_stt_raw", return_value=body
    ), patch.object(
        ta, "prepare_elevenlabs_upload_file", return_value=(stereo_path, None)
    ), patch.object(
        ta, "get_audio_channel_count", return_value=2
    ), patch.dict(
        os.environ, {"ELEVENLABS_API_KEY": "fake-key"}
    ):
        result = ta.transcribe_two_files(mic_path, remote_path, language="en")

    assert "Bottega8" in result["transcription_text"]
    assert "Pitague" not in result["transcription_text"]


# --------------------------------------------------------------------------
# Regression guards — ElevenLabs single-channel and multichannel already
# worked before this PR; these protect against the hook being accidentally
# removed later.
# --------------------------------------------------------------------------


def test_elevenlabs_single_channel_regression_guard():
    body = {
        "text": "We discussed Ateli and the Automa.",
        "language_code": "en",
        "words": [],
    }
    text, _lang = ta._parse_elevenlabs_stt_response(body, "en")
    assert "Ateles" in text
    assert "Neotoma" in text
    assert "Ateli" not in text
    assert "Automa" not in text


def test_elevenlabs_multichannel_regression_guard():
    body = {
        "transcripts": [
            {"text": "Pitague launched today.", "language_code": "en", "words": []},
            {"text": "Yes, Bottega8 did.", "language_code": "en", "words": []},
        ]
    }
    text, _lang = ta._parse_elevenlabs_stt_response(body, "en")
    assert "Bottega8" in text
    assert "Pitague" not in text
