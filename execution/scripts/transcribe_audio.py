#!/usr/bin/env python3
"""
Audio Transcription Script

Transcribes audio with ElevenLabs speech-to-text (diarization or multichannel)
when ELEVENLABS_API_KEY is set and diarization is not disabled; otherwise OpenAI
Whisper. Persists each result as a Neotoma ``transcription`` entity with the WAV
attached (``neotoma store`` combined file + entities). Optional: after store,
creates ``REFERS_TO`` edges from the new ``transcription`` to ``contact`` and/or
``feedback_analysis`` entities (CLI flags, env vars, or ``<stem>_neotoma_relations.json``
next to the WAV — see ``save_transcription`` / ``record_meeting`` skill).

Usage:
    python transcribe_audio.py <audio_file_path> [--language <language_code>]
    python transcribe_audio.py file.wav --no-diarize   # force Whisper only

Examples:
    python transcribe_audio.py data/imports/audio/recording.wav
    python transcribe_audio.py data/imports/audio/recording.wav --language en
"""

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import UTC, date, datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv
from openai import OpenAI, RateLimitError

# Add project root to path
PROJECT_ROOT = Path(
    __file__
).parent.parent.parent  # execution/scripts -> execution -> personal (repo root)
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(
    0, str(PROJECT_ROOT / "execution" / "scripts")
)  # Add scripts directory to path

# Load environment variables from .env file
# Check for parsing errors and abort if found
import contextlib
import io

stderr_capture = io.StringIO()
with contextlib.redirect_stderr(stderr_capture):
    result = load_dotenv(PROJECT_ROOT / ".env")

stderr_output = stderr_capture.getvalue()
if "could not parse statement" in stderr_output.lower():
    print(f"\n{'=' * 80}")
    print("ERROR: .env file parsing errors detected")
    print(f"{'=' * 80}")
    print(stderr_output.strip())
    print(f"\n.env file path: {PROJECT_ROOT / '.env'}")
    print(
        "Fix the .env file syntax errors (check lines mentioned above) before continuing."
    )
    print(f"{'=' * 80}\n")
    sys.exit(1)

# Import config - try both paths
try:
    from scripts.config import get_data_dir
except ImportError:
    from config import get_data_dir

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
                    import tempfile, shutil as _shutil
                    _tmp = tempfile.NamedTemporaryFile(
                        suffix=".m4a", delete=False,
                        dir=audio_path_obj.parent,
                    )
                    _tmp.close()
                    _shutil.copy2(str(audio_path_obj), _tmp.name)
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
                    import os as _os; _os.unlink(_mp4_tmp)
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
    if not shutil.which("neotoma"):
        return []
    entity_id = os.environ.get("TRANSCRIBE_VOCABULARY_ENTITY_ID", "").strip()
    base = _neotoma_prod_base_url()
    # `entities get <id>` when we have an id; otherwise `entities search <name>`.
    if entity_id:
        cmd = ["neotoma", "--base-url", base, "--api-only", "--json",
               "entities", "get", entity_id]
    else:
        cmd = ["neotoma", "--base-url", base, "--api-only", "--json",
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


def _proper_noun_corrections() -> list[tuple[str, str]]:
    """Merge the operator vocabulary from env + file + Neotoma (all optional)."""
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

    gap = _utterance_gap_seconds()
    stitch = _utterance_stitch_seconds()

    # Segment each speaker independently into utterances on silence gaps.
    by_group: dict = {}
    for w in words:
        by_group.setdefault(w["group"], []).append(w)

    utterances: list[dict] = []
    for grp, grp_words in by_group.items():
        grp_words.sort(key=lambda w: w["start"])
        cur: list[dict] = []

        def emit(run: list[dict]) -> None:
            utterances.append(
                {
                    "group": grp,
                    "start": run[0]["start"],
                    "end": run[-1]["end"],
                    "text": " ".join(x["text"].strip() for x in run).strip(),
                }
            )

        for w in grp_words:
            if cur and (w["start"] - cur[-1]["end"]) > gap:
                emit(cur)
                cur = []
            cur.append(w)
        if cur:
            emit(cur)

    if not utterances:
        return None

    # Order utterances by start time; break ties by group string for determinism.
    utterances.sort(key=lambda u: (u["start"], str(u["group"])))

    # Stitch consecutive same-speaker utterances separated only by the other
    # speaker's brief backchannel (or a short pause) back into one turn.
    merged: list[dict] = []
    for u in utterances:
        if (
            merged
            and merged[-1]["group"] == u["group"]
            and (u["start"] - merged[-1]["end"]) < stitch
        ):
            merged[-1]["text"] = (merged[-1]["text"] + " " + u["text"]).strip()
            merged[-1]["end"] = u["end"]
        else:
            merged.append(dict(u))

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
    """Return channel count for WAV via stdlib wave; else pydub if available."""
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
        return None


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


def _parse_elevenlabs_stt_response_raw(
    body: object, language: str | None
) -> tuple[str, str]:
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
) -> dict:
    """
    Transcribe a mic track + a system/remote track as one 2-speaker dialogue.

    The two mono tracks are combined into a single 2-channel file (system → left
    / channel 0, mic → right / channel 1 — the order record_meeting also uses)
    and transcribed via ElevenLabs MULTICHANNEL mode. Speaker identity then comes
    from the channel number, not
    from an acoustic diarization guess — deterministic and correct by
    construction for known-separate tracks. The readable dialogue is built by
    utterance (see _merge_word_events_by_utterance), so cross-talk stays legible.

    This supersedes the old approach of transcribing each track in a separate API
    call and interleaving words by timestamp, which shredded overlapping speech
    and could mis-order words when the two independent calls' clocks drifted.

    mic_path    — operator microphone track (mono) → labeled [Mic]
    remote_path — system-wide audio track (mono)   → labeled [System]

    Returns same shape as transcribe_audio_file().
    """
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
) -> dict:
    """
    Transcribe an audio file using ElevenLabs (diarization / multichannel) when
    ELEVENLABS_API_KEY is set and diarization is enabled; otherwise OpenAI Whisper.

    Args:
        audio_path: Path to audio file
        language: Optional language code (e.g., 'en', 'es'). If None, auto-detect.
        use_diarization: If True, use ElevenLabs when key is set. If False, Whisper only.
            If None (default), use ElevenLabs when ELEVENLABS_API_KEY is set and
            RECORD_MEETING_DIARIZE is not ``0``.

    Returns:
        Dictionary with transcription results including text and metadata
    """
    if not audio_path.exists():
        raise FileNotFoundError(f"Audio file not found: {audio_path}")

    if use_diarization is None:
        use_diarization = (
            bool(os.environ.get("ELEVENLABS_API_KEY", "").strip())
            and os.environ.get("RECORD_MEETING_DIARIZE", "1") != "0"
        )

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
            }

        # OpenAI Whisper path
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


