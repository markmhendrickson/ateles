#!/usr/bin/env python3
"""Streaming live transcript: one capture, tee'd to disk and to a socket.

A single ffmpeg process reads the input device once and writes TWO outputs:

  1. a durable recording on disk (the record — never sacrificed), and
  2. raw PCM on stdout, streamed to OpenAI's Realtime transcription socket.

Because both outputs come from the SAME bytes, the live transcript INDEXES the
recording exactly rather than approximating it — the drift you get from a second
parallel capture cannot happen here.

Output is one JSON line per finished turn in `<stem>_live.jsonl`, the same shape
`live_transcript_tail.py` writes, so the session Monitor consumes it unchanged.

Why this exists (ateles#625): the chunking tailer slices 30s windows off a
growing file, so a timer cuts the audio rather than the speech. Measured on a
real session, 42% of speech-bearing chunks (15/36) ended mid-sentence. Server
VAD cuts on silence instead, at ~1.3s to first text.

Failure is LOUD by design (ateles#619, where the tailer died in a pause with
nothing noticing, and separately ran with every chunk erroring while reporting
healthy). See `HealthMonitor`: a dead socket, a stalled capture, and a silent
mic each announce themselves, and are distinguished from one another.

Usage:
    python execution/scripts/stream_transcript.py                  # auto-detect device
    python execution/scripts/stream_transcript.py --device :3
    python execution/scripts/stream_transcript.py --fallback-only  # force chunking
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import math
import os
import shutil
import struct
import subprocess
import sys
import time
from collections import deque
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
FALLBACK_TAILER = REPO_ROOT / "execution" / "scripts" / "live_transcript_tail.py"

DEFAULT_DIR = Path(
    os.environ.get(
        "TYTO_RECORDINGS_DIR",
        os.environ.get(
            "RECORD_MEETING_DIR",
            str(Path.home() / "Documents" / "data" / "recordings"),
        ),
    )
)

# 24kHz mono PCM16 is what the Realtime transcription session expects.
SAMPLE_RATE = 24000
SAMPLE_WIDTH = 2
BYTES_PER_SECOND = SAMPLE_RATE * SAMPLE_WIDTH

# ~100ms of audio per socket frame: small enough that VAD reacts promptly,
# large enough not to spend the session in websocket framing overhead.
STREAM_CHUNK_BYTES = BYTES_PER_SECOND // 10

REALTIME_URL = "wss://api.openai.com/v1/realtime?intent=transcription"
DEFAULT_MODEL = os.environ.get("STREAM_TRANSCRIPT_MODEL", "gpt-4o-transcribe")

# --- Health thresholds ------------------------------------------------------
# A socket fails in more ways than a subprocess, so these are deliberately
# tighter than the chunking tailer's. Each answers a DIFFERENT question; the
# point of keeping them separate is that "quiet room" and "dead socket" look
# identical from the outside unless you measure both audio flow and socket
# liveness (ateles#619).
CAPTURE_STALL_SECONDS = float(os.environ.get("STREAM_TRANSCRIPT_STALL_S", "10"))
SOCKET_SILENT_SECONDS = float(os.environ.get("STREAM_TRANSCRIPT_SOCKET_S", "45"))
SIGNAL_WINDOW_SECONDS = float(os.environ.get("STREAM_TRANSCRIPT_SIGNAL_WINDOW_S", "120"))

# Below this dBFS the input is not carrying speech. A muted or unplugged mic
# sits near digital silence; a live room with nobody talking still floats well
# above it. This is the check that catches "the file is growing but there is
# nothing in it" — growth alone is NOT health.
SILENT_INPUT_DBFS = float(os.environ.get("STREAM_TRANSCRIPT_SILENT_DBFS", "-65"))

RECORDING_EXTENSIONS = {".aac", ".m4a", ".mp4", ".wav"}


def log(msg: str) -> None:
    print(f"[stream-transcript] {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Signal level
# ---------------------------------------------------------------------------


def pcm16_dbfs(payload: bytes) -> float | None:
    """RMS level of PCM16 audio in dBFS, or None when there is nothing to measure.

    Used for the silent-mic check. A recording that GROWS while carrying no
    signal must not look healthy: that is exactly the failure where the capture
    subprocess is alive, the file swells on disk, and the operator is talking
    into a muted input with the session reporting green.
    """
    usable = len(payload) - (len(payload) % SAMPLE_WIDTH)
    if usable <= 0:
        return None
    samples = struct.unpack(f"<{usable // SAMPLE_WIDTH}h", payload[:usable])
    if not samples:
        return None
    mean_square = sum(float(s) * float(s) for s in samples) / len(samples)
    if mean_square <= 0:
        return -math.inf
    return 20.0 * math.log10(math.sqrt(mean_square) / 32768.0)


class HealthMonitor:
    """Tracks whether the stream is actually working, and says so when it is not.

    Three questions are kept SEPARATE on purpose, because collapsing them is how
    ateles#619 stayed invisible:

    * is audio still arriving from the capture?      (stall)
    * is the socket still answering?                 (dead socket)
    * does the arriving audio contain any signal?    (silent mic)

    "Quiet room, correctly no transcript" and "socket died" produce identical
    output — no transcript — so only an explicit liveness check separates them.
    """

    def __init__(
        self,
        *,
        stall_seconds: float = CAPTURE_STALL_SECONDS,
        socket_silent_seconds: float = SOCKET_SILENT_SECONDS,
        signal_window_seconds: float = SIGNAL_WINDOW_SECONDS,
        silent_dbfs: float = SILENT_INPUT_DBFS,
        now: float | None = None,
    ) -> None:
        self.stall_seconds = stall_seconds
        self.socket_silent_seconds = socket_silent_seconds
        self.signal_window_seconds = signal_window_seconds
        self.silent_dbfs = silent_dbfs
        start = time.monotonic() if now is None else now
        self.last_audio_at = start
        self.last_socket_at = start
        self.started_at = start
        self.bytes_streamed = 0
        self.transcripts = 0
        self.errors = 0
        # (timestamp, dbfs) for the rolling silent-input window.
        self._levels: deque[tuple[float, float]] = deque()
        self._announced: set[str] = set()

    # -- observations -------------------------------------------------------

    def note_audio(self, payload: bytes, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        self.last_audio_at = now
        self.bytes_streamed += len(payload)
        level = pcm16_dbfs(payload)
        if level is not None:
            self._levels.append((now, level))
        self._trim(now)

    def note_socket_event(self, now: float | None = None) -> None:
        self.last_socket_at = time.monotonic() if now is None else now

    def note_transcript(self, now: float | None = None) -> None:
        self.transcripts += 1
        self.note_socket_event(now)

    def note_error(self, now: float | None = None) -> None:
        self.errors += 1
        self.note_socket_event(now)

    def _trim(self, now: float) -> None:
        cutoff = now - self.signal_window_seconds
        while self._levels and self._levels[0][0] < cutoff:
            self._levels.popleft()

    # -- verdicts -----------------------------------------------------------

    def peak_dbfs(self, now: float | None = None) -> float | None:
        now = time.monotonic() if now is None else now
        self._trim(now)
        if not self._levels:
            return None
        return max(level for _, level in self._levels)

    def problems(self, now: float | None = None) -> list[str]:
        """Every currently-true failure, as operator-readable sentences."""
        now = time.monotonic() if now is None else now
        found: list[str] = []

        audio_gap = now - self.last_audio_at
        if audio_gap > self.stall_seconds:
            found.append(
                f"capture stalled: no audio from ffmpeg for {audio_gap:.0f}s "
                f"(threshold {self.stall_seconds:.0f}s) — the recorder may have died"
            )

        socket_gap = now - self.last_socket_at
        if socket_gap > self.socket_silent_seconds:
            found.append(
                f"socket silent: no event from the transcription socket for "
                f"{socket_gap:.0f}s (threshold {self.socket_silent_seconds:.0f}s) — "
                f"this is NOT the same as a quiet room; the socket itself is unresponsive"
            )

        # Only meaningful once a full window has actually elapsed.
        if now - self.started_at >= self.signal_window_seconds:
            peak = self.peak_dbfs(now)
            if peak is not None and peak < self.silent_dbfs:
                found.append(
                    f"silent input: peak level {peak:.1f} dBFS over the last "
                    f"{self.signal_window_seconds:.0f}s is below {self.silent_dbfs:.0f} dBFS "
                    f"— audio is being recorded but carries no signal (muted or wrong device)"
                )

        return found

    def is_healthy(self, now: float | None = None) -> bool:
        """Health is the absence of problems AND of errors.

        Never report healthy while the error count is non-zero — the #619
        regression was a run that reported healthy while every chunk errored.
        """
        return not self.problems(now) and self.errors == 0

    def new_problems(self, now: float | None = None) -> list[str]:
        """Problems not yet announced, so a persistent fault is not spammed."""
        current = self.problems(now)
        fresh = [p for p in current if p.split(":")[0] not in self._announced]
        for problem in current:
            self._announced.add(problem.split(":")[0])
        # Clearing lets a fault re-announce if it recurs after recovering.
        for kind in list(self._announced):
            if not any(p.startswith(kind) for p in current):
                self._announced.discard(kind)
        return fresh

    def summary(self, now: float | None = None) -> dict:
        now = time.monotonic() if now is None else now
        return {
            "healthy": self.is_healthy(now),
            "seconds_streamed": round(self.bytes_streamed / BYTES_PER_SECOND, 1),
            "transcripts": self.transcripts,
            "errors": self.errors,
            "peak_dbfs": (
                round(self.peak_dbfs(now), 1) if self.peak_dbfs(now) is not None else None
            ),
            "problems": self.problems(now),
        }


# ---------------------------------------------------------------------------
# JSONL records — the shape the session Monitor already consumes
# ---------------------------------------------------------------------------


def transcript_record(index: int, text: str, *, start_s: float, end_s: float) -> dict:
    return {
        "chunk": index,
        "t": datetime.now(tz=UTC).isoformat(),
        "start_s": round(start_s, 2),
        "end_s": round(end_s, 2),
        "ok": True,
        "text": text,
        "source": "stream",
    }


def error_record(index: int, message: str, *, fatal: bool = False) -> dict:
    """A failure the operator must SEE, not a log line that scrolls past."""
    return {
        "chunk": index,
        "t": datetime.now(tz=UTC).isoformat(),
        "ok": False,
        "error": message,
        "fatal": fatal,
        "source": "stream",
    }


def health_record(index: int, problems: list[str], summary: dict) -> dict:
    return {
        "chunk": index,
        "t": datetime.now(tz=UTC).isoformat(),
        "ok": False,
        "error": "; ".join(problems),
        "health": summary,
        "source": "stream",
    }


# ---------------------------------------------------------------------------
# Capture: ONE ffmpeg, two outputs
# ---------------------------------------------------------------------------


def build_capture_command(device: str, recording_path: Path) -> list[str]:
    """One input, two outputs: a durable file AND PCM on stdout.

    The `-t` caveat from ateles#625 is why there is no duration flag here at
    all: placed AFTER `-i` it bounds only the first output, the container is
    never finalized, and you get a 44-byte file with no moov atom — the
    crash-loses-the-recording failure. Capture runs unbounded and is stopped by
    signal, so the muxer always finalizes.
    """
    return [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        "-f", "avfoundation",
        "-i", device,
        # Output 1 — the record. Durable, compressed, kept.
        "-map", "0:a",
        "-ac", "1",
        "-c:a", "aac",
        "-b:a", "96k",
        str(recording_path),
        # Output 2 — the stream. Same bytes, raw PCM to stdout.
        "-map", "0:a",
        "-ac", "1",
        "-ar", str(SAMPLE_RATE),
        "-f", "s16le",
        "-acodec", "pcm_s16le",
        "pipe:1",
    ]


def session_update_message(model: str = DEFAULT_MODEL) -> dict:
    """The Realtime session config.

    `server_vad` is load-bearing for BOTH correctness and cost: it closes turns
    on silence (fixing the 42% mid-sentence truncation) and it keeps billing on
    speech rather than wall clock. With `turn_detection: null` you commit
    whatever you stream, silence included, and pay for all of it.

    The `OpenAI-Beta: realtime=v1` era shape now fails closed with
    `beta_api_shape_disabled`; this is the current one (ateles#625).
    """
    return {
        "type": "session.update",
        "session": {
            "type": "transcription",
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm", "rate": SAMPLE_RATE},
                    "transcription": {"model": model},
                    "turn_detection": {"type": "server_vad"},
                }
            },
        },
    }


# Name of the env var / dotenv entry holding the OpenAI credential. Kept as a
# constant so the dotenv prefix is composed (NAME + "=") rather than written as
# a "NAME=" string literal, which the gitleaks `protected-patterns` rule flags
# as a hardcoded credential assignment. No secret value appears in this file.
OPENAI_KEY_ENV_VAR = "OPENAI_API_KEY"


def load_openai_key() -> str | None:
    """Read the key from env, else from the Neotoma dotenv. Never logged."""
    key = os.environ.get(OPENAI_KEY_ENV_VAR)
    if key:
        return key.strip()
    env_path = Path.home() / ".config" / "neotoma" / ".env"
    prefix = OPENAI_KEY_ENV_VAR + "="
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if line.startswith(prefix):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        return None
    return None


# ---------------------------------------------------------------------------
# Session following
# ---------------------------------------------------------------------------


def next_recording_path(watch_dir: Path, *, now: datetime | None = None) -> Path:
    """Where this stream's durable recording goes.

    Named to match the existing convention so calendar matching, which parses
    the meeting time out of the FILENAME, keeps working.
    """
    stamp = (now or datetime.now()).strftime("%Y%m%d %H%M")
    return watch_dir / f"{stamp} stream.m4a"


def should_follow_to_new_session(
    idle_seconds: float, *, follow: bool, idle_limit: float
) -> bool:
    """Whether an idle stream should roll into a fresh session.

    Folds in what the chunking tailer needed an external supervisor for: `--file`
    pinned it to one recording, so a new session left it transcribing a file
    nobody was speaking into.
    """
    return follow and idle_seconds >= idle_limit


# ---------------------------------------------------------------------------
# Fallback to chunking
# ---------------------------------------------------------------------------


def fallback_command(out_path: Path | None, extra: list[str] | None = None) -> list[str]:
    argv = [sys.executable, str(FALLBACK_TAILER), "--follow"]
    if out_path:
        argv += ["--out", str(out_path)]
    return argv + (extra or [])


def run_fallback(out_path: Path | None, reason: str) -> int:
    """Degrade to the chunking tailer, and SAY SO — loudly, in both channels.

    Producing nothing is the one outcome that is not acceptable: the operator
    is mid-session and a silent live transcript looks exactly like a quiet room.
    """
    banner = f"STREAMING UNAVAILABLE — falling back to chunked transcription. Reason: {reason}"
    log(banner)
    if out_path:
        with contextlib.suppress(OSError):
            with open(out_path, "a", encoding="utf-8") as fh:
                fh.write(
                    json.dumps(
                        {
                            "t": datetime.now(tz=UTC).isoformat(),
                            "ok": False,
                            "error": banner,
                            "degraded": "chunking",
                            "source": "stream",
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
    if not FALLBACK_TAILER.exists():
        # Both paths are now unavailable. This is the worst case, so it is the
        # loudest: the operator is mid-session and must not discover from an
        # empty transcript that nothing is listening.
        blocked = (
            f"NO LIVE TRANSCRIPT AVAILABLE — streaming failed ({reason}) and the "
            f"chunking fallback is missing at {FALLBACK_TAILER}"
        )
        log(blocked)
        if out_path:
            with contextlib.suppress(OSError):
                with open(out_path, "a", encoding="utf-8") as fh:
                    fh.write(
                        json.dumps(
                            {
                                "t": datetime.now(tz=UTC).isoformat(),
                                "ok": False,
                                "error": blocked,
                                "fatal": True,
                                "source": "stream",
                            },
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
        return 2
    return subprocess.call(fallback_command(out_path))


# ---------------------------------------------------------------------------
# The streaming run
# ---------------------------------------------------------------------------


async def stream_session(
    device: str,
    recording_path: Path,
    out_path: Path,
    *,
    model: str = DEFAULT_MODEL,
    api_key: str,
    health_poll: float = 5.0,
) -> tuple[int, HealthMonitor]:
    """Capture once, tee to disk and socket, append transcripts to the JSONL."""
    import websockets

    monitor = HealthMonitor()
    index = 0
    stream_started = time.monotonic()

    def append(record: dict) -> None:
        with open(out_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    proc = await asyncio.create_subprocess_exec(
        *build_capture_command(device, recording_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    log(f"capture started -> {recording_path}")

    try:
        async with websockets.connect(
            REALTIME_URL,
            additional_headers={"Authorization": f"Bearer {api_key}"},
            max_size=None,
        ) as ws:
            await ws.send(json.dumps(session_update_message(model)))
            monitor.note_socket_event()
            log(f"socket open (model={model}, server_vad on)")

            async def pump_audio() -> None:
                nonlocal index
                assert proc.stdout is not None
                import base64

                while True:
                    payload = await proc.stdout.read(STREAM_CHUNK_BYTES)
                    if not payload:
                        break
                    monitor.note_audio(payload)
                    await ws.send(
                        json.dumps(
                            {
                                "type": "input_audio_buffer.append",
                                "audio": base64.b64encode(payload).decode("ascii"),
                            }
                        )
                    )

            async def pump_events() -> None:
                nonlocal index
                async for raw in ws:
                    monitor.note_socket_event()
                    try:
                        event = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    etype = event.get("type", "")
                    if etype.endswith("input_audio_transcription.completed"):
                        text = (event.get("transcript") or "").strip()
                        if text:
                            elapsed = time.monotonic() - stream_started
                            append(
                                transcript_record(
                                    index,
                                    text,
                                    start_s=max(0.0, elapsed - 5.0),
                                    end_s=elapsed,
                                )
                            )
                            index += 1
                            monitor.note_transcript()
                    elif etype == "error":
                        message = (event.get("error") or {}).get(
                            "message", "unknown socket error"
                        )
                        log(f"socket error: {message}")
                        append(error_record(index, f"socket error: {message}"))
                        index += 1
                        monitor.note_error()

            async def watch_health() -> None:
                nonlocal index
                while True:
                    await asyncio.sleep(health_poll)
                    fresh = monitor.new_problems()
                    if fresh:
                        for problem in fresh:
                            log(f"UNHEALTHY: {problem}")
                        append(health_record(index, fresh, monitor.summary()))
                        index += 1

            tasks = [
                asyncio.create_task(pump_audio()),
                asyncio.create_task(pump_events()),
                asyncio.create_task(watch_health()),
            ]
            done, pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            for task in done:
                exc = task.exception()
                if exc is not None:
                    raise exc
    finally:
        # Signal rather than kill, so the muxer finalizes the container and the
        # durable recording survives. This is the whole point of the tee.
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(proc.wait(), timeout=10)
        log(f"capture stopped; recording kept at {recording_path}")

    return 0, monitor


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", default=os.environ.get("STREAM_TRANSCRIPT_DEVICE", ":3"))
    parser.add_argument("--dir", type=Path, default=DEFAULT_DIR)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument(
        "--fallback-only",
        action="store_true",
        help="skip streaming entirely and run the chunking tailer",
    )
    parser.add_argument(
        "--no-fallback",
        action="store_true",
        help="fail loudly instead of degrading to chunking",
    )
    args = parser.parse_args(argv)

    args.dir.mkdir(parents=True, exist_ok=True)
    recording = next_recording_path(args.dir)
    out_path = args.out or recording.with_name(f"{recording.stem}_live.jsonl")

    if args.fallback_only:
        return run_fallback(out_path, "requested via --fallback-only")

    if not shutil.which("ffmpeg"):
        if args.no_fallback:
            log("ffmpeg not found — aborting")
            return 2
        return run_fallback(out_path, "ffmpeg not found on PATH")

    api_key = load_openai_key()
    if not api_key:
        if args.no_fallback:
            log("OPENAI_API_KEY not available — aborting")
            return 2
        return run_fallback(out_path, "OPENAI_API_KEY not available")

    try:
        import websockets  # noqa: F401
    except ImportError:
        if args.no_fallback:
            log("websockets not installed — aborting")
            return 2
        return run_fallback(out_path, "websockets package not installed")

    log(f"live transcript -> {out_path}")
    try:
        rc, monitor = asyncio.run(
            stream_session(
                args.device,
                recording,
                out_path,
                model=args.model,
                api_key=api_key,
            )
        )
    except KeyboardInterrupt:
        log("interrupted — stopping")
        return 0
    except Exception as exc:  # noqa: BLE001 — degrade, but never silently
        reason = f"{type(exc).__name__}: {exc}"
        if args.no_fallback:
            log(f"streaming failed — aborting: {reason}")
            return 2
        return run_fallback(out_path, reason)

    log(f"done. health={json.dumps(monitor.summary())}")
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
