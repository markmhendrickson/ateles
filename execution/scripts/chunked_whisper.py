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
   more times is a collapse. The chunk is re-run once without prior context. A
   loop that survives is recorded on the chunk (``loop_after_rerun``) and counted
   in the transcript header; the merge collapses it after same-speaker segments
   are joined. If the output screen rejects the chunk instead, see 5;
5. the output screen (``hallucination_filter``) judges each chunk. A rejected
   chunk is NEVER dropped silently: its text and the reason stay in the chunk's
   record (``withheld_text``, ``reason``, ``detail``), and the merge shows a timed
   gap marker in the transcript and counts it in the header;
6. each chunk boundary is re-transcribed over a 28 s window and the words cut at
   the seam are replaced by the window's.

Whisper is injected (``asr``) so the logic is testable without a model.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import tempfile
import wave
from pathlib import Path
from typing import Callable

try:
    from scripts.hallucination_filter import DEFAULT_PLAUSIBLE_LANGUAGES, screen_transcription
    from scripts.local_whisper import detected_language_from_json, timed_segments_from_json
    from scripts.silence_gate import DEFAULT_SILENCE_THRESHOLD_DB, measure_sustained_rms_db
    from scripts.two_track_merge import collapse_repeats
except ImportError:  # pragma: no cover - path-dependent import
    from hallucination_filter import DEFAULT_PLAUSIBLE_LANGUAGES, screen_transcription  # type: ignore[no-redef]
    from local_whisper import detected_language_from_json, timed_segments_from_json  # type: ignore[no-redef]
    from silence_gate import DEFAULT_SILENCE_THRESHOLD_DB, measure_sustained_rms_db  # type: ignore[no-redef]
    from two_track_merge import collapse_repeats  # type: ignore[no-redef]

CHUNK_SECONDS = 120
BOUNDARY_WINDOW_SECONDS = 28
# Words inside this distance of a seam are the ones the cut can have damaged.
SEAM_REPAIR_RADIUS_SECONDS = 7
NORMALIZE_FILTER = "highpass=f=80,dynaudnorm=f=150:g=15"
# Re-run of a collapsed chunk: no context carried between segments.
RERUN_ARGS = ["-mc", "0"]
# One whisper call transcribes at most one chunk; far above any real need.
WHISPER_CALL_TIMEOUT_SECONDS = 900

# asr(wav, language, extra_args) -> (segments, detected_language)
AsrFn = Callable[[Path, str | None, list[str]], tuple[list[dict], str | None]]

# Runs a command and kills it if the parent process disappears. The parent being
# killed (Tyto's outer timeout does that) would otherwise leave a whisper-cli
# running for minutes: a SIGKILL cannot be handled, so a watcher outside the
# parent notices that it is gone. Watcher output is detached so it never holds
# the caller's pipes open.
_PARENT_GUARD = (
    'parent=$1; shift; "$@" & child=$!; '
    '( while kill -0 "$parent" 2>/dev/null; do sleep 1; done; kill -9 "$child" 2>/dev/null ) '
    ">/dev/null 2>&1 & watcher=$!; "
    'wait "$child"; status=$?; kill "$watcher" 2>/dev/null; exit "$status"'
)


def run_guarded(cmd: list[str], *, timeout: float) -> subprocess.CompletedProcess:
    """Like ``subprocess.run(capture_output=True, text=True)`` but the whole process
    group is killed on timeout and the command dies with its parent."""
    proc = subprocess.Popen(
        ["/bin/sh", "-c", _PARENT_GUARD, "sh", str(os.getpid()), *cmd],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True,
    )
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
        raise
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


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
        proc = run_guarded(cmd, timeout=WHISPER_CALL_TIMEOUT_SECONDS)
        out = Path(f"{stem}.json")
        if proc.returncode != 0 or not out.is_file():
            tail = ((proc.stderr or "").strip().splitlines() or [""])[-1][:200]
            raise RuntimeError(f"whisper-cli failed (exit {proc.returncode}): {tail}")
        data = json.loads(out.read_text(encoding="utf-8"))
        return timed_segments_from_json(data), detected_language_from_json(data)

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


