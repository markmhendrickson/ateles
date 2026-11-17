#!/usr/bin/env python3
"""Local speech-to-text via whisper.cpp (``whisper-cli``).

This is the DEFAULT transcription backend. It runs entirely on the machine: no
API key, no per-minute billing, no network. The metered OpenAI Whisper API and
ElevenLabs remain available but are now explicit opt-ins (see
``resolve_backend``), because for the operator's actual workload — short
personal voice memos, one speaker, no diarization needed — a local model is
equally effective and free.

Three things this module is careful about, each of which has cost real work
before:

1. **The model path is resolved, never assumed.** A missing model must fail
   loudly and name the path plus the command to fetch it. Silently falling back
   to a paid backend when the free one is merely unconfigured is how a "free by
   default" policy quietly bills the operator.

2. **Format conversion is explicit.** whisper-cli accepts only flac/mp3/ogg/wav
   (verified against ``whisper-cli --help``, v1.8.2), and the real inputs are
   ``.m4a`` from Voice Memos and ``.mp4`` from Audio Hijack. Everything is
   normalized through ffmpeg to 16 kHz mono WAV, which is what whisper.cpp wants
   anyway. A missing ffmpeg fails with the install command, not a decode error.

3. **The silence gate runs BEFORE the model.** Whisper fabricates on near-silent
   audio rather than returning empty — observed on this machine as "Bon Appetit!",
   "thank you for watching", "please subscribe", and fluent sentences in
   languages nobody spoke. The gate is the calibrated one from
   ``silence_gate`` (95th-percentile windowed RMS at -50 dB, measured against 39
   labelled chunks); this module deliberately reuses it instead of inventing a
   second threshold that would drift from the first.
"""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

try:
    from scripts.silence_gate import (
        DEFAULT_SILENCE_THRESHOLD_DB,
        measure_sustained_rms_db,
    )
    from scripts.hallucination_filter import screen_transcription
except ImportError:  # pragma: no cover - path-dependent import
    from silence_gate import (  # type: ignore[no-redef]
        DEFAULT_SILENCE_THRESHOLD_DB,
        measure_sustained_rms_db,
    )
    from hallucination_filter import screen_transcription  # type: ignore[no-redef]

# --- Backend selection ------------------------------------------------------
BACKEND_LOCAL = "local"
BACKEND_FLUID = "fluid"
BACKEND_ELEVENLABS = "elevenlabs"
BACKEND_OPENAI = "openai"
VALID_BACKENDS = (BACKEND_LOCAL, BACKEND_FLUID, BACKEND_ELEVENLABS, BACKEND_OPENAI)
TRANSCRIPTION_ENGINE_LOCAL = "local_whisper_cpp"

# The env var that names a backend outright. Highest precedence, because an
# operator who says which backend to use has settled the question.
BACKEND_ENV_VAR = "TRANSCRIBE_BACKEND"

# --- Model resolution -------------------------------------------------------
# Conventional location for whisper.cpp GGML models on this platform. Checked in
# order; the first that exists wins. TRANSCRIBE_WHISPER_MODEL overrides all of
# them with an explicit path.
MODEL_ENV_VAR = "TRANSCRIBE_WHISPER_MODEL"
DEFAULT_MODEL_NAME = "ggml-large-v3-turbo.bin"
MODEL_SEARCH_DIRS = (
    Path.home() / ".cache" / "whisper-cpp",
    Path("/opt/homebrew/share/whisper-cpp"),
    Path("/usr/local/share/whisper-cpp"),
)

WHISPER_CLI_ENV_VAR = "TRANSCRIBE_WHISPER_CLI"
DEFAULT_WHISPER_CLI = "whisper-cli"
WHISPER_PROMPT_ENV_VAR = "TRANSCRIBE_WHISPER_PROMPT"

# Markers stored in place of a transcript when there is no speech. Explicit
# prose, not an empty string and not "." — the operator must be able to tell
# "nobody spoke" from "transcription failed" when reading it back months later.
#
# Two markers because there are two genuinely different detections, caught by two
# different instruments:
NO_SPEECH_MARKER_LEVEL = (
    "[NO SPEECH DETECTED — audio measures {rms:.1f} dB sustained RMS, below the "
    "{threshold:.0f} dB speech floor. Not transcribed: Whisper fabricates "
    "plausible text on near-silent input rather than returning empty. There is "
    "no operator content in this file.]"
)
NO_SPEECH_MARKER_RESULT = (
    "[NO SPEECH DETECTED — the model returned no speech content ({reason}: "
    "{detail}). Raw output was {raw!r}. Sustained level {rms}. Recorded as an "
    "empty capture; there is no operator content in this file.]"
)


