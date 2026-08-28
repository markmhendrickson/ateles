#!/usr/bin/env python3
"""Live transcript tailer for in-progress Audio Hijack recordings.

Watches a recordings directory for a *still-growing* remote/system track, slices
it with ffmpeg on a fixed cadence, transcribes each slice, and appends one JSON
line per chunk to `<stem>_live.jsonl`.

This is the inverse of Tyto's settle check: Tyto waits for mtime to go stable
(post-hoc, authoritative, diarized). This watches the file *while* it grows, to
feed a live session. Chunks are Whisper-only and cut on arbitrary boundaries —
they are advisory context, never a source for durable entities.

Nothing here writes to Neotoma and nothing touches Tyto; the stop-time pipeline
is unaffected.

Usage:
    python execution/scripts/live_transcript_tail.py                 # auto-detect
    python execution/scripts/live_transcript_tail.py --file REC.mp4  # explicit
    python execution/scripts/live_transcript_tail.py --interval 45

Then tail the JSONL it prints at startup (Monitor, or `tail -f`).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
TRANSCRIBE = REPO_ROOT / "execution" / "scripts" / "transcribe_audio.py"
VENV_PYTHON = REPO_ROOT / "execution" / "venv" / "bin" / "python"

# Matches Tyto's conventions so both halves see the same files.
REMOTE_TRACK_NAMES = ("remote", "system")
RECORDING_EXTENSIONS = {".aac", ".m4a", ".mp4", ".wav"}

DEFAULT_DIR = Path(
    os.environ.get(
        "TYTO_RECORDINGS_DIR",
        os.environ.get(
            "RECORD_MEETING_DIR",
            str(Path.home() / "Documents" / "data" / "recordings"),
        ),
    )
)
DEFAULT_INTERVAL = int(os.environ.get("LIVE_TRANSCRIPT_INTERVAL", "30"))

# A slice shorter than this is mostly silence padding at the tail of the file;
# waiting one more cycle yields a better transcript than pushing a fragment.
MIN_SLICE_SECONDS = 5.0


def log(msg: str) -> None:
    print(f"[live-tail] {msg}", file=sys.stderr, flush=True)


def is_remote_track(path: Path) -> bool:
    name = path.name.lower()
    return (
        path.suffix.lower() in RECORDING_EXTENSIONS
        and any(t in name for t in REMOTE_TRACK_NAMES)
        and "mic" not in name
    )


def probe_duration(path: Path) -> float | None:
    """Duration in seconds, read live from a growing file (None if unreadable)."""
    try:
        out = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "csv=p=0",
                str(path),
            ],
            capture_output=True, text=True, timeout=30,
        )
        if out.returncode != 0:
            return None
        return float((out.stdout or "").strip())
    except (ValueError, subprocess.SubprocessError, OSError):
        return None


def find_growing_recording(watch_dir: Path, settle_probe: float = 3.0) -> Path | None:
    """Return the most recent remote/system track that is actively growing."""
    if not watch_dir.exists():
        log(f"watch dir does not exist: {watch_dir}")
        return None

    candidates = [p for p in watch_dir.iterdir() if p.is_file() and is_remote_track(p)]
    if not candidates:
        return None

    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    newest = candidates[0]

    try:
        size_before = newest.stat().st_size
    except OSError:
        return None
    time.sleep(settle_probe)
    try:
        size_after = newest.stat().st_size
    except OSError:
        return None

    if size_after > size_before:
        return newest

    log(f"newest recording is not growing (finished?): {newest.name}")
    return None


def transcribe_slice(wav_path: Path, env: dict) -> tuple[bool, str]:
    """Transcribe one slice. Returns (ok, text_or_error)."""
    python_bin = str(VENV_PYTHON) if VENV_PYTHON.exists() else sys.executable
    try:
        result = subprocess.run(
            [
                python_bin, str(TRANSCRIBE), str(wav_path),
                "--no-store", "--no-diarize",
            ],
            capture_output=True, text=True, env=env, timeout=300,
        )
    except subprocess.TimeoutExpired:
        return False, "transcription timed out after 300s"
    except OSError as exc:
        return False, f"failed to run transcribe_audio.py: {exc}"

    if result.returncode != 0:
        tail = (result.stderr or "").strip().splitlines()
        return False, tail[-1] if tail else f"exit {result.returncode}"

    # transcribe_audio.py prints a "Transcribing audio file: ..." banner ahead of
    # the transcript; drop it so the JSONL carries only spoken text.
    lines = [ln for ln in (result.stdout or "").splitlines()
             if ln.strip() and not ln.startswith("Transcribing audio file:")]
    text = " ".join(ln.strip() for ln in lines).strip()
    if not text:
        return False, "empty transcript (silence?)"
    return True, text


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--file", type=Path, default=None,
                    help="Recording to tail (default: auto-detect growing file)")
    ap.add_argument("--dir", type=Path, default=DEFAULT_DIR,
                    help=f"Directory to watch (default: {DEFAULT_DIR})")
    ap.add_argument("--interval", type=int, default=DEFAULT_INTERVAL,
                    help=f"Seconds per chunk (default: {DEFAULT_INTERVAL})")
    ap.add_argument("--out", type=Path, default=None,
                    help="JSONL path (default: <stem>_live.jsonl beside recording)")
    ap.add_argument("--start-at", type=float, default=None,
                    help="Cursor start in seconds (default: current duration — "
                         "only new audio is transcribed)")
    args = ap.parse_args(argv)

    if not TRANSCRIBE.exists():
        log(f"transcribe_audio.py not found at {TRANSCRIBE}")
        return 1

    # transcribe_audio.py needs OPENAI_API_KEY; SOPS materializes it here.
    env = {**os.environ}
    materialized = Path.home() / ".config" / "neotoma" / ".env"
    if materialized.exists():
        for line in materialized.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    if not env.get("OPENAI_API_KEY"):
        log("OPENAI_API_KEY not set (checked env and ~/.config/neotoma/.env)")
        return 1

    recording = args.file
    if recording is None:
        log(f"looking for a growing recording in {args.dir} …")
        recording = find_growing_recording(args.dir)
        if recording is None:
            log("no active recording found — start recording first, or pass --file")
            return 1
    if not recording.exists():
        log(f"recording not found: {recording}")
        return 1

    out_path = args.out or recording.with_name(f"{recording.stem}_live.jsonl")

    cursor = args.start_at
    if cursor is None:
        cursor = probe_duration(recording) or 0.0

    log(f"tailing: {recording.name}")
    log(f"chunk interval: {args.interval}s   starting at: {cursor:.0f}s")
    log(f"JSONL: {out_path}")
    print(str(out_path), flush=True)  # stdout: the path, for scripting

    chunk_index = 0
    consecutive_failures = 0

    try:
        while True:
            time.sleep(args.interval)

            duration = probe_duration(recording)
            if duration is None:
                log("could not probe duration — recording ended?")
                break

            available = duration - cursor
            if available < MIN_SLICE_SECONDS:
                # Recording stopped, or it is not producing audio fast enough yet.
                if available <= 0.05:
                    try:
                        stalled = (time.time() - recording.stat().st_mtime) > (args.interval * 2)
                    except OSError:
                        stalled = True
                    if stalled:
                        log("recording appears to have stopped — exiting")
                        break
                continue

            tmp = tempfile.NamedTemporaryFile(
                suffix=f"_live{chunk_index:04d}.wav", delete=False, prefix="livetail_"
            )
            tmp.close()
            tmp_path = Path(tmp.name)

            try:
                proc = subprocess.run(
                    [
                        "ffmpeg", "-v", "error", "-y",
                        "-ss", f"{cursor:.3f}", "-t", f"{available:.3f}",
                        "-i", str(recording),
                        "-ac", "1", "-ar", "16000",
                        str(tmp_path),
                    ],
                    capture_output=True, text=True, timeout=120,
                )
                if proc.returncode != 0:
                    ok, payload = False, f"ffmpeg slice failed: {(proc.stderr or '').strip()[:200]}"
                else:
                    ok, payload = transcribe_slice(tmp_path, env)
            except subprocess.TimeoutExpired:
                ok, payload = False, "ffmpeg slice timed out"
            finally:
                tmp_path.unlink(missing_ok=True)

            record = {
                "chunk": chunk_index,
                "t": datetime.now(tz=UTC).isoformat(),
                "start_s": round(cursor, 2),
                "end_s": round(cursor + available, 2),
                "ok": ok,
            }
            if ok:
                record["text"] = payload
                consecutive_failures = 0
            else:
                record["error"] = payload
                consecutive_failures += 1

            with out_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")

            # Advance regardless of transcription success: a failed chunk must not
            # re-slice the same audio forever.
            cursor += available
            chunk_index += 1

            if consecutive_failures >= 5:
                log("5 consecutive failures — stopping (check the JSONL error lines)")
                break

    except KeyboardInterrupt:
        log("interrupted — stopping")

    log(f"done. {chunk_index} chunk(s) written to {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
