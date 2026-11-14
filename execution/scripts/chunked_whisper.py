"""Chunked local whisper for one audio track, with a repeat check and boundary repair.

Ported from the recordings desk's hand method (sweep.py and the long-audio steps
in its method doc). Whisper collapses on long audio (it repeats a sentence for
minutes or drops passages), and it varies between runs on quiet stretches, so a
long track is never given to it whole:

1. normalise (highpass + dynaudnorm) so a quiet microphone is not suppressed;
2. cut into 2-minute chunks, each a fresh whisper process (decoder state reset);
3. a chunk below the speech floor is not transcribed (whisper fabricates on
   silence), using the repo's calibrated silence gate on the un-normalised audio;
4. repeat check per chunk: a phrase of 1 to 6 words repeated back to back 4 or
   more times is a collapse. The chunk is re-run once without prior context; if
   it still loops, the loop is collapsed in the merge step;
5. each chunk boundary is re-transcribed over a 28 s window and the words cut at
   the seam are replaced by the window's.

Whisper is injected (``asr``) so the logic is testable without a model.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from typing import Callable

try:
    from scripts.hallucination_filter import screen_transcription
    from scripts.silence_gate import DEFAULT_SILENCE_THRESHOLD_DB, measure_sustained_rms_db
    from scripts.two_track_merge import collapse_repeats
except ImportError:  # pragma: no cover - path-dependent import
    from hallucination_filter import screen_transcription  # type: ignore[no-redef]
    from silence_gate import DEFAULT_SILENCE_THRESHOLD_DB, measure_sustained_rms_db  # type: ignore[no-redef]
    from two_track_merge import collapse_repeats  # type: ignore[no-redef]

CHUNK_SECONDS = 120
BOUNDARY_WINDOW_SECONDS = 28
# Words inside this distance of a seam are the ones the cut can have damaged.
SEAM_REPAIR_RADIUS_SECONDS = 7
NORMALIZE_FILTER = "highpass=f=80,dynaudnorm=f=150:g=15"
# Re-run of a collapsed chunk: no context carried between segments.
RERUN_ARGS = ["-mc", "0"]

# asr(wav, language, extra_args) -> (segments, detected_language)
AsrFn = Callable[[Path, str | None, list[str]], tuple[list[dict], str | None]]


def has_repeat_loop(text: str) -> bool:
    """True when ``collapse_repeats`` would remove anything (adjacent 4+ repeats)."""
    return collapse_repeats(text)[1] > 0


def whisper_cli_asr(cli: str, model: Path, *, threads: int | None = None) -> AsrFn:
    """Real ASR: one whisper-cli process per call, timed segments from its JSON."""

    def run(wav: Path, language: str | None, extra: list[str]) -> tuple[list[dict], str | None]:
        stem = wav.with_suffix(".asr")
        cmd = [cli, "-m", str(model), "-f", str(wav), "--output-json", "--output-file", str(stem),
               "--no-prints", "-l", language or "auto", *extra]
        if threads and threads > 0:
            cmd += ["-t", str(threads)]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        out = Path(f"{stem}.json")
        if proc.returncode != 0 or not out.is_file():
            raise RuntimeError(f"whisper-cli failed on a chunk (exit {proc.returncode})")
        data = json.loads(out.read_text(encoding="utf-8"))
        segments = []
        for seg in data.get("transcription") or []:
            text = (seg.get("text") or "").strip()
            offsets = seg.get("offsets") or {}
            if text and "from" in offsets and "to" in offsets:
                segments.append({"start": offsets["from"] / 1000.0, "end": offsets["to"] / 1000.0, "text": text})
        result = data.get("result")
        detected = result.get("language") if isinstance(result, dict) else None
        return segments, detected

    return run


def _ffmpeg(ffmpeg: str, args: list[str]) -> None:
    subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", *args],
                   check=True, capture_output=True, timeout=600)


def _normalized(ffmpeg: str, src: Path, dest: Path, *, start: float | None = None, duration: float | None = None) -> Path:
    args: list[str] = []
    if start is not None:
        args += ["-ss", f"{start:.3f}"]
    args += ["-i", str(src)]
    if duration is not None:
        args += ["-t", f"{duration:.3f}"]
    _ffmpeg(ffmpeg, [*args, "-af", NORMALIZE_FILTER, "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(dest)])
    return dest


def _run_clip(
    asr: AsrFn, raw: Path, norm: Path, language: str | None, ffmpeg: str,
    offset: float, stats: dict, gate_db: float,
) -> list[dict]:
    """Transcribe one clip: silence gate, repeat check with one re-run, screen."""
    level = measure_sustained_rms_db(raw, ffmpeg=ffmpeg)
    stats["rms_db"] = None if level is None else round(level, 1)
    if level is not None and level < gate_db:
        stats["status"] = "silent"
        return []
    segments, detected = asr(norm, language, [])
    text = " ".join(s["text"] for s in segments)
    if has_repeat_loop(text):
        stats["rerun"] = True
        segments, detected = asr(norm, language, RERUN_ARGS)
        text = " ".join(s["text"] for s in segments)
        stats["loop_after_rerun"] = has_repeat_loop(text)
    verdict = screen_transcription(text, expected_language=language, detected_language=detected, window_seconds=None)
    if segments and verdict.filtered:
        stats["status"] = f"screened: {verdict.reason}"
        return []
    stats["status"] = "ok"
    stats["words"] = len(text.split())
    return [{**s, "start": s["start"] + offset, "end": s["end"] + offset} for s in segments]


def transcribe_track(
    wav16: Path,
    *,
    asr: AsrFn,
    language: str | None = None,
    ffmpeg: str,
    duration: float | None = None,
    gate_db: float = DEFAULT_SILENCE_THRESHOLD_DB,
    chunk_seconds: int = CHUNK_SECONDS,
) -> dict:
    """Transcribe a 16 kHz mono track in chunks. Returns ``{"segments", "chunks"}``.

    ``chunks`` holds one QA record per chunk (level, status, whether it was
    re-run for a repeat loop) and per boundary repair, for the raw evidence.
    """
    with tempfile.TemporaryDirectory(prefix="chunked-whisper-") as tmp:
        work = Path(tmp)
        _ffmpeg(ffmpeg, ["-i", str(wav16), "-f", "segment", "-segment_time", str(chunk_seconds),
                         "-c:a", "pcm_s16le", str(work / "raw%04d.wav")])
        raw_chunks = sorted(work.glob("raw*.wav"))
        segments: list[dict] = []
        qa: list[dict] = []
        for index, raw in enumerate(raw_chunks):
            offset = float(index * chunk_seconds)
            info = {"kind": "chunk", "index": index, "start": offset}
            norm = _normalized(ffmpeg, raw, work / f"norm{index:04d}.wav")
            segments += _run_clip(asr, raw, norm, language, ffmpeg, offset, info, gate_db)
            qa.append(info)

        # Boundary repair: seam k sits at k * chunk_seconds.
        for k in range(1, len(raw_chunks)):
            seam = float(k * chunk_seconds)
            start = max(0.0, seam - BOUNDARY_WINDOW_SECONDS / 2)
            info = {"kind": "seam", "at": seam}
            raw = work / f"seam{k:04d}.raw.wav"
            _ffmpeg(ffmpeg, ["-ss", f"{start:.3f}", "-i", str(wav16), "-t", str(BOUNDARY_WINDOW_SECONDS),
                             "-c:a", "pcm_s16le", str(raw)])
            norm = _normalized(ffmpeg, raw, work / f"seam{k:04d}.norm.wav")
            repaired = _run_clip(asr, raw, norm, language, ffmpeg, start, info, gate_db)
            lo, hi = seam - SEAM_REPAIR_RADIUS_SECONDS, seam + SEAM_REPAIR_RADIUS_SECONDS
            inside = lambda s: lo <= (s["start"] + s["end"]) / 2 <= hi  # noqa: E731
            replacement = [s for s in repaired if inside(s)]
            info["replaced"] = False
            if replacement:
                kept = [s for s in segments if not inside(s)]
                # A kept segment that still overlaps a replacement is the same words.
                kept = [s for s in kept if not any(
                    min(s["end"], r["end"]) - max(s["start"], r["start"]) > 0.5 * (s["end"] - s["start"])
                    for r in replacement)]
                segments = kept + replacement
                info["replaced"] = True
            qa.append(info)
        segments.sort(key=lambda s: (s["start"], s["end"]))
    return {"segments": segments, "chunks": qa}