class LocalWhisperUnavailable(RuntimeError):
    """Raised when the local backend cannot run. Never caught into a fallback.

    Degrading to a metered backend because the free one is misconfigured would
    bill the operator for a fixable local problem, so every raise site here
    names the path or binary at fault and how to fix it.
    """


def resolve_backend(
    *,
    explicit: str | None = None,
    use_diarization: bool | None = None,
    channel_count: int | None = None,
    env: dict[str, str] | None = None,
) -> str:
    """Decide which speech-to-text backend to use.

    Precedence, highest first:

    1. ``explicit`` — a caller (CLI ``--backend``) naming a backend outright.
    2. ``TRANSCRIBE_BACKEND`` — the same decision made in the environment.
    3. A diarization request selects local FluidAudio, independent of API keys.
    4. Multiple channels select FluidAudio and preserve channel identity unless
       diarization was explicitly disabled.
    5. Mono or unknown-channel audio without a diarization request uses Whisper.

    The metered OpenAI path is reachable ONLY through steps 1 and 2. It is never
    selected implicitly and never used as a fallback: an unavailable free
    backend raises rather than spending money.
    """
    env = os.environ if env is None else env

    if explicit:
        choice = explicit.strip().lower()
        if choice not in VALID_BACKENDS:
            raise ValueError(
                f"Unknown transcription backend {explicit!r}. "
                f"Valid backends: {', '.join(VALID_BACKENDS)}."
            )
        return choice

    from_env = (env.get(BACKEND_ENV_VAR) or "").strip().lower()
    if from_env:
        if from_env not in VALID_BACKENDS:
            raise ValueError(
                f"{BACKEND_ENV_VAR}={from_env!r} is not a known backend. "
                f"Valid backends: {', '.join(VALID_BACKENDS)}."
            )
        return from_env

    if use_diarization is True:
        return BACKEND_FLUID
    if use_diarization is False or env.get("RECORD_MEETING_DIARIZE") == "0":
        return BACKEND_LOCAL
    if use_diarization is None and env.get("RECORD_MEETING_DIARIZE") == "1":
        return BACKEND_FLUID
    if use_diarization is None and channel_count is not None and channel_count >= 2:
        return BACKEND_FLUID

    return BACKEND_LOCAL


def resolve_whisper_cli(env: dict[str, str] | None = None) -> str:
    """Absolute path to the whisper-cli binary, or raise naming the fix."""
    env = os.environ if env is None else env
    candidate = (env.get(WHISPER_CLI_ENV_VAR) or "").strip() or DEFAULT_WHISPER_CLI

    found = shutil.which(candidate)
    if found:
        return found
    if os.path.isabs(candidate) and Path(candidate).is_file():
        return candidate

    raise LocalWhisperUnavailable(
        f"whisper-cli not found (looked for {candidate!r} on PATH). The local "
        "transcription backend needs whisper.cpp installed:\n"
        "    brew install whisper-cpp\n"
        f"Or set {WHISPER_CLI_ENV_VAR} to the binary's absolute path. "
        "Not falling back to a metered backend: the local path is the default "
        "precisely so transcription costs nothing."
    )


def resolve_ffmpeg(env: dict[str, str] | None = None) -> str:
    """Absolute path to ffmpeg, or raise naming the fix.

    whisper-cli reads only flac/mp3/ogg/wav, so ffmpeg is not optional for the
    real inputs (.m4a voice memos, .mp4 recordings).
    """
    env = os.environ if env is None else env
    candidate = (env.get("TRANSCRIBE_FFMPEG") or "").strip() or "ffmpeg"
    found = shutil.which(candidate)
    if found:
        return found
    if os.path.isabs(candidate) and Path(candidate).is_file():
        return candidate

    raise LocalWhisperUnavailable(
        f"ffmpeg not found (looked for {candidate!r} on PATH). It is required to "
        "convert audio to the 16 kHz mono WAV that whisper-cli accepts — "
        "whisper-cli itself reads only flac/mp3/ogg/wav, and voice memos are "
        ".m4a. Install it with:\n"
        "    brew install ffmpeg"
    )


