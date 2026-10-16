#!/usr/bin/env python3
"""The calibrated pre-transcription silence gate, shared by every audio path.

Extracted from ``live_transcript_tail.py`` so the batch path
(``local_whisper.py``) reuses the measured threshold instead of inventing a
second one. Two thresholds for the same question drift apart, and the one that
drifts is the one nobody calibrated.

WHY A GATE AT ALL
Whisper does not return empty on silence — it HALLUCINATES subtitle boilerplate
("thank you for watching", "please subscribe", "Bon Appetit!", full sentences in
Japanese, Korean, Ukrainian). Gating on measured level BEFORE transcription is
the only thing that actually stops it; post-hoc phrase filtering is a losing arms
race against an open-ended set of fabrications in arbitrary languages.

WHY THE 95TH PERCENTILE
The statistic is the 95th percentile of ffmpeg's windowed RMS — NOT the median
and NOT the peak. Measured on 39 labelled chunks of a real session:

  - median FAILS: a speaker who pauses between sentences leaves a 35s window
    with a median of -75 to -82 dB, indistinguishable from true silence.
  - peak FAILS: transient clicks push silent windows to -22 dB.
  - p95 separates: it asks "was there sustained energy in the loudest ~5% of
    this window", which is exactly what "someone spoke at some point" means.

WHAT THIS GATE CANNOT DO
It answers "was there sustained acoustic energy", which does not separate a
fabrication that arrives at speech level (observed: a Georgian-script chunk at
-31.6 dB, inside the operator's verified -28 to -37 dB speech range). That class
is the job of ``hallucination_filter.py``, which inspects the RESULT. The two are
complements: this gate saves the work on true silence, that filter catches what
gets past it.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

# -50 dB sustained. Overridable, but the default is measured — see the module
# docstring before changing it. The env var keeps its original name so existing
# streaming setups are unaffected by the extraction.
DEFAULT_SILENCE_THRESHOLD_DB = float(
    os.environ.get("LIVE_TRANSCRIPT_SILENCE_THRESHOLD_DB", "-50")
)
RMS_PERCENTILE = 0.95
_RMS_RE = re.compile(r"RMS_level=(-?[\d.]+)")


def parse_rms_levels(stderr: str) -> list[float]:
    """Extract finite windowed RMS_level values (dB) from ffmpeg astats output."""
    return [
        float(m) for m in _RMS_RE.findall(stderr or "")
        if "inf" not in m.lower()
    ]


def sustained_rms_db(
    values: list[float], percentile: float = RMS_PERCENTILE
) -> float | None:
    """Representative *sustained* level: the ``percentile`` of windowed RMS.

    Deliberately not the mean/median (a pausing speaker drags those down to
    silence levels) and not the max (a single click lifts silence to speech
    levels). See the module docstring for the measured rationale.
    """
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(percentile * len(ordered)))
    return ordered[idx]


def measure_sustained_rms_db(
    wav_path: Path,
    *,
    ffmpeg: str = "ffmpeg",
    window_seconds: float = 3.0,
    log=None,
) -> float | None:
    """Sustained RMS (dB) of an audio file, or None if the measurement failed.

    None is the caller's signal to transcribe anyway: a broken measurement must
    never silently discard audio.
    """
    try:
        proc = subprocess.run(
            [
                ffmpeg, "-hide_banner", "-nostdin", "-i", str(wav_path),
                "-af",
                f"astats=metadata=1:reset=1:length={window_seconds:g},"
                "ametadata=print:key=lavfi.astats.Overall.RMS_level",
                "-f", "null", os.devnull,
            ],
            capture_output=True, text=True, timeout=300,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        if log:
            log(f"RMS measurement failed ({exc}) — transcribing anyway")
        return None

    level = sustained_rms_db(parse_rms_levels(proc.stderr))
    if level is None and log:
        log("RMS measurement returned no usable values — transcribing anyway")
    return level
