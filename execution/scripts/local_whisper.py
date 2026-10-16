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

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
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
BACKEND_ELEVENLABS = "elevenlabs"
BACKEND_OPENAI = "openai"
VALID_BACKENDS = (BACKEND_LOCAL, BACKEND_ELEVENLABS, BACKEND_OPENAI)
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
    3. ``use_diarization`` true, or ``RECORD_MEETING_DIARIZE=1`` with a key
       present → ElevenLabs. Diarization is the one capability local genuinely
       lacks, and meeting recordings need speaker separation, so this stays
       ahead of the local default rather than behind it. Regressing
       multi-speaker meetings to a diarization-less backend would silently
       destroy the thing ``record_meeting`` and ``analyze-meeting`` exist for.
    4. Multi-channel audio with a key present → ElevenLabs.
    5. Mono or unknown-channel audio → local.

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

    has_key = bool((env.get("ELEVENLABS_API_KEY") or "").strip())
    if use_diarization is True:
        return BACKEND_ELEVENLABS
    if use_diarization is None and has_key and env.get("RECORD_MEETING_DIARIZE") == "1":
        return BACKEND_ELEVENLABS
    if use_diarization is None and has_key and channel_count is not None and channel_count >= 2:
        return BACKEND_ELEVENLABS

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


def _parse_whisper_json(json_path: Path) -> tuple[str, str | None]:
    """Extract (text, detected_language) from whisper-cli's JSON output."""
    data = json.loads(json_path.read_text(encoding="utf-8"))

    segments = data.get("transcription") or []
    parts: list[str] = []
    for seg in segments:
        chunk = (seg.get("text") or "").strip()
        if chunk:
            parts.append(chunk)
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


def transcribe_local(
    audio_path: Path,
    *,
    language: str | None = None,
    verbose: bool = False,
    silence_threshold_db: float | None = None,
    threads: int | None = None,
) -> dict:
    """Transcribe one file with whisper-cli.

    Returns ``{"transcription_text", "language", "backend", "rms_db",
    "silence"}``. On a silence-gated file the text is an explicit no-speech
    marker and ``silence`` is True — the model is never run, so it cannot
    fabricate.

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
        text,
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
                raw=text,
                rms="unmeasured" if rms_db is None else f"{rms_db:.1f} dB",
            ),
            "language": detected or language or "en",
            "backend": BACKEND_LOCAL,
            "transcription_engine": TRANSCRIPTION_ENGINE_LOCAL,
            "transcription_model": model.name,
            "rms_db": None if rms_db is None else round(rms_db, 1),
            "silence": True,
            "filtered_reason": verdict.reason,
        }

    return {
        "transcription_text": text,
        "language": detected or language or "en",
        "backend": BACKEND_LOCAL,
        "transcription_engine": TRANSCRIPTION_ENGINE_LOCAL,
        "transcription_model": model.name,
        "rms_db": None if rms_db is None else round(rms_db, 1),
        "silence": False,
    }


def main(argv: list[str] | None = None) -> int:
    """Thin CLI, for checking the local path in isolation."""
    import argparse

    ap = argparse.ArgumentParser(description="Transcribe one file with local whisper-cli")
    ap.add_argument("audio_file")
    ap.add_argument("--language", default=None)
    ap.add_argument("--threads", type=int, default=None)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    try:
        result = transcribe_local(
            Path(args.audio_file).expanduser(),
            language=args.language,
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