def resolve_model_path(env: dict[str, str] | None = None) -> Path:
    """Locate the GGML model, or raise naming the expected path and the fetch.

    A missing model is a loud failure by design. The alternative — quietly
    switching to the metered API — turns "the model was never downloaded" into a
    bill, which is exactly the behaviour this backend exists to remove.
    """
    env = os.environ if env is None else env

    override = (env.get(MODEL_ENV_VAR) or "").strip()
    if override:
        path = Path(override).expanduser()
        if path.is_file():
            return path
        raise LocalWhisperUnavailable(
            f"{MODEL_ENV_VAR} points at {path}, which is not a readable file. "
            "Either correct it or unset it to use the default search path "
            f"({MODEL_SEARCH_DIRS[0] / DEFAULT_MODEL_NAME}). "
            "Not falling back to a metered backend."
        )

    model_name = (env.get("TRANSCRIBE_WHISPER_MODEL_NAME") or "").strip() or DEFAULT_MODEL_NAME
    searched: list[Path] = []
    for directory in MODEL_SEARCH_DIRS:
        candidate = directory / model_name
        searched.append(candidate)
        if candidate.is_file():
            return candidate

    expected = searched[0]
    searched_list = "\n".join(f"      {p}" for p in searched)
    raise LocalWhisperUnavailable(
        f"Local whisper model {model_name!r} not found. Searched:\n"
        f"{searched_list}\n"
        "Fetch it with:\n"
        f"    mkdir -p {expected.parent}\n"
        f"    curl -L -o {expected} \\\n"
        "      https://huggingface.co/ggerganov/whisper.cpp/resolve/main/"
        f"{model_name}\n"
        f"Or point {MODEL_ENV_VAR} at an existing model file.\n"
        "NOT falling back to the metered OpenAI API: a missing local model is a "
        "configuration problem to fix, not a reason to start billing."
    )


def convert_to_wav16k_mono(
    audio_path: Path,
    dest_dir: Path,
    *,
    ffmpeg: str | None = None,
    verbose: bool = False,
) -> Path:
    """Normalize any input to the 16 kHz mono WAV whisper.cpp expects.

    Done unconditionally rather than only for unsupported extensions: a .wav at
    48 kHz stereo is accepted by whisper-cli but resampled internally anyway, and
    one code path is easier to reason about than two.
    """
    ffmpeg = ffmpeg or resolve_ffmpeg()
    out_path = dest_dir / f"{audio_path.stem}.16k.wav"
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel", "error",
        "-nostdin",
        "-y",
        "-i", str(audio_path),
        "-ac", "1",
        "-ar", "16000",
        "-c:a", "pcm_s16le",
        str(out_path),
    ]
    if verbose:
        print(f"    Converting to 16 kHz mono WAV: {audio_path.name}")
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if proc.returncode != 0 or not out_path.is_file():
        raise LocalWhisperUnavailable(
            f"ffmpeg failed to convert {audio_path.name} to 16 kHz mono WAV "
            f"(exit {proc.returncode}). stderr:\n{(proc.stderr or '').strip()[:2000]}"
        )
    return out_path


def _validated_segments(data: object) -> list[dict]:
    """whisper-cli ``transcription`` entries, each checked to be an object.

    Raises ``TypeError`` on a malformed shape; callers wrap it into
    ``LocalWhisperUnavailable`` with the source file named.
    """
    if not isinstance(data, dict):
        raise TypeError(f"whisper JSON is {type(data).__name__}, not object")
    segments = data.get("transcription") or []
    if not isinstance(segments, list):
        raise TypeError(
            f"transcription is {type(segments).__name__}, not a list"
        )
    for seg in segments:
        if not isinstance(seg, dict):
            raise TypeError(
                f"transcription segment is {type(seg).__name__}, not object"
            )
    return segments


