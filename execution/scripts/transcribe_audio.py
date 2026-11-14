#!/usr/bin/env python3
"""
Audio Transcription Script

Transcribes audio with LOCAL whisper-cli by default (free, on-device, no API
key). FluidAudio performs local diarization and timestamped ASR when speaker
separation is requested. ElevenLabs and the METERED OpenAI Whisper API run
only on an explicit opt-in (``--backend openai`` / ``TRANSCRIBE_BACKEND``); it
is never a silent fallback. Persists each result as a Neotoma ``transcription`` entity with the WAV
attached (``neotoma store`` combined file + entities). Optional: after store,
creates ``REFERS_TO`` edges from the new ``transcription`` to ``contact`` and/or
``feedback_analysis`` entities (CLI flags, env vars, or ``<stem>_neotoma_relations.json``
next to the WAV — see ``save_transcription`` / ``record_meeting`` skill).

Usage:
    python transcribe_audio.py <audio_file_path> [--language <language_code>]
    python transcribe_audio.py file.wav                # local whisper-cli (default)
    python transcribe_audio.py file.wav --diarize      # local FluidAudio, speaker labels
    python transcribe_audio.py file.wav --backend openai  # METERED OpenAI API

Examples:
    python transcribe_audio.py data/imports/audio/recording.wav
    python transcribe_audio.py data/imports/audio/recording.wav --language en
"""

import argparse
import contextlib
import errno
import hashlib
import io
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import date
from pathlib import Path

import requests
from openai import OpenAI, RateLimitError

# Add project root to path
PROJECT_ROOT = Path(
    __file__
).parent.parent.parent  # execution/scripts -> execution -> personal (repo root)
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(
    0, str(PROJECT_ROOT / "execution" / "scripts")
)  # Add scripts directory to path

from lib.daemon_runtime import neotoma_store_guard  # noqa: E402
from lib.daemon_runtime.neotoma_store_guard import StoreDeferred  # noqa: E402
from lib.daemon_runtime.pinned_cli_identity import (  # noqa: E402
    PIN_ENV as _PIN_ENV,
    cli_isolation_prefix,
    pinned_child_problem,
)
from lib.daemon_runtime import shared_env  # noqa: E402

# Settings come from ONE host-level file (~/.config/neotoma/.env, or
# ATELES_DOTENV_FILE), never a per-checkout .env, so a git worktree or runtime
# checkout needs no hand-made files. lib.daemon_runtime already loaded it at
# import; this call makes the script's own contract explicit and loud: an
# unreadable or malformed file stops the run naming the file and line.
# Missing settings are named where they are needed (require_settings).
if shared_env.should_load():
    try:
        shared_env.load_shared_env()
    except shared_env.SharedEnvError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

from ateles.runtime_paths import get_data_dir, is_cloud_evicted  # noqa: E402
from local_fluid import BACKEND_FLUID, transcribe_fluid, format_events  # noqa: E402
from transcript_utterances import group_utterances  # noqa: E402

try:
    from scripts.local_whisper import (
        BACKEND_ELEVENLABS,
        BACKEND_LOCAL,
        BACKEND_OPENAI,
        DEFAULT_MODEL_NAME,
        TRANSCRIPTION_ENGINE_LOCAL,
        VALID_BACKENDS,
        resolve_backend,
        resolve_model_path as _resolve_local_model_path,
        transcribe_local,
    )
except ImportError:  # pragma: no cover - path-dependent import
    from local_whisper import (  # type: ignore[no-redef]
        BACKEND_ELEVENLABS,
        BACKEND_LOCAL,
        BACKEND_OPENAI,
        DEFAULT_MODEL_NAME,
        TRANSCRIPTION_ENGINE_LOCAL,
        VALID_BACKENDS,
        resolve_backend,
        resolve_model_path as _resolve_local_model_path,
        transcribe_local,
    )

# Configuration
DATA_DIR = get_data_dir()
IMPORTS_DIR = DATA_DIR / "imports"

# Audio file extensions to process
AUDIO_EXTENSIONS = {
    ".wav",
    ".mp3",
    ".m4a",
    ".ogg",
    ".flac",
    ".aac",
    ".wma",
    ".mp4",
    ".webm",
}

# Rate limiting configuration
RATE_LIMIT_DELAY_SECONDS = float(
    os.getenv("OPENAI_RATE_LIMIT_DELAY", "2.0")
)  # Delay between requests
MAX_RETRIES = int(
    os.getenv("OPENAI_MAX_RETRIES", "5")
)  # Max retries for rate limit errors
INITIAL_RETRY_DELAY = float(
    os.getenv("OPENAI_INITIAL_RETRY_DELAY", "5.0")
)  # Initial retry delay in seconds
MAX_RETRY_DELAY = float(
    os.getenv("OPENAI_MAX_RETRY_DELAY", "300.0")
)  # Max retry delay (5 minutes)


def transcribe_with_retry(
    client: OpenAI,
    audio_file,
    model: str = "whisper-1",
    language: str | None = None,
    verbose: bool = False,
):
    """
    Transcribe audio with retry logic for rate limit errors.

    Args:
        client: OpenAI client instance
        audio_file: File-like object (already opened) or path to audio file
        model: Whisper model to use
        language: Language code (None for auto-detect)
        verbose: Print retry messages

    Returns:
        Transcription result

    Raises:
        RuntimeError: If quota is permanently exceeded (insufficient_quota) or max retries exceeded
    """
    last_exception = None
    file_opened_here = False

    for attempt in range(MAX_RETRIES):
        file_obj = None
        try:
            # Add delay before request to prevent hitting rate limits
            if attempt > 0:
                delay = min(
                    INITIAL_RETRY_DELAY * (2 ** (attempt - 1)),  # Exponential backoff
                    MAX_RETRY_DELAY,
                )
                if verbose:
                    print(
                        f"    Retrying after {delay:.1f}s (attempt {attempt + 1}/{MAX_RETRIES})..."
                    )
                time.sleep(delay)
            elif RATE_LIMIT_DELAY_SECONDS > 0:
                # Initial delay to prevent hitting rate limits
                time.sleep(RATE_LIMIT_DELAY_SECONDS)

            # Open file if it's a path, otherwise use the file object directly
            if isinstance(audio_file, str | Path):
                audio_path_obj = Path(audio_file)
                # OpenAI uses the filename extension to determine MIME type.
                # Audio Hijack saves AAC audio as .mp4 containers; the SDK
                # maps .mp4 → video/mp4 which Whisper rejects.  Open the file
                # with a .m4a name so the SDK sends audio/mp4 (m4a alias).
                if audio_path_obj.suffix.lower() == ".mp4":
                    _tmp = tempfile.NamedTemporaryFile(
                        suffix=".m4a", delete=False,
                        dir=audio_path_obj.parent,
                    )
                    _tmp.close()
                    shutil.copy2(str(audio_path_obj), _tmp.name)
                    raw_fh = open(_tmp.name, "rb")
                    _mp4_tmp = _tmp.name  # track for cleanup
                else:
                    raw_fh = open(audio_file, "rb")
                    _mp4_tmp = None
                file_obj = raw_fh
                file_opened_here = True
            else:
                # File object already provided - use it directly
                # Note: We can't re-read from a file object, so for retries we need the path
                # This function should be called with a path for retries to work properly
                raw_fh = audio_file
                file_obj = audio_file
                _mp4_tmp = None

            transcript = client.audio.transcriptions.create(
                model=model, file=file_obj, language=language
            )

            # Close file if we opened it
            if file_opened_here and raw_fh:
                raw_fh.close()
                file_opened_here = False
            if _mp4_tmp:
                try:
                    os.unlink(_mp4_tmp)
                except Exception:
                    pass
                _mp4_tmp = None

            return transcript

        except RateLimitError as e:
            last_exception = e

            # Close file if we opened it before retrying
            if file_opened_here and raw_fh:
                raw_fh.close()
                file_opened_here = False

            # Extract error details
            error_data = {}
            if hasattr(e, "response") and e.response:
                if hasattr(e.response, "json"):
                    try:
                        error_data = e.response.json().get("error", {})
                    except Exception:
                        pass
                elif isinstance(e.response, dict):
                    error_data = e.response.get("error", {})

            error_code = error_data.get("code", "")
            error_type = error_data.get("type", "")
            error_message = str(e).lower()

            # Check if it's a permanent quota issue (insufficient_quota)
            if (
                error_code == "insufficient_quota"
                or error_type == "insufficient_quota"
                or "insufficient_quota" in error_message
            ):
                if verbose:
                    print(
                        "    ⚠️  Quota exceeded (insufficient_quota). This requires account quota increase."
                    )
                raise RuntimeError(
                    f"OpenAI API quota exceeded (insufficient_quota). "
                    f"Please check your billing and increase your quota at https://platform.openai.com/account/limits. "
                    f"Error: {e}"
                ) from e

            # For transient rate limits, retry with exponential backoff
            if attempt < MAX_RETRIES - 1:
                if verbose:
                    print(
                        "    ⚠️  Rate limit hit (429). Will retry with exponential backoff..."
                    )
                # For retries, we need the file path, not the file object
                if not isinstance(audio_file, str | Path):
                    raise RuntimeError(
                        "Cannot retry transcription: file object provided but path needed for retries. "
                        "Please call with file path instead."
                    ) from e
                continue
            else:
                # Max retries exceeded
                raise RuntimeError(
                    f"OpenAI API rate limit exceeded after {MAX_RETRIES} retries. "
                    f"Please wait and try again later, or increase rate limits. Error: {e}"
                ) from e

        except Exception:
            # Close file if we opened it
            if file_opened_here and file_obj:
                file_obj.close()
                file_opened_here = False
            # For non-rate-limit errors, don't retry
            raise

    # Should never reach here, but just in case
    if last_exception:
        raise RuntimeError(
            f"Failed to transcribe after {MAX_RETRIES} retries: {last_exception}"
        ) from last_exception


def get_audio_duration(audio_path: Path) -> float | None:
    """Get audio duration in seconds using basic file info or try-catch fallback."""
    try:
        import wave

        if audio_path.suffix.lower() == ".wav":
            with wave.open(str(audio_path), "rb") as wav_file:
                frames = wav_file.getnframes()
                rate = wav_file.getframerate()
                if rate > 0:
                    return frames / float(rate)
    except Exception:
        pass

    # Try pydub for more formats
    try:
        from pydub import AudioSegment

        audio = AudioSegment.from_file(str(audio_path))
        return len(audio) / 1000.0  # pydub returns milliseconds
    except Exception:
        pass

    return None


def _channel_labels() -> dict[int, str]:
    """
    Speaker labels by channel index. Channel 0 = system (remote party), channel
    1 = mic (operator) — the order _combine_tracks_to_stereo and record_meeting
    both produce. Override the two primary labels with TRANSCRIBE_LABEL_SYSTEM /
    TRANSCRIBE_LABEL_MIC (e.g. the remote person's name and the operator's name)
    for a human-readable dialogue.
    """
    system_label = os.environ.get("TRANSCRIBE_LABEL_SYSTEM", "System").strip() or "System"
    mic_label = os.environ.get("TRANSCRIBE_LABEL_MIC", "Mic").strip() or "Mic"
    return {
        0: system_label,
        1: mic_label,
        2: "Channel 2",
        3: "Channel 3",
        4: "Channel 4",
    }


