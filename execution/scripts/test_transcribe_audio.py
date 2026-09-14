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

import json
import os
import subprocess
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPTS_DIR))

os.environ.setdefault("DATA_DIR", str(_SCRIPTS_DIR / "_test_data_dir"))

import transcribe_audio as ta  # noqa: E402


def test_neotoma_store_uses_explicit_cli_runtime(tmp_path, monkeypatch):
    script = tmp_path / "bootstrap.js"
    script.write_text("// synthetic runtime")
    monkeypatch.setenv("NEOTOMA_CLI_SCRIPT", str(script))
    monkeypatch.setenv("NODE_BIN", "/fixture/node")
    monkeypatch.setattr(ta.shutil, "which", lambda value: value if value == "/fixture/node" else None)
    assert ta._neotoma_cli_available()
    argv = ta._neotoma_prod_cli_argv(["store"])
    assert argv[:2] == ["/fixture/node", str(script)]
    assert argv[-1] == "store"


def test_missing_explicit_cli_does_not_fall_back_to_global(monkeypatch):
    monkeypatch.setenv("NEOTOMA_CLI_SCRIPT", "/missing/bootstrap.js")
    monkeypatch.setattr(ta.shutil, "which", lambda value: "/fixture/" + value)
    with patch.object(ta.subprocess, "run") as run:
        assert ta._neotoma_cli_json(["entities", "list"]) is None
    run.assert_not_called()


# A fictional vocabulary used by every test — no operator data. Two products
# ("Vexcorp", "Zolium"), each with a couple of made-up mishears.
_SYNTH_VOCAB = "Vexcorp=Vex Corp|Vexcorb|Vexcore, Zolium=Zolium's|Zolyum|the Zolium"


def test_ffprobe_reads_channel_count_for_non_wav_capture(tmp_path):
    """The audio-driven router can classify the M4A/MP4 files Tyto receives."""
    capture = tmp_path / "synthetic.m4a"
    capture.write_bytes(b"not-real-audio")
    completed = types.SimpleNamespace(returncode=0, stdout="2\n", stderr="")

    with patch.object(ta.shutil, "which", return_value="/usr/bin/ffprobe"), patch.object(
        ta.subprocess, "run", return_value=completed
    ) as run:
        assert ta._ffprobe_channel_count(capture) == 2

    command = run.call_args.args[0]
    assert command[-1] == str(capture)
    assert "stream=channels" in command


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
    # The Whisper path is metered and now requires an explicit key before
    # it will construct a client; OpenAI itself is mocked below.
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-a-real-credential")

    with patch.object(ta, "OpenAI") as mock_openai_cls, patch.object(
        ta, "transcribe_with_retry"
    ) as mock_retry, patch.object(ta, "get_audio_duration", return_value=12.0):
        mock_openai_cls.return_value = MagicMock()
        mock_retry.return_value = _FakeTranscript(
            "We discussed Vex Corp's roadmap with Zolyum."
        )
        # `use_diarization=False` used to imply the OpenAI path. It now means
        # only "no ElevenLabs", and the default is the free local backend — so
        # the metered path must be named explicitly to be exercised.
        result = ta.transcribe_audio_file(
            audio_path, language="en", backend="openai"
        )

    assert "Vexcorp" in result["transcription_text"]
    assert "Zolium" in result["transcription_text"]
    assert "Zolyum" not in result["transcription_text"]