def _segment_texts(data: object, label: str) -> list[str]:
    """Non-empty stripped segment texts; malformed shapes name ``label``."""
    try:
        return [
            text
            for text in (
                (seg.get("text") or "").strip()
                for seg in _validated_segments(data)
            )
            if text
        ]
    except (TypeError, AttributeError) as exc:
        raise LocalWhisperUnavailable(
            f"malformed whisper JSON in {label}: {exc}"
        ) from exc


def _parse_whisper_json(json_path: Path) -> tuple[str, str | None]:
    """Extract (text, detected_language) from whisper-cli's JSON output.

    Malformed output (unparseable JSON, a non-object document or a non-object
    ``transcription`` entry) raises ``LocalWhisperUnavailable`` naming the file
    and cause, the same contract as ``timed_segments_from_json`` on the long
    path — never a bare ``AttributeError``.
    """
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise LocalWhisperUnavailable(
            f"malformed whisper JSON in {json_path.name}: {exc}"
        ) from exc
    parts = _segment_texts(data, json_path.name)
    text = " ".join(parts).strip()

    # whisper.cpp sometimes opens a segment with a subtitle-style speaker dash
    # ("- 30 by eight failure.") — an artifact of the caption corpus it was
    # trained on, not something the speaker said. Observed on 2 of 23 memos in
    # the 2026-09-12 batch. Stripped only at the very start and only when a
    # space follows, so a genuine leading hyphen in content ("-5 degrees") and
    # every mid-text dash survive untouched.
    text = re.sub(r"^-\s+(?=\S)", "", text)

    language = None
    result = data.get("result")
    if isinstance(result, dict):
        language = result.get("language")
    if not language:
        model = data.get("model")
        if isinstance(model, dict):
            language = model.get("language")

    return text, language


def timed_segments_from_json(
    data: dict, *, source: str | Path | None = None
) -> list[dict]:
    """Timed ``{"start", "end", "text"}`` segments (seconds) from whisper-cli JSON.

    A non-object transcription entry (or other malformed segment shape) raises
    ``LocalWhisperUnavailable`` naming the source file and cause, matching other
    whisper JSON failures — never a bare ``AttributeError``.
    """
    label = Path(source).name if source else "whisper JSON"
    segments = []
    try:
        for seg in _validated_segments(data):
            chunk = (seg.get("text") or "").strip()
            offsets = seg.get("offsets") or {}
            if chunk:
                segments.append({
                    "start": float(offsets["from"]) / 1000.0,
                    "end": float(offsets["to"]) / 1000.0,
                    "text": chunk,
                })
    except (TypeError, AttributeError, KeyError, ValueError) as exc:
        raise LocalWhisperUnavailable(
            f"malformed whisper JSON in {label}: {exc}"
        ) from exc
    return segments


def _chunk_raw_response(found: dict) -> dict:
    """``raw_response`` body carrying the chunker's own records, unchanged.

    ``chunked_whisper.transcribe_track`` records are the shared chunk-evidence
    contract: ``two_track_merge.chunk_outcomes`` and the result validator read
    ``screened: …`` statuses, ``withheld_text`` and seam records from exactly
    this shape, the same one the two-track path stores under each track. A
    deep copy keeps the stored evidence independent of later mutation.
    """
    return {"chunks": copy.deepcopy(list(found.get("chunks") or []))}


def detected_language_from_json(data: dict) -> str | None:
    """The language whisper-cli reports having detected or used, if any."""
    result = data.get("result")
    if isinstance(result, dict) and result.get("language"):
        return result["language"]
    model = data.get("model")
    if isinstance(model, dict):
        return model.get("language")
    return None


def _wav_duration_seconds(wav_path: Path) -> float:
    """Duration of the normalized WAV used to choose the transcription route."""
    try:
        with wave.open(str(wav_path), "rb") as handle:
            return handle.getnframes() / float(handle.getframerate() or 16000)
    except (OSError, EOFError, wave.Error):
        return 0.0


def _initial_prompt(
    explicit: str | None,
    env: dict[str, str] | None = None,
) -> str | None:
    """Runtime-supplied whisper prompt; an explicit empty value disables env."""
    env = os.environ if env is None else env
    raw = explicit if explicit is not None else env.get(WHISPER_PROMPT_ENV_VAR)
    prompt = (raw or "").strip()
    return prompt or None


