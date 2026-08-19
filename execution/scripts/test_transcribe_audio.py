"""Tests for the proper-noun / stutter post-transcription corrector and its
coverage across every code path that returns a stored transcript: ElevenLabs
single-channel/diarized, ElevenLabs multichannel split/merge, OpenAI Whisper
(single-file and chunked), and the mic+remote two-file merge.

The proper-noun vocabulary is OPERATOR-SPECIFIC and is loaded at runtime from
env / file / a Neotoma entity — this public repo (and these public tests) ship
NO real operator vocabulary. Every test below injects a SYNTHETIC vocabulary
(fictional product names via TRANSCRIBE_PROPER_NOUNS) to exercise the machinery,
so nothing here leaks the operator's meeting terms.

Loads transcribe_audio as a module, stubbing the ``config`` import it expects
(execution/scripts has no config.py in this checkout) so the module can be
imported and exercised directly.
"""

import os
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPTS_DIR))

if "config" not in sys.modules:
    _stub_config = types.ModuleType("config")
    _stub_config.get_data_dir = lambda: Path(_SCRIPTS_DIR / "_test_data_dir")
    sys.modules["config"] = _stub_config

import transcribe_audio as ta  # noqa: E402


# A fictional vocabulary used by every test — no operator data. Two products
# ("Vexcorp", "Zolium"), each with a couple of made-up mishears.
_SYNTH_VOCAB = "Vexcorp=Vex Corp|Vexcorb|Vexcore, Zolium=Zolium's|Zolyum|the Zolium"


@pytest.fixture(autouse=True)
def _synthetic_vocab(monkeypatch):
    """Inject synthetic proper-noun vocabulary and never hit Neotoma in tests."""
    monkeypatch.setenv("TRANSCRIBE_PROPER_NOUNS", _SYNTH_VOCAB)
    monkeypatch.setenv("TRANSCRIBE_DISABLE_NEOTOMA_VOCAB", "1")
    monkeypatch.delenv("TRANSCRIBE_PROPER_NOUNS_FILE", raising=False)
    yield


# --------------------------------------------------------------------------
# Unit tests: _apply_proper_noun_corrections (synthetic vocabulary)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mishear,canonical",
    [
        ("Vexcorb", "Vexcorp"),
        ("Vex Corp", "Vexcorp"),
        ("Vexcore", "Vexcorp"),
        ("Zolyum", "Zolium"),
        ("the Zolium", "Zolium"),
    ],
)
def test_named_mishears_corrected(mishear, canonical):
    text = f"We discussed {mishear} in the call."
    corrected = ta._apply_proper_noun_corrections(text)
    assert canonical in corrected


def test_no_vocabulary_is_a_no_op(monkeypatch):
    # With no env/file/Neotoma vocabulary, the public default corrects nothing.
    monkeypatch.delenv("TRANSCRIBE_PROPER_NOUNS", raising=False)
    text = "Vexcorb and Zolyum shipped it."
    assert ta._apply_proper_noun_corrections(text) == text


def test_mixed_multi_name_text_all_corrected():
    text = "Vexcorb integrates with the Zolium, and Vex Corp's API calls it too."
    corrected = ta._apply_proper_noun_corrections(text)
    assert "Vexcorp" in corrected
    assert "Zolium" in corrected
    assert "Vexcorb" not in corrected
    assert "Zolyum" not in corrected


# --------------------------------------------------------------------------
# Idempotency / already-correct-input edge cases
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "canonical_text",
    [
        "Vexcorp shipped the release.",
        "Zolium stores the memory.",
    ],
)
def test_already_correct_text_is_byte_identical(canonical_text):
    assert ta._apply_proper_noun_corrections(canonical_text) == canonical_text


def test_case_insensitivity():
    assert ta._apply_proper_noun_corrections("vexcorb shipped it.").startswith(
        "Vexcorp"
    )


def test_word_boundary_no_false_positive_substring_rewrite():
    # A variant must not fire inside an unrelated longer token.
    text = "Supervexcorb9000 is unrelated."
    corrected = ta._apply_proper_noun_corrections(text)
    assert corrected == text


def test_empty_and_whitespace_input_unchanged():
    assert ta._apply_proper_noun_corrections("") == ""
    assert ta._apply_proper_noun_corrections("   ") == "   "