def test_whisper_chunked_returns_corrected_text(tmp_path, monkeypatch):
    audio_path = tmp_path / "long_call.m4a"
    audio_path.write_bytes(b"fake-audio-bytes")
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    # The Whisper path is metered and now requires an explicit key before
    # it will construct a client; OpenAI itself is mocked below.
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-a-real-credential")

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
            # Names the metered backend explicitly: the default is now local.
            result = ta.transcribe_audio_file(
                audio_path, language="en", backend="openai"
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


# ---------------------------------------------------------------------------
# Content-addressed transcription idempotency (ateles#625)
#
# The keys used to be sha256(absolute_path). Moving or renaming a recording
# therefore changed its key, so the file silently re-transcribed and re-uploaded:
# duplicate entities plus real API spend, with no error raised. These tests pin
# the property that actually matters — the key follows the BYTES, not the path.
# ---------------------------------------------------------------------------


def _write_audio(path: Path, payload: bytes = b"RIFFfake-audio-bytes-0123456789") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def test_idempotency_key_survives_a_move(tmp_path):
    """The same bytes at a new path must produce the same key.

    This is the exact migration case: recordings move to a staging dir on their
    way to object storage. Under path-keying this assertion fails and the file
    re-transcribes.
    """
    original = _write_audio(tmp_path / "here" / "meeting.m4a")
    key_before = ta._transcription_idempotency_key(original)

    moved = tmp_path / "there" / "renamed-by-the-migration.m4a"
    moved.parent.mkdir(parents=True, exist_ok=True)
    original.rename(moved)

    assert ta._transcription_idempotency_key(moved) == key_before


def test_file_idempotency_key_survives_a_move(tmp_path):
    """The audio-upload key must be content-addressed too.

    A path-keyed file key re-uploads the same bytes under a second source id.
    """
    original = _write_audio(tmp_path / "a" / "rec.m4a")
    key_before = ta._transcription_file_idempotency_key(original)

    moved = tmp_path / "b" / "rec.m4a"
    moved.parent.mkdir(parents=True, exist_ok=True)
    original.rename(moved)

    assert ta._transcription_file_idempotency_key(moved) == key_before


def test_different_content_gets_a_different_key(tmp_path):
    """Content-addressing must still separate genuinely different recordings."""
    one = _write_audio(tmp_path / "one.m4a", b"first recording bytes")
    two = _write_audio(tmp_path / "two.m4a", b"second recording bytes")

    assert ta._transcription_idempotency_key(one) != ta._transcription_idempotency_key(two)


def test_same_content_at_two_paths_shares_a_key(tmp_path):
    """A copy is the same recording; it must not transcribe twice."""
    payload = b"identical audio payload"
    one = _write_audio(tmp_path / "x" / "rec.m4a", payload)
    two = _write_audio(tmp_path / "y" / "different-name.m4a", payload)

    assert ta._transcription_idempotency_key(one) == ta._transcription_idempotency_key(two)


def test_key_is_stable_across_repeated_calls(tmp_path):
    audio = _write_audio(tmp_path / "rec.m4a")
    assert ta._transcription_idempotency_key(audio) == ta._transcription_idempotency_key(audio)


def test_missing_file_falls_back_to_path_keying(tmp_path):
    """An unreadable file must still yield a key rather than raising.

    Hashing needs bytes; when they are not there, degrade to the old path key
    rather than crashing a transcription run.
    """
    missing = tmp_path / "not-here.m4a"
    key = ta._transcription_idempotency_key(missing)
    assert key.startswith("transcription-audio-")


def test_is_already_transcribed_looks_up_by_content_hash(tmp_path, monkeypatch):
    """The dedupe lookup must query the content hash, not the absolute path.

    Keying the WRITE on content while the READ still asks for the old path means
    every moved file looks new, re-transcribes, and only then collides — which is
    the silent duplicate-spend path this fix exists to close.
    """
    audio = _write_audio(tmp_path / "deep" / "rec.m4a")
    seen = {}

    def fake_cli_json(args):
        seen["args"] = args
        return {"entities": [{"entity_id": "ent_x"}]}

    monkeypatch.setattr(ta, "_neotoma_cli_json", fake_cli_json)
    assert ta.is_already_transcribed(audio) is True

    args = seen["args"]
    identifier = args[args.index("--identifier") + 1]
    by_field = args[args.index("--by") + 1]

    assert identifier == ta._audio_content_hash(audio)
    assert by_field == "audio_content_sha256"
    assert str(audio.resolve()) not in args


def test_is_already_transcribed_finds_a_moved_recording(tmp_path, monkeypatch):
    """End-to-end of the defect: transcribe at path A, move to B, must dedupe.

    The stub stands in for Neotoma, indexing stored recordings by the identifier
    the writer used. Under path-keying the lookup for B misses and the file
    re-transcribes; under content-addressing it hits.
    """
    audio = _write_audio(tmp_path / "orig" / "session.m4a")
    stored = {ta._transcription_idempotency_key(audio): True}

    def fake_cli_json(args):
        identifier = args[args.index("--identifier") + 1]
        key = f"transcription-audio-{identifier}"
        return {"entities": [{"entity_id": "ent_x"}] if key in stored else []}

    monkeypatch.setattr(ta, "_neotoma_cli_json", fake_cli_json)

    moved = tmp_path / "archive" / "2026-09-01-session.m4a"
    moved.parent.mkdir(parents=True, exist_ok=True)
    audio.rename(moved)

    assert ta.is_already_transcribed(moved) is True


def test_stored_entity_records_the_content_hash(tmp_path, monkeypatch):
    """The hash must be persisted, or the lookup has nothing to match against."""
    audio = _write_audio(tmp_path / "rec.m4a")
    captured = {}

    class _Proc:
        returncode = 0
        stdout = json.dumps(
            {"structured": {"entities": [{"entity_id": "ent_stored", "entity_type": "transcription"}]}}
        )
        stderr = ""

    def fake_run(cmd, *a, **kw):
        for i, tok in enumerate(cmd):
            if tok == "--file":
                captured["entities"] = json.loads(Path(cmd[i + 1]).read_text())
        return _Proc()

    monkeypatch.setattr(ta.subprocess, "run", fake_run)
    monkeypatch.setattr(ta, "_neotoma_prod_cli_argv", lambda args: ["neotoma", *args])
    monkeypatch.setattr(ta.shutil, "which", lambda name: "/usr/bin/neotoma")
    monkeypatch.setattr(ta, "_neotoma_auth_preflight", lambda: (True, "ok"))
    monkeypatch.setattr(ta, "_write_transcript_sidecars", lambda *a, **k: None)

    ta.save_transcription(
        audio,
        {
            "transcription_text": "hello",
            "language": "en",
            "audio_duration_seconds": 1.0,
            "file_size_bytes": 10,
        },
        attach_audio_file=False,
    )

    entity = captured["entities"][0]
    assert entity["audio_content_sha256"] == ta._audio_content_hash(audio)


def test_stored_entity_records_transcription_engine_and_model(tmp_path, monkeypatch):
    """Stored provenance must identify whether audio stayed local or left-device."""
    audio = _write_audio(tmp_path / "rec.m4a")
    captured = {}

    class _Proc:
        returncode = 0
        stdout = json.dumps(
            {"structured": {"entities": [{"entity_id": "ent_stored", "entity_type": "transcription"}]}}
        )
        stderr = ""

    def fake_run(cmd, *args, **kwargs):
        for i, token in enumerate(cmd):
            if token == "--file":
                captured["entities"] = json.loads(Path(cmd[i + 1]).read_text())
        return _Proc()

    monkeypatch.setattr(ta.subprocess, "run", fake_run)
    monkeypatch.setattr(ta, "_neotoma_prod_cli_argv", lambda args: ["neotoma", *args])
    monkeypatch.setattr(ta.shutil, "which", lambda name: "/usr/bin/neotoma")
    monkeypatch.setattr(ta, "_neotoma_auth_preflight", lambda: (True, "ok"))

    ta.save_transcription(
        audio,
        {
            "transcription_text": "hello",
            "language": "en",
            "transcription_engine": "local_whisper_cpp",
            "transcription_model": "ggml-test.bin",
        },
        attach_audio_file=False,
    )

    entity = captured["entities"][0]
    assert entity["transcription_engine"] == "local_whisper_cpp"
    assert entity["transcription_model"] == "ggml-test.bin"


def test_record_meeting_audio_imports_in_clean_checkout(tmp_path):
    """The recorder must not depend on the gitignored scripts/config.py."""
    script = Path(__file__).with_name("record_meeting_audio.py")
    code = """
import importlib.util, sys, types
numpy = types.ModuleType('numpy')
numpy.ndarray = object
numpy.int16 = object()
sys.modules['numpy'] = numpy
sounddevice = types.ModuleType('sounddevice')
sounddevice.PortAudioError = RuntimeError
sys.modules['sounddevice'] = sounddevice
spec = importlib.util.spec_from_file_location('record_meeting_audio_clean', sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
print(module.DATA_DIR)
"""
    env = {**os.environ, "DATA_DIR": str(tmp_path / "data")}
    env.pop("PYTHONPATH", None)
    proc = subprocess.run(
        [sys.executable, "-c", code, str(script)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == str(tmp_path / "data")


# --- Backend routing: the free local path is the default ---------------------


def test_elevenlabs_without_a_key_raises_rather_than_billing_openai(
    tmp_path, monkeypatch
):
    """Diarization requested but unusable must not silently become a paid call.

    Falling through here would spend money on the metered OpenAI path AND drop
    the diarization the caller asked for — a meeting transcript with every
    speaker merged into one voice looks fine and is wrong.
    """
    import transcribe_audio as ta

    audio = tmp_path / "memo.wav"
    audio.write_bytes(b"RIFF0000WAVE")
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-decoy-must-not-be-used")
    monkeypatch.setattr(ta, "get_audio_duration", lambda _p: 5.0)

    with pytest.raises(RuntimeError, match="ELEVENLABS_API_KEY"):
        ta.transcribe_audio_file(audio, use_diarization=True)


def test_default_routing_uses_the_local_backend(tmp_path, monkeypatch):
    """With both paid keys present, an ordinary memo still goes local (free)."""
    import transcribe_audio as ta

    audio = tmp_path / "memo.wav"
    audio.write_bytes(b"RIFF0000WAVE")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "k")
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.delenv("RECORD_MEETING_DIARIZE", raising=False)
    monkeypatch.setattr(ta, "get_audio_duration", lambda _p: 5.0)

    seen = {}

    def fake_local(path, **kwargs):
        seen["path"] = path
        return {
            "transcription_text": "Local text.",
            "language": "en",
            "backend": "local",
            "transcription_engine": "local_whisper_cpp",
            "transcription_model": "ggml-test.bin",
            "rms_db": -30.0,
            "silence": False,
        }

    monkeypatch.setattr(ta, "transcribe_local", fake_local)

    result = ta.transcribe_audio_file(audio)

    assert result["backend"] == "local"
    assert result["transcription_engine"] == "local_whisper_cpp"
    assert result["transcription_model"] == "ggml-test.bin"
    assert result["transcription_text"] == "Local text."
    assert seen["path"] == audio


def test_m4a_ffprobe_channel_count_drives_public_backend_routing(tmp_path, monkeypatch):
    """A real public-call path must use ffprobe for compressed multichannel audio."""
    audio = tmp_path / "meeting.m4a"
    audio.write_bytes(b"not-real-audio")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "configured")
    monkeypatch.delenv("RECORD_MEETING_DIARIZE", raising=False)
    monkeypatch.setattr(ta, "get_audio_duration", lambda _path: 5.0)
    completed = types.SimpleNamespace(returncode=0, stdout="2\n", stderr="")
    monkeypatch.setattr(ta.shutil, "which", lambda name: "/usr/bin/ffprobe")
    monkeypatch.setattr(ta.subprocess, "run", lambda *args, **kwargs: completed)

    def fake_elevenlabs(path, **kwargs):
        return {
            "transcription_text": "Two speakers.",
            "language": "en",
            "audio_duration_seconds": 5.0,
            "file_size_bytes": path.stat().st_size,
            "transcription_engine": "elevenlabs_stt",
            "transcription_model": "scribe_v2",
        }

    monkeypatch.setattr(ta, "transcribe_with_elevenlabs_speech_to_text", fake_elevenlabs)

    result = ta.transcribe_audio_file(audio)

    assert result["transcription_engine"] == "elevenlabs_stt"


def test_no_speech_marker_is_not_run_through_stutter_cleanup(tmp_path, monkeypatch):
    """The marker is prose we wrote, not a transcript; postprocessing would maul it."""
    import transcribe_audio as ta

    audio = tmp_path / "quiet.wav"
    audio.write_bytes(b"RIFF0000WAVE")
    monkeypatch.setattr(ta, "get_audio_duration", lambda _p: 0.75)

    marker = "[NO SPEECH DETECTED — raw output was '.'. No content.]"
    monkeypatch.setattr(
        ta,
        "transcribe_local",
        lambda path, **kw: {
            "transcription_text": marker,
            "language": "en",
            "backend": "local",
            "rms_db": -31.5,
            "silence": True,
        },
    )

    result = ta.transcribe_audio_file(audio)

    assert result["transcription_text"] == marker
    assert result["silence"] is True