# Utterance segmentation tuning (overridable via env for edge cases).
# GAP: a silence longer than this on one channel ends an utterance.
# STITCH: consecutive same-speaker utterances closer than this are re-joined.
def _env_float(name: str, default: float) -> float:
    """Parse a float env override, falling back to default on malformed input."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        print(
            f"    (ignoring malformed {name}={raw!r}; using {default})",
            file=sys.stderr,
        )
        return default


def _utterance_gap_seconds() -> float:
    return _env_float("TRANSCRIBE_UTTERANCE_GAP_SECONDS", 1.2)


def _utterance_stitch_seconds() -> float:
    return _env_float("TRANSCRIBE_UTTERANCE_STITCH_SECONDS", 4.0)


# Proper-noun corrections are OPERATOR-SPECIFIC vocabulary (the product,
# company, and people names that recur in the operator's own meetings, plus the
# ways the STT model mishears them). This repo is public, so it ships NO baked-in
# vocabulary — the map is loaded at runtime, in priority order, from:
#   1. TRANSCRIBE_PROPER_NOUNS env: "Canonical=variant1|variant2, Other=v3"
#   2. TRANSCRIBE_PROPER_NOUNS_FILE: path to a JSON file, either a
#      {"corrections": [{"canonical","variants_regex"}, ...]} object or a plain
#      [["Canonical","regex"], ...] list.
#   3. A Neotoma `transcription_vocabulary` entity (set
#      TRANSCRIBE_VOCABULARY_ENTITY_ID, or it auto-resolves by name) — the
#      canonical operator store, kept out of this public repo.
# All sources are merged; later sources add to (never replace) earlier ones.
# Each entry is (canonical, variants_regex); matching is case-insensitive and
# word-bounded. Nothing here is operator data.


def _proper_nouns_from_env() -> list[tuple[str, str]]:
    raw = os.environ.get("TRANSCRIBE_PROPER_NOUNS", "").strip()
    pairs: list[tuple[str, str]] = []
    if not raw:
        return pairs
    for entry in raw.split(","):
        if "=" in entry:
            canon, variants = entry.split("=", 1)
            canon, variants = canon.strip(), variants.strip()
            if canon and variants:
                pairs.append((canon, variants))
    return pairs


def _proper_nouns_from_file() -> list[tuple[str, str]]:
    path = os.environ.get("TRANSCRIBE_PROPER_NOUNS_FILE", "").strip()
    if not path or not os.path.exists(path):
        return []
    try:
        data = json.loads(open(path, encoding="utf-8").read())
    except Exception:
        return []
    return _coerce_correction_pairs(data)


def _proper_nouns_from_neotoma() -> list[tuple[str, str]]:
    """
    Load the operator's proper-noun vocabulary from a Neotoma
    `transcription_vocabulary` entity. Best-effort and offline-safe: any failure
    (no CLI, no auth, entity missing) returns an empty list without raising.
    """
    if os.environ.get("TRANSCRIBE_DISABLE_NEOTOMA_VOCAB", "").strip() in (
        "1",
        "true",
        "yes",
    ):
        return []
    if not _neotoma_cli_available():
        return []
    entity_id = os.environ.get("TRANSCRIBE_VOCABULARY_ENTITY_ID", "").strip()
    base = _neotoma_prod_base_url()
    # `entities get <id>` when we have an id; otherwise `entities search <name>`.
    if entity_id:
        cmd = [*_neotoma_cli_prefix(), "--base-url", base, "--api-only", "--json",
               "entities", "get", entity_id]
    else:
        cmd = [*_neotoma_cli_prefix(), "--base-url", base, "--api-only", "--json",
               "entities", "search", "operator transcription proper-noun corrections",
               "--type", "transcription_vocabulary"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        if result.returncode != 0 or not result.stdout.strip():
            return []
        data = json.loads(result.stdout)
    except Exception:
        return []
    # Unwrap common shapes: {entities:[{snapshot:{snapshot:{corrections}}}]}, etc.
    corrections = _find_corrections_field(data)
    return _coerce_correction_pairs(corrections)


def _find_corrections_field(obj: object) -> object:
    """Depth-first search for a `corrections` list in a Neotoma response."""
    if isinstance(obj, dict):
        if isinstance(obj.get("corrections"), list):
            return obj["corrections"]
        for v in obj.values():
            found = _find_corrections_field(v)
            if found:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _find_corrections_field(v)
            if found:
                return found
    return None


def _coerce_correction_pairs(data: object) -> list[tuple[str, str]]:
    """Accept [{canonical,variants_regex}] or [[canonical, regex]] shapes."""
    pairs: list[tuple[str, str]] = []
    if isinstance(data, dict):
        data = data.get("corrections", [])
    for item in data or []:
        if isinstance(item, dict):
            canon = str(item.get("canonical", "")).strip()
            variants = str(item.get("variants_regex", "")).strip()
        elif isinstance(item, (list, tuple)) and len(item) == 2:
            canon, variants = str(item[0]).strip(), str(item[1]).strip()
        else:
            continue
        if canon and variants:
            pairs.append((canon, variants))
    return pairs


_ACTIVE_CORRECTIONS: list[tuple[str, str]] | None = None


def _proper_noun_corrections() -> list[tuple[str, str]]:
    """Merge the operator vocabulary from env + file + Neotoma (all optional)."""
    if _ACTIVE_CORRECTIONS is not None:
        return _ACTIVE_CORRECTIONS
    return (
        _proper_nouns_from_env()
        + _proper_nouns_from_file()
        + _proper_nouns_from_neotoma()
    )


def _apply_proper_noun_corrections(text: str) -> str:
    """
    Repair known STT mistranscriptions of proper nouns using the operator's
    runtime-loaded vocabulary. Regex-based, word-bounded, case-insensitive. Runs
    on the merged transcript, so it applies to every path (multichannel,
    diarized, and the plain single-speaker case). A no-op when no vocabulary is
    configured, so the public default corrects nothing.
    """
    if not text:
        return text
    for canon, variant_re in _proper_noun_corrections():
        text = re.sub(rf"\b(?:{variant_re})\b", canon, text, flags=re.IGNORECASE)
        # Collapse a doubled canonical an overlapping rule may have produced
        # (e.g. a "<canon><digit>" rule firing on already-corrected text).
        text = re.sub(re.escape(canon) + r"(\d)\1\b", canon + r"\1", text)
    return text


# Real English word-forming prefixes: never treat "<prefix>-<word>" as a
# restart stutter (e.g. "re-read", "co-coordinate", "pre-preview" are real
# closed-sense hyphenated words even when the root repeats the prefix).
_REAL_PREFIXES = {
    "re", "co", "pre", "de", "un", "non", "anti", "post", "sub", "mid",
    "over", "under", "self", "ex", "pro", "semi", "multi", "inter", "intra",
}


def _clean_stutters(text: str) -> str:
    """
    Remove speech-disfluency stutter artifacts that ElevenLabs transcribes
    faithfully but that hurt readability. CONSERVATIVE by design — it only
    removes false-start fragments and immediate repeats; it never drops a whole
    real word or filler ("um"/"uh"/"like" stay). Disable with
    TRANSCRIBE_CLEAN_STUTTERS=0 for a verbatim transcript.

    Handles:
      - word-restart stubs: "b-before" -> "before", "s-set" -> "set",
        "in- interacting" -> "interacting" (fragment is a prefix of the next word)
      - hyphen/space false starts: "topolog- topology" -> "topology"
      - immediate word repeats: "the, the, the" / "I I I" -> one
    """
    if os.environ.get("TRANSCRIBE_CLEAN_STUTTERS", "1").strip() in ("0", "false", "no"):
        return text
    if not text:
        return text

    # 1) Word-restart stub immediately followed (no space) by the full word,
    #    where the stub is a leading fragment of that word: "b-before" ->
    #    "before". Require the stub to be a case-insensitive prefix of the
    #    following word so we don't delete real closed hyphenated terms.
    #    BUT skip real English word-forming prefixes ("re-read", "co-coordinate",
    #    "pre-preview") — there the hyphenated word is legitimate and the root
    #    happens to repeat the prefix, so a naive prefix match would wrongly
    #    collapse it and silently change meaning.
    def _restart(m: re.Match) -> str:
        stub, word = m.group(1), m.group(2)
        if stub.lower() in _REAL_PREFIXES:
            return m.group(0)
        return word if word.lower().startswith(stub.lower()) else m.group(0)

    text = re.sub(r"\b([A-Za-z]{1,})-([A-Za-z]+)\b", _restart, text)

    # 2) Abandoned false-start fragment: a short stub ending in a hyphen, then a
    #    SPACE, then another word — the speaker began a word, cut off, and said
    #    something else ("s- you" -> "you", "th- what" -> "what", "in-- First" ->
    #    "First"). Only fires with the space + trailing hyphen, so real closed
    #    compounds ("high-level", "one-on-one", "Mm-hmm") — no space around the
    #    hyphen — are never touched. The stub must NOT be a real word, so a real
    #    short word that legitimately trails a dash ("So-- but", "I mean-- like",
    #    "fine-- I think") is kept.
    _REAL_SHORT_WORDS = {
        "a", "an", "and", "as", "at", "be", "but", "by", "do", "for", "go",
        "he", "i", "if", "in", "is", "it", "its", "me", "my", "no", "of", "on",
        "or", "so", "to", "up", "us", "we", "yes", "you", "the", "this", "that",
        "then", "them", "they", "here", "mean", "fine", "well", "like", "just",
        "not", "now", "how", "why", "who", "our", "out", "she", "her", "his",
        "was", "are", "all", "can", "did", "get", "got", "had", "has", "him",
        "let", "may", "new", "one", "two", "way", "yet",
    }

    def _abandon(m: re.Match) -> str:
        stub = m.group(1)
        return "" if stub.lower() not in _REAL_SHORT_WORDS else m.group(0)

    text = re.sub(r"\b([A-Za-z]{1,4})-{1,2}\s+(?=[A-Za-z])", _abandon, text)

    # If the restart form left a space-separated prefix stutter
    # ("topolog- topology"), collapse it too (prefix check, longer stubs only).
    def _restart_spaced(m: re.Match) -> str:
        stub, word = m.group(1), m.group(2)
        return word if word.lower().startswith(stub.lower()) else m.group(0)

    text = re.sub(r"\b([A-Za-z]{2,})-\s+([A-Za-z]+)\b", _restart_spaced, text)

    # 2) Immediate identical word repeats (with optional comma/space between):
    #    "the, the, the" -> "the", "I I I" -> "I". Case-insensitive match, keep
    #    the first occurrence's casing.
    text = re.sub(
        r"\b(\w+)(?:[,\s]+\1\b)+",
        lambda m: m.group(1),
        text,
        flags=re.IGNORECASE,
    )

    # 3) Tidy spacing/punctuation the removals may have left.
    text = re.sub(r"\s+([,.?!])", r"\1", text)
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip()


def _postprocess_transcript(text: str) -> str:
    """Standard post-merge cleanup: fix proper nouns, then de-stutter."""
    return _clean_stutters(_apply_proper_noun_corrections(text))


def _merge_word_events_by_utterance(
    events: list[dict],
    labels: dict | None = None,
) -> str | None:
    """
    Build a readable dialogue from timestamped word events by grouping each
    speaker's continuous speech into whole UTTERANCES, then ordering utterances
    by start time — instead of interleaving individual words chronologically.

    Word-level chronological ordering shreds cross-talk: when both people speak
    at once, their words alternate one-by-one into an unreadable ping-pong. An
    utterance (a run of one speaker's words with no internal silence gap beyond
    TRANSCRIBE_UTTERANCE_GAP_SECONDS) stays whole even when the other person
    interjects mid-sentence, which is how real dialogue reads.

    Each event: {"start": float, "end": float, "group": hashable, "text": str}.
    ``group`` is the speaker key — a channel index (multichannel) or a
    speaker_id (diarized mono). ``labels`` maps group → display name; unknown
    groups fall back to the group value itself.

    Returns None if there are no usable events (caller falls back).
    """
    labels = labels or {}
    words = [
        e
        for e in events
        if str(e.get("text", "")).strip() and e.get("group") is not None
    ]
    if not words:
        return None

    merged = group_utterances(words, gap=_utterance_gap_seconds(), stitch=_utterance_stitch_seconds())

    parts: list[str] = []
    for u in merged:
        label = labels.get(u["group"], str(u["group"]))
        text = re.sub(r"\s+", " ", u["text"]).strip()
        if text:
            parts.append(f"[{label}]\n{text}")

    return "\n\n".join(parts) if parts else None


def _multichannel_words_to_events(
    transcripts: list, time_offset: float = 0.0
) -> list[dict]:
    """
    Flatten ElevenLabs multichannel `transcripts` into word events with an
    optional time_offset added to every timestamp (used to re-base the words of
    a later segment when a long recording is split under the 1-hour cap).
    """
    events: list[dict] = []
    for ch_idx, t in enumerate(transcripts):
        if not isinstance(t, dict):
            continue
        for w in t.get("words") or []:
            if not isinstance(w, dict):
                continue
            if w.get("type") not in (None, "word"):
                # Drop spacing/audio_event tokens from the readable transcript.
                continue
            start = w.get("start")
            end = w.get("end", start)
            text = w.get("text")
            if start is None or text is None or not str(text).strip():
                continue
            ch = w.get("channel_index")
            if ch is None:
                ch = ch_idx
            try:
                ch_int = int(ch)
            except (TypeError, ValueError):
                ch_int = ch_idx
            events.append(
                {
                    "start": float(start) + time_offset,
                    "end": float(end if end is not None else start) + time_offset,
                    "group": ch_int,
                    "text": str(text),
                }
            )
    return events


def _diarized_words_to_events(words: list, time_offset: float = 0.0) -> list[dict]:
    """
    Flatten a diarized single-file `words` array (flat list, each word carrying a
    ``speaker_id``) into word events grouped by speaker. Same event shape as
    _multichannel_words_to_events, so the utterance merge handles both.
    """
    events: list[dict] = []
    for w in words or []:
        if not isinstance(w, dict):
            continue
        if w.get("type") not in (None, "word"):
            continue
        start = w.get("start")
        end = w.get("end", start)
        text = w.get("text")
        if start is None or text is None or not str(text).strip():
            continue
        speaker = w.get("speaker_id") or w.get("speaker") or "speaker_0"
        events.append(
            {
                "start": float(start) + time_offset,
                "end": float(end if end is not None else start) + time_offset,
                "group": str(speaker),
                "text": str(text),
            }
        )
    return events


def _diarized_speaker_labels(events: list[dict]) -> dict:
    """
    Map diarized speaker_ids to display names. If exactly two speakers are
    present, use TRANSCRIBE_LABEL_SYSTEM / TRANSCRIBE_LABEL_MIC (the same knobs
    as multichannel) in first-appearance order so a mixed single-file recording
    reads with the same human labels. Otherwise fall back to Speaker 1..N.
    """
    order: list[str] = []
    for e in sorted(events, key=lambda e: e["start"]):
        g = e["group"]
        if g not in order:
            order.append(g)
    labels: dict = {}
    if len(order) == 2:
        two = _channel_labels()
        # channel 0 label → first speaker to appear, channel 1 label → second
        labels[order[0]] = two.get(0, "Speaker 1")
        labels[order[1]] = two.get(1, "Speaker 2")
    else:
        for i, g in enumerate(order):
            labels[g] = f"Speaker {i + 1}"
    return labels


def _format_multichannel_transcripts_chronologically(transcripts: list) -> str | None:
    """
    Merge per-channel transcripts into one readable dialogue by utterance.
    Returns None if there are no usable word-level timestamps (caller falls back
    to channel-index order).
    """
    events = _multichannel_words_to_events(transcripts)
    if not events:
        return None
    return _merge_word_events_by_utterance(events, _channel_labels())


def get_audio_channel_count(audio_path: Path) -> int | None:
    """Return channel count via WAV, pydub, then ffprobe; None when unknown."""
    try:
        if audio_path.suffix.lower() == ".wav":
            import wave

            with wave.open(str(audio_path), "rb") as wav_file:
                return int(wav_file.getnchannels())
    except Exception:
        pass
    try:
        from pydub import AudioSegment

        segment = AudioSegment.from_file(str(audio_path))
        return int(segment.channels)
    except Exception:
        pass
    return _ffprobe_channel_count(audio_path)


def _ffprobe_channel_count(audio_path: Path) -> int | None:
    """Return the first audio stream's channel count without raising."""
    ffprobe_path = shutil.which("ffprobe")
    if not ffprobe_path:
        return None
    try:
        result = subprocess.run(
            [
                ffprobe_path,
                "-v",
                "error",
                "-select_streams",
                "a:0",
                "-show_entries",
                "stream=channels",
                "-of",
                "csv=p=0",
                str(audio_path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    first_line = (result.stdout or "").strip().splitlines()
    if not first_line:
        return None
    try:
        channels = int(first_line[0].strip().rstrip(","))
    except ValueError:
        return None
    return channels if channels > 0 else None


def _elevenlabs_request_timeout() -> tuple[float, float]:
    """
    (connect, read) timeouts for ElevenLabs STT. Large WAV uploads can exceed
    short defaults on slow links; long jobs need a generous read timeout.
    ELEVENLABS_STT_TIMEOUT="600,3600" or ELEVENLABS_STT_TIMEOUT_CONNECT / _READ.
    """
    raw = os.environ.get("ELEVENLABS_STT_TIMEOUT", "").strip()
    if raw:
        parts = [p.strip() for p in raw.replace(" ", "").split(",") if p.strip()]
        if len(parts) == 2:
            return (float(parts[0]), float(parts[1]))
        if len(parts) == 1:
            t = float(parts[0])
            return (t, t)
    connect = float(os.environ.get("ELEVENLABS_STT_TIMEOUT_CONNECT", "600"))
    read = float(os.environ.get("ELEVENLABS_STT_TIMEOUT_READ", "3600"))
    return (connect, read)


def _combine_tracks_to_stereo(
    mic_path: Path, system_path: Path, verbose: bool = False
) -> tuple[Path, Path | None]:
    """
    Combine two mono tracks into one 2-channel WAV using the SAME channel
    convention record_meeting produces: system → channel 0 (left), mic →
    channel 1 (right). Downmixed to 16 kHz mono per channel, 16-bit PCM
    (ElevenLabs' recommended STT sample rate; lossless so no MP3 artifacts).

    Keeping the same order as record_meeting means _channel_labels()
    ({0: System, 1: Mic}) applies unchanged to both stereo sources.

    The two tracks MUST share a t=0 origin (same-length captures from the same
    session, as record_meeting produces). Channel order is what makes speaker
    labels deterministic downstream, so it is fixed here, not guessed later.

    Returns (stereo_wav_path, temp_dir). Caller shutil.rmtree(temp_dir) when done.
    """
    ffmpeg_path = shutil.which("ffmpeg")
    if not ffmpeg_path:
        raise RuntimeError(
            "ffmpeg is required to combine mic + system tracks for multichannel "
            "transcription but was not found on PATH."
        )
    temp_dir = Path(tempfile.mkdtemp(prefix="stt_stereo_"))
    out = temp_dir / "combined_stereo.wav"
    cmd = [
        ffmpeg_path,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        # Input 0 = system, input 1 = mic → channel 0 = system, channel 1 = mic.
        "-i",
        str(system_path),
        "-i",
        str(mic_path),
        "-filter_complex",
        # Force each input to mono, then join as L=system(ch0), R=mic(ch1).
        "[0:a]aformat=channel_layouts=mono[l];"
        "[1:a]aformat=channel_layouts=mono[r];"
        "[l][r]join=inputs=2:channel_layout=stereo[a]",
        "-map",
        "[a]",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        str(out),
    ]
    if verbose:
        print(
            "    Combining tracks → stereo (system=ch0, mic=ch1) at 16 kHz",
            file=sys.stderr,
        )
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0 or not out.exists():
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise RuntimeError(
            f"ffmpeg failed to combine tracks to stereo: {result.stderr.strip()}"
        )
    return out, temp_dir


def prepare_elevenlabs_upload_file(
    audio_path: Path, verbose: bool = False
) -> tuple[Path, Path | None]:
    """
    Re-encode large files to MP3 (channel layout preserved) for faster, reliable uploads.
    Returns (path_to_upload, temp_dir_or_none). Caller must shutil.rmtree(temp_dir) when done.
    """
    threshold_mb = float(os.environ.get("ELEVENLABS_STT_COMPRESS_THRESHOLD_MB", "64"))
    size_mb = audio_path.stat().st_size / (1024 * 1024)
    if size_mb <= threshold_mb:
        return audio_path, None

    ffmpeg_path = shutil.which("ffmpeg")
    if not ffmpeg_path:
        if verbose:
            print(
                f"    File is {size_mb:.1f} MB (> {threshold_mb} MB) but ffmpeg not found; "
                "uploading raw (may timeout on slow networks).",
                file=sys.stderr,
            )
        return audio_path, None

    temp_dir = Path(tempfile.mkdtemp(prefix="el_stt_upload_"))
    out_mp3 = temp_dir / f"{audio_path.stem}_el_upload.mp3"
    bitrate = (
        os.environ.get("ELEVENLABS_STT_UPLOAD_MP3_BITRATE", "192k").strip() or "192k"
    )
    cmd = [
        ffmpeg_path,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(audio_path),
        "-c:a",
        "libmp3lame",
        "-b:a",
        bitrate,
        str(out_mp3),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise RuntimeError(
            f"ffmpeg re-encode for ElevenLabs upload failed: {result.stderr or result.stdout}"
        )

    if verbose:
        new_mb = out_mp3.stat().st_size / (1024 * 1024)
        print(
            f"    Re-encoded for ElevenLabs upload: {size_mb:.1f} MB -> {new_mb:.1f} MB",
            file=sys.stderr,
        )

    return out_mp3, temp_dir


def _ffmpeg_split_audio_segments(
    audio_path: Path, segment_seconds: float, verbose: bool = False
) -> tuple[list[Path], Path]:
    """
    Split audio into time-aligned MP3 segments (channel layout preserved).
    Returns (segment_paths, temp_parent_dir). Caller deletes temp_parent_dir.
    """
    ffmpeg_path = shutil.which("ffmpeg")
    ffprobe_path = shutil.which("ffprobe")
    if not ffmpeg_path or not ffprobe_path:
        raise RuntimeError(
            "ffmpeg and ffprobe are required to split multichannel audio over the "
            "ElevenLabs ~1 hour multichannel limit. Install ffmpeg (e.g. brew install ffmpeg)."
        )

    pr = subprocess.run(
        [
            ffprobe_path,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(audio_path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    total_duration = float(pr.stdout.strip())
    if segment_seconds <= 0:
        raise ValueError("segment_seconds must be positive")

    temp_dir = Path(tempfile.mkdtemp(prefix="el_mc_seg_"))
    paths: list[Path] = []
    idx = 0
    start = 0.0
    bitrate = (
        os.environ.get("ELEVENLABS_STT_UPLOAD_MP3_BITRATE", "192k").strip() or "192k"
    )
    while start < total_duration - 0.01:
        duration = min(segment_seconds, total_duration - start)
        out = temp_dir / f"seg_{idx:04d}.mp3"
        cmd = [
            ffmpeg_path,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(audio_path),
            "-ss",
            str(start),
            "-t",
            str(duration),
            "-c:a",
            "libmp3lame",
            "-b:a",
            bitrate,
            str(out),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise RuntimeError(
                f"ffmpeg segment split failed at start={start}s: {result.stderr}"
            )
        paths.append(out)
        if verbose:
            print(
                f"    Multichannel segment {idx + 1}: {duration:.0f}s "
                f"(ends {start + duration:.0f}s / {total_duration:.0f}s)",
                file=sys.stderr,
            )
        start += segment_seconds
        idx += 1

    return paths, temp_dir


def _parse_elevenlabs_stt_response(
    body: object, language: str | None
) -> tuple[str, str]:
    """Parse the STT response, then fix proper nouns and clean stutters."""
    text, lang = _parse_elevenlabs_stt_response_raw(body, language)
    return _postprocess_transcript(text), lang


def _validate_elevenlabs_raw_body(body: object) -> None:
    try:
        _check_elevenlabs_raw_body(body)
    except (ValueError, TypeError, OverflowError) as exc:
        raise IncompleteTranscriptionResult(
            "ERR_INCOMPLETE_TRANSCRIPTION_RESULT: invalid ElevenLabs raw evidence"
        ) from exc


def _check_elevenlabs_raw_body(body: object) -> None:
    """Check the synchronous parser's consumed structure, including silent parts.

    Text-only bodies and optional/null word metadata are supported. A body must
    still retain an explicit content field; an absent body is not silence.
    """
    if not isinstance(body, dict):
        raise ValueError("ElevenLabs body must be an object")
    if "transcripts" in body:
        transcripts = body["transcripts"]
        if not isinstance(transcripts, list):
            raise ValueError("ElevenLabs transcripts must be a list")
        for transcript in transcripts:
            if not isinstance(transcript, dict) or "transcripts" in transcript:
                raise ValueError("ElevenLabs channel must be a content body")
            _validate_elevenlabs_raw_body(transcript)
        return
    if not ({"text", "words"} & body.keys()):
        raise ValueError("ElevenLabs body lacks text/words")
    for field in ("text", "language_code"):
        if body.get(field) is not None and not isinstance(body[field], str):
            raise ValueError(f"ElevenLabs {field} must be a string or null")
    words = body.get("words")
    if words is None:
        return
    if not isinstance(words, list):
        raise ValueError("ElevenLabs words must be a list or null")
    for word in words:
        if not isinstance(word, dict) or "text" not in word:
            raise ValueError("ElevenLabs word must retain its text field")
        for field in ("text", "type"):
            if word.get(field) is not None and not isinstance(word[field], str):
                raise ValueError(f"ElevenLabs word {field} must be a string or null")
        # Both existing event parsers consume timestamps with float(). Keep
        # numeric strings supported, but refuse malformed/nonfinite timing.
        for field in ("start", "end"):
            if word.get(field) is not None:
                value = word[field]
                if isinstance(value, bool) or not isinstance(value, (str, int, float)) or not math.isfinite(float(value)):
                    raise ValueError("ElevenLabs word has invalid timing")
        if word.get("start") is not None and word.get("end") is not None:
            if float(word["end"]) < float(word["start"]):
                raise ValueError("ElevenLabs word ends before it starts")


def _parse_elevenlabs_stt_response_raw(
    body: object, language: str | None
) -> tuple[str, str]:
    _validate_elevenlabs_raw_body(body)
    if isinstance(body, dict) and "message" in body and "request_id" in body:
        if "text" not in body and "transcripts" not in body:
            raise RuntimeError(
                "ElevenLabs returned an async/webhook-style response; "
                "expected synchronous transcription."
            )

    if isinstance(body, dict) and "transcripts" in body:
        transcripts = body["transcripts"]
        chronological = _format_multichannel_transcripts_chronologically(transcripts)
        if chronological:
            transcription_text = chronological
        else:
            lines: list[str] = []
            label_map = _channel_labels()
            labels = tuple(label_map[i] for i in sorted(label_map))
            for idx, t in enumerate(transcripts):
                if not isinstance(t, dict):
                    continue
                label = labels[idx] if idx < len(labels) else f"Channel {idx}"
                chunk_text = (t.get("text") or "").strip()
                if chunk_text:
                    lines.append(f"[{label}]\n{chunk_text}")
            transcription_text = "\n\n".join(lines)
        transcript_language = (
            (transcripts[0].get("language_code") if transcripts else None)
            or language
            or "auto"
        )
        return transcription_text, transcript_language

    if isinstance(body, dict):
        transcript_language = body.get("language_code") or language or "auto"
        # Diarized single (mixed) file: build a speaker-attributed dialogue from
        # the per-word speaker_id stream via the same utterance merge, so one
        # mixed recording gets the same fidelity + labeling as a two-track one.
        # (Previously this branch returned body["text"] with NO speaker labels.)
        events = _diarized_words_to_events(body.get("words") or [])
        speaker_ids = {e["group"] for e in events}
        if events and len(speaker_ids) >= 2:
            merged = _merge_word_events_by_utterance(
                events, _diarized_speaker_labels(events)
            )
            if merged:
                return merged, transcript_language
        # Single speaker (or no word-level data): plain text is correct.
        transcription_text = (body.get("text") or "").strip()
        if not transcription_text and events:
            transcription_text = " ".join(e["text"] for e in events).strip()
        return transcription_text, transcript_language

    raise RuntimeError(
        f"Unexpected ElevenLabs response shape: {json.dumps(body)[:500]}"
    )


def _post_elevenlabs_stt_raw(
    upload_path: Path,
    data: dict,
    api_key: str,
    verbose: bool,
) -> object:
    """POST to ElevenLabs STT and return the raw parsed JSON body."""
    timeout = _elevenlabs_request_timeout()
    with open(upload_path, "rb") as audio_fp:
        files = {"file": (upload_path.name, audio_fp)}
        resp = requests.post(
            "https://api.elevenlabs.io/v1/speech-to-text",
            headers={"xi-api-key": api_key},
            data=data,
            files=files,
            timeout=timeout,
        )

    if resp.status_code != 200:
        try:
            detail = resp.json()
        except Exception:
            detail = resp.text
        raise RuntimeError(
            f"ElevenLabs speech-to-text failed ({resp.status_code}): {detail}"
        )

    return resp.json()


def _post_elevenlabs_stt(
    upload_path: Path,
    data: dict,
    api_key: str,
    language: str | None,
    verbose: bool,
) -> tuple[str, str]:
    body = _post_elevenlabs_stt_raw(upload_path, data, api_key, verbose)
    return _parse_elevenlabs_stt_response(body, language)


def transcribe_with_elevenlabs_speech_to_text(
    audio_path: Path,
    language: str | None = None,
    verbose: bool = False,
) -> dict:
    """
    Transcribe via ElevenLabs speech-to-text with speaker handling.

    Stereo (2+ channels): use_multi_channel=True (e.g. system vs mic from record_meeting).
    Mono: diarize=True for speaker labels in the merged text.

    Multichannel API duration is capped at ~1 hour; longer stereo recordings are split
    automatically (see ELEVENLABS_STT_MULTICHANNEL_MAX_SECONDS). Large WAV files are
    re-encoded to MP3 before upload when over ELEVENLABS_STT_COMPRESS_THRESHOLD_MB.
    """
    api_key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("ELEVENLABS_API_KEY is not set")

    original_path = audio_path
    channels = get_audio_channel_count(original_path)
    if channels is None:
        if verbose:
            print(
                "    Could not detect channel count; assuming mono for ElevenLabs.",
                file=sys.stderr,
            )
        channels = 1

    use_multi_channel = channels >= 2
    send_diarize = not use_multi_channel

    # scribe_v1 was deprecated/removed 2026-07-09; scribe_v2 is the current
    # flagship STT model (highest accuracy, tuned for long recordings).
    model_id = os.environ.get("ELEVENLABS_STT_MODEL_ID", "scribe_v2").strip()

    # tag_audio_events off by default → clean verbatim transcript (no "(laughter)"
    # tokens interleaved with speech). Override with TRANSCRIBE_TAG_AUDIO_EVENTS=1.
    tag_events = os.environ.get("TRANSCRIBE_TAG_AUDIO_EVENTS", "0").strip() in (
        "1",
        "true",
        "yes",
    )

    data = {
        "model_id": model_id,
        "tag_audio_events": "true" if tag_events else "false",
        "diarize": "true" if send_diarize else "false",
        "use_multi_channel": "true" if use_multi_channel else "false",
        "timestamps_granularity": "word",
        "webhook": "false",
    }
    if use_multi_channel:
        # "separate" → one transcript array per channel, each a clean word list
        # with a single channel_index. We build the readable dialogue ourselves
        # by utterance (see _merge_word_events_by_utterance) so cross-talk stays
        # legible. ("combined" returns a flat, already-interleaved word stream
        # whose plain text fuses overlapping speech into a run-on blob.)
        data["multichannel_output_style"] = "separate"
    elif send_diarize:
        # Known 2-party calls: hint the speaker count to sharpen diarization.
        num_speakers = os.environ.get("TRANSCRIBE_NUM_SPEAKERS", "").strip()
        if num_speakers:
            data["num_speakers"] = num_speakers
    # Default to an English hint when the caller gave none (our calls are English);
    # override per-call with --language or TRANSCRIBE_DEFAULT_LANGUAGE.
    effective_language = language or os.environ.get(
        "TRANSCRIBE_DEFAULT_LANGUAGE", "eng"
    ).strip()
    if effective_language:
        data["language_code"] = effective_language

    # NOTE: ElevenLabs' keyterms/keywords biasing is rejected on this
    # scribe_v2 + diarize path ("invalid_keyword"), so proper-noun accuracy is
    # handled entirely by the post-transcription corrector
    # (_apply_proper_noun_corrections), not a model-side hint.

    if verbose:
        mode = "multichannel" if use_multi_channel else "diarized"
        print(
            f"    ElevenLabs STT model={model_id} mode={mode} channels={channels} "
            f"lang={data.get('language_code', 'auto')}",
            file=sys.stderr,
        )

    duration = get_audio_duration(original_path)
    max_multichannel_sec = float(
        os.environ.get("ELEVENLABS_STT_MULTICHANNEL_MAX_SECONDS", "3500")
    )

    if use_multi_channel and duration is not None and duration > max_multichannel_sec:
        if verbose:
            print(
                f"    Multichannel duration {duration:.0f}s exceeds API limit "
                f"({max_multichannel_sec:.0f}s); splitting into segments.",
                file=sys.stderr,
            )
        segment_paths, split_dir = _ffmpeg_split_audio_segments(
            original_path, max_multichannel_sec, verbose=verbose
        )
        # Collect word events across all segments, re-basing each segment's
        # timestamps by its start offset, then merge ONCE by utterance. This
        # keeps a speaker's turn whole across a segment boundary instead of
        # emitting disconnected [Part N] blobs.
        all_events: list[dict] = []
        raw_segments: list[object] = []
        transcript_language = language or "auto"
        try:
            for i, seg in enumerate(segment_paths):
                offset = i * max_multichannel_sec
                up, up_dir = prepare_elevenlabs_upload_file(seg, verbose=verbose)
                try:
                    body = _post_elevenlabs_stt_raw(up, data, api_key, verbose)
                finally:
                    if up_dir is not None:
                        shutil.rmtree(up_dir, ignore_errors=True)
                raw_segments.append(body)
                if isinstance(body, dict) and "transcripts" in body:
                    _validate_elevenlabs_raw_body(body)
                    all_events.extend(
                        _multichannel_words_to_events(
                            body["transcripts"], time_offset=offset
                        )
                    )
                    if i == 0 and body["transcripts"]:
                        transcript_language = (
                            body["transcripts"][0].get("language_code")
                            or transcript_language
                        )
        finally:
            shutil.rmtree(split_dir, ignore_errors=True)

        transcription_text = _postprocess_transcript(
            _merge_word_events_by_utterance(all_events, _channel_labels()) or ""
        )
        return {
            "transcription_text": transcription_text,
            "language": transcript_language,
            "audio_duration_seconds": duration,
            "file_size_bytes": original_path.stat().st_size,
            "raw_response": {"segments": raw_segments},
            "backend": BACKEND_ELEVENLABS,
            "transcription_engine": "elevenlabs_stt",
            "transcription_model": model_id,
        }

    upload_path, upload_temp = prepare_elevenlabs_upload_file(
        original_path, verbose=verbose
    )
    try:
        body = _post_elevenlabs_stt_raw(upload_path, data, api_key, verbose)
    finally:
        if upload_temp is not None:
            shutil.rmtree(upload_temp, ignore_errors=True)

    transcription_text, transcript_language = _parse_elevenlabs_stt_response(
        body, language
    )
    return {
        "transcription_text": transcription_text,
        "language": transcript_language,
        "audio_duration_seconds": get_audio_duration(original_path),
        "file_size_bytes": original_path.stat().st_size,
        "raw_response": body,
        "backend": BACKEND_ELEVENLABS,
        "transcription_engine": "elevenlabs_stt",
        "transcription_model": model_id,
    }


def convert_qta_to_m4a(audio_path: Path, verbose: bool = False) -> Path:
    """
    Convert .qta (QuickTime Audio) file to .m4a format for OpenAI Whisper API compatibility.
    Uses ffmpeg directly via subprocess to avoid pydub dependency issues.

    Args:
        audio_path: Path to .qta file
        verbose: Print progress messages

    Returns:
        Path to converted .m4a file (temporary file that should be cleaned up)
    """
    if audio_path.suffix.lower() != ".qta":
        raise ValueError(
            f"convert_qta_to_m4a expects .qta file, got: {audio_path.suffix}"
        )

    if verbose:
        print("    Converting .qta file to .m4a format...")

    # Check if ffmpeg is available
    ffmpeg_path = shutil.which("ffmpeg")
    if not ffmpeg_path:
        raise RuntimeError(
            "ffmpeg is required for converting .qta files but not found. "
            "Install with: brew install ffmpeg (macOS) or apt-get install ffmpeg (Linux)."
        )

    # Create temporary file for converted audio
    temp_dir = tempfile.mkdtemp(prefix="qta_convert_")
    converted_path = Path(temp_dir) / f"{audio_path.stem}.m4a"

    try:
        # Use ffmpeg to convert .qta to .m4a
        # -i: input file
        # -c:a aac: audio codec AAC
        # -b:a 128k: audio bitrate 128k
        # -y: overwrite output file
        cmd = [
            ffmpeg_path,
            "-i",
            str(audio_path),
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-y",  # Overwrite output
            str(converted_path),
        ]

        result = subprocess.run(cmd, capture_output=True, text=True, check=False)

        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg conversion failed: {result.stderr}")

        if not converted_path.exists():
            raise RuntimeError("Conversion completed but output file not found")

        if verbose:
            print(
                f"    Converted to: {converted_path.name} ({converted_path.stat().st_size / 1024 / 1024:.2f} MB)"
            )

        return converted_path

    except FileNotFoundError as e:
        raise RuntimeError(
            f"ffmpeg is required for converting .qta files but not found. "
            f"Install with: brew install ffmpeg (macOS) or apt-get install ffmpeg (Linux). "
            f"Original error: {e}"
        ) from e
    except subprocess.CalledProcessError as e:
        # Clean up on error
        try:
            if converted_path.exists():
                converted_path.unlink()
        except Exception:
            pass
        try:
            Path(temp_dir).rmdir()
        except Exception:
            pass
        raise RuntimeError(
            f"Failed to convert .qta file to .m4a: {e.stderr if hasattr(e, 'stderr') else e}"
        ) from e
    except Exception as e:
        # Clean up on error
        try:
            if converted_path.exists():
                converted_path.unlink()
        except Exception:
            pass
        try:
            Path(temp_dir).rmdir()
        except Exception:
            pass
        raise RuntimeError(f"Failed to convert .qta file to .m4a: {e}") from e


def split_audio_file(
    audio_path: Path, max_size_mb: float = 20.0, verbose: bool = False
) -> list[Path]:
    """
    Split an audio file into chunks that are under max_size_mb.

    Args:
        audio_path: Path to audio file
        max_size_mb: Maximum size per chunk in MB (default 20MB to stay under 25MB limit)
        verbose: Print progress messages

    Returns:
        List of paths to chunk files (temporary files that should be cleaned up)
    """
    # Check for ffmpeg/ffprobe
    ffmpeg_path = shutil.which("ffmpeg")
    ffprobe_path = shutil.which("ffprobe")
    if not ffmpeg_path or not ffprobe_path:
        raise RuntimeError(
            "ffmpeg and ffprobe are required for audio splitting but not found. "
            "Install with: brew install ffmpeg (macOS) or apt-get install ffmpeg (Linux)."
        )

    if verbose:
        print(
            f"    Splitting audio file into chunks (max {max_size_mb} MB per chunk)..."
        )

    # Get audio duration using ffprobe
    try:
        result = subprocess.run(
            [
                ffprobe_path,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(audio_path),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        total_duration_sec = float(result.stdout.strip())
        int(total_duration_sec * 1000)
    except (subprocess.CalledProcessError, ValueError) as e:
        raise RuntimeError(f"Failed to get audio duration: {e}") from e

    # Calculate chunk duration based on file size
    file_size_mb = audio_path.stat().st_size / (1024 * 1024)
    chunk_duration_sec = (max_size_mb / file_size_mb) * total_duration_sec
    chunk_duration_ms = int(chunk_duration_sec * 1000)

    # Ensure minimum chunk size (at least 1 second)
    chunk_duration_ms = max(chunk_duration_ms, 1000)
    chunk_duration_sec = chunk_duration_ms / 1000.0

    chunk_files = []
    temp_dir = tempfile.mkdtemp(prefix="audio_chunks_")

    try:
        num_chunks = int(
            (total_duration_sec / chunk_duration_sec) + 0.999
        )  # Ceiling division

        if verbose:
            print(f"    Creating {num_chunks} chunk(s)...")

        for i in range(num_chunks):
            start_sec = i * chunk_duration_sec
            end_sec = min((i + 1) * chunk_duration_sec, total_duration_sec)

            # Export chunk to temporary file (use mp4 format which ffmpeg recognizes)
            chunk_path = Path(temp_dir) / f"chunk_{i:03d}_{audio_path.stem}.mp4"

            # Use ffmpeg to extract the chunk
            duration = end_sec - start_sec
            try:
                result = subprocess.run(
                    [
                        ffmpeg_path,
                        "-i",
                        str(audio_path),
                        "-ss",
                        str(start_sec),
                        "-t",
                        str(duration),
                        "-c",
                        "copy",  # Copy codec to avoid re-encoding
                        "-y",  # Overwrite output file
                        str(chunk_path),
                    ],
                    check=True,
                    capture_output=True,
                )
            except subprocess.CalledProcessError as e:
                error_msg = e.stderr.decode() if e.stderr else str(e)
                raise RuntimeError(
                    f"Failed to extract chunk {i + 1}: {error_msg}"
                ) from e

            # Verify chunk size
            chunk_size_mb = chunk_path.stat().st_size / (1024 * 1024)
            if chunk_size_mb > 25.0:
                # If still too large, re-encode with lower bitrate
                if verbose:
                    print(
                        f"      Chunk {i + 1} too large ({chunk_size_mb:.2f} MB), re-encoding with lower bitrate..."
                    )
                try:
                    temp_path = chunk_path.with_suffix(".tmp.mp4")
                    result = subprocess.run(
                        [
                            ffmpeg_path,
                            "-i",
                            str(chunk_path),
                            "-c:a",
                            "aac",
                            "-b:a",
                            "96k",
                            "-y",
                            str(temp_path),
                        ],
                        check=True,
                        capture_output=True,
                    )
                    temp_path.replace(chunk_path)
                    chunk_size_mb = chunk_path.stat().st_size / (1024 * 1024)
                except subprocess.CalledProcessError as e:
                    error_msg = e.stderr.decode() if e.stderr else str(e)
                    raise RuntimeError(
                        f"Failed to re-encode chunk {i + 1}: {error_msg}"
                    ) from e

            if verbose:
                print(
                    f"      Chunk {i + 1}/{num_chunks}: {chunk_size_mb:.2f} MB ({start_sec:.1f}s - {end_sec:.1f}s)"
                )

            chunk_files.append(chunk_path)

        return chunk_files

    except Exception as e:
        # Clean up on error
        for chunk_file in chunk_files:
            try:
                chunk_file.unlink()
            except Exception:
                pass
        try:
            Path(temp_dir).rmdir()
        except Exception:
            pass
        raise RuntimeError(f"Failed to split audio file: {e}") from e


def transcribe_two_files(
    mic_path: Path,
    remote_path: Path,
    language: str | None = None,
    verbose: bool = False,
    backend: str | None = None,
) -> dict:
    """
    Transcribe mic and system tracks with deterministic channel prefixes.

    Local FluidAudio processes each original mono track on its original timeline.
    Speaker IDs remain scoped to their physical track; overlapping utterances
    stay whole. An explicitly selected ElevenLabs backend combines the tracks
    into stereo for its multichannel API. Both paths preserve raw model output.

    mic_path    — operator microphone track (mono) → labeled [Mic]
    remote_path — system-wide audio track (mono)   → labeled [System]

    Returns same shape as transcribe_audio_file().
    """
    selected = resolve_backend(explicit=backend, channel_count=2)
    if selected == BACKEND_FLUID:
        tracks = []
        events = []
        for label, path in (("System", remote_path), ("Mic", mic_path)):
            result = transcribe_fluid(path, language=language, verbose=verbose)
            tracks.append({"channel": label, "result": result["raw_response"]})
            for word in result["raw_response"]["aligned_words"]:
                events.append({**word, "speaker": f"{label}/{word['speaker']}"})
        return {
            "transcription_text": format_events(events, gap=_utterance_gap_seconds(), stitch=_utterance_stitch_seconds()), "language": language or "auto",
            "backend": BACKEND_FLUID, "transcription_engine": "local_fluid_audio",
            "transcription_model": "FluidAudio offline + Parakeet v3",
            "audio_duration_seconds": max(get_audio_duration(mic_path) or 0, get_audio_duration(remote_path) or 0),
            "file_size_bytes": mic_path.stat().st_size + remote_path.stat().st_size,
            "raw_response": {"tracks": tracks, "aligned_words": events},
            "mic_file": str(mic_path), "remote_file": str(remote_path),
        }
    if selected != BACKEND_ELEVENLABS:
        raise RuntimeError("Paired transcription supports local FluidAudio or explicitly selected ElevenLabs")

    if verbose:
        print(
            f"    Two-track transcription (multichannel): "
            f"mic={mic_path.name} system={remote_path.name}",
            file=sys.stderr,
        )

    stereo_path, stereo_dir = _combine_tracks_to_stereo(
        mic_path, remote_path, verbose=verbose
    )
    try:
        result = transcribe_with_elevenlabs_speech_to_text(
            stereo_path, language=language, verbose=verbose
        )
    finally:
        if stereo_dir is not None:
            shutil.rmtree(stereo_dir, ignore_errors=True)

    # Duration = max of both source files (the stereo file equals this).
    mic_dur = get_audio_duration(mic_path)
    remote_dur = get_audio_duration(remote_path)
    durations = [d for d in (mic_dur, remote_dur) if d is not None]
    if durations:
        result["audio_duration_seconds"] = max(durations)

    result["mic_file"] = str(mic_path)
    result["remote_file"] = str(remote_path)
    return result


def transcribe_audio_file(
    audio_path: Path,
    language: str | None = None,
    verbose: bool = False,
    use_diarization: bool | None = None,
    backend: str | None = None,
) -> dict:
    """
    Transcribe an audio file. The DEFAULT backend is local ``whisper-cli``: free,
    already installed, and for single-speaker personal memos as effective as the
    paid alternatives.

    Backend precedence (see ``local_whisper.resolve_backend``):

    1. ``backend`` argument / ``--backend`` — an explicit choice wins outright.
    2. ``TRANSCRIBE_BACKEND`` env var — the same decision in the environment.
    3. Diarization requested, or multichannel input → local FluidAudio.
    4. Otherwise → local whisper-cli.

    The metered OpenAI path is reachable only via 1 and 2. It is NEVER a silent
    fallback: if the local backend is misconfigured, that raises rather than
    quietly starting to bill.

    Args:
        audio_path: Path to audio file
        language: Optional language code (e.g., 'en', 'es'). If None, auto-detect.
        use_diarization: True requests local speaker separation; False disables it.
        backend: Explicit backend name ('local', 'fluid', 'elevenlabs', 'openai').

    Returns:
        Dictionary with transcription results including text and metadata
    """
    if not audio_path.exists():
        raise FileNotFoundError(f"Audio file not found: {audio_path}")

    channels = get_audio_channel_count(audio_path)
    resolved_backend = resolve_backend(
        explicit=backend,
        use_diarization=use_diarization,
        channel_count=channels,
    )
    if channels and channels > 1 and resolved_backend not in (BACKEND_FLUID, BACKEND_ELEVENLABS):
        raise RuntimeError(
            "Multichannel recording requires speaker separation; remove the plain "
            "transcription override. Original channels retained."
        )
    if verbose:
        print(f"TRANSCRIPTION_BACKEND_SELECTED={resolved_backend}", flush=True)

    if resolved_backend == BACKEND_FLUID:
        return transcribe_fluid(
            audio_path, language=language, verbose=verbose,
            channel_count=channels or 1,
        )

    if resolved_backend == BACKEND_LOCAL:
        if verbose:
            print("    Transcribing locally with whisper-cli (free, on-device)...")
        local = transcribe_local(
            audio_path, language=language, verbose=verbose
        )
        return {
            "transcription_text": _postprocess_transcript(
                local["transcription_text"]
            )
            if not local.get("silence")
            else local["transcription_text"],
            "language": local["language"],
            "audio_duration_seconds": get_audio_duration(audio_path),
            "file_size_bytes": audio_path.stat().st_size,
            "backend": BACKEND_LOCAL,
            "transcription_engine": local.get(
                "transcription_engine", TRANSCRIPTION_ENGINE_LOCAL
            ),
            "transcription_model": local.get(
                "transcription_model", DEFAULT_MODEL_NAME
            ),
            "rms_db": local.get("rms_db"),
            "silence": bool(local.get("silence")),
        }

    use_diarization = resolved_backend == BACKEND_ELEVENLABS

    # Convert .qta files to .m4a for OpenAI compatibility
    converted_file = None
    temp_dir = None
    original_path = audio_path

    if audio_path.suffix.lower() == ".qta":
        converted_file = convert_qta_to_m4a(audio_path, verbose=verbose)
        audio_path = converted_file
        temp_dir = converted_file.parent

    # Check file size (OpenAI has 25MB limit)
    file_size = audio_path.stat().st_size
    max_size = 25 * 1024 * 1024  # 25MB
    needs_chunking = file_size > max_size

    # Check audio duration (OpenAI requires minimum 0.1 seconds)
    audio_duration = get_audio_duration(audio_path)
    if audio_duration is not None and audio_duration < 0.1:
        raise ValueError(
            f"SKIP: Audio file too short: {audio_duration:.3f} seconds "
            f"(minimum: 0.1 seconds). File: {audio_path.name}"
        )

    # Handle chunking for large files
    chunk_files = []
    chunk_temp_dir = None

    try:
        if use_diarization and not os.environ.get("ELEVENLABS_API_KEY", "").strip():
            # Chosen explicitly (or by RECORD_MEETING_DIARIZE=1) but unusable.
            # Falling through here would silently spend money on the metered
            # OpenAI path AND drop the diarization the caller asked for, which
            # is the worse of the two failures — a meeting transcript with every
            # speaker merged into one voice looks fine and is wrong.
            raise RuntimeError(
                "ElevenLabs was selected for diarization but ELEVENLABS_API_KEY "
                f"is not set (looked in the environment and {shared_env.shared_env_path()}). Set the key, or drop the diarization request to use "
                "the free local backend (which cannot separate speakers). Not "
                "falling back to the metered OpenAI API."
            )

        if use_diarization and os.environ.get("ELEVENLABS_API_KEY", "").strip():
            if verbose:
                print(
                    "    Transcribing with ElevenLabs (diarization or multichannel)..."
                )
            el = transcribe_with_elevenlabs_speech_to_text(
                audio_path, language=language, verbose=verbose
            )
            metadata_path = original_path if converted_file else audio_path
            return {
                "transcription_text": el["transcription_text"],
                "language": el["language"],
                "audio_duration_seconds": get_audio_duration(metadata_path),
                "file_size_bytes": metadata_path.stat().st_size,
                # Carry the raw ElevenLabs response through so the single-file
                # path also writes the .stt_raw.json sidecar for offline re-merge.
                "raw_response": el.get("raw_response"),
                "backend": el.get("backend", BACKEND_ELEVENLABS),
                "transcription_engine": el.get(
                    "transcription_engine", "elevenlabs_stt"
                ),
                "transcription_model": el.get(
                    "transcription_model", "scribe_v2"
                ),
            }

        # OpenAI Whisper path — METERED platform billing, not a subscription.
        #
        # This endpoint (/v1/audio/transcriptions) cannot be served by a ChatGPT
        # subscription. Codex's OAuth token carries scopes
        # ["openid","profile","email","offline_access","api.connectors.read",
        # "api.connectors.invoke"] and routes to chatgpt.com/backend-api/codex,
        # which exposes only codex/responses. Presenting that token to
        # api.openai.com returns 403 "Missing scopes: api.model.read". So there
        # is no OAuth path here to fall back to; the only options are a metered
        # platform key, ElevenLabs, or a local model. Say which path is in use
        # rather than letting the SDK fail opaquely on a missing key.
        if not os.environ.get("OPENAI_API_KEY", "").strip():
            raise RuntimeError(
                "No transcription backend available. The OpenAI Whisper path "
                "requires OPENAI_API_KEY (METERED platform billing) and none is "
                f"set (looked in the environment and {shared_env.shared_env_path()}). A ChatGPT subscription cannot serve this endpoint: its "
                "OAuth token is scoped to the Codex backend, not the platform "
                "audio API. Set ELEVENLABS_API_KEY to use ElevenLabs instead, "
                "or transcribe locally with whisper-cli."
            )
        if verbose:
            print("    Transcribing with OpenAI Whisper (METERED platform API key)...")
        try:
            client = OpenAI()
        except Exception as e:
            raise RuntimeError(f"Failed to initialize OpenAI client: {e}") from e

        if needs_chunking:
            # Split into chunks
            chunk_files = split_audio_file(
                audio_path, max_size_mb=20.0, verbose=verbose
            )
            chunk_temp_dir = chunk_files[0].parent if chunk_files else None

            if verbose:
                print(f"    Transcribing {len(chunk_files)} chunk(s)...")

            # Transcribe each chunk
            chunk_transcriptions = []
            detected_language = language

            for i, chunk_path in enumerate(chunk_files):
                if verbose:
                    print(f"    Transcribing chunk {i + 1}/{len(chunk_files)}...")

                try:
                    chunk_transcript = transcribe_with_retry(
                        client=client,
                        audio_file=chunk_path,
                        model="whisper-1",
                        language=detected_language,  # Use detected language from first chunk
                        verbose=verbose,
                    )

                    # Detect language from first chunk if not specified
                    if (
                        i == 0
                        and not language
                        and hasattr(chunk_transcript, "language")
                    ):
                        detected_language = chunk_transcript.language
                        if verbose:
                            print(f"    Detected language: {detected_language}")

                    chunk_transcriptions.append(chunk_transcript.text)

                    if verbose:
                        print(
                            f"      Chunk {i + 1} transcribed: {len(chunk_transcript.text)} characters"
                        )

                except Exception as e:
                    error_str = str(e).lower()
                    error_type = type(e).__name__

                    # Check for network/connection errors (transient, should retry)
                    if (
                        "connection" in error_str
                        or "connect" in error_str
                        or "network" in error_str
                    ):
                        raise RuntimeError(
                            f"Network connection error for chunk {i + 1} of {audio_path.name}: {e}. "
                            f"This is likely a transient network issue. Check your internet connection and try again."
                        ) from e

                    raise RuntimeError(
                        f"OpenAI API error for chunk {i + 1} of {audio_path.name} ({error_type}): {e}"
                    ) from e

            # Combine transcriptions
            combined_text = "\n\n".join(chunk_transcriptions)
            transcript_text = combined_text
            transcript_language = detected_language or language or "auto"

            if verbose:
                print(
                    f"    Combined transcription: {len(combined_text)} characters from {len(chunk_files)} chunk(s)"
                )

        else:
            # Single file transcription
            if verbose:
                print(
                    f"    Opening audio file: {audio_path.name} ({file_size / 1024 / 1024:.2f} MB)"
                )

            try:
                # Transcribe using Whisper API
                # Pass path instead of file object so retries can re-open the file
                if verbose:
                    print("    Calling OpenAI Whisper API (model: whisper-1)...")
                try:
                    transcript = transcribe_with_retry(
                        client=client,
                        audio_file=audio_path,  # Pass path for retry support
                        model="whisper-1",
                        language=language,  # None for auto-detect
                        verbose=verbose,
                    )
                    if verbose:
                        print(
                            f"    Received transcription ({len(transcript.text)} characters)"
                        )

                    transcript_text = transcript.text
                    transcript_language = (
                        transcript.language
                        if hasattr(transcript, "language")
                        else language or "auto"
                    )

                except Exception as e:
                    error_str = str(e).lower()
                    error_type = type(e).__name__

                    # Check for file corruption errors from OpenAI
                    if "invalid" in error_str and "file" in error_str:
                        raise ValueError(
                            f"SKIP: Audio file appears corrupted or invalid: {audio_path.name}. "
                            f"OpenAI error: {e}"
                        ) from e

                    # Check for network/connection errors (transient, should retry)
                    if (
                        "connection" in error_str
                        or "connect" in error_str
                        or "network" in error_str
                    ):
                        raise RuntimeError(
                            f"Network connection error for {audio_path.name}: {e}. "
                            f"This is likely a transient network issue. Check your internet connection and try again."
                        ) from e

                    # Rate limit errors are already handled in transcribe_with_retry
                    # Re-raise as-is
                    raise
            except FileNotFoundError:
                raise
            except PermissionError as e:
                raise PermissionError(
                    f"Cannot read audio file {audio_path.name}: {e}"
                ) from e
            except ValueError:
                # Re-raise ValueError (for SKIP cases)
                raise
            except Exception as e:
                raise RuntimeError(
                    f"Error reading audio file {audio_path.name}: {e}"
                ) from e

        # Get file metadata (use original file if converted)
        metadata_path = original_path if converted_file else audio_path
        file_size = metadata_path.stat().st_size
        audio_duration = get_audio_duration(metadata_path)

        return {
            "transcription_text": _postprocess_transcript(transcript_text),
            "language": transcript_language,
            "audio_duration_seconds": audio_duration,
            "file_size_bytes": file_size,
            "backend": BACKEND_OPENAI,
            "transcription_engine": "openai_whisper_api",
            "transcription_model": "whisper-1",
        }

    finally:
        # Clean up chunk files
        if chunk_files:
            for chunk_file in chunk_files:
                try:
                    chunk_file.unlink()
                except Exception:
                    pass
        # Clean up converted .qta file
        if converted_file and converted_file.exists():
            try:
                converted_file.unlink()
            except Exception:
                pass
        # Clean up temp directories (chunk temp dir)
        if chunk_temp_dir and Path(chunk_temp_dir).exists():
            try:
                # Remove all files in temp dir first
                for file in Path(chunk_temp_dir).iterdir():
                    try:
                        file.unlink()
                    except Exception:
                        pass
                Path(chunk_temp_dir).rmdir()
            except Exception:
                pass
        # Clean up converted file temp dir
        if converted_file and temp_dir and Path(temp_dir).exists():
            try:
                # Remove all files in temp dir first
                for file in Path(temp_dir).iterdir():
                    try:
                        file.unlink()
                    except Exception:
                        pass
                Path(temp_dir).rmdir()
            except Exception:
                pass


_NEOTOMA_PROD_BASE_URL_DEFAULT = ""


def _neotoma_prod_base_url() -> str:
    """Local prod Neotoma API base URL; override via ``NEOTOMA_PROD_BASE_URL``."""
    return (
        os.environ.get("NEOTOMA_PROD_BASE_URL", "").strip()
        or _NEOTOMA_PROD_BASE_URL_DEFAULT
    )


def _neotoma_auth_preflight() -> tuple[bool, str]:
    """
    Confirm the CLI can authenticate against prod before any write.

    Returns ``(ok, reason)``. The recorder MUST NOT silently land on dev: if no
    bearer token is configured we want a loud failure so the SKILL's prod-mirror
    contract holds. (See ``.cursor/skills/record_meeting/SKILL.md``.)
    """
    if os.environ.get(_PIN_ENV, "").strip():
        # Pinned to the daemon's own identity: the signature is the only
        # credential. A bearer beside it, or a mismatched key, is a refusal,
        # never a fallback to the shared token.
        problem = pinned_child_problem()
        if problem:
            return False, f"pinned Neotoma identity refused: {problem}."
        return True, "ok"
    try:
        shared_env.require_settings(
            "NEOTOMA_BEARER_TOKEN",
            purpose="store to prod Neotoma (refusing to fall back to the "
            "unauthenticated dev server)",
        )
    except shared_env.MissingSettingError as exc:
        return False, str(exc)
    return True, "ok"


def _neotoma_cli_core() -> list[str]:
    """The runtime itself: a daemon's deployed CLI when configured, else `neotoma`."""
    script = os.environ.get("NEOTOMA_CLI_SCRIPT", "").strip()
    if script:
        return [os.environ.get("NODE_BIN", "node"), script]
    return ["neotoma"]


def _neotoma_cli_prefix() -> list[str]:
    """argv that runs the CLI; in a pinned run ONLY the CLI gets the private
    HOME and project root (so it cannot reach a stored token or a project .env)."""
    return [*cli_isolation_prefix(), *_neotoma_cli_core()]


def _neotoma_cli_available() -> bool:
    core = _neotoma_cli_core()
    return bool(shutil.which(core[0])) and (
        len(core) == 1 or Path(core[1]).is_file()
    )


def _neotoma_prod_cli_argv(extra: list[str]) -> list[str]:
    """Build a CLI argv that forces the prod HTTP API and fails loudly if unreachable."""
    return [
        *_neotoma_cli_prefix(),
        "--json",
        "--api-only",
        "--base-url",
        _neotoma_prod_base_url(),
        *extra,
    ]


def _neotoma_cli_json(cli_args: list[str]) -> dict | None:
    """Run prod-targeted ``neotoma --json --api-only --base-url <prod>`` and parse JSON.

    Returns ``None`` only when the CLI is missing or the response is not JSON.
    Auth/network failures from prod surface in the parsed payload's ``error_code``;
    callers that need strict failure semantics should use ``_neotoma_cli_or_raise``.
    """
    if not _neotoma_cli_available():
        return None
    cmd = _neotoma_prod_cli_argv(cli_args)
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=900,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    out = (proc.stdout or "").strip()
    if not out:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return None


def _json_safe_float(value):
    """Coerce to float for JSON; ``None`` if missing or non-finite."""
    if value is None:
        return None
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(x) or math.isinf(x):
        return None
    return x


def _json_safe_nonnegative_int(value) -> int:
    if value is None:
        return 0
    try:
        x = int(float(value))
    except (TypeError, ValueError):
        return 0
    return max(0, x)


# Read in blocks: recordings run to hundreds of MB and a whole-file read would
# spike RSS on the daemon host for no gain.
_HASH_BLOCK_BYTES = 1024 * 1024


# Reason reported when audio bytes are unreadable because cloud storage evicted
# them to a placeholder, as distinct from any other read failure.
AUDIO_EVICTED_BY_CLOUD_STORAGE = "evicted_by_cloud_storage"

# One line, because the importer's failure summary shows the last stderr line.
EVICTED_AUDIO_MESSAGE = (
    f"Cannot bind pending store: audio unreadable ({AUDIO_EVICTED_BY_CLOUD_STORAGE})."
    " Its bytes are in iCloud, not on this Mac. Download it from iCloud"
    " (in Finder, choose Download Now), then re-run the import."
)


def _audio_unreadable_reason(audio_path: Path, exc: OSError) -> str:
    """Classify a failed audio read without naming the file.

    Returns ``evicted_by_cloud_storage`` for a cloud-evicted placeholder,
    otherwise ``errno=<code> (<NAME>)``. The path is deliberately left out:
    recording names are private.
    """
    if is_cloud_evicted(audio_path):
        return AUDIO_EVICTED_BY_CLOUD_STORAGE
    code = exc.errno
    name = errno.errorcode.get(code, "UNKNOWN") if code is not None else "UNKNOWN"
    return f"errno={code} ({name})"


def _audio_content_hash_or_reason(audio_path: Path) -> tuple[str | None, str | None]:
    """(sha256, None) when the bytes are readable, else (None, reason)."""
    try:
        digest = hashlib.sha256()
        with open(audio_path, "rb") as fh:
            while chunk := fh.read(_HASH_BLOCK_BYTES):
                digest.update(chunk)
        return digest.hexdigest(), None
    except OSError as exc:
        reason = _audio_unreadable_reason(audio_path, exc)
        print(f"Audio content hash unavailable: {reason}", file=sys.stderr)
        return None, reason


def _audio_content_hash(audio_path: Path) -> str | None:
    """sha256 of the audio BYTES, or None when they cannot be read.

    Returning None rather than raising keeps an unreadable file on the old
    path-keyed behaviour instead of failing a transcription run outright. The
    failure's errno (or cloud eviction) is logged to stderr, never the path.
    """
    return _audio_content_hash_or_reason(audio_path)[0]


def _transcription_idempotency_key(audio_path: Path) -> str:
    """Idempotency key for the ``transcription`` entity, keyed on CONTENT.

    This was ``sha256(absolute_path)`` until ateles#625. Under path-keying,
    moving or renaming a recording changed its key, so the file silently
    re-transcribed and re-uploaded — duplicate entities plus real API spend,
    with no error raised. That blocked the move to object storage, where every
    file changes path by definition.

    Falls back to the path digest when the bytes are unreadable, so a missing
    file degrades to the old behaviour rather than crashing.
    """
    digest = _audio_content_hash(audio_path)
    if digest is None:
        digest = hashlib.sha256(str(audio_path.resolve()).encode()).hexdigest()
    return f"transcription-audio-{digest}"


def _transcription_file_idempotency_key(audio_path: Path) -> str:
    """Idempotency key for the uploaded audio source, keyed on CONTENT.

    Path-keyed, this re-uploaded identical bytes under a second source id every
    time a recording moved.
    """
    digest = _audio_content_hash(audio_path)
    if digest is None:
        digest = hashlib.sha256(str(audio_path.resolve()).encode()).hexdigest()
    return f"transcription-wav-{digest}"


def _neotoma_relations_sidecar_path(audio_path: Path) -> Path:
    """Optional JSON next to the WAV: ``<stem>_neotoma_relations.json`` (snake_case)."""
    return audio_path.with_name(f"{audio_path.stem}_neotoma_relations.json")


def _parse_entity_id_list(raw: str | None) -> list[str]:
    if not raw or not str(raw).strip():
        return []
    out: list[str] = []
    for part in str(raw).replace(" ", "").split(","):
        if part.startswith("ent_"):
            out.append(part)
    return out


def _load_neotoma_relations_sidecar(audio_path: Path) -> tuple[list[str], str | None]:
    """
    Load optional relation targets from ``<stem>_neotoma_relations.json``.

    Shape::
        {
          "relate_contact_entity_ids": ["ent_..."],
          "relate_feedback_analysis_entity_id": "ent_..."
        }
    """
    path = _neotoma_relations_sidecar_path(audio_path)
    if not path.is_file():
        return [], None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [], None
    if not isinstance(data, dict):
        return [], None
    contacts: list[str] = []
    raw_c = data.get("relate_contact_entity_ids") or data.get("contact_entity_ids")
    if isinstance(raw_c, list):
        for x in raw_c:
            if isinstance(x, str) and x.startswith("ent_"):
                contacts.append(x)
    fba = data.get("relate_feedback_analysis_entity_id") or data.get(
        "feedback_analysis_entity_id"
    )
    fba_id = fba if isinstance(fba, str) and fba.startswith("ent_") else None
    return contacts, fba_id


def _merge_transcription_relate_targets(
    audio_path: Path,
    cli_contact_ids: list[str] | None,
    cli_feedback_analysis_id: str | None,
) -> tuple[list[str], str | None]:
    """
    Merge CLI args, sidecar JSON, and env. Contact ids: CLI first, then sidecar, then env.

    Env:
        ``NEOTOMA_TRANSCRIPTION_CONTACT_ENTITY_IDS`` — comma-separated ``ent_`` ids
        ``NEOTOMA_TRANSCRIPTION_FEEDBACK_ANALYSIS_ENTITY_ID`` — single ``ent_`` id
    """
    env_contacts = _parse_entity_id_list(
        os.environ.get("NEOTOMA_TRANSCRIPTION_CONTACT_ENTITY_IDS")
    )
    env_fba = os.environ.get(
        "NEOTOMA_TRANSCRIPTION_FEEDBACK_ANALYSIS_ENTITY_ID", ""
    ).strip()
    env_fba_id = env_fba if env_fba.startswith("ent_") else None

    sidecar_c, sidecar_fba = _load_neotoma_relations_sidecar(audio_path)

    contacts: list[str] = []
    # CLI wins over sidecar over env (first-seen order preserved).
    for bucket in (cli_contact_ids or [], sidecar_c, env_contacts):
        for eid in bucket:
            if eid not in contacts:
                contacts.append(eid)

    fba_id = cli_feedback_analysis_id or sidecar_fba or env_fba_id
    return contacts, fba_id


def _neotoma_cli_relationship_create(
    source_entity_id: str, target_entity_id: str, relationship_type: str = "REFERS_TO"
) -> tuple[bool, str]:
    """Create one relationship on prod API via CLI; returns (ok, message)."""
    if not _neotoma_cli_available():
        return False, "configured Neotoma CLI runtime unavailable"
    auth_ok, auth_reason = _neotoma_auth_preflight()
    if not auth_ok:
        return False, auth_reason
    cmd = _neotoma_prod_cli_argv(
        [
            "relationships",
            "create",
            "--source-entity-id",
            source_entity_id,
            "--target-entity-id",
            target_entity_id,
            "--relationship-type",
            relationship_type,
        ]
    )
    if not neotoma_store_guard.active():
        proc = neotoma_store_guard.paced(
            lambda: subprocess.run(
                cmd, capture_output=True, text=True, timeout=120, check=False
            )
        )
        raw = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        if proc.returncode != 0:
            return False, err or raw or f"exit {proc.returncode}"
        return True, raw or "ok"
    return _pinned_relationship_create(
        cmd, source_entity_id, target_entity_id, relationship_type
    )


def _relationship_attempt(cmd: list[str]) -> "subprocess.CompletedProcess":
    """One paced relationship write; a timeout becomes a failed result."""
    try:
        return neotoma_store_guard.paced(
            lambda: subprocess.run(
                cmd, capture_output=True, text=True, timeout=120, check=False
            )
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", "relationship write timed out")


def _relationship_exists(source_entity_id: str, target_entity_id: str, relationship_type: str) -> bool:
    """Read-back by the relationship's own identity (source, target, type)."""
    data = _neotoma_cli_json(
        [
            "relationships", "list",
            "--source-entity-id", source_entity_id,
            "--target-entity-id", target_entity_id,
            "--relationship-type", relationship_type,
            "--limit", "1",
        ]
    ) or {}
    rows = data.get("relationships") or data.get("results") or []
    return bool(rows)


def _pinned_relationship_create(
    cmd: list[str], source_entity_id: str, target_entity_id: str, relationship_type: str
) -> tuple[bool, str]:
    """The store's recovery policy for a relationship write.

    A relationship is identified by (source, target, type), which is its
    idempotency key: re-sending the identical command cannot duplicate it. On a
    502/503/timeout: read back first; if absent, wait and retry once with the
    same command; if that fails the same way, read back again, then defer
    (StoreDeferred -> exit 76). A permanent failure keeps the normal path.
    """

    def transient(proc) -> bool:
        return proc.returncode != 0 and neotoma_store_guard.is_transient_failure(
            f"{proc.stderr or ''} {proc.stdout or ''}"
        )

    proc = _relationship_attempt(cmd)
    if transient(proc):
        if _relationship_exists(source_entity_id, target_entity_id, relationship_type):
            return True, "ok (confirmed by read-back)"
        _store_sleep(neotoma_store_guard.retry_delay())
        proc = _relationship_attempt(cmd)
        if transient(proc):
            if _relationship_exists(source_entity_id, target_entity_id, relationship_type):
                return True, "ok (confirmed by read-back)"
            raise StoreDeferred(
                "relationship write failed twice with a transient error; deferring"
            )
    raw = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if proc.returncode != 0:
        return False, err or raw or f"exit {proc.returncode}"
    return True, raw or "ok"


def apply_transcription_neotoma_relationships(
    transcription_entity_id: str,
    relate_contact_entity_ids: list[str],
    relate_feedback_analysis_entity_id: str | None,
    *,
    verbose: bool = False,
) -> None:
    """
    Link ``transcription`` → contacts and optional ``feedback_analysis`` with ``REFERS_TO``.

    Edges: ``transcription`` REFERS_TO ``contact`` / ``feedback_analysis`` (source is the
    transcription so graph walks from the artifact to people and analysis).
    """
    for cid in relate_contact_entity_ids:
        ok, msg = _neotoma_cli_relationship_create(
            transcription_entity_id, cid, "REFERS_TO"
        )
        if verbose or not ok:
            line = f"Neotoma REFERS_TO transcription→{cid}: {'ok' if ok else 'FAILED'} ({msg[:500]})"
            print(line, flush=True, file=sys.stderr if not ok else sys.stdout)
    if relate_feedback_analysis_entity_id:
        ok, msg = _neotoma_cli_relationship_create(
            transcription_entity_id,
            relate_feedback_analysis_entity_id,
            "REFERS_TO",
        )
        if verbose or not ok:
            line = (
                f"Neotoma REFERS_TO transcription→feedback_analysis: "
                f"{'ok' if ok else 'FAILED'} ({msg[:500]})"
            )
            print(line, flush=True, file=sys.stderr if not ok else sys.stdout)


def is_already_transcribed(audio_path: Path) -> bool:
    """Return True if Neotoma already holds a ``transcription`` for these BYTES.

    Keyed on content, matching the idempotency keys above (ateles#625). Keying
    the write on content while this read still asked for the absolute path would
    be worse than either alone: a moved file would look new, re-transcribe at
    full API cost, and only then collide on store.

    Falls back to the legacy ``audio_file_path`` lookup when the bytes cannot be
    hashed, and also when the content lookup misses — rows stored before this
    change carry no ``audio_content_sha256``, and treating them as new would
    re-transcribe the entire existing archive.
    """

    def _hit(identifier: str, by_field: str) -> bool:
        data = _neotoma_cli_json(
            [
                "entities",
                "search",
                "--identifier",
                identifier,
                "--entity-type",
                "transcription",
                "--by",
                by_field,
                "--limit",
                "1",
            ]
        )
        if not data:
            return False
        return len(data.get("entities") or []) > 0

    content_hash = _audio_content_hash(audio_path)
    if content_hash and _hit(content_hash, "audio_content_sha256"):
        return True

    # Legacy rows predate content hashing; match them on the path they were
    # stored under. A miss here is a genuine "not transcribed yet".
    return _hit(str(audio_path.resolve()), "audio_file_path")


def _pending_store_identity(audio_path: Path, mic_path: Path | None) -> dict:
    identity = {}
    for name, path in (("audio", audio_path), ("mic", mic_path)):
        if path is not None:
            digest, reason = _audio_content_hash_or_reason(path)
            if digest is None:
                if reason == AUDIO_EVICTED_BY_CLOUD_STORAGE:
                    raise RuntimeError(EVICTED_AUDIO_MESSAGE)
                raise RuntimeError(
                    f"Cannot bind pending store to unreadable audio: {reason}"
                )
            identity[name] = {"path": str(path.resolve()), "sha256": digest}
    return identity


def _processing_fingerprint() -> str:
    """Bind cached output to resolved vocabulary, processing controls and setup.

    Only non-secret processing settings are selected. Values/vocabulary are
    hashed rather than copied into the pending result's context. Model/binary
    metadata is inspected without loading a model or invoking a backend.
    """
    names = (
        "TRANSCRIBE_BACKEND", "RECORD_MEETING_DIARIZE", "TRANSCRIBE_DEFAULT_LANGUAGE",
        "TRANSCRIBE_CLEAN_STUTTERS", "TRANSCRIBE_LABEL_SYSTEM", "TRANSCRIBE_LABEL_MIC",
        "TRANSCRIBE_UTTERANCE_GAP_SECONDS", "TRANSCRIBE_UTTERANCE_STITCH_SECONDS",
        "TRANSCRIBE_TAG_AUDIO_EVENTS", "TRANSCRIBE_NUM_SPEAKERS",
        "TRANSCRIBE_WHISPER_MODEL", "TRANSCRIBE_WHISPER_MODEL_NAME", "TRANSCRIBE_WHISPER_CLI",
        "TRANSCRIBE_FLUID_CLI", "TRANSCRIBE_FLUID_COMPUTE_UNITS", "TRANSCRIBE_FFMPEG",
        "ELEVENLABS_STT_MODEL_ID", "ELEVENLABS_STT_COMPRESS_THRESHOLD_MB",
        "ELEVENLABS_STT_UPLOAD_MP3_BITRATE", "ELEVENLABS_STT_MULTICHANNEL_MAX_SECONDS",
        "LIVE_TRANSCRIPT_SILENCE_THRESHOLD_DB",
    )
    def metadata(path: Path) -> dict:
        try:
            stat = path.stat()
            return {"path": str(path.resolve()), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
        except OSError:
            return {"path": str(path), "unavailable": True}

    try:
        model = metadata(_resolve_local_model_path())
    except RuntimeError:
        model = {"unavailable": True}
    binaries = {}
    for variable, default in (("TRANSCRIBE_WHISPER_CLI", "whisper-cli"),
                              ("TRANSCRIBE_FLUID_CLI", "fluidaudiocli"),
                              ("TRANSCRIBE_FFMPEG", "ffmpeg")):
        configured = os.environ.get(variable, "").strip() or default
        binaries[variable] = metadata(Path(shutil.which(configured) or configured))
    payload = {
        "settings": {name: os.environ.get(name) for name in names},
        "vocabulary": _proper_noun_corrections(), "labels": _channel_labels(),
        "gap": _utterance_gap_seconds(), "stitch": _utterance_stitch_seconds(),
        "model": model, "binaries": binaries,
        "source": {name: _audio_content_hash(Path(__file__).with_name(name)) for name in (
            "transcribe_audio.py", "local_whisper.py", "local_fluid.py",
            "transcript_utterances.py", "hallucination_filter.py", "silence_gate.py",
        )},
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


class IncompleteTranscriptionResult(ValueError):
    pass


def _validate_transcription_result(result: object, *, paired: bool) -> None:
    """Validate the actual supported constructors before preserving or storing.

    Nullable measurements still require their keys where the constructor emits
    them. Raw Whisper/OpenAI responses and paired Fluid silence/RMS are optional.
    Extra JSON fields are retained; this is completeness validation, not signing.
    """
    def require(ok, field):
        if not ok:
            raise IncompleteTranscriptionResult(
                f"ERR_INCOMPLETE_TRANSCRIPTION_RESULT: invalid or missing {field}"
            )

    def text(value):
        return isinstance(value, str) and bool(value.strip())

    def number(value, *, nullable=False, nonnegative=True):
        try:
            return (nullable and value is None) or (
                not isinstance(value, bool) and isinstance(value, (int, float))
                and math.isfinite(value) and (not nonnegative or value >= 0)
            )
        except OverflowError:
            return False

    def words(events, field):
        require(isinstance(events, list), field)
        for word in events:
            require(isinstance(word, dict), field + "[]")
            require(number(word.get("start")) and number(word.get("end"))
                    and word["end"] >= word["start"], field + ".timing")
            require(text(word.get("text")) and text(word.get("speaker")), field + ".text/speaker")
            require(isinstance(word.get("speaker_candidates"), list)
                    and all(text(speaker) for speaker in word["speaker_candidates"]), field + ".speaker_candidates")
            require("confidence" in word, field + ".confidence")

    def alignment_matches(actual, expected):
        # Confidence is backend passthrough, not a newly constrained vocabulary.
        fields = ("start", "end", "text", "speaker", "speaker_candidates")
        return len(actual) == len(expected) and all(
            all(left[field] == right[field] for field in fields)
            for left, right in zip(actual, expected)
        )

    def fluid_raw(raw, *, two_tracks=False):
        require(isinstance(raw, dict), "raw_response")
        require(isinstance(raw.get("tracks"), list) and bool(raw["tracks"]), "raw_response.tracks")
        require("aligned_words" in raw, "raw_response.aligned_words")
        words(raw["aligned_words"], "raw_response.aligned_words")
        expected_events = []
        if two_tracks:
            require(len(raw["tracks"]) == 2 and all(isinstance(track, dict) for track in raw["tracks"]),
                    "raw_response.tracks paired originals")
            require(all(text(track.get("channel")) for track in raw["tracks"]), "raw_response.tracks channels")
            require({track.get("channel") for track in raw["tracks"]} == {"System", "Mic"},
                    "raw_response.tracks channels")
            for track in raw["tracks"]:
                child_events = fluid_raw(track.get("result"))
                expected_events.extend({**event, "speaker": f"{track['channel']}/{event['speaker']}"}
                                       for event in child_events)
            require(alignment_matches(raw["aligned_words"], expected_events), "raw_response.aligned_words consistency")
            return expected_events
        for track in raw["tracks"]:
            require(isinstance(track, dict), "raw_response.tracks[]")
            require("channel" in track and (track["channel"] is None or text(track["channel"])), "raw_response.channel")
            diar, asr = track.get("diarization"), track.get("asr")
            require(isinstance(diar, dict) and isinstance(diar.get("segments"), list), "raw_response.diarization.segments")
            require(isinstance(asr, dict) and isinstance(asr.get("wordTimings"), list), "raw_response.asr.wordTimings")
            if "text" in asr:
                require(isinstance(asr["text"], str), "raw_response.asr.text")
            for segment in diar["segments"]:
                require(isinstance(segment, dict), "raw_response.segment")
                require(number(segment.get("startTimeSeconds")) and number(segment.get("endTimeSeconds"))
                        and segment["endTimeSeconds"] > segment["startTimeSeconds"]
                        and text(segment.get("speakerId")), "raw_response.segment timing/speaker")
            for word in asr["wordTimings"]:
                require(isinstance(word, dict), "raw_response.wordTiming")
                require(number(word.get("startTime")) and number(word.get("endTime"))
                        and word["endTime"] >= word["startTime"] and text(word.get("word")), "raw_response.wordTiming")
            # Reuse the producer's actual no-word/speech and overlap rules;
            # presence of arrays alone cannot prove retained inference evidence.
            from local_fluid import LocalFluidUnavailable, align_words
            try:
                expected_events.extend(align_words(asr, diar, channel=track["channel"]))
            except LocalFluidUnavailable as exc:
                raise IncompleteTranscriptionResult(
                    "ERR_INCOMPLETE_TRANSCRIPTION_RESULT: inconsistent Fluid raw evidence"
                ) from exc
        require(alignment_matches(raw["aligned_words"], expected_events), "raw_response.aligned_words consistency")
        return expected_events

    require(isinstance(result, dict), "result")
    backend = result.get("backend")
    require(isinstance(backend, str) and backend in VALID_BACKENDS, "backend")
    require(not paired or backend in (BACKEND_FLUID, BACKEND_ELEVENLABS), "paired backend")
    engines = {BACKEND_LOCAL: TRANSCRIPTION_ENGINE_LOCAL, BACKEND_FLUID: "local_fluid_audio",
               BACKEND_ELEVENLABS: "elevenlabs_stt", BACKEND_OPENAI: "openai_whisper_api"}
    require(result.get("transcription_engine") == engines[backend], "transcription_engine")
    require(text(result.get("transcription_model")), "transcription_model")
    require(text(result.get("transcription_text")), "transcription_text")
    require("language" in result and (text(result["language"]) or backend == BACKEND_OPENAI
            and result["language"] is None), "language")
    require(type(result.get("file_size_bytes")) is int and result["file_size_bytes"] >= 0, "file_size_bytes")
    if not (paired and backend == BACKEND_ELEVENLABS and "audio_duration_seconds" not in result):
        require("audio_duration_seconds" in result and number(result["audio_duration_seconds"],
                nullable=backend != BACKEND_FLUID), "audio_duration_seconds")
    if paired:
        require(text(result.get("mic_file")) and text(result.get("remote_file")), "mic_file/remote_file")
    if backend == BACKEND_LOCAL:
        require("rms_db" in result and number(result["rms_db"], nullable=True, nonnegative=False), "rms_db")
        require(type(result.get("silence")) is bool, "silence")
    elif backend == BACKEND_FLUID:
        events = fluid_raw(result.get("raw_response"), two_tracks=paired)
        if not events:
            require(result["transcription_text"] == format_events([]), "silent Fluid transcription_text")
        if not paired:
            require(type(result.get("silence")) is bool and result["silence"] == (not events), "silence")
    elif backend == BACKEND_ELEVENLABS:
        require(isinstance(result.get("raw_response"), dict), "raw_response")
        raw = result["raw_response"]
        try:
            if "segments" in raw:
                require(isinstance(raw["segments"], list) and bool(raw["segments"]), "raw_response.segments")
                events = []
                for segment in raw["segments"]:
                    _validate_elevenlabs_raw_body(segment)
                    require("transcripts" in segment, "raw_response.segment.transcripts")
                    events.extend(_multichannel_words_to_events(segment["transcripts"]))
                require(bool(events), "raw_response.segments content")
            else:
                parsed, _language = _parse_elevenlabs_stt_response_raw(raw, result["language"])
                require(text(parsed), "raw_response content")
        except (ValueError, TypeError, OverflowError, RuntimeError) as exc:
            raise IncompleteTranscriptionResult(
                "ERR_INCOMPLETE_TRANSCRIPTION_RESULT: invalid ElevenLabs raw evidence"
            ) from exc
    try:
        json.dumps(result, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise IncompleteTranscriptionResult("ERR_INCOMPLETE_TRANSCRIPTION_RESULT: result is not finite JSON") from exc


def _write_pending_store(
    audio_path: Path, mic_path: Path | None, context: dict, result: dict,
    *, identity: dict | None = None,
) -> None:
    """Durably retain the complete result before any store; never log its text."""
    _validate_transcription_result(result, paired=mic_path is not None)
    current = _pending_store_identity(audio_path, mic_path)
    if identity is not None and current != identity:
        raise RuntimeError("Audio changed during transcription; refusing pending store")
    target = audio_path.with_suffix(audio_path.suffix + ".pending-store.json")
    payload = {"version": 1, "identity": current, "context": context, "result": result}
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=target.parent, delete=False) as handle:
            temporary = Path(handle.name)
            os.chmod(temporary, 0o600)
            json.dump(payload, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _read_pending_store(
    audio_path: Path, mic_path: Path | None, context: dict, *, required: bool = False,
) -> dict | None:
    target = audio_path.with_suffix(audio_path.suffix + ".pending-store.json")
    if not target.exists() and not required:
        return None
    try:
        payload = json.loads(target.read_text())
        if (payload.get("version") != 1
                or payload.get("identity") != _pending_store_identity(audio_path, mic_path)
                or payload.get("context") != context):
            raise ValueError("source or context mismatch")
        _validate_transcription_result(payload.get("result"), paired=mic_path is not None)
        return payload["result"]
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        if isinstance(exc, IncompleteTranscriptionResult):
            raise RuntimeError(f"{exc}; pending store retained; no ASR was run") from exc
        raise RuntimeError("Missing, malformed or mismatched pending store; no ASR was run") from exc


def _write_transcript_sidecars(
    audio_path: Path, result: dict, verbose: bool = False
) -> None:
    """
    Write two sidecars next to the audio file:
      <stem>.transcript.txt   — the merged, speaker-labeled transcript
      <stem>.stt_raw.json     — the raw ElevenLabs response (word timings, per
                                channel/speaker), for offline re-merge

    Best-effort: never raise (a sidecar failure must not fail transcription).
    """
    try:
        text = (result.get("transcription_text") or "").strip()
        if text:
            txt_path = audio_path.with_suffix(audio_path.suffix + ".transcript.txt")
            txt_path.write_text(text, encoding="utf-8")
            if verbose:
                print(f"    Wrote transcript sidecar: {txt_path.name}", file=sys.stderr)
        raw = result.get("raw_response")
        if raw is not None:
            json_path = audio_path.with_suffix(audio_path.suffix + ".stt_raw.json")
            json_path.write_text(
                json.dumps(raw, ensure_ascii=False), encoding="utf-8"
            )
            if verbose:
                print(
                    f"    Wrote raw STT JSON sidecar: {json_path.name}",
                    file=sys.stderr,
                )
    except Exception as e:  # noqa: BLE001 — sidecars are best-effort
        if verbose:
            print(f"    (sidecar write skipped: {e})", file=sys.stderr)


_TRANSCRIPTION_REQUIRED_FIELDS = (
    "audio_content_sha256", "original_source_file", "capture_method",
    "transcription_engine", "consent_basis", "transcription_text", "file_size_bytes",
)


# The payload itself. A store whose transcript cannot be read back has not
# landed, so this is verified even if the schema lookup says it is undeclared —
# an undeclared transcript is a broken schema, not a field to skip.
_TRANSCRIPTION_ALWAYS_VERIFIED_FIELDS = ("transcription_text",)

# Recorded when the audio is too large for the remote transport and the
# transcription is stored without it. Read back like any other field it sets.
_ATTACHMENT_STATUS_FIELDS = ("audio_attachment_status", "audio_attachment_note")


def _declared_transcription_fields() -> set[str] | None:
    """Field names the live ``transcription`` schema declares, or None if unknown.

    Neotoma routes an undeclared field to ``raw_fragments`` and leaves it out of
    the snapshot, so a read-back that demands it fails on every store even when
    the store landed (2026-09-25: five provenance fields undeclared on
    ``transcription`` 1.3.0 made Tyto re-store every voice memo every five
    minutes). ``None`` means the lookup failed; the caller then verifies every
    field, which fails closed rather than silently narrowing the check.
    """
    data = _neotoma_cli_json(["schemas", "get", "--entity-type", "transcription"])
    if not isinstance(data, dict):
        return None
    schema = data.get("schema", data)
    if not isinstance(schema, dict):
        return None
    fields = schema.get("fields")
    if not isinstance(fields, dict):
        fields = (schema.get("schema_definition") or {}).get("fields")
    if not isinstance(fields, dict) or not fields:
        return None
    return set(fields)


def _fields_to_verify(expected: dict, declared: set[str] | None) -> list[str]:
    """Required fields the snapshot can be expected to carry.

    With the schema known, a required field the schema does not declare is
    reported on stderr (it was stored, but only as a raw fragment) and skipped;
    the transcript itself is always verified.
    """
    wanted = list(_TRANSCRIPTION_REQUIRED_FIELDS) + [
        field for field in _ATTACHMENT_STATUS_FIELDS if field in expected
    ]
    if declared is None:
        return wanted
    undeclared = [
        field for field in wanted
        if field not in declared and field not in _TRANSCRIPTION_ALWAYS_VERIFIED_FIELDS
    ]
    if undeclared:
        print(
            "Warning: transcription schema does not declare "
            + ", ".join(undeclared)
            + "; stored as raw fragments and not read back from the snapshot. "
            "Declare them on the schema to verify them.",
            file=sys.stderr,
            flush=True,
        )
    return [field for field in wanted if field not in undeclared]


def _verify_transcription_storage(entity_id, expected, source_id=None, declared_fields=None):
    """Verify the stored record and provenance before reporting completion.

    ``declared_fields`` is the live schema's field set; pass ``None`` to verify
    every required field regardless (the fail-closed default).
    """
    result = _neotoma_cli_json(["entities", "get", entity_id]) or {}
    record = result.get("entity", result)
    snapshot = record.get("snapshot") or {}
    missing = [field for field in _fields_to_verify(expected, declared_fields)
               if field not in snapshot or snapshot[field] != expected[field]]
    if missing:
        raise RuntimeError("Transcription ingestion incomplete: read-back mismatch for "
                           + ", ".join(missing))
    if source_id:
        # The audio is its own source. `ingest --source-file` stores it beside
        # the structured payload but creates no observation tying it to the
        # entity (the entity's observations cite the JSON source), so an
        # observation-link check can never pass on this write path — it was
        # masked only because the field read-back above failed first. Verify
        # what the write actually produced: the source exists and holds these
        # exact bytes.
        result = _neotoma_cli_json(["sources", "get", source_id]) or {}
        source = result.get("source", result)
        if source.get("content_hash") != expected["audio_content_sha256"]:
            raise RuntimeError("Transcription ingestion incomplete: audio source hash mismatch")


def _verify_exact_transcription_storage(entity_id, expected, source_id, *, attachment_expected):
    """Prove current-result equivalence independently of historical import lookup.

    Dates are intentionally excluded: a later retry can resolve the same result.
    Required identity/context fields cannot be waived by schema declarations.
    The returned receipt values come from the actual persisted snapshot.
    """
    result = _neotoma_cli_json(["entities", "get", entity_id]) or {}
    record = result.get("entity", result)
    snapshot = record.get("snapshot") or {}
    fields = [*_TRANSCRIPTION_REQUIRED_FIELDS, "audio_file_path", "language"]
    fields += [field for field in ("transcription_model", "audio_duration_seconds", *_ATTACHMENT_STATUS_FIELDS)
               if field in expected and expected[field] is not None]
    missing = [field for field in fields
               if field not in snapshot or snapshot[field] != expected[field]]
    if missing:
        raise RuntimeError("ERR_PERSISTED_RESULT_MISMATCH: persisted read-back mismatch for "
                           + ", ".join(missing))
    if attachment_expected:
        if not source_id:
            raise RuntimeError("ERR_PERSISTED_RESULT_MISMATCH: required audio source missing")
        result = _neotoma_cli_json(["sources", "get", source_id]) or {}
        source = result.get("source", result)
        if source.get("content_hash") != expected["audio_content_sha256"]:
            raise RuntimeError("ERR_PERSISTED_RESULT_MISMATCH: audio source hash mismatch")
    return {
        "exact_result_verified": True,
        "capture_method": snapshot["capture_method"],
        "engine": snapshot["transcription_engine"],
        "transcription_text_sha256": hashlib.sha256(snapshot["transcription_text"].encode()).hexdigest(),
    }


def _find_existing_transcription(content_hash: str | None, resolved_audio: Path) -> str | None:
    """Entity id of a ``transcription`` already stored for these bytes, or None.

    Matches on content first, then on the absolute path — rows written while
    ``audio_content_sha256`` was undeclared carry it only as a raw fragment, so
    the content lookup can miss them while the path lookup still hits.
    """
    for identifier, by_field in (
        (content_hash, "audio_content_sha256"),
        (str(resolved_audio), "audio_file_path"),
    ):
        if not identifier:
            continue
        data = _neotoma_cli_json(
            [
                "entities", "search",
                "--identifier", identifier,
                "--entity-type", "transcription",
                "--by", by_field,
                "--limit", "1",
            ]
        ) or {}
        for row in data.get("entities") or []:
            entity_id = row.get("entity_id") or row.get("id")
            if entity_id:
                return entity_id
    return None


def _find_audio_source(audio_name: str, content_hash: str) -> str | None:
    """Id of a stored source holding exactly these audio bytes, or None.

    Searched by filename and confirmed by content hash: the audio source is
    not linked to the transcription entity by any observation (see
    ``_verify_transcription_storage``), so the entity cannot lead to it.
    """
    data = _neotoma_cli_json(
        ["sources", "list", "--search", audio_name, "--limit", "20"]
    ) or {}
    for source in data.get("sources") or []:
        if source.get("content_hash") == content_hash and source.get("id"):
            return source["id"]
    return None


def _recover_prior_transcription_write(
    expected: dict,
    resolved_audio: Path,
    *,
    attachment_expected: bool,
) -> str:
    """Resolve and verify the entity an earlier attempt stored under this key.

    ``ERR_IDEMPOTENCY_MISMATCH`` on a content-keyed store means a prior write
    for these exact audio bytes already landed with a different payload — in
    practice a retry on a later day, since the payload carries today's date.
    Re-sending can never succeed, so the prior write is the durable record:
    find it, and read back the fields that identify it as this recording.
    The transcript text is required to be present, not equal — the earlier
    attempt's transcript is the one that was stored.

    Raises ``RuntimeError`` carrying ``ERR_IDEMPOTENCY_MISMATCH`` when no
    matching entity can be found, which the daemon treats as permanent.
    """
    content_hash = expected["audio_content_sha256"]
    entity_id = _find_existing_transcription(content_hash, resolved_audio)
    if not entity_id:
        raise RuntimeError(
            "Neotoma store failed: ERR_IDEMPOTENCY_MISMATCH and no stored "
            "transcription matches this audio; the prior write under this "
            "content key cannot be resolved"
        )
    result = _neotoma_cli_json(["entities", "get", entity_id]) or {}
    record = result.get("entity", result)
    snapshot = record.get("snapshot") or {}
    problems = []
    if not str(snapshot.get("transcription_text") or "").strip():
        problems.append("transcription_text")
    identity_ok = snapshot.get("audio_content_sha256") == content_hash or (
        snapshot.get("audio_file_path") == str(resolved_audio)
    )
    if not identity_ok:
        problems.append("audio_content_sha256/audio_file_path")
    if "file_size_bytes" in snapshot and snapshot["file_size_bytes"] != expected["file_size_bytes"]:
        problems.append("file_size_bytes")
    if problems:
        raise RuntimeError(
            "Transcription ingestion incomplete: prior write read-back mismatch for "
            + ", ".join(problems)
        )
    if attachment_expected and not _find_audio_source(resolved_audio.name, content_hash):
        raise RuntimeError(
            "Transcription ingestion incomplete: prior write has no stored audio source"
        )
    print(
        f"Recovered prior transcription write {entity_id} after ERR_IDEMPOTENCY_MISMATCH",
        file=sys.stderr,
        flush=True,
    )
    return entity_id


# Neotoma's CLI refuses a remote upload whose base64 body would exceed the
# server's 10 MiB JSON cap: floor(10 MiB * 0.72) raw bytes (neotoma
# src/cli/index.ts, MAX_UPLOADABLE_BYTES). A file over it can never be attached
# over the hosted transport, so retrying only repeats the refusal.
_DEFAULT_REMOTE_UPLOAD_MAX_BYTES = int(10 * 1024 * 1024 * 0.72)


def _remote_upload_max_bytes() -> int:
    raw = os.environ.get("NEOTOMA_REMOTE_UPLOAD_MAX_BYTES", "").strip()
    try:
        return int(raw) if raw else _DEFAULT_REMOTE_UPLOAD_MAX_BYTES
    except ValueError:
        return _DEFAULT_REMOTE_UPLOAD_MAX_BYTES


def _neotoma_target_is_localhost() -> bool:
    """True only when the store target is known to be a loopback API.

    An unknown target is treated as remote: the CLI uploads bytes to anything
    that is not localhost, so remote is the case whose size limit applies.
    """
    url = _neotoma_prod_base_url() or os.environ.get("NEOTOMA_BASE_URL", "").strip()
    if not url:
        return False
    from urllib.parse import urlparse

    host = (urlparse(url).hostname or "").lower()
    return host in {"localhost", "127.0.0.1", "::1"}


_store_sleep = time.sleep  # patched by tests


def _store_attempt(cmd: list[str]) -> "subprocess.CompletedProcess":
    """One paced store attempt; a timeout becomes a failed result, not a raise."""
    try:
        return neotoma_store_guard.paced(
            lambda: subprocess.run(
                cmd, capture_output=True, text=True, timeout=900, check=False
            )
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", "store timed out")


def _store_failed_transiently(proc: "subprocess.CompletedProcess") -> bool:
    if proc.returncode == 0 and (proc.stdout or "").strip():
        return False
    return neotoma_store_guard.is_transient_failure(
        f"{proc.stderr or ''} {proc.stdout or ''}"
    )


def _run_store_with_recovery(
    cmd: list[str], content_hash: str | None, resolved_audio: Path
) -> "tuple[subprocess.CompletedProcess, str | None]":
    """Run the store; in a pinned run, survive a restarting server safely.

    A 502/503/timeout may mean the write landed. So: read back by the same
    identifiers first; only if it is absent, wait and retry ONCE with the very
    same command (same idempotency keys); if that fails the same way, read back
    again and otherwise raise StoreDeferred. Returns (result, landed_entity_id);
    landed_entity_id is set when a read-back found the write.
    """
    proc = _store_attempt(cmd)
    if not neotoma_store_guard.active() or not _store_failed_transiently(proc):
        return proc, None
    landed = _find_existing_transcription(content_hash, resolved_audio)
    if landed:
        return proc, landed
    delay = neotoma_store_guard.retry_delay()
    print(
        f"Neotoma store failed transiently; read-back found nothing. "
        f"Retrying once in {int(delay)}s with the same idempotency key.",
        file=sys.stderr,
        flush=True,
    )
    _store_sleep(delay)
    proc = _store_attempt(cmd)
    if not _store_failed_transiently(proc):
        return proc, None
    landed = _find_existing_transcription(content_hash, resolved_audio)
    if landed:
        return proc, landed
    raise StoreDeferred("Neotoma store failed twice with a transient error; deferring")


def save_transcription(
    audio_path: Path,
    transcription_result: dict,
    source_directory: str | None = None,
    *,
    observation_source: str = "sensor",
    idempotency_key: str | None = None,
    file_idempotency_key: str | None = None,
    attach_audio_file: bool = True,
    require_audio_attachment: bool = False,
    require_exact_result: bool = False,
    extra_entity_fields: dict | None = None,
    relate_contact_entity_ids: list[str] | None = None,
    relate_feedback_analysis_entity_id: str | None = None,
    relate_verbose: bool = False,
    original_source_file: str | None = None,
) -> dict:
    """
    Save transcription to Neotoma (``transcription`` entity + WAV via combined store).

    Args:
        audio_path: Path to audio file (used for metadata; WAV attached when present)
        transcription_result: Dictionary from transcribe_audio_file()
        source_directory: Optional source directory path (relative to imports)
        observation_source: Neotoma ``observation_source`` (``sensor`` for live STT;
            use ``import`` when migrating from parquet).
        idempotency_key: Override default fingerprint-based idempotency (for migrations).
        file_idempotency_key: Override default file idempotency key.
        attach_audio_file: When False, structured store only (e.g. missing WAV on disk).
        extra_entity_fields: Optional extra fields merged onto the ``transcription`` payload.
        relate_contact_entity_ids: After store, create ``REFERS_TO`` edges from this
            ``transcription`` to each contact entity id (optional).
        relate_feedback_analysis_entity_id: Optional ``feedback_analysis`` entity id for
            a ``REFERS_TO`` edge from this ``transcription``.
        relate_verbose: Print one line per relationship attempt even on success.

    Returns:
        Dictionary with saved transcription metadata including ``entity_id``
    """
    if not _neotoma_cli_available():
        raise RuntimeError(
            "Configured Neotoma CLI runtime unavailable; check NEOTOMA_CLI_SCRIPT "
            "and NODE_BIN, or install the Neotoma CLI on PATH."
        )

    auth_ok, auth_reason = _neotoma_auth_preflight()
    if not auth_ok:
        raise RuntimeError(
            f"Neotoma store aborted before any HTTP call: {auth_reason} "
            f"Targeting {_neotoma_prod_base_url()} (override via NEOTOMA_PROD_BASE_URL)."
        )

    resolved_audio = audio_path.resolve()

    if source_directory is None:
        try:
            rel_path = audio_path.relative_to(IMPORTS_DIR)
            source_directory = (
                str(rel_path.parent) if rel_path.parent != Path(".") else ""
            )
        except ValueError:
            source_directory = audio_path.parent.name

    try:
        if str(audio_path).startswith(str(DATA_DIR)):
            audio_file_path_rel = str(audio_path.relative_to(DATA_DIR))
        else:
            audio_file_path_rel = str(audio_path)
    except ValueError:
        audio_file_path_rel = str(audio_path)

    title = f"Transcription — {audio_path.name}"
    duration = _json_safe_float(transcription_result.get("audio_duration_seconds"))
    file_present = resolved_audio.is_file()
    extras = dict(extra_entity_fields or {})

    # File-backed stores fail closed when the audio is missing. Metadata-only
    # imports (attach_audio_file=False) may supply size/hash from the result.
    if attach_audio_file and not file_present:
        raise RuntimeError("Transcription ingestion incomplete: audio file missing")
    if require_audio_attachment and not attach_audio_file:
        raise RuntimeError("ERR_AUDIO_ATTACHMENT_REQUIRED: metadata-only storage is not accepted")

    content_hash = _audio_content_hash(audio_path) if file_present else None
    if not content_hash:
        content_hash = (
            transcription_result.get("audio_content_sha256")
            or extras.get("audio_content_sha256")
        )
    if file_present:
        try:
            size_i = int(audio_path.stat().st_size)
        except OSError as exc:
            if attach_audio_file:
                raise RuntimeError(
                    "Transcription ingestion incomplete: audio file unreadable"
                ) from exc
            size_i = transcription_result.get("file_size_bytes", extras.get("file_size_bytes"))
    else:
        size_i = transcription_result.get("file_size_bytes", extras.get("file_size_bytes"))
    if size_i is None:
        raise RuntimeError("Transcription ingestion incomplete: file size unavailable")
    size_i = int(size_i)

    # Stable under the content-keyed idempotency key: a changing timestamp would
    # turn a post-write read-back retry into ERR_IDEMPOTENCY_MISMATCH forever.
    data_source = (
        f"transcribe_audio.py store audio_sha256={content_hash}"
        if content_hash
        else f"transcribe_audio.py store path={resolved_audio}"
    )

    entity = {
        "entity_type": "transcription",
        "title": title,
        "transcription_text": transcription_result["transcription_text"],
        "language": transcription_result.get("language", "auto"),
        "transcription_date": date.today().isoformat(),
        "audio_duration_seconds": duration,
        "file_size_bytes": size_i,
        "import_date": date.today().isoformat(),
        "import_source_file": audio_path.name,
        "audio_file_path": str(resolved_audio),
        "audio_file_path_data_dir_relative": audio_file_path_rel,
        # Path-independent identity. The path fields above stay for provenance,
        # but this is what dedupe matches on, so a recording that moves to object
        # storage is still recognised as already transcribed (ateles#625).
        "audio_content_sha256": content_hash,
        "audio_file_name": audio_path.name,
        "original_source_file": original_source_file or audio_path.name,
        "source_directory": source_directory,
        "data_source": data_source,
    }
    for field in ("transcription_engine", "transcription_model"):
        value = transcription_result.get(field)
        if value:
            entity[field] = value
    if extras:
        entity.update(extras)
    # Capture method never implies consent; unknown is an explicit absence of attestation.
    entity["transcription_engine"] = transcription_result.get("transcription_engine") or entity.get("transcription_engine") or "unknown"
    for field in ("capture_method", "consent_basis", "transcription_engine"):
        entity[field] = str(entity.get(field) or "unknown").strip() or "unknown"
    entity["audio_content_sha256"] = content_hash
    entity["file_size_bytes"] = size_i
    entity["original_source_file"] = original_source_file or audio_path.name
    entity["transcription_text"] = transcription_result["transcription_text"]
    entity["data_source"] = data_source
    if not entity["audio_content_sha256"]:
        raise RuntimeError("Transcription ingestion incomplete: audio hash unavailable")
    attachment_requested = attach_audio_file

    # Refuse a requested attachment above the caller limit before writing metadata.
    # The remote service independently enforces its configured upload limit.
    max_wav_attach = int(
        os.getenv(
            "NEOTOMA_MAX_TRANSCRIPTION_WAV_BYTES", str(2 * 1024 * 1024 * 1024 - 1024)
        )
    )
    attach_wav = bool(attach_audio_file and file_present)
    if attach_wav:
        try:
            wav_bytes = resolved_audio.stat().st_size
        except OSError:
            raise RuntimeError(
                "Transcription ingestion incomplete: audio file unreadable"
            )
        else:
            if wav_bytes > max_wav_attach:
                raise RuntimeError(
                    "Transcription ingestion incomplete: requested audio exceeds attachment limit"
                )
            remote_limit = _remote_upload_max_bytes()
            if wav_bytes > remote_limit and not _neotoma_target_is_localhost():
                if require_audio_attachment:
                    raise RuntimeError(
                        "ERR_AUDIO_ATTACHMENT_REQUIRED: audio exceeds the remote upload limit; "
                        "no metadata-only store was attempted"
                    )
                # The hosted transport cannot carry these bytes (the CLI
                # refuses before any HTTP call), and it never will on retry.
                # Store the transcript without the audio and record why, so
                # the omission is a visible, verified field rather than a
                # retry loop (2026-09-25: two-minute .qta memos retried every
                # five minutes for a week).
                attach_wav = False
                attachment_requested = False
                entity["audio_attachment_status"] = "omitted_exceeds_remote_upload_limit"
                entity["audio_attachment_note"] = (
                    f"audio is {wav_bytes} bytes; the remote upload limit is "
                    f"{remote_limit} bytes, so the transcription was stored "
                    "without the audio attachment"
                )
                print(
                    f"Audio {audio_path.name} is {wav_bytes} bytes, over the remote "
                    f"upload limit of {remote_limit} bytes; storing the transcription "
                    "without the audio attachment.",
                    file=sys.stderr,
                    flush=True,
                )

    idem = idempotency_key or _transcription_idempotency_key(audio_path)
    file_idem = file_idempotency_key or _transcription_file_idempotency_key(audio_path)

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    ) as tmp:
        json.dump([entity], tmp)
        entities_path = tmp.name

    try:
        if attach_wav:
            # `store --file-path` sends a path the server resolves on its OWN
            # filesystem. That was correct when Neotoma ran co-located with
            # this daemon; against hosted Neotoma the server has no such path
            # and rejects it with ERR_FILE_PATH_IS_SERVER_LOCAL (ateles#1083),
            # silently losing every voice-memo transcription. `ingest
            # --source-file` composes the same /store call but has the CLI
            # decide the transport itself: it uploads the bytes as
            # `file_content` whenever the target base URL is not localhost,
            # and only falls back to a server-side `file_path` when the API
            # actually is local. Do not reintroduce `store --file-path` here.
            cmd = _neotoma_prod_cli_argv(
                [
                    "ingest",
                    "--entities",
                    entities_path,
                    "--source-file",
                    str(resolved_audio),
                    "--idempotency-key",
                    idem,
                    "--file-idempotency-key",
                    file_idem,
                    "--observation-source",
                    observation_source,
                ]
            )
        else:
            cmd = _neotoma_prod_cli_argv(
                [
                    "store",
                    "--file",
                    entities_path,
                    "--idempotency-key",
                    idem,
                    "--observation-source",
                    observation_source,
                ]
            )
        proc, landed_id = _run_store_with_recovery(
            cmd, entity["audio_content_sha256"], resolved_audio
        )
    finally:
        try:
            os.unlink(entities_path)
        except OSError:
            pass

    raw = (proc.stdout or "").strip()
    source_id = None
    if landed_id:
        # A 502/503/timeout answered, but a read-back found the write.
        entity_id = landed_id
        if attach_wav:
            source_id = _find_audio_source(resolved_audio.name, entity["audio_content_sha256"])
            if not source_id:
                raise StoreDeferred(
                    "store may have landed but the audio attachment could not be confirmed"
                )
        if not require_exact_result:
            _verify_transcription_storage(
                entity_id, entity, source_id if attach_wav else None,
                declared_fields=_declared_transcription_fields(),
            )
    elif proc.returncode != 0 or not raw:
        err = (proc.stderr or "").strip() or raw or f"exit {proc.returncode}"
        if "ERR_IDEMPOTENCY_MISMATCH" not in err:
            raise RuntimeError(f"Neotoma store failed: {err}")
        if require_exact_result:
            entity_id = _find_existing_transcription(entity["audio_content_sha256"], resolved_audio)
            if not entity_id:
                raise RuntimeError("ERR_PERSISTED_RESULT_MISMATCH: prior transcription cannot be resolved")
        else:
            entity_id = _recover_prior_transcription_write(
                entity, resolved_audio, attachment_expected=attach_wav,
            )
        if attach_wav:
            source_id = _find_audio_source(resolved_audio.name, entity["audio_content_sha256"])
    else:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"Neotoma store returned non-JSON: {raw[:500]}") from e

        structured = payload.get("structured") or {}
        rows = structured.get("entities") or payload.get("entities") or []
        if not rows:
            raise RuntimeError(f"Neotoma store missing entities in response: {raw[:800]}")

        entity_id = rows[0].get("entity_id")
        if not entity_id:
            raise RuntimeError(f"Neotoma store missing entity_id: {raw[:800]}")

        source_id = (payload.get("unstructured") or {}).get("source_id")
        if attachment_requested and (not attach_wav or not source_id):
            raise RuntimeError("Transcription ingestion incomplete: audio attachment missing")
        if not require_exact_result:
            _verify_transcription_storage(
                entity_id, entity, source_id if attach_wav else None,
                declared_fields=_declared_transcription_fields(),
            )
    verified_result = None
    if require_exact_result:
        verified_result = _verify_exact_transcription_storage(
            entity_id, entity, source_id, attachment_expected=attach_wav,
        )
    print(f"NEOTOMA_TRANSCRIPTION_ENTITY_ID={entity_id}", flush=True)

    contacts = [c for c in (relate_contact_entity_ids or []) if c.startswith("ent_")]
    fba = (
        relate_feedback_analysis_entity_id
        if relate_feedback_analysis_entity_id
        and relate_feedback_analysis_entity_id.startswith("ent_")
        else None
    )
    if contacts or fba:
        apply_transcription_neotoma_relationships(
            entity_id,
            contacts,
            fba,
            verbose=relate_verbose,
        )

    return {
        "transcription_id": entity_id,
        "entity_id": entity_id,
        "audio_source_id": source_id,
        "audio_attachment_verified": bool(attach_wav and source_id),
        "verified_result": verified_result,
        "audio_file_path": audio_file_path_rel,
        "audio_file_name": audio_path.name,
        "source_directory": source_directory,
        "transcription_text": transcription_result["transcription_text"],
        "language": transcription_result.get("language", "auto"),
        "transcription_date": date.today(),
        "audio_duration_seconds": transcription_result.get("audio_duration_seconds"),
        "file_size_bytes": transcription_result.get("file_size_bytes", 0),
        "import_date": date.today(),
        "import_source_file": audio_path.name,
    }


def main():
    global _ACTIVE_CORRECTIONS
    parser = argparse.ArgumentParser(
        description="Transcribe locally with Whisper or FluidAudio speaker diarization"
    )
    parser.add_argument("audio_file", type=str, help="Path to audio file")
    parser.add_argument(
        "--language",
        type=str,
        default=None,
        help="Language code for transcription (e.g., en, es). If not provided, auto-detect.",
    )
    parser.add_argument(
        "--backend",
        type=str,
        default=None,
        choices=list(VALID_BACKENDS),
        help="Transcription backend. Default: 'local' for mono memos, 'fluid' for "
        "diarization or separate channels. Both run on-device. 'elevenlabs' "
        "is an explicit cloud opt-in (needs ELEVENLABS_API_KEY); "
        "'openai' is the METERED platform Whisper API and is never selected "
        "automatically.",
    )
    parser.add_argument(
        "--diarize",
        action="store_true",
        help="Use local FluidAudio speaker diarization and timestamped ASR.",
    )
    parser.add_argument(
        "--no-diarize",
        action="store_true",
        help="Disable automatic diarization for a single-speaker mono file, using "
        "local whisper-cli. Paired and multichannel recordings reject this override.",
    )
    parser.add_argument(
        "--relate-contact-entity-id",
        action="append",
        default=[],
        metavar="ENT_ID",
        help="After Neotoma store, create REFERS_TO from transcription to this contact "
        "(repeatable). Overrides env/sidecar order merge; see also <stem>_neotoma_relations.json.",
    )
    parser.add_argument(
        "--relate-feedback-analysis-entity-id",
        type=str,
        default=None,
        metavar="ENT_ID",
        help="After Neotoma store, create REFERS_TO from transcription to this feedback_analysis.",
    )
    parser.add_argument(
        "--relate-verbose",
        action="store_true",
        help="Print one line per Neotoma relationship create attempt (including successes).",
    )
    parser.add_argument(
        "--mic-file",
        type=str,
        default=None,
        metavar="PATH",
        help="Path to mic-only audio file. Together with audio_file (system audio), "
        "uses local FluidAudio with Mic/ and System/ speaker prefixes. Both original "
        "tracks and raw output are preserved. Cannot combine with --no-diarize.",
    )
    parser.add_argument(
        "--no-store",
        action="store_true",
        help="Transcribe only — print the text but skip saving to Neotoma. "
        "Used by realtime chunk transcription.",
    )
    parser.add_argument(
        "--original-source-file",
        type=str,
        default=None,
        metavar="FILENAME",
        help="Original filename before any rename (e.g. the Voice Memos filename). "
        "Stored as original_source_file in the Neotoma transcription entity for dedup.",
    )
    parser.add_argument(
        "--consent-basis",
        default=os.environ.get("TRANSCRIPTION_CONSENT_BASIS", "unknown"),
        help="Explicit capture consent attestation or unknown; never inferred from capture method.",
    )
    parser.add_argument(
        "--capture-method",
        type=str,
        default=None,
        metavar="METHOD",
        help="How the audio was captured, for consent-posture auditing. Stored as "
        "capture_method on the Neotoma transcription entity. Recommended values: "
        "platform_native (Zoom/Meet/Teams recording — built-in consent disclosure), "
        "audio_hijack_system, blackhole_system (local system capture — no built-in "
        "disclosure), voice_memo, import, unknown.",
    )

    parser.add_argument(
        "--store-only", action="store_true",
        help="Store a source-bound pending result without running transcription. "
        "Missing or changed source/context fails closed; there is no ASR fallback.",
    )
    parser.add_argument("--require-audio-attachment", action="store_true",
                        help="Fail before storage if requested audio cannot be attached.")
    parser.add_argument("--receipt-file", type=Path, default=None,
                        help="Write a private structured verified storage receipt for a supervising caller.")
    args = parser.parse_args()
    if args.store_only and args.no_store:
        parser.error("--store-only cannot be combined with --no-store")
    if args.receipt_file and args.no_store:
        parser.error("--receipt-file requires storage")

    if args.no_diarize:
        use_diarization = False
    elif args.diarize:
        use_diarization = True
    else:
        use_diarization = None

    audio_path = Path(args.audio_file)

    # Validate file exists
    if not audio_path.exists():
        print(f"Error: Audio file not found: {audio_path}", file=sys.stderr)
        sys.exit(1)

    # Validate file extension
    if audio_path.suffix.lower() not in AUDIO_EXTENSIONS:
        print(
            f"Warning: File extension {audio_path.suffix} may not be supported. Processing anyway..."
        )

    previous_corrections = _ACTIVE_CORRECTIONS
    try:
        _ACTIVE_CORRECTIONS = (
            _proper_nouns_from_env() + _proper_nouns_from_file() + _proper_nouns_from_neotoma()
        )
        mic_path = Path(args.mic_file) if args.mic_file else None
        if mic_path is not None and args.no_diarize:
            parser.error("--mic-file requires speaker separation; remove --no-diarize")
        relate_c, relate_fba = _merge_transcription_relate_targets(
            audio_path, args.relate_contact_entity_id or None,
            args.relate_feedback_analysis_entity_id,
        ) if not args.no_store else ([], None)
        extra_fields = {
            "capture_method": (args.capture_method or "unknown").strip(),
            "consent_basis": (args.consent_basis or "unknown").strip(),
        }
        context = {
            "processing_fingerprint": _processing_fingerprint(),
            "language": args.language, "backend": args.backend,
            "diarization": use_diarization,
            "backend_env": os.environ.get("TRANSCRIBE_BACKEND", ""),
            "diarization_env": os.environ.get("RECORD_MEETING_DIARIZE", ""),
            "original_source_file": args.original_source_file,
            "extra_fields": extra_fields, "relate_contacts": relate_c,
            "relate_feedback": relate_fba,
        }
        identity = _pending_store_identity(audio_path, mic_path) if not args.no_store else None
        transcription_result = _read_pending_store(
            audio_path, mic_path, context, required=args.store_only,
        ) if not args.no_store else None
        # Reuse a pending result only against both unchanged tracks and context.
        if transcription_result is not None:
            print("Reusing source-bound pending transcription; ASR skipped")
        # Two-file merge path (mic + remote)
        elif args.mic_file:
            if args.no_diarize:
                parser.error("--mic-file requires speaker separation; remove --no-diarize")
            mic_path = Path(args.mic_file)
            if not mic_path.exists():
                print(f"Error: Mic file not found: {mic_path}", file=sys.stderr)
                sys.exit(1)
            print(f"Two-file transcription: mic={mic_path.name} remote={audio_path.name}")
            transcription_result = transcribe_two_files(
                mic_path=mic_path,
                remote_path=audio_path,
                language=args.language,
                verbose=True,
                backend=args.backend,
            )
        else:
            # Single-file path
            print(f"Transcribing audio file: {audio_path}")
            transcription_result = transcribe_audio_file(
                audio_path,
                language=args.language,
                verbose=True,
                use_diarization=use_diarization,
                backend=args.backend,
            )

        _validate_transcription_result(transcription_result, paired=mic_path is not None)
        engine = transcription_result["transcription_engine"]
        print(f"TRANSCRIPTION_ENGINE={engine}", flush=True)

        # Always drop sidecars next to the audio: a .txt of the merged transcript
        # (recoverable even if the Neotoma store is later rejected as a duplicate
        # idempotency key) and the raw ElevenLabs JSON (so the dialogue can be
        # re-merged with different segmentation without re-billing the API).
        _write_transcript_sidecars(audio_path, transcription_result, verbose=True)

        if args.no_store:
            text = (transcription_result.get("transcription_text") or "").strip()
            if text:
                print(text)
            return

        _write_pending_store(audio_path, mic_path, context, transcription_result, identity=identity)
        try:
            transcription_record = save_transcription(
                audio_path,
                transcription_result,
                relate_contact_entity_ids=relate_c,
                relate_feedback_analysis_entity_id=relate_fba,
                relate_verbose=bool(args.relate_verbose),
                original_source_file=args.original_source_file or None,
                extra_entity_fields=extra_fields,
                require_audio_attachment=args.require_audio_attachment,
                require_exact_result=bool(args.receipt_file),
            )
        except StoreDeferred as exc:
            print(f"STORE_DEFERRED: {exc}", file=sys.stderr, flush=True)
            sys.exit(neotoma_store_guard.EXIT_STORE_DEFERRED)

        if args.receipt_file:
            if _pending_store_identity(audio_path, mic_path) != identity:
                raise RuntimeError("Audio changed during store; verified receipt refused")
            proof = transcription_record.get("verified_result") or {}
            if proof.get("exact_result_verified") is not True:
                raise RuntimeError("Verified receipt requires exact persisted-result proof")
            receipt = {
                "version": 2, "entity_id": transcription_record["entity_id"],
                "identity": identity, **proof,
                "audio_attachment_verified": transcription_record.get("audio_attachment_verified") is True,
                "audio_source_id": transcription_record.get("audio_source_id"),
            }
            with tempfile.NamedTemporaryFile(mode="w", dir=args.receipt_file.parent, delete=False) as handle:
                temporary = Path(handle.name)
                try:
                    os.chmod(temporary, 0o600)
                    json.dump(receipt, handle)
                    handle.flush()
                    os.fsync(handle.fileno())
                    os.replace(temporary, args.receipt_file)
                finally:
                    temporary.unlink(missing_ok=True)

        print("\nTranscription complete:")
        print(f"  File: {audio_path.name}")
        print(f"  Neotoma entity ID: {transcription_record['entity_id']}")
        print(f"  Language: {transcription_record['language']}")
        print(
            f"  Duration: {transcription_record.get('audio_duration_seconds', 'N/A')} seconds"
        )
        print(f"\nTranscription text:\n{transcription_record['transcription_text']}")
        print(
            f"\nSaved to Neotoma (transcription + WAV): entity {transcription_record['entity_id']}"
        )

    except Exception as e:
        print(f"Error transcribing audio: {e}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        sys.exit(1)
    finally:
        _ACTIVE_CORRECTIONS = previous_corrections


if __name__ == "__main__":
    main()