def _duration(wav: Path) -> float:
    try:
        with wave.open(str(wav), "rb") as handle:
            return handle.getnframes() / float(handle.getframerate() or 16000)
    except (OSError, EOFError, wave.Error):
        return 0.0


def _run_clip(
    asr: AsrFn, raw: Path, norm: Path, language: str | None, ffmpeg: str,
    offset: float, stats: dict, gate_db: float, plausible: tuple[str, ...],
) -> list[dict]:
    """Transcribe one clip: silence gate, repeat check with one re-run, screen.

    Nothing is discarded without a trace: a screened clip keeps its text, reason
    and detail in ``stats``.
    """
    stats["end"] = round(offset + _duration(raw), 2)
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
    verdict = screen_transcription(
        text, expected_language=language, detected_language=detected,
        window_seconds=None, plausible_languages=plausible,
    )
    if segments and verdict.filtered:
        stats["status"] = f"screened: {verdict.reason}"
        stats["reason"] = verdict.reason
        stats["detail"] = verdict.detail
        stats["withheld_text"] = text
        stats["withheld_segments"] = [dict(s, start=s["start"] + offset, end=s["end"] + offset) for s in segments]
        return []
    stats["status"] = "ok"
    stats["words"] = len(text.split())
    return [{**s, "start": s["start"] + offset, "end": s["end"] + offset} for s in segments]


def _clock(seconds: float) -> str:
    return f"{int(seconds) // 60}:{int(seconds) % 60:02d}"


def transcribe_track(
    wav16: Path,
    *,
    asr: AsrFn,
    language: str | None = None,
    ffmpeg: str,
    duration: float | None = None,
    gate_db: float = DEFAULT_SILENCE_THRESHOLD_DB,
    chunk_seconds: int = CHUNK_SECONDS,
    plausible_languages: tuple[str, ...] = DEFAULT_PLAUSIBLE_LANGUAGES,
    label: str = "track",
    progress: Callable[[str], None] | None = None,
) -> dict:
    """Transcribe a 16 kHz mono track in chunks. Returns ``{"segments", "chunks"}``.

    ``chunks`` holds one QA record per chunk (level, status, whether it was
    re-run for a repeat loop, and for a screened chunk its withheld text and
    reason) and per boundary repair, for the raw evidence.
    """
    say = progress or (lambda message: None)

    def guarded(kind: str, at: float, fn, *args):
        try:
            return fn(*args)
        except RuntimeError as exc:
            raise RuntimeError(f"{label} track, {kind} at {_clock(at)}: {exc}") from exc

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
            say(f"{label}: chunk {index + 1}/{len(raw_chunks)} ({_clock(offset)})")
            norm = _normalized(ffmpeg, raw, work / f"norm{index:04d}.wav")
            segments += guarded("chunk", offset, _run_clip, asr, raw, norm, language, ffmpeg, offset, info,
                                gate_db, plausible_languages)
            qa.append(info)

        # Boundary repair: seam k sits at k * chunk_seconds.
        for k in range(1, len(raw_chunks)):
            seam = float(k * chunk_seconds)
            start = max(0.0, seam - BOUNDARY_WINDOW_SECONDS / 2)
            info = {"kind": "seam", "at": seam}
            say(f"{label}: seam {k}/{len(raw_chunks) - 1} ({_clock(seam)})")
            raw = work / f"seam{k:04d}.raw.wav"
            _ffmpeg(ffmpeg, ["-ss", f"{start:.3f}", "-i", str(wav16), "-t", str(BOUNDARY_WINDOW_SECONDS),
                             "-c:a", "pcm_s16le", str(raw)])
            norm = _normalized(ffmpeg, raw, work / f"seam{k:04d}.norm.wav")
            repaired = guarded("seam", seam, _run_clip, asr, raw, norm, language, ffmpeg, start, info,
                               gate_db, plausible_languages)
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
