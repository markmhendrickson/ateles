"""Two-track call transcription: per-track ASR, mixed-track diarization, segment merge.

One automated step in place of the hand procedure:

1. each track to 16 kHz mono (``raw16``);
2. per-track ASR segments from ``track_asr`` (chunked whisper by default, see
   ``chunked_whisper``), each track on its own so neither speaker is lost to the
   other's level;
3. each track's level per 100 ms frame (``EnergyProfile``);
4. both tracks normalised and mixed, run through FluidAudio offline diarization
   (fail-open: a failure is recorded in the header, the transcript still lands);
5. ``two_track_merge.merge_tracks`` by segment, with clusters mapped to tracks by
   energy and cross-talk dropped.
"""

from __future__ import annotations

import math
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Callable

try:
    from scripts.chunked_whisper import NORMALIZE_FILTER
    from scripts.two_track_merge import FRAME_SECONDS, MIC, SYSTEM, EnergyProfile, merge_tracks
except ImportError:  # pragma: no cover - path-dependent import
    from chunked_whisper import NORMALIZE_FILTER  # type: ignore[no-redef]
    from two_track_merge import FRAME_SECONDS, MIC, SYSTEM, EnergyProfile, merge_tracks  # type: ignore[no-redef]

# track_asr(label, wav16, original_path) -> {"segments": [...], <evidence keys>}
TrackAsr = Callable[[str, Path, Path], dict]

_LEVEL = re.compile(r"lavfi\.astats\.Overall\.RMS_level=(-?inf|-?[0-9.]+)")


def to_wav16(ffmpeg: str, src: Path, dest: Path) -> Path:
    subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", str(src),
         "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(dest)],
        check=True, capture_output=True, timeout=1800,
    )
    return dest


def frame_levels(ffmpeg: str, wav: Path) -> list[float]:
    """RMS level in dB for each ``FRAME_SECONDS`` of a 16 kHz track (``-inf`` = silent)."""
    samples = int(16000 * FRAME_SECONDS)
    proc = subprocess.run(
        [ffmpeg, "-hide_banner", "-nostats", "-loglevel", "error", "-nostdin", "-i", str(wav), "-af",
         f"asetnsamples=n={samples}:p=0,astats=metadata=1:reset=1,"
         "ametadata=mode=print:key=lavfi.astats.Overall.RMS_level:file=-",
         "-f", "null", "-"],
        capture_output=True, text=True, timeout=1800, check=True,
    )
    values = []
    for match in _LEVEL.finditer(proc.stdout):
        raw = match.group(1)
        values.append(-math.inf if "inf" in raw else float(raw))
    return values


def mix_for_diarization(ffmpeg: str, system16: Path, mic16: Path, dest: Path) -> Path:
    """Mix the two tracks, each normalised first so the quiet mic is audible to the model."""
    graph = (
        f"[0:a]{NORMALIZE_FILTER}[s];[1:a]{NORMALIZE_FILTER}[m];"
        "[s][m]amix=inputs=2:duration=longest:normalize=0,alimiter=limit=0.95[out]"
    )
    subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", str(system16), "-i", str(mic16),
         "-filter_complex", graph, "-map", "[out]", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(dest)],
        check=True, capture_output=True, timeout=1800,
    )
    return dest


def transcribe_two_tracks(
    mic_path: Path,
    remote_path: Path,
    *,
    track_asr: TrackAsr,
    ffmpeg: str,
    diarize: Callable[[Path], dict],
    stitch_seconds: float = 4.0,
) -> dict:
    """Run the whole step. Returns ``{"text", "turns", "tracks", "merge"}``.

    ``tracks`` is the per-track evidence (whatever ``track_asr`` returned, in
    System, Mic order). ``merge`` is the merge statistics and the diarization
    outcome, for the stored raw evidence.
    """
    with tempfile.TemporaryDirectory(prefix="two-track-") as tmp:
        work = Path(tmp)
        sources = {SYSTEM: remote_path, MIC: mic_path}
        wavs = {label: to_wav16(ffmpeg, path, work / f"{label.lower()}.wav") for label, path in sources.items()}
        results = {label: track_asr(label, wavs[label], sources[label]) for label in (SYSTEM, MIC)}

        energy: dict[str, EnergyProfile] | None
        try:
            energy = {label: EnergyProfile(frame_levels(ffmpeg, wavs[label])) for label in (SYSTEM, MIC)}
        except (subprocess.SubprocessError, OSError, ValueError):
            energy = None

        diarization = None
        if any(results[label]["segments"] for label in (SYSTEM, MIC)):
            try:
                diarization = diarize(mix_for_diarization(ffmpeg, wavs[SYSTEM], wavs[MIC], work / "mix.wav"))
            except (subprocess.SubprocessError, OSError):
                diarization = {"status": "failed", "category": "unreadable output"}

    merged = merge_tracks(
        {label: results[label]["segments"] for label in (SYSTEM, MIC)},
        diarization=diarization,
        energy=energy,
        stitch_seconds=stitch_seconds,
    )
    return {
        "text": merged.text,
        "turns": merged.turns,
        "tracks": [{"channel": label, **results[label]} for label in (SYSTEM, MIC)],
        "merge": {
            "method": "segment merge, clusters mapped to tracks by energy",
            "diarization": diarization,
            "energy_measured": energy is not None,
            "turns": merged.turns,
            **merged.stats,
        },
    }