# --------------------------------------------------------------------------
# Stutter cleaner: real hyphenated words must survive; stutters must clean.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "real_word_text",
    [
        "re-read the doc",
        "re-record the call",
        "re-review it",
        "co-coordinate the launch",
        "pre-preview mode",
        "de-duplicate rows",
        "non-agentic setup",
        "co-founders met",
    ],
)
def test_stutter_cleaner_keeps_real_prefix_words(real_word_text):
    # A "<real-prefix>-<word>" where the root repeats the prefix (re-read) is a
    # legitimate hyphenated word, not a restart stutter — must not collapse.
    assert ta._clean_stutters(real_word_text) == real_word_text


@pytest.mark.parametrize(
    "stutter,expected_fragment",
    [
        ("b-before it happened", "before it happened"),
        ("topolog-topology diagram", "topology diagram"),
        ("in-interacting with it", "interacting with it"),
        ("s- you know the drill", "you know the drill"),
    ],
)
def test_stutter_cleaner_still_cleans_genuine_restarts(stutter, expected_fragment):
    assert expected_fragment in ta._clean_stutters(stutter)


def test_stutter_cleaner_collapses_immediate_repeats():
    assert ta._clean_stutters("the, the, the plan") == "the plan"
    assert ta._clean_stutters("I I I think so") == "I think so"


def test_stutter_cleaner_disabled_by_env(monkeypatch):
    monkeypatch.setenv("TRANSCRIBE_CLEAN_STUTTERS", "0")
    text = "b-before the, the meeting"
    assert ta._clean_stutters(text) == text


# --------------------------------------------------------------------------
# Vocabulary loading: env / file / merge
# --------------------------------------------------------------------------


def test_env_pairs_parse(monkeypatch):
    monkeypatch.setenv("TRANSCRIBE_PROPER_NOUNS", "Widgetco=Widget Co|Wigetco")
    pairs = ta._proper_nouns_from_env()
    assert ("Widgetco", "Widget Co|Wigetco") in pairs


def test_multi_entry_env_format_parses_both(monkeypatch):
    monkeypatch.setenv(
        "TRANSCRIBE_PROPER_NOUNS", "Widgetco=Widget Co|Wigetco, Acme=Ack Me"
    )
    names = {c for c, _ in ta._proper_nouns_from_env()}
    assert names == {"Widgetco", "Acme"}


@pytest.mark.parametrize("raw_value", ["", "NoEqualsSignHere", "   "])
def test_malformed_or_empty_env_yields_no_pairs(monkeypatch, raw_value):
    monkeypatch.setenv("TRANSCRIBE_PROPER_NOUNS", raw_value)
    assert ta._proper_nouns_from_env() == []


def test_file_source_object_shape(tmp_path, monkeypatch):
    import json

    p = tmp_path / "vocab.json"
    p.write_text(
        json.dumps(
            {"corrections": [{"canonical": "Fooco", "variants_regex": "Foo Co|Fuco"}]}
        )
    )
    monkeypatch.setenv("TRANSCRIBE_PROPER_NOUNS_FILE", str(p))
    assert ("Fooco", "Foo Co|Fuco") in ta._proper_nouns_from_file()
    assert "Fooco" in ta._apply_proper_noun_corrections("We use Fuco daily.")


def test_file_source_list_shape(tmp_path, monkeypatch):
    import json

    p = tmp_path / "vocab.json"
    p.write_text(json.dumps([["Barco", "Bar Co|Barko"]]))
    monkeypatch.setenv("TRANSCRIBE_PROPER_NOUNS_FILE", str(p))
    assert ("Barco", "Bar Co|Barko") in ta._proper_nouns_from_file()


def test_neotoma_vocab_disabled_returns_empty(monkeypatch):
    monkeypatch.setenv("TRANSCRIBE_DISABLE_NEOTOMA_VOCAB", "1")
    assert ta._proper_nouns_from_neotoma() == []


def test_coerce_correction_pairs_both_shapes():
    obj = {"corrections": [{"canonical": "A", "variants_regex": "a|aa"}]}
    assert ta._coerce_correction_pairs(obj) == [("A", "a|aa")]
    lst = [["B", "b|bb"], ["C", "c"]]
    assert ta._coerce_correction_pairs(lst) == [("B", "b|bb"), ("C", "c")]


