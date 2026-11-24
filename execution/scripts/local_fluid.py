"""On-device diarization and timestamped ASR using the FluidAudio CLI.

The caller explicitly owns installing the CLI/models. Missing dependencies or
invalid output fail loudly; no network transcription or paid fallback exists.
Raw model outputs are returned for preservation alongside the readable text.
"""

from __future__ import annotations

import hashlib
from array import array
import json
import logging
import math
import os
import shutil
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

from transcript_utterances import group_utterances

try:
    from scripts.two_track_merge import collapse_item_repeats
except ImportError:  # pragma: no cover - path-dependent import
    from two_track_merge import collapse_item_repeats  # type: ignore[no-redef]

log = logging.getLogger("local_fluid")

BACKEND_FLUID = "fluid"
ENGINE_FLUID = "local_fluid_audio"

DUAL_MONO_RATE = 16000
DUAL_MONO_MIN_RMS = 128.0  # Below about -48 dBFS, a segment carries no speech to compare.
# "Same speech in both channels": two microphones (or two copies) that hear the same speaker. It is
# measured at ZERO lag, which is exactly what an unshifted downmix adds, over one-second segments sampled
# across the whole clip. Real two-microphone voice memos correlate at 0.80 to 0.91 over the clip, and at
# most 0.0001 of their speech energy sits in seconds below the 0.20 floor (a stray noisy second; one
# five-second memo reached 0.063), while two different speakers on separate tracks correlate at about 0.003
# with most of their energy in seconds below the floor. A brief independent stretch is not a reason to keep
# two transcripts: the downmix keeps every sound from either microphone.
DUAL_MONO_MIN_CORRELATION = 0.70
DUAL_MONO_SEGMENT_FLOOR = 0.20  # a measured second below this holds audio the other channel does not
DUAL_MONO_MAX_INDEPENDENT_SHARE = 0.10  # share of the measured speech energy allowed below the floor
DUAL_MONO_SEGMENT_SAMPLES = 16000  # one second at 16 kHz
DUAL_MONO_MAX_SEGMENTS = 300  # bounds work and memory on long clips; segments span the whole clip
DUAL_MONO_MIN_ACTIVE_SEGMENTS = 3
DUAL_MONO_DOWNMIX_FILTER = "pan=mono|c0=0.5*c0+0.5*c1"


class LocalFluidUnavailable(RuntimeError):
    pass


def resolve_fluid_cli() -> str:
    candidate = os.environ.get("TRANSCRIBE_FLUID_CLI", "fluidaudiocli").strip()
    found = shutil.which(candidate)
    if not found:
        raise LocalFluidUnavailable(
            "FluidAudio CLI unavailable. Install FluidInference/FluidAudio and "
            "set TRANSCRIBE_FLUID_CLI to the built fluidaudiocli executable. "
            "No cloud fallback was attempted."
        )
    return found


