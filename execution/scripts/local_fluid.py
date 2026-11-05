"""On-device diarization and timestamped ASR using the FluidAudio CLI.

The caller explicitly owns installing the CLI/models. Missing dependencies or
invalid output fail loudly; no network transcription or paid fallback exists.
Raw model outputs are returned for preservation alongside the readable text.
"""

from __future__ import annotations

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
    try:
        result = subprocess.run(
            [cli, *args, str(output)],
            capture_output=True,
            text=True,
            timeout=float(os.environ.get("TRANSCRIBE_FLUID_TIMEOUT", "3600")),
        )
    except subprocess.TimeoutExpired as exc:
        raise LocalFluidUnavailable(
            "FluidAudio timed out; no fallback attempted"
        ) from exc
    # Some CLI versions log an error but exit zero. The output is required too.
    if result.returncode != 0 or not output.is_file():
        raise LocalFluidUnavailable(
            f"FluidAudio failed ({result.returncode}): {(result.stderr or '')[-2000:]}"
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
                ["process", str(source), "--mode", "offline", "--output"],
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