# --------------------------------------------------------------------------
# Per-surface effect tests — assert the RETURNED transcription text is
# corrected, not merely that config loaded.
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
    ) as mock_retry, patch.object(ta, "get_audio_duration", return_value=12.0):
        mock_openai_cls.return_value = MagicMock()
        mock_retry.return_value = _FakeTranscript(
            "We discussed Vex Corp's roadmap with Zolyum."
        )
        result = ta.transcribe_audio_file(
            audio_path, language="en", use_diarization=False
        )

    assert "Vexcorp" in result["transcription_text"]
    assert "Zolium" in result["transcription_text"]
    assert "Zolyum" not in result["transcription_text"]


def test_whisper_chunked_returns_corrected_text(tmp_path, monkeypatch):
    audio_path = tmp_path / "long_call.m4a"
    audio_path.write_bytes(b"fake-audio-bytes")
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)

    chunk_paths = [tmp_path / "chunk0.m4a", tmp_path / "chunk1.m4a"]
    for c in chunk_paths:
        c.write_bytes(b"fake-chunk-bytes")

    chunk_texts = [
        "First half discusses the Zolium integration.",
        "Second half mentions Vex Corp's API and Vexcorb's release.",
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
        with patch.object(Path, "stat") as mock_stat:
            mock_stat.return_value = types.SimpleNamespace(st_size=30 * 1024 * 1024)
            result = ta.transcribe_audio_file(
                audio_path, language="en", use_diarization=False
            )

    combined = result["transcription_text"]
    assert "Zolium" in combined
    assert "Vexcorp" in combined
    assert "Vexcorb" not in combined


def test_transcribe_two_files_delegates_and_preserves_text(tmp_path):
    mic_path = tmp_path / "mic.wav"
    remote_path = tmp_path / "remote.wav"
    mic_path.write_bytes(b"fake-mic-bytes")
    remote_path.write_bytes(b"fake-remote-bytes")
    stereo_path = tmp_path / "combined_stereo.wav"
    stereo_path.write_bytes(b"fake-stereo-bytes")

    fake_el_result = {
        "transcription_text": "Zolium",  # already corrected by the ElevenLabs path
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
    ) as mock_el, patch.object(ta, "get_audio_duration", return_value=30.0):
        result = ta.transcribe_two_files(mic_path, remote_path, language="en")

    assert result["transcription_text"] == "Zolium"
    mock_el.assert_called_once()


def test_transcribe_two_files_fallback_concatenation_corrected(tmp_path, monkeypatch):
    mic_path = tmp_path / "mic.wav"
    remote_path = tmp_path / "remote.wav"
    mic_path.write_bytes(b"fake-mic-bytes")
    remote_path.write_bytes(b"fake-remote-bytes")
    stereo_path = tmp_path / "combined_stereo.wav"
    stereo_path.write_bytes(b"fake-stereo-bytes")

    body = {
        "transcripts": [
            {"text": "Vexcorb shipped it.", "language_code": "en", "words": []},
            {"text": "The Zolium too.", "language_code": "en", "words": []},
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

    assert "Vexcorp" in result["transcription_text"]
    assert "Vexcorb" not in result["transcription_text"]


# --------------------------------------------------------------------------
# Regression guards for the ElevenLabs single-channel and multichannel paths.
# --------------------------------------------------------------------------


def test_elevenlabs_single_channel_regression_guard():
    body = {
        "text": "We discussed Vexcorb and the Zolium.",
        "language_code": "en",
        "words": [],
    }
    text, _lang = ta._parse_elevenlabs_stt_response(body, "en")
    assert "Vexcorp" in text
    assert "Zolium" in text
    assert "Vexcorb" not in text


def test_elevenlabs_multichannel_regression_guard():
    body = {
        "transcripts": [
            {"text": "Vexcorb launched today.", "language_code": "en", "words": []},
            {"text": "Yes, Vexcorp did.", "language_code": "en", "words": []},
        ]
    }
    text, _lang = ta._parse_elevenlabs_stt_response(body, "en")
    assert "Vexcorp" in text
    assert "Vexcorb" not in text
