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
   energy and cross-talk removed, every removal marked and counted.

ffmpeg and FluidAudio are tied to this process (``guarded_process``): Tyto's outer
deadline kills it with SIGKILL, and neither child may keep running afterwards.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import Callable

try:
    from scripts.chunked_whisper import NORMALIZE_FILTER, frame_levels
    from scripts.guarded_process import check_guarded, guard_pattern
    from scripts.two_track_merge import MIC, SYSTEM, EnergyProfile, merge_tracks
except ImportError:  # pragma: no cover - path-dependent import
    from chunked_whisper import NORMALIZE_FILTER, frame_levels  # type: ignore[no-redef]
    from guarded_process import check_guarded, guard_pattern  # type: ignore[no-redef]
    from two_track_merge import MIC, SYSTEM, EnergyProfile, merge_tracks  # type: ignore[no-redef]

__all__ = ["frame_levels", "to_wav16", "mix_for_diarization", "transcribe_two_tracks"]

# track_asr(label, wav16, original_path) -> {"segments": [...], <evidence keys>}
TrackAsr = Callable[[str, Path, Path], dict]

FFMPEG_TIMEOUT_SECONDS = 1800


def to_wav16(ffmpeg: str, src: Path, dest: Path) -> Path:
    check_guarded(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", str(src),
         "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(dest)],
        timeout=FFMPEG_TIMEOUT_SECONDS,
    )
    return dest


def mix_for_diarization(ffmpeg: str, system16: Path, mic16: Path, dest: Path) -> Path:
    """Mix the two tracks, each normalised first so the quiet mic is audible to the model."""
    graph = (
        f"[0:a]{NORMALIZE_FILTER}[s];[1:a]{NORMALIZE_FILTER}[m];"
        "[s][m]amix=inputs=2:duration=longest:normalize=0,alimiter=limit=0.95[out]"
    )
    check_guarded(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", str(system16), "-i", str(mic16),
         "-filter_complex", graph, "-map", "[out]", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(dest)],
        timeout=FFMPEG_TIMEOUT_SECONDS,
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
    notes: list[str] | None = None,
) -> dict:
    """Run the whole step. Returns ``{"text", "turns", "tracks", "merge"}``.

    ``tracks`` is the per-track evidence (whatever ``track_asr`` returned, in
    System, Mic order). ``merge`` is the merge statistics and the diarization
    outcome, for the stored raw evidence. ``notes`` become header clauses.
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
            mix = work / "mix.wav"
            try:
                with guard_pattern(str(mix)):
                    diarization = diarize(mix_for_diarization(ffmpeg, wavs[SYSTEM], wavs[MIC], mix))
            except (subprocess.SubprocessError, OSError):
                diarization = {"status": "failed", "category": "unreadable output"}

    merged = merge_tracks(
        {label: results[label]["segments"] for label in (SYSTEM, MIC)},
        diarization=diarization,
        energy=energy,
        stitch_seconds=stitch_seconds,
        chunks={label: results[label].get("chunks", []) for label in (SYSTEM, MIC)},
        notes=notes,
    )
    return {
        "text": merged.text,
        "turns": merged.turns,
        "tracks": [{"channel": label, **results[label]} for label in (SYSTEM, MIC)],
        "merge": {
            "method": "segment merge, clusters mapped to tracks by energy",
            "diarization": diarization,
            "turns": merged.turns,
            **merged.stats,
            "energy_measured": energy is not None,
        },
    }
