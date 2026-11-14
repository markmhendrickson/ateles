"""On-device diarization and timestamped ASR using the FluidAudio CLI.

The caller explicitly owns installing the CLI/models. Missing dependencies or
invalid output fail loudly; no network transcription or paid fallback exists.
Raw model outputs are returned for preservation alongside the readable text.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from transcript_utterances import group_utterances

BACKEND_FLUID = "fluid"
ENGINE_FLUID = "local_fluid_audio"


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


def _run_json(cli: str, args: list[str], output: Path) -> dict:
    stage = args[0] if args else "unknown"
    try:
        result = subprocess.run(
            [cli, *args, str(output)],
            capture_output=True,
            text=True,
            timeout=float(os.environ.get("TRANSCRIBE_FLUID_TIMEOUT", "3600")),
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
_FAILURE_REASON_MAX = 300


def _cli_build_id(cli: str) -> str | None:
    """Short content hash of the CLI binary: the CLI reports no version string."""
    try:
        digest = hashlib.sha256()
        with open(cli, "rb") as handle:
            while block := handle.read(1024 * 1024):
                digest.update(block)
        return digest.hexdigest()[:12]
    except OSError:
        return None


def diarize_mono(wav_path: Path) -> dict:
    """Diarize one 16 kHz mono WAV with the same offline FluidAudio stage the
    multichannel path uses, and describe the outcome. Never raises.

    Diarization is fail-open: a failure is returned as ``status="failed"`` with
    the reason, so the transcript still lands and its header says so. Raw
    speaker embeddings are dropped; only speaker/time segments are kept.
    """
    try:
        cli = resolve_fluid_cli()
        compute_units = _resolve_compute_units()
        with tempfile.TemporaryDirectory(prefix="local-fluid-diar-") as temporary:
            raw = _run_json(
                cli,
                ["process", str(wav_path), "--mode", "offline",
                 "--compute-units", compute_units, "--output"],
                Path(temporary) / "diar.json",
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
        reason = " ".join(str(exc).split()) or type(exc).__name__
        if len(reason) > _FAILURE_REASON_MAX:
            reason = reason[:_FAILURE_REASON_MAX] + "..."
        return {"status": "failed", "method": DIARIZATION_METHOD, "reason": reason}


def diarization_header(diarization: dict) -> str:
    """One-line transcript header naming the diarization method and outcome."""
    method = diarization.get("method") or DIARIZATION_METHOD
    if diarization.get("status") == "ok":
        build = diarization.get("cli_build")
        detail = ", ".join(
            [f"cli build {build}" if build else "version not reported by CLI"]
            + ([diarization["compute_units"]] if diarization.get("compute_units") else [])
        )
        count = diarization.get("speaker_count", 0)
        if count == 0:
            outcome = "no speech segments detected"
        else:
            outcome = f"{count} speaker{'' if count == 1 else 's'}"
        note = diarization.get("note")
        return f"[Diarization: {method} ({detail}) - {outcome}{'; ' + note if note else ''}]"
    return (
        f"[Diarization: {method} FAILED - {diarization.get('reason', 'unknown reason')}; "
        "transcript is not speaker-attributed]"
    )


def attribute_segments(segments: list[dict], diarization: dict) -> list[dict]:
    """Label timed ASR segments with the diarized speaker of greatest overlap.

    A segment overlapping no speech stays ``Unassigned``; nothing is guessed.
    Consecutive segments from one speaker merge into one turn.
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
    return turns


def format_turns(turns: list[dict]) -> str:
    return "\n\n".join(
        f"[{turn['start']:.2f}\u2013{turn['end']:.2f}] [{turn['speaker']}]\n{turn['text']}"
        for turn in turns
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
        for index in range(channel_count):
            source = audio_path
            channel = None
            if channel_count > 1:
                ffmpeg = shutil.which(os.environ.get("TRANSCRIBE_FFMPEG", "ffmpeg"))
                if not ffmpeg:
                    raise LocalFluidUnavailable(
                        "ffmpeg required to preserve separate audio channels"
                    )
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
                        "16000",
                        str(source),
                    ],
                    check=True,
                    capture_output=True,
                    timeout=300,
                )
                channel = f"Channel_{index}"
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
    return {
        "transcription_text": format_events(all_events),
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