def _chunk_marker_segments(found: dict) -> list[dict]:
    """Timed disclosure markers for chunk outcomes that produced no segment."""
    try:
        from scripts.two_track_merge import (
            KIND_EMPTY,
            KIND_SILENT,
            KIND_WITHHELD,
            MARKERS,
            chunk_outcomes,
        )
    except ImportError:  # pragma: no cover - path-dependent import
        from two_track_merge import (  # type: ignore[no-redef]
            KIND_EMPTY,
            KIND_SILENT,
            KIND_WITHHELD,
            MARKERS,
            chunk_outcomes,
        )

    withheld_text = {
        (
            float(chunk.get("start", 0.0)),
            float(chunk.get("end", chunk.get("start", 0.0))),
        ): str(chunk.get("withheld_text", ""))
        for chunk in found.get("chunks", [])
        if chunk.get("kind") == "chunk"
        and str(chunk.get("status", "")).startswith("screened")
    }
    markers = []
    for gap in chunk_outcomes(found.get("chunks", []))["gaps"]:
        kind = gap["kind"]
        if kind == KIND_WITHHELD:
            text = (
                "[SPEECH WITHHELD — the model returned text for this stretch but "
                f"the output screen rejected it ({gap['reason']}: "
                f"{gap['detail'] or 'no detail'}). It is not silence and was not "
                "silently dropped.]"
            )
        elif kind in (KIND_SILENT, KIND_EMPTY):
            text = MARKERS[kind]
        else:  # Defensive: chunk_outcomes currently emits only these kinds.
            continue
        markers.append(
            {
                "start": gap["start"],
                "end": gap["end"],
                "text": text,
                **(
                    {
                        "withheld_text": withheld_text.get(
                            (float(gap["start"]), float(gap["end"])),
                            "",
                        )
                    }
                    if kind == KIND_WITHHELD
                    else {}
                ),
            }
        )
    return markers


def _chunked_speech_blocks(found: dict) -> list[dict]:
    """Join chunk segments around disclosure markers, then collapse repeat loops."""
    try:
        from scripts.two_track_merge import collapse_item_repeats
    except ImportError:  # pragma: no cover - path-dependent import
        from two_track_merge import collapse_item_repeats  # type: ignore[no-redef]

    events = [
        {
            "start": float(segment["start"]),
            "end": float(segment["end"]),
            "text": segment["text"].strip(),
            "marker": False,
        }
        for segment in found.get("segments", [])
        if segment.get("text", "").strip()
    ]
    events += [{**marker, "marker": True} for marker in _chunk_marker_segments(found)]
    events.sort(key=lambda item: (item["start"], item["end"], item["marker"]))

    blocks: list[dict] = []
    current = None
    for event in events:
        if event["marker"]:
            if current is not None:
                blocks.append(current)
                current = None
            blocks.append(event)
        elif current is None:
            current = dict(event)
        else:
            current["end"] = max(current["end"], event["end"])
            current["text"] += " " + event["text"]
    if current is not None:
        blocks.append(current)

    collapse_item_repeats([block for block in blocks if not block["marker"]])
    return blocks


def _chunked_transcription_text(found: dict) -> str:
    """Build one transcript from chunk segments, retaining timed gap markers."""
    try:
        from scripts.two_track_merge import format_clock
    except ImportError:  # pragma: no cover - path-dependent import
        from two_track_merge import format_clock  # type: ignore[no-redef]

    blocks = _chunked_speech_blocks(found)
    if not any(block["marker"] for block in blocks):
        return " ".join(block["text"] for block in blocks).strip()

    return "\n\n".join(
        f"[{format_clock(block['start'])}–{format_clock(block['end'])}]\n"
        f"{block['text']}"
        for block in blocks
    )


def _chunked_speech_text(found: dict) -> str:
    """Collapsed usable speech only, excluding disclosure markers."""
    return " ".join(
        block["text"]
        for block in _chunked_speech_blocks(found)
        if not block["marker"]
    ).strip()


def _chunked_is_silence(found: dict) -> bool:
    """True only when every source chunk was positively judged silent."""
    chunks = [
        chunk
        for chunk in found.get("chunks", [])
        if chunk.get("kind") == "chunk"
    ]
    return bool(chunks) and all(chunk.get("status") == "silent" for chunk in chunks)