def _run_json(cli: str, args: list[str], output: Path, *, timeout: float | None = None) -> dict:
    stage = args[0] if args else "unknown"
    try:
        result = subprocess.run(
            [cli, *args, str(output)],
            capture_output=True,
            text=True,
            timeout=timeout if timeout is not None else float(os.environ.get("TRANSCRIBE_FLUID_TIMEOUT", "3600")),
        )
    except subprocess.TimeoutExpired as exc:
        raise LocalFluidUnavailable(
            f"FluidAudio stage={stage} timed out; no fallback attempted"
        ) from exc
    # Some CLI versions log an error but exit zero. The output is required too.
    if result.returncode != 0 or not output.is_file():
        stderr = (result.stderr or "").strip()
        # Profiling output can bury the initial Metal error. Keep both ends,
        # bounded, rather than losing the cause to the final timing summary.
        detail = stderr if len(stderr) <= 2000 else stderr[:1000] + " [...] " + stderr[-1000:]
        raise LocalFluidUnavailable(
            f"FluidAudio failed ({result.returncode}) stage={stage}: {detail}"
        )
    try:
        value = json.loads(output.read_text())
    except (ValueError, OSError) as exc:
        raise LocalFluidUnavailable("FluidAudio returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise LocalFluidUnavailable("FluidAudio output must be an object")
    return value


def _time(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise LocalFluidUnavailable("Invalid timestamp in FluidAudio output")
    if not math.isfinite(value) or value < 0:
        raise LocalFluidUnavailable("Invalid timestamp in FluidAudio output")
    return float(value)


def align_words(
    asr: dict, diarization: dict, *, channel: str | None = None
) -> list[dict]:
    """Assign words by temporal overlap; preserve uncertain/overlapping labels.

    A word outside all speech segments stays Unassigned. More than one active
    speaker stays ambiguous rather than assigning the nearest/first speaker.
    """
    segments = diarization.get("segments")
    words = asr.get("wordTimings")
    if not isinstance(segments, list) or not isinstance(words, list):
        raise LocalFluidUnavailable("FluidAudio output lacks segments/wordTimings")
    speech = []
    for segment in segments:
        start = _time(segment.get("startTimeSeconds"))
        end = _time(segment.get("endTimeSeconds"))
        speaker = segment.get("speakerId")
        if end <= start or not isinstance(speaker, str) or not speaker:
            raise LocalFluidUnavailable("Invalid FluidAudio speaker segment")
        speech.append((start, end, speaker))
    events = []
    for word in words:
        start, end = _time(word.get("startTime")), _time(word.get("endTime"))
        text = word.get("word")
        if end < start or not isinstance(text, str) or not text.strip():
            raise LocalFluidUnavailable("Invalid FluidAudio word timing")
        speakers = sorted(
            {speaker for a, b, speaker in speech if a < end and b > start}
        )
        label = " + ".join(speakers) if speakers else "Unassigned"
        if channel:
            label = f"{channel}/{label}"
        events.append(
            {
                "start": start,
                "end": end,
                "text": text,
                "speaker": label,
                "speaker_candidates": speakers,
                "confidence": word.get("confidence"),
            }
        )
    if asr.get("text", "").strip() and not events:
        raise LocalFluidUnavailable(
            "ASR text has no word timestamps; refusing unaligned transcript"
        )
    if speech and not events:
        raise LocalFluidUnavailable(
            "Diarization detected speech but ASR returned no words"
        )
    return events


def format_events(events: list[dict], *, gap: float = 1.2, stitch: float = 4.0) -> str:
    turns = group_utterances(
        [{**event, "group": event["speaker"]} for event in events],
        gap=gap,
        stitch=stitch,
    )
    return (
        "\n\n".join(
            f"[{turn['start']:.2f}–{turn['end']:.2f}] [{turn['group']}]\n{turn['text']}"
            for turn in turns
        )
        or "[NO SPEECH DETECTED — local diarization and ASR returned no words.]"
    )


def _resolve_compute_units() -> str:
    # The offline CLI's default `all` placement may use Metal and fail with a
    # command-buffer timeout. Use its documented off-GPU mode from the outset;
    # this keeps the same local diarization model and does not drop speakers.
    compute_units = os.environ.get("TRANSCRIBE_FLUID_COMPUTE_UNITS", "cpu-only").strip()
    if compute_units not in {"cpu-only", "ane", "all"}:
        raise LocalFluidUnavailable(
            "TRANSCRIBE_FLUID_COMPUTE_UNITS must be cpu-only, ane or all"
        )
    return compute_units


DIARIZATION_METHOD = "FluidAudio offline"

# Optional step on a short memo: bound it by the audio, never the 1 h default.
MONO_TIMEOUT_FLOOR_SECONDS = 60.0
MONO_TIMEOUT_CAP_SECONDS = 900.0
MONO_TIMEOUT_PER_AUDIO_SECOND = 4.0

# Fixed, path-free vocabulary for the stored header. Detail goes to the log.
FAIL_CLI_NOT_FOUND = "cli not found"
FAIL_TIMED_OUT = "timed out"
FAIL_EXITED = "exited with error"
FAIL_UNREADABLE = "unreadable output"

_CLI_MISSING_LOGGED = False
_BUILD_ID_CACHE: dict[str, str | None] = {}


def _cli_build_id(cli: str) -> str | None:
    """Short content hash of the CLI binary (it reports no version); cached."""
    if cli in _BUILD_ID_CACHE:
        return _BUILD_ID_CACHE[cli]
    try:
        digest = hashlib.sha256()
        with open(cli, "rb") as handle:
            while block := handle.read(1024 * 1024):
                digest.update(block)
        value = digest.hexdigest()[:8]
    except OSError:
        value = None
    _BUILD_ID_CACHE[cli] = value
    return value


def _mono_timeout(wav_path: Path) -> float:
    try:
        with wave.open(str(wav_path), "rb") as handle:
            duration = handle.getnframes() / float(handle.getframerate() or 1)
    except (OSError, EOFError, wave.Error):
        duration = 0.0
    return min(
        MONO_TIMEOUT_CAP_SECONDS,
        max(MONO_TIMEOUT_FLOOR_SECONDS, duration * MONO_TIMEOUT_PER_AUDIO_SECOND),
    )


def _failure_category(message: str) -> str:
    if "unavailable" in message and "CLI" in message:
        return FAIL_CLI_NOT_FOUND
    if "timed out" in message:
        return FAIL_TIMED_OUT
    if "FluidAudio failed (" in message:
        return FAIL_EXITED
    return FAIL_UNREADABLE


def diarize_mono(wav_path: Path) -> dict:
    """Diarize one 16 kHz mono WAV with the same offline FluidAudio stage the
    multichannel path uses, and describe the outcome. Never raises.

    Diarization is fail-open: a failure is returned as ``status="failed"`` with
    a fixed ``category`` (no paths or CLI output), so the transcript still lands
    and its header says so. The detail is logged. Raw speaker embeddings are
    dropped; only speaker/time segments are kept.
    """
    global _CLI_MISSING_LOGGED
    try:
        cli = resolve_fluid_cli()
        compute_units = _resolve_compute_units()
        with tempfile.TemporaryDirectory(prefix="local-fluid-diar-") as temporary:
            raw = _run_json(
                cli,
                ["process", str(wav_path), "--mode", "offline",
                 "--compute-units", compute_units, "--output"],
                Path(temporary) / "diar.json",
                timeout=_mono_timeout(wav_path),
            )
        segments = []
        for segment in raw.get("segments") or []:
            start = _time(segment.get("startTimeSeconds"))
            end = _time(segment.get("endTimeSeconds"))
            speaker = segment.get("speakerId")
            if end <= start or not isinstance(speaker, str) or not speaker:
                raise LocalFluidUnavailable("Invalid FluidAudio speaker segment")
            segments.append({"start": start, "end": end, "speaker": speaker})
        segments.sort(key=lambda item: (item["start"], item["speaker"]))
        return {
            "status": "ok",
            "method": DIARIZATION_METHOD,
            "compute_units": compute_units,
            "cli_build": _cli_build_id(cli),
            "speaker_count": len({item["speaker"] for item in segments}),
            "segments": segments,
        }
    except Exception as exc:  # noqa: BLE001 - diarization must never block a transcript
        message = " ".join(str(exc).split()) or type(exc).__name__
        category = _failure_category(message)
        if category != FAIL_CLI_NOT_FOUND or not _CLI_MISSING_LOGGED:
            _CLI_MISSING_LOGGED = _CLI_MISSING_LOGGED or category == FAIL_CLI_NOT_FOUND
            log.warning("Mono diarization failed (%s); transcript continues "
                        "undiarized: %s", category, message[:1000])
        return {"status": "failed", "method": DIARIZATION_METHOD, "category": category}


def diarization_header(diarization: dict) -> str:
    """One-line transcript header: the outcome first, then method and build."""
    method = diarization.get("method") or DIARIZATION_METHOD
    if diarization.get("status") == "ok":
        build = diarization.get("cli_build")
        how = f"detected locally by {method}" + (f" (build {build})" if build else "")
        count = diarization.get("speaker_count", 0)
        outcome = str(count) if count else "no separate speakers found"
        note = diarization.get("note")
        return f"[Speakers: {outcome}, {how}{'; ' + note if note else ''}]"
    return (
        f"[Speakers: not detected, {method} failed "
        f"({diarization.get('category', FAIL_UNREADABLE)}); "
        "transcript is not speaker-attributed]"
    )


def format_clock(seconds: float) -> str:
    total = int(seconds)
    return f"{total // 60}:{total % 60:02d}"


def attribute_segments(
    segments: list[dict], diarization: dict, *, collapse_repeats: bool = False
) -> list[dict]:
    """Label timed ASR segments with the diarized speaker of greatest overlap.

    A segment overlapping no speech stays ``Unassigned``; nothing is guessed.
    Consecutive segments from one speaker merge into one turn.

    ``collapse_repeats`` collapses whisper repeat loops on the joined turns. It
    is for chunked long-audio results only (``chunked_whisper`` emits one loop
    as many consecutive segments); a short single-pass memo keeps every
    segment's words exactly as before, including genuine repetition.
    """
    speech = diarization.get("segments") or []
    turns: list[dict] = []
    for segment in segments:
        overlap: dict[str, float] = {}
        for item in speech:
            shared = min(segment["end"], item["end"]) - max(segment["start"], item["start"])
            if shared > 0:
                overlap[item["speaker"]] = overlap.get(item["speaker"], 0.0) + shared
        speaker = max(sorted(overlap), key=overlap.get) if overlap else "Unassigned"
        if turns and turns[-1]["speaker"] == speaker:
            turns[-1]["end"] = segment["end"]
            turns[-1]["text"] += " " + segment["text"]
        else:
            turns.append({**segment, "speaker": speaker})
    if collapse_repeats:
        collapse_item_repeats(turns)
    return turns


def format_turns(turns: list[dict]) -> str:
    return "\n\n".join(
        f"[{format_clock(turn['start'])}\u2013{format_clock(turn['end'])}] [{turn['speaker']}]\n{turn['text']}"
        for turn in turns
    )


def _wav_frames(path: Path) -> int | None:
    """Frame count of a 16 kHz mono 16-bit WAV, or None for anything else."""
    try:
        with wave.open(str(path), "rb") as wav:
            if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) != (1, 2, DUAL_MONO_RATE):
                return None
            return wav.getnframes()
    except (OSError, EOFError, wave.Error):
        return None


def _segment_starts(frames: int) -> list[int]:
    """Start frames of the one-second segments measured: all of them for a short clip, evenly spread
    across the whole clip (never just the beginning) for a long one, and always the very end."""
    size = DUAL_MONO_SEGMENT_SAMPLES
    if frames < size:
        return []
    whole = frames // size
    if whole <= DUAL_MONO_MAX_SEGMENTS:
        starts = [index * size for index in range(whole)]
    else:
        step = whole / DUAL_MONO_MAX_SEGMENTS
        starts = [int(index * step) * size for index in range(DUAL_MONO_MAX_SEGMENTS)]
    tail = frames - size
    if not starts or tail > starts[-1]:
        starts.append(tail)
    return starts


def _read_segments(path: Path, starts: list[int]) -> list[array] | None:
    segments: list[array] = []
    try:
        with wave.open(str(path), "rb") as wav:
            for start in starts:
                wav.setpos(start)
                samples = array("h")
                samples.frombytes(wav.readframes(DUAL_MONO_SEGMENT_SAMPLES))
                if sys.byteorder != "little":
                    samples.byteswap()
                if len(samples) != DUAL_MONO_SEGMENT_SAMPLES:
                    return None
                segments.append(samples)
    except (OSError, EOFError, wave.Error):
        return None
    return segments


def _dual_mono_similarity(left_path: Path, right_path: Path) -> tuple[float, float] | None:
    """Return (overall correlation, independent energy share) of two aligned mono channels.

    Both are measured at zero lag over the sampled one-second segments that carry speech-level audio. The
    share is the fraction of that audio's energy lying in seconds whose correlation is below the floor
    (including a second heard on only one channel). A missing, malformed, unequal, short or silent
    measurement is inconclusive (None) and cannot authorise a downmix.
    """
    frames = _wav_frames(left_path)
    if frames is None or frames != _wav_frames(right_path):
        return None
    starts = _segment_starts(frames)
    left, right = _read_segments(left_path, starts), _read_segments(right_path, starts)
    if not starts or left is None or right is None:
        return None
    count = DUAL_MONO_SEGMENT_SAMPLES
    active = 0
    dot_total = left_total = right_total = 0
    peak_total = independent = 0
    for a, b in zip(left, right):
        dot = energy_a = energy_b = 0
        for x, y in zip(a, b):
            dot += x * y
            energy_a += x * x
            energy_b += y * y
        peak = max(energy_a, energy_b)
        if math.sqrt(peak / count) < DUAL_MONO_MIN_RMS:
            continue  # silence: nothing to compare
        active += 1
        denominator = math.sqrt(energy_a * energy_b)
        if (dot / denominator if denominator else 0.0) < DUAL_MONO_SEGMENT_FLOOR:
            independent += peak
        peak_total += peak
        dot_total += dot
        left_total += energy_a
        right_total += energy_b
    denominator = math.sqrt(left_total * right_total)
    if active < DUAL_MONO_MIN_ACTIVE_SEGMENTS or not denominator:
        return None
    return dot_total / denominator, independent / peak_total


def _is_dual_mono(similarity: tuple[float, float] | None) -> bool:
    return bool(
        similarity is not None
        and similarity[0] >= DUAL_MONO_MIN_CORRELATION
        and similarity[1] <= DUAL_MONO_MAX_INDEPENDENT_SHARE
    )


def transcribe_fluid(
    audio_path: Path,
    *,
    language: str | None = None,
    verbose: bool = False,
    channel_count: int = 1,
) -> dict:
    cli = resolve_fluid_cli()
    if not audio_path.is_file():
        raise FileNotFoundError(audio_path)
    compute_units = _resolve_compute_units()
    all_events, raw = [], []
    with tempfile.TemporaryDirectory(prefix="local-fluid-") as temporary:
        root = Path(temporary)
        sources = [(audio_path, None)]
        similarity = None
        if channel_count > 1:
            ffmpeg = shutil.which(os.environ.get("TRANSCRIBE_FFMPEG", "ffmpeg"))
            if not ffmpeg:
                raise LocalFluidUnavailable("ffmpeg required to preserve separate audio channels")
            sources = []
            for index in range(channel_count):
                source = root / f"channel-{index}.wav"
                subprocess.run(
                    [
                        ffmpeg,
                        "-v",
                        "error",
                        "-i",
                        str(audio_path),
                        "-af",
                        f"pan=mono|c0=c{index}",
                        "-ar",
                        str(DUAL_MONO_RATE),
                        str(source),
                    ],
                    check=True,
                    capture_output=True,
                    timeout=300,
                )
                sources.append((source, f"Channel_{index}"))
            if channel_count == 2:
                similarity = _dual_mono_similarity(sources[0][0], sources[1][0])
                if _is_dual_mono(similarity):
                    # Both channels hear the same speech (zero-lag correlation, almost no independent
                    # audio), so their sum cannot cancel; the downmix keeps every sound from either
                    # microphone and transcribes it once. If it cannot be made, keep both channels.
                    mix = root / "downmix.wav"
                    try:
                        subprocess.run(
                            [
                                ffmpeg,
                                "-v",
                                "error",
                                "-i",
                                str(audio_path),
                                "-af",
                                DUAL_MONO_DOWNMIX_FILTER,
                                "-ar",
                                str(DUAL_MONO_RATE),
                                str(mix),
                            ],
                            check=True,
                            capture_output=True,
                            timeout=300,
                        )
                        sources = [(mix, None)]
                    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
                        similarity = None
        collapsed = channel_count == 2 and _is_dual_mono(similarity)
        for index, (source, channel) in enumerate(sources):
            diar = _run_json(
                cli,
                ["process", str(source), "--mode", "offline",
                 "--compute-units", compute_units, "--output"],
                root / f"diar-{index}.json",
            )
            args = ["transcribe", str(source), "--model-version", "v3"]
            if language and language != "auto":
                args += ["--language", language]
            asr = _run_json(cli, [*args, "--output-json"], root / f"asr-{index}.json")
            events = align_words(asr, diar, channel=channel)
            all_events.extend(events)
            raw.append({"channel": channel, "diarization": diar, "asr": asr})
    transcript = format_events(all_events)
    if collapsed and all_events:
        # A silent result keeps the exact headerless silent text the result validator requires.
        speakers = {segment["speakerId"] for segment in raw[0]["diarization"]["segments"]}
        # Reuse the one header helper so the zero-speaker wording matches every other result.
        header = diarization_header({
            "status": "ok",
            "speaker_count": len(speakers),
            "note": (
                f"dual-mono, collapsed; correlation={similarity[0]:.6f}; "
                f"independent_share={similarity[1]:.6f}"
            ),
        })
        transcript = f"{header}\n\n{transcript}"
    return {
        "transcription_text": transcript,
        "language": language or "auto",
        "audio_duration_seconds": max(
            (x["diarization"].get("durationSeconds", 0) for x in raw), default=0
        ),
        "file_size_bytes": audio_path.stat().st_size,
        "backend": BACKEND_FLUID,
        "transcription_engine": ENGINE_FLUID,
        "transcription_model": "FluidAudio offline + Parakeet v3",
        "raw_response": {"tracks": raw, "aligned_words": all_events},
        "silence": not all_events,
    }