_NEOTOMA_PROD_BASE_URL_DEFAULT = "http://localhost:9180"


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
    token = os.environ.get("NEOTOMA_BEARER_TOKEN", "").strip()
    if not token:
        return (
            False,
            "NEOTOMA_BEARER_TOKEN is not set; refusing to fall back to the "
            "unauthenticated dev server (3080). Source the ateles .env or "
            "set NEOTOMA_BEARER_TOKEN to the prod token before recording.",
        )
    return True, "ok"


def _neotoma_prod_cli_argv(extra: list[str]) -> list[str]:
    """Build a CLI argv that forces the prod HTTP API and fails loudly if unreachable."""
    return [
        "neotoma",
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
    if not shutil.which("neotoma"):
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


def _transcription_idempotency_key(audio_path: Path) -> str:
    digest = hashlib.sha256(str(audio_path.resolve()).encode()).hexdigest()
    return f"transcription-audio-{digest}"


def _transcription_file_idempotency_key(audio_path: Path) -> str:
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
    if not shutil.which("neotoma"):
        return False, "neotoma CLI not on PATH"
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
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
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
    """
    Return True if Neotoma already has a ``transcription`` for this absolute audio path.
    """
    data = _neotoma_cli_json(
        [
            "entities",
            "search",
            "--identifier",
            str(audio_path.resolve()),
            "--entity-type",
            "transcription",
            "--by",
            "audio_file_path",
            "--limit",
            "1",
        ]
    )
    if not data:
        return False
    entities = data.get("entities") or []
    return len(entities) > 0


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


def save_transcription(
    audio_path: Path,
    transcription_result: dict,
    source_directory: str | None = None,
    *,
    observation_source: str = "sensor",
    idempotency_key: str | None = None,
    file_idempotency_key: str | None = None,
    attach_audio_file: bool = True,
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
    if not shutil.which("neotoma"):
        raise RuntimeError(
            "neotoma CLI not found on PATH; install Neotoma CLI or add it to PATH "
            "to store transcriptions."
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

    now_utc = datetime.now(UTC).isoformat()
    title = f"Transcription — {audio_path.name}"
    data_source = f"transcribe_audio.py store {now_utc} path={resolved_audio}"

    duration = _json_safe_float(transcription_result.get("audio_duration_seconds"))
    size_i = _json_safe_nonnegative_int(transcription_result.get("file_size_bytes", 0))

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
        "audio_file_name": audio_path.name,
        "original_source_file": original_source_file or audio_path.name,
        "source_directory": source_directory,
        "data_source": data_source,
    }
    if extra_entity_fields:
        entity.update(extra_entity_fields)

    # Neotoma rejects source uploads over ~2 GiB; store structured transcription only.
    max_wav_attach = int(
        os.getenv(
            "NEOTOMA_MAX_TRANSCRIPTION_WAV_BYTES", str(2 * 1024 * 1024 * 1024 - 1024)
        )
    )
    attach_wav = bool(attach_audio_file and resolved_audio.is_file())
    if attach_wav:
        try:
            wav_bytes = resolved_audio.stat().st_size
        except OSError:
            attach_wav = False
            wav_bytes = 0
        else:
            if wav_bytes > max_wav_attach:
                entity["wav_attachment_omitted_bytes"] = wav_bytes
                entity[
                    "wav_attachment_omit_reason"
                ] = "exceeds_neotoma_source_size_limit"
                attach_wav = False

    idem = idempotency_key or _transcription_idempotency_key(audio_path)
    file_idem = file_idempotency_key or _transcription_file_idempotency_key(audio_path)

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    ) as tmp:
        json.dump([entity], tmp)
        entities_path = tmp.name

    try:
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
        if attach_wav:
            cmd += [
                "--file-path",
                str(resolved_audio),
                "--file-idempotency-key",
                file_idem,
            ]
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=900,
            check=False,
        )
    finally:
        try:
            os.unlink(entities_path)
        except OSError:
            pass

    raw = (proc.stdout or "").strip()
    if proc.returncode != 0 or not raw:
        err = (proc.stderr or "").strip() or raw or f"exit {proc.returncode}"
        # Idempotency mismatch means this recording was already stored.
        # Extract the existing entity_id from stdout or error text and exit 0
        # so callers (Tyto) treat it as a successful duplicate detection.
        if "ERR_IDEMPOTENCY_MISMATCH" in err or "already used" in err:
            import re as _re, sys as _sys
            existing_id = None
            # Check stdout first — prior run may have printed the entity line
            for line in raw.splitlines():
                if line.startswith("NEOTOMA_TRANSCRIPTION_ENTITY_ID="):
                    existing_id = line.split("=", 1)[1].strip()
                    break
            # Also scan the error text for an entity ID pattern
            if not existing_id:
                m = _re.search(r"ent_[0-9a-f]{24}", err)
                if m:
                    existing_id = m.group(0)
            if existing_id:
                print(f"NEOTOMA_TRANSCRIPTION_ENTITY_ID={existing_id}", flush=True)
                print(f"\nSaved to Neotoma (transcription + WAV): entity {existing_id}", flush=True)
                _sys.exit(0)
            # Can't recover entity_id — exit 0 silently (already stored)
            print(
                f"[transcribe_audio] Idempotency mismatch for {audio_path.name} — "
                f"already stored. Entity ID unknown.",
                file=_sys.stderr,
            )
            _sys.exit(0)
        raise RuntimeError(f"Neotoma store failed: {err}")

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
    parser = argparse.ArgumentParser(
        description="Transcribe audio via ElevenLabs (when key + diarization) or OpenAI Whisper"
    )
    parser.add_argument("audio_file", type=str, help="Path to audio file")
    parser.add_argument(
        "--language",
        type=str,
        default=None,
        help="Language code for transcription (e.g., en, es). If not provided, auto-detect.",
    )
    parser.add_argument(
        "--diarize",
        action="store_true",
        help="Use ElevenLabs speech-to-text with diarization (mono) or multichannel labels (stereo) when ELEVENLABS_API_KEY is set.",
    )
    parser.add_argument(
        "--no-diarize",
        action="store_true",
        help="Force OpenAI Whisper even if ELEVENLABS_API_KEY is set.",
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
        help="Path to mic-only audio file (your voice). When provided alongside "
        "audio_file (the remote/system file), enables two-file merge: mic is "
        "transcribed as single speaker [You], remote is diarized as [Speaker_N]. "
        "Requires ELEVENLABS_API_KEY.",
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

    args = parser.parse_args()

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

    try:
        # Two-file merge path (mic + remote)
        if args.mic_file:
            mic_path = Path(args.mic_file)
            if not mic_path.exists():
                print(f"Error: Mic file not found: {mic_path}", file=sys.stderr)
                sys.exit(1)
            if not os.environ.get("ELEVENLABS_API_KEY", "").strip():
                print(
                    "Error: --mic-file requires ELEVENLABS_API_KEY (word-level timestamps needed for merge).",
                    file=sys.stderr,
                )
                sys.exit(1)
            print(f"Two-file transcription: mic={mic_path.name} remote={audio_path.name}")
            transcription_result = transcribe_two_files(
                mic_path=mic_path,
                remote_path=audio_path,
                language=args.language,
                verbose=True,
            )
        else:
            # Single-file path
            print(f"Transcribing audio file: {audio_path}")
            transcription_result = transcribe_audio_file(
                audio_path,
                language=args.language,
                use_diarization=use_diarization,
            )

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

        relate_c, relate_fba = _merge_transcription_relate_targets(
            audio_path,
            args.relate_contact_entity_id or None,
            args.relate_feedback_analysis_entity_id,
        )
        extra_fields = (
            {"capture_method": args.capture_method.strip()}
            if args.capture_method and args.capture_method.strip()
            else None
        )
        transcription_record = save_transcription(
            audio_path,
            transcription_result,
            relate_contact_entity_ids=relate_c,
            relate_feedback_analysis_entity_id=relate_fba,
            relate_verbose=bool(args.relate_verbose),
            original_source_file=args.original_source_file or None,
            extra_entity_fields=extra_fields,
        )

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


if __name__ == "__main__":
    main()