def _timed_segments(cmd: list[str], tmp_dir: Path) -> tuple[list[dict] | None, str]:
    """Re-run the identical whisper command with timestamps; (segments, reason)."""
    stem = tmp_dir / "timed.out"
    timed = []
    skip_next = False
    for index, part in enumerate(cmd):
        if skip_next:
            skip_next = False
            continue
        if part == "--no-timestamps":
            continue
        if part == "--output-file":
            timed += [part, str(stem)]
            skip_next = True
            continue
        timed.append(part)
    try:
        proc = subprocess.run(timed, capture_output=True, text=True, timeout=3600)
        json_path = Path(f"{stem}.json")
        if proc.returncode != 0 or not json_path.is_file():
            return None, f"timed whisper run failed (exit {proc.returncode})"
        segments = timed_segments_from_json(
            json.loads(json_path.read_text(encoding="utf-8")),
            source=json_path,
        )
        return (segments or None), ("no timed segments" if not segments else "")
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        LocalWhisperUnavailable,
        subprocess.SubprocessError,
    ) as exc:
        return None, f"{type(exc).__name__}"


def transcribe_local(
    audio_path: Path,
    *,
    language: str | None = None,
    initial_prompt: str | None = None,
    verbose: bool = False,
    silence_threshold_db: float | None = None,
    threads: int | None = None,
    diarize: bool = False,
) -> dict:
    """Transcribe one file with whisper-cli.

    With ``diarize=True`` the same 16 kHz mono WAV is also run through local
    FluidAudio (``local_fluid.diarize_mono``) and the outcome is returned under
    ``diarization``. That stage is fail-open: a failure is recorded, never
    raised. Only when it finds more than one speaker is whisper run a second
    time WITH timestamps, to return timed ``segments`` for speaker attribution;
    the single-speaker text is exactly what a non-diarized run produces.

    Returns ``{"transcription_text", "language", "backend", "rms_db",
    "silence"}``. On a silence-gated file the text is an explicit no-speech
    marker and ``silence`` is True — the model is never run, so it cannot
    fabricate. Files longer than ``chunked_whisper.CHUNK_SECONDS`` reuse the
    meeting transcription chunker. ``initial_prompt`` (or
    ``TRANSCRIBE_WHISPER_PROMPT``) may supply runtime proper nouns to whisper;
    this module never fetches or hardcodes product-specific context.

    Raises LocalWhisperUnavailable when the binary, model, or ffmpeg is missing.
    That exception is deliberately NOT convertible into a paid fallback.
    """
    if not audio_path.exists():
        raise FileNotFoundError(f"Audio file not found: {audio_path}")

    # Resolve everything up front so a misconfiguration fails before any work.
    cli = resolve_whisper_cli()
    model = resolve_model_path()
    ffmpeg = resolve_ffmpeg()

    threshold = (
        DEFAULT_SILENCE_THRESHOLD_DB
        if silence_threshold_db is None
        else silence_threshold_db
    )
    prompt = _initial_prompt(initial_prompt)

    if verbose:
        print(f"    Local whisper-cli: {cli}")
        print(f"    Model: {model.name} ({model.stat().st_size / 1e6:.0f} MB)")

    with tempfile.TemporaryDirectory(prefix="local-whisper-") as tmp:
        tmp_dir = Path(tmp)
        wav_path = convert_to_wav16k_mono(
            audio_path, tmp_dir, ffmpeg=ffmpeg, verbose=verbose
        )

        # --- Silence gate, BEFORE the model ---------------------------------
        # Reuses the threshold calibrated for the streaming path. A failed
        # measurement returns None and transcribes anyway: a broken measurement
        # must never silently discard real audio.
        rms_db = measure_sustained_rms_db(wav_path, ffmpeg=ffmpeg)
        if rms_db is not None and rms_db < threshold:
            if verbose:
                print(
                    f"    Silence gate: {rms_db:.1f} dB sustained RMS is below "
                    f"{threshold:.0f} dB — not transcribing (Whisper would "
                    "fabricate)."
                )
            return {
                "transcription_text": NO_SPEECH_MARKER_LEVEL.format(
                    rms=rms_db, threshold=threshold
                ),
                "language": language or "en",
                "backend": BACKEND_LOCAL,
                "transcription_engine": TRANSCRIPTION_ENGINE_LOCAL,
                "transcription_model": model.name,
                "rms_db": round(rms_db, 1),
                "silence": True,
            }

        duration = _wav_duration_seconds(wav_path)
        chunked_found = None
        cmd = None
        if duration > 0:
            try:
                from scripts import chunked_whisper
            except ImportError:  # pragma: no cover - path-dependent import
                import chunked_whisper  # type: ignore[no-redef]
        else:
            chunked_whisper = None

        if chunked_whisper is not None and duration > chunked_whisper.CHUNK_SECONDS:
            detected_languages: list[str] = []
            real_asr = chunked_whisper.whisper_cli_asr(
                cli,
                model,
                threads=threads,
                initial_prompt=prompt,
            )

            def asr(wav: Path, expected: str | None, extra: list[str]):
                found_segments, found_language = real_asr(wav, expected, extra)
                if found_language:
                    detected_languages.append(found_language)
                return found_segments, found_language

            if verbose:
                print(
                    f"    Running whisper-cli in {chunked_whisper.CHUNK_SECONDS}-"
                    f"second chunks on {wav_path.name}..."
                )
            try:
                chunked_found = chunked_whisper.transcribe_track(
                    wav_path,
                    asr=asr,
                    language=language,
                    ffmpeg=ffmpeg,
                    duration=duration,
                    label="Memo",
                    progress=(
                        lambda message: print(f"    {message}", file=sys.stderr, flush=True)
                    )
                    if verbose
                    else None,
                )
            except (
                RuntimeError,
                OSError,
                ValueError,
                KeyError,
                TypeError,
                subprocess.SubprocessError,
            ) as exc:
                raise LocalWhisperUnavailable(
                    f"chunked whisper-cli failed on {audio_path.name}: {exc}"
                ) from exc
            text = _chunked_transcription_text(chunked_found)
            text_for_screen = _chunked_speech_text(chunked_found)
            detected = detected_languages[0] if detected_languages else None
        else:
            out_stem = tmp_dir / f"{audio_path.stem}.out"
            cmd = [
                cli,
                "-m", str(model),
                "-f", str(wav_path),
                "--output-json",
                "--output-file", str(out_stem),
                "--no-prints",
                "--no-timestamps",
                "-l", (language or "auto"),
            ]
            if prompt:
                cmd += ["--prompt", prompt]
            if threads and threads > 0:
                cmd += ["-t", str(threads)]

            if verbose:
                print(f"    Running whisper-cli on {wav_path.name}...")
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
            json_path = Path(f"{out_stem}.json")
            if proc.returncode != 0 or not json_path.is_file():
                raise LocalWhisperUnavailable(
                    f"whisper-cli failed on {audio_path.name} (exit "
                    f"{proc.returncode}). stderr:\n"
                    f"{(proc.stderr or '').strip()[:2000]}"
                )

            text, detected = _parse_whisper_json(json_path)
            text_for_screen = text

        diarization = None
        segments = None
        chunked_silence = (
            _chunked_is_silence(chunked_found)
            if chunked_found is not None
            else False
        )
        if diarize and (
            text_for_screen
            or (chunked_found is not None and not chunked_silence)
        ):
            try:
                from scripts.local_fluid import diarize_mono
            except ImportError:  # pragma: no cover - path-dependent import
                from local_fluid import diarize_mono  # type: ignore[no-redef]
            diarization = diarize_mono(wav_path)
            if diarization["status"] == "ok" and diarization["speaker_count"] > 1:
                if chunked_found is not None:
                    segments = sorted(
                        [*chunked_found["segments"], *_chunk_marker_segments(chunked_found)],
                        key=lambda segment: (segment["start"], segment["end"]),
                    )
                    why = ""
                else:
                    assert cmd is not None
                    segments, why = _timed_segments(cmd, tmp_dir)
                    if segments is not None:
                        # The timed text did not pass the result-side screen the
                        # first run's text does; screen it the same way.
                        timed_verdict = screen_transcription(
                            " ".join(seg["text"] for seg in segments),
                            expected_language=language,
                            detected_language=detected,
                            window_seconds=None,
                        )
                        if timed_verdict.filtered:
                            segments, why = None, f"screened: {timed_verdict.reason}"
                if segments is None:
                    diarization["note"] = "speaker turns could not be attached"
                    print(f"Timed whisper pass unusable ({why}); "
                          "keeping untimed text.", file=sys.stderr)

    chunk_raw = (
        _chunk_raw_response(chunked_found) if chunked_found is not None else None
    )

    if chunked_found is not None and not text_for_screen:
        return {
            "transcription_text": text,
            "language": detected or language or "en",
            "backend": BACKEND_LOCAL,
            "transcription_engine": TRANSCRIPTION_ENGINE_LOCAL,
            "transcription_model": model.name,
            "rms_db": None if rms_db is None else round(rms_db, 1),
            "silence": chunked_silence,
            "raw_response": chunk_raw,
            **({"diarization": diarization} if diarization is not None else {}),
            **({"segments": segments} if segments else {}),
        }

    # --- Result-side screen, AFTER the model ------------------------------
    # The loudness gate above has a structural ceiling and this file class is
    # exactly where it runs out. Measured here on 2026-09-13: the operator's
    # accidental 0.75s capture scores -31.5 dB p95 — inside their own verified
    # speech range of -28 to -37 dB, 18 dB ABOVE the gate. No level threshold
    # separates it, and one set low enough to try would clip real quiet speech.
    # (The -35.2 dB figure recorded for this file earlier is its whole-file
    # average, not the p95 the gate uses; the two are not comparable.)
    #
    # So the question is asked of the OUTPUT instead, using the already-calibrated
    # signals in ``hallucination_filter`` rather than a second threshold of our
    # own. On this file whisper-cli returns "." — `has_no_letters` catches it as
    # `no_speech_content`. Reusing that module also means a fabricated transcript
    # in a language nobody spoke is caught on the batch path, not just streaming.
    verdict = screen_transcription(
        text_for_screen,
        expected_language=language,
        detected_language=detected,
        # Deliberately not passed: `too_short_for_window` is for fixed-length
        # streaming windows. A batch memo is as long as the operator spoke, so a
        # short file yielding short text is ordinary, not suspicious.
        window_seconds=None,
    )
    if verdict.filtered:
        if verbose:
            print(
                f"    No-speech screen: {verdict.reason} ({verdict.detail}) — "
                f"recording as an empty capture rather than {text!r}."
            )
        return {
            "transcription_text": NO_SPEECH_MARKER_RESULT.format(
                reason=verdict.reason,
                detail=verdict.detail,
                raw=text_for_screen,
                rms="unmeasured" if rms_db is None else f"{rms_db:.1f} dB",
            ),
            "language": detected or language or "en",
            "backend": BACKEND_LOCAL,
            "transcription_engine": TRANSCRIPTION_ENGINE_LOCAL,
            "transcription_model": model.name,
            "rms_db": None if rms_db is None else round(rms_db, 1),
            "silence": True,
            "filtered_reason": verdict.reason,
            **({"raw_response": chunk_raw} if chunk_raw is not None else {}),
        }

    return {
        "transcription_text": text,
        "language": detected or language or "en",
        "backend": BACKEND_LOCAL,
        "transcription_engine": TRANSCRIPTION_ENGINE_LOCAL,
        "transcription_model": model.name,
        "rms_db": None if rms_db is None else round(rms_db, 1),
        "silence": False,
        **({"raw_response": chunk_raw} if chunk_raw is not None else {}),
        **({"diarization": diarization} if diarization is not None else {}),
        **({"segments": segments} if segments else {}),
    }


def main(argv: list[str] | None = None) -> int:
    """Thin CLI, for checking the local path in isolation."""
    import argparse

    ap = argparse.ArgumentParser(description="Transcribe one file with local whisper-cli")
    ap.add_argument("audio_file")
    ap.add_argument("--language", default=None)
    ap.add_argument(
        "--prompt",
        default=None,
        help=(
            "Optional runtime proper-noun prompt for whisper-cli "
            f"(otherwise {WHISPER_PROMPT_ENV_VAR})"
        ),
    )
    ap.add_argument("--threads", type=int, default=None)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    try:
        result = transcribe_local(
            Path(args.audio_file).expanduser(),
            language=args.language,
            initial_prompt=args.prompt,
            verbose=not args.quiet,
            threads=args.threads,
        )
    except (LocalWhisperUnavailable, FileNotFoundError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(result["transcription_text"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
