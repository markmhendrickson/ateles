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

# The filter is a sibling module, shared verbatim with the chunking tailer.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from hallucination_filter import screen_transcription

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

# The language the session is expected to be in. Drives the hallucination
# filter's language and orthography checks.
DEFAULT_LANGUAGE = os.environ.get("STREAM_TRANSCRIPT_LANGUAGE", "en")

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

# Above this dBFS the capture is carrying speech, so a transcript IS expected.
# The operator's verified speech measures -21 to -37 dBFS; room tone with nobody
# talking sits well below. -50 leaves margin under his quietest verified line
# without admitting ordinary room noise.
SPEECH_PRESENT_DBFS = float(os.environ.get("STREAM_TRANSCRIPT_SPEECH_DBFS", "-50"))

# --- Input gate -------------------------------------------------------------
# The single most effective defence against fabrication, and the one this path
# lost when it stopped chunking.
#
# `live_transcript_tail.py` measures a window and NEVER SENDS it when it is
# below threshold, so the model cannot invent from silence. Streaming a
# continuous PCM feed removed that: every second of room tone, breathing and
# keyboard noise reaches the decoder, and an autoregressive decoder has no
# "emit nothing" option — it always produces some token sequence. That is
# exactly where caption boilerplate and foreign-language text come from.
# Measured across the operator's live sessions: 96 of 781 turns (12.3%) were
# non-Latin script in English-only sessions, and that excludes the Latin-script
# fabrications, so the true rate is higher.
#
# Gating the INPUT stops most fabrications being generated at all; the output
# filter then catches what survives. Neither alone is sufficient.
#
# The threshold and the STATISTIC are both inherited from the chunking path
# rather than reinvented (LIVE_TRANSCRIPT_SILENCE_THRESHOLD_DB = -50 dB on the
# p95 of windowed RMS). The statistic is load-bearing: measured on 39 labelled
# chunks, the median fails (a speaker pausing between sentences drags a window
# to -75 dB, indistinguishable from true silence) and the peak fails (a single
# click lifts silent windows to -22 dB). p95 asks "was there sustained energy
# in the loudest ~5%", which is what "someone spoke" actually means.
INPUT_GATE_DBFS = float(
    os.environ.get(
        "STREAM_TRANSCRIPT_INPUT_GATE_DBFS",
        os.environ.get("LIVE_TRANSCRIPT_SILENCE_THRESHOLD_DB", "-50"),
    )
)

# The gate decides over a rolling window rather than per 100ms frame, because a
# frame is far shorter than the pause between two words. Gating per frame would
# chop the gaps out of ordinary speech and desync the server's own VAD.
GATE_WINDOW_SECONDS = float(
    os.environ.get("STREAM_TRANSCRIPT_GATE_WINDOW_S", "1.5")
)

# Once speech is detected, keep sending for this long after it drops below the
# threshold. Speech ends in low-energy consonants and trailing vowels; cutting
# at the instant RMS dips would clip word endings and rob the server VAD of the
# silence it needs to CLOSE a turn — which would reintroduce the mid-sentence
# truncation this path exists to fix.
# Server VAD closes a turn on silence, so a single turn is an utterance, not a
# monologue. Measured on real sessions turns run 0.6-3.5s; 30s is far above any
# genuine one and was exceeded only by the interleaving bug (18.91s measured,
# 31.36s in the operator's session).
# Server VAD may open the next turn slightly before reporting the previous
# stop. Measured at 0.23s on a real capture with both spans otherwise correct.
VAD_OVERLAP_TOLERANCE_SECONDS = float(
    os.environ.get("STREAM_TRANSCRIPT_OVERLAP_TOLERANCE_S", "0.5")
)

MAX_PLAUSIBLE_TURN_SECONDS = float(
    os.environ.get("STREAM_TRANSCRIPT_MAX_TURN_S", "30")
)

# How long the SERVER must hear silence before it closes a turn.
#
# Left unset, the API applies its own default of 500ms, which is shorter than
# an ordinary mid-sentence pause: the operator draws breath, the server closes
# the turn, and one sentence is transcribed as several fragments. Those
# fragments then decode without the surrounding clause that disambiguates them,
# which is why SHORT turns come back garbled while long ones come back clean.
#
# 1000ms is derived from the gap distribution of a real session rather than
# picked. Across 32 turn boundaries the gaps are sharply bimodal: 22 fall below
# 0.79s (pauses inside a sentence) and the rest sit at 1.46s and above (genuine
# utterance boundaries). Nothing at all lands between 0.79s and 1.46s, so any
# value inside that empty band separates the two populations identically —
# 800ms and 1200ms merge exactly the same 22 boundaries. 1000ms is the middle
# of the band, which is the value most tolerant of a speaker who pauses a
# little longer or a little shorter than this sample.
#
# The cost is latency: a turn is reported one threshold-delay after its last
# word, so the added wait is the increase over the 500ms default, not the whole
# value. This stays well inside GATE_HANGOVER_SECONDS (2.0s), so the local RMS
# gate is still forwarding audio while the server waits out the silence — a
# threshold above the hangover would starve the server of the very silence it
# is waiting for and turns would stop closing at all.
VAD_SILENCE_DURATION_MS = int(
    os.environ.get("STREAM_TRANSCRIPT_VAD_SILENCE_MS", "1000")
)

# How much audio BEFORE the detected speech onset the server keeps.
#
# Speech often opens on a low-energy consonant that crosses the VAD threshold
# a beat after the word actually starts. With too little padding the onset is
# clipped and the decode loses the very phoneme that identifies the word — a
# separate contributor to garbled short turns from the silence threshold above.
# 300ms matches the API default; it is set EXPLICITLY so the value is visible
# and tunable rather than inherited silently, which is the defect this whole
# block exists to correct.
VAD_PREFIX_PADDING_MS = int(
    os.environ.get("STREAM_TRANSCRIPT_VAD_PREFIX_PADDING_MS", "300")
)

GATE_HANGOVER_SECONDS = float(
    os.environ.get("STREAM_TRANSCRIPT_GATE_HANGOVER_S", "2.0")
)

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


class InputGate:
    """Decides which audio is worth sending to the transcription socket.

    Two layers, because they fail on different things:

    * **RMS** (layer 1) — catches silence and room tone. Cheap, and it removes
      the bulk. This is the defence the chunking path had and this path lost.
    * **Local VAD** (layer 2) — catches LOUD non-speech, which RMS structurally
      cannot. The decisive counterexample is in the corpus: a fabricated
      Georgian chunk arrived at -31.6 dBFS, inside the operator's verified
      speech range of -28 to -37 dBFS. No threshold separates -31.6 from -31.2;
      volume is not the distinguishing property, spectral shape is.

    Both run BEFORE the socket, so noise never crosses the wire and the decoder
    is never asked to describe it. An autoregressive decoder has no "emit
    nothing" option — given any input it produces some token sequence, which is
    where caption boilerplate and foreign-language text come from.

    Interaction with the server's own VAD is the main implementation risk, and
    it is why this gate is deliberately conservative:

    * It decides over a rolling window, never per 100ms frame, so it cannot chop
      the gaps out of ordinary speech.
    * It applies a HANGOVER after speech drops below threshold, so word endings
      survive and the server still receives the trailing silence it needs to
      CLOSE a turn. Cutting audio the instant RMS dips would starve server VAD
      of its turn boundary and reintroduce mid-sentence truncation — the very
      defect this path exists to fix.
    * When no local VAD is installed it degrades to RMS alone rather than
      failing closed. Sending too much audio costs money; sending too little
      loses the operator's words.
    """

    def __init__(
        self,
        *,
        threshold_dbfs: float = INPUT_GATE_DBFS,
        window_seconds: float = GATE_WINDOW_SECONDS,
        hangover_seconds: float = GATE_HANGOVER_SECONDS,
        vad: "SpeechDetector | None" = None,
    ) -> None:
        self.threshold_dbfs = threshold_dbfs
        self.window_seconds = window_seconds
        self.hangover_seconds = hangover_seconds
        self.vad = vad
        self._levels: deque[tuple[float, float]] = deque()
        self._open_until: float | None = None
        self.frames_sent = 0
        self.frames_suppressed = 0

    def _sustained_dbfs(self, now: float) -> float | None:
        """p95 of windowed RMS — the statistic the chunking path calibrated.

        Not the median (a speaker pausing between sentences drags it to silence
        levels) and not the peak (one keyboard click lifts silence to speech
        levels).
        """
        cutoff = now - self.window_seconds
        while self._levels and self._levels[0][0] < cutoff:
            self._levels.popleft()
        if not self._levels:
            return None
        ordered = sorted(level for _, level in self._levels)
        idx = min(len(ordered) - 1, int(0.95 * len(ordered)))
        return ordered[idx]

    def should_send(self, payload: bytes, now: float) -> bool:
        """Whether this frame goes to the socket."""
        level = pcm16_dbfs(payload)
        if level is None:
            # A measurement we could not take must never silently discard
            # audio — the chunking path takes the same position.
            self.frames_sent += 1
            return True
        self._levels.append((now, level))

        sustained = self._sustained_dbfs(now)
        loud_enough = sustained is not None and sustained >= self.threshold_dbfs

        speechlike = True
        if loud_enough and self.vad is not None:
            # Only consulted on audio that already passed RMS: the VAD is there
            # to reject loud non-speech, not to second-guess silence.
            speechlike = self.vad.is_speech(payload)

        if loud_enough and speechlike:
            self._open_until = now + self.hangover_seconds

        send = self._open_until is not None and now < self._open_until
        if send:
            self.frames_sent += 1
        else:
            self.frames_suppressed += 1
        return send

    def summary(self) -> dict:
        total = self.frames_sent + self.frames_suppressed
        return {
            "frames_sent": self.frames_sent,
            "frames_suppressed": self.frames_suppressed,
            "suppressed_fraction": (
                round(self.frames_suppressed / total, 3) if total else 0.0
            ),
            "threshold_dbfs": self.threshold_dbfs,
            "vad": self.vad.name if self.vad else None,
        }


class SpeechDetector:
    """Local VAD over the PCM already being tee'd.

    Wraps ``webrtcvad`` when it is installed. Kept behind a tiny interface so
    the gate has no hard dependency: the operator's machine may not have it, and
    a missing optional package must not take the live transcript down with it.
    """

    def __init__(self, aggressiveness: int = 2) -> None:
        import webrtcvad  # imported lazily; optional dependency

        self._vad = webrtcvad.Vad(aggressiveness)
        self.name = f"webrtcvad(aggressiveness={aggressiveness})"

    # webrtcvad only accepts 10/20/30ms frames at 8/16/32/48kHz. The capture is
    # 24kHz, so frames are resampled by decimation to 12kHz-equivalent... which
    # webrtcvad does not accept either. Rather than resample badly, the frame is
    # split into the largest supported size and any sub-frame voting speech
    # makes the whole frame speech (biased towards KEEPING audio).
    _FRAME_MS = 20

    def is_speech(self, payload: bytes) -> bool:
        rate = 16000
        frame_bytes = int(rate * (self._FRAME_MS / 1000.0)) * SAMPLE_WIDTH
        pcm = _resample_pcm16(payload, SAMPLE_RATE, rate)
        if len(pcm) < frame_bytes:
            return True  # too little to judge — keep it
        for offset in range(0, len(pcm) - frame_bytes + 1, frame_bytes):
            try:
                if self._vad.is_speech(pcm[offset:offset + frame_bytes], rate):
                    return True
            except Exception:  # noqa: BLE001 — a VAD fault must not drop audio
                return True
        return False


def _resample_pcm16(payload: bytes, src_rate: int, dst_rate: int) -> bytes:
    """Nearest-sample resample. Crude, but a VAD decision does not need better."""
    usable = len(payload) - (len(payload) % SAMPLE_WIDTH)
    if usable <= 0 or src_rate == dst_rate:
        return payload[:usable]
    samples = struct.unpack(f"<{usable // SAMPLE_WIDTH}h", payload[:usable])
    ratio = src_rate / dst_rate
    out_len = int(len(samples) / ratio)
    if out_len <= 0:
        return b""
    picked = [samples[min(len(samples) - 1, int(i * ratio))] for i in range(out_len)]
    return struct.pack(f"<{len(picked)}h", *picked)


def load_speech_detector(enabled: bool = True) -> SpeechDetector | None:
    """The local VAD when available, else None (gate degrades to RMS alone)."""
    if not enabled:
        return None
    try:
        return SpeechDetector()
    except ImportError:
        log(DEGRADED_VAD_MESSAGE)
        return None


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
        speech_dbfs: float = SPEECH_PRESENT_DBFS,
        now: float | None = None,
    ) -> None:
        self.stall_seconds = stall_seconds
        self.socket_silent_seconds = socket_silent_seconds
        self.signal_window_seconds = signal_window_seconds
        self.silent_dbfs = silent_dbfs
        self.speech_dbfs = speech_dbfs
        start = time.monotonic() if now is None else now
        self.last_audio_at = start
        self.last_socket_at = start
        self.last_speech_at = start
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
            # A dead socket and a quiet room both produce "no transcript". The
            # only thing that separates them is whether any speech was OFFERED.
            # Track the last moment the capture carried speech-level audio, so
            # the socket-silence timer can run against speech rather than
            # against wall clock (ateles#631 review).
            if level >= self.speech_dbfs:
                self.last_speech_at = now
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

        # Silence on the socket is only a FAULT if speech was offered and went
        # unanswered. With server VAD the API deliberately sends nothing while
        # nobody is speaking, so timing this against wall clock fires on every
        # ordinary pause — which is what it did on the operator's real session
        # (ateles#631 review): three alarms in four minutes, each of them a
        # working stream and a quiet room. An alert that fires on healthy
        # operation gets ignored, which is the #619 failure arriving from the
        # opposite direction.
        #
        # Correlating with the capture's own RMS makes the alert mean what it
        # says: it uses the same bytes already being teed, and it detects a
        # genuinely dead socket in seconds rather than 45, because speech that
        # goes unanswered is the actual evidence.
        socket_gap = now - self.last_socket_at
        unanswered_speech = now - self.last_speech_at
        if socket_gap > self.socket_silent_seconds and unanswered_speech < socket_gap:
            found.append(
                f"socket silent: speech has been arriving for "
                f"{unanswered_speech:.0f}s with no event from the transcription "
                f"socket in {socket_gap:.0f}s (threshold "
                f"{self.socket_silent_seconds:.0f}s) — audio is being offered and "
                f"is going unanswered, so this is the socket, not a quiet room"
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


class TurnBoundaries:
    """Queues VAD speech spans so each turn keeps the boundaries that are ITS OWN.

    The Realtime API reports ``input_audio_buffer.speech_started`` /
    ``speech_stopped`` with ``audio_start_ms`` / ``audio_end_ms`` — offsets on
    the AUDIO clock, i.e. positions in the very byte stream tee'd to the durable
    recording. Those are the only honest boundaries for a turn.

    The events INTERLEAVE across turns, and that is the subtlety this class
    exists for. Captured from a real session:

        52.53  speech_started  audio_start_ms=27540     <- turn 1 opens
        55.81  speech_stopped  audio_end_ms=30784       <- turn 1 closes
        55.92  speech_started  audio_start_ms=30548     <- TURN 2 OPENS
        56.46  completed       "Have we finished..."    <- turn 1's TEXT
        57.91  speech_stopped  audio_end_ms=32608       <- turn 2 closes
        58.55  completed       "Ort a fhágfaidh..."     <- turn 2's text

    Transcription is asynchronous, so the next utterance's ``speech_started``
    routinely arrives BEFORE the previous utterance's ``completed``. Holding the
    boundaries in a single mutable slot therefore hands turn 1 the boundaries of
    turn 2, and an earlier revision did exactly that, producing two distinct
    corruptions from one bug:

      * turn 1 got a start from turn 2 and an end from the fallback, yielding an
        absurdly long span (18.91s measured; 31.36s in the operator's session).
        The TEXT was complete and correct, which is what made this dangerous:
        it reads as a finished thought with no cue that its span is wrong.
      * turn 2, whose start had been consumed and then cleared, fell back for
        BOTH ends and emitted a zero-length window (91.78-91.78).

    So spans are queued in arrival order and each ``completed`` takes the OLDEST
    unclaimed one — the API completes turns in the order it opened them.
    """

    def __init__(self) -> None:
        self._spans: deque[list[float | None]] = deque()

    @staticmethod
    def _seconds(event: dict, key: str) -> float | None:
        ms = event.get(key)
        return float(ms) / 1000.0 if isinstance(ms, (int, float)) else None

    def observe(self, event: dict) -> bool:
        """Consume a VAD event. True when it was one (and should not fall through)."""
        etype = event.get("type", "")
        if etype == "input_audio_buffer.speech_started":
            self._spans.append([self._seconds(event, "audio_start_ms"), None])
            return True
        if etype == "input_audio_buffer.speech_stopped":
            end_s = self._seconds(event, "audio_end_ms")
            # Close the most recent span still open. A stop with no open span
            # means the start was missed, so record the end alone rather than
            # dropping it.
            for span in reversed(self._spans):
                if span[1] is None:
                    span[1] = end_s
                    return True
            self._spans.append([None, end_s])
            return True
        return False

    def claim(self, streamed_s: float) -> tuple[float, float, bool]:
        """Boundaries for the turn now completing, plus whether VAD closed it.

        Takes the OLDEST unclaimed span: the API completes turns in the order it
        opened them, so the oldest span belongs to the transcript arriving now.

        ``streamed_s`` — seconds of audio actually streamed — is the fallback,
        NOT wall clock: it is a position on the same audio clock, so a missing
        boundary degrades to a coarse offset rather than a fabricated one.
        """
        span = self._spans.popleft() if self._spans else [None, None]
        start_s = span[0] if span[0] is not None else streamed_s
        end_s = span[1] if span[1] is not None else streamed_s
        if end_s < start_s:
            end_s = start_s
        return start_s, end_s, span[1] is not None

    @property
    def pending(self) -> int:
        """Spans opened but not yet claimed by a transcript."""
        return len(self._spans)


def timestamp_anomaly(
    start_s: float, end_s: float, previous_end_s: float | None
) -> str | None:
    """Why this turn's stamps are not a valid advance on the last one, if so.

    Turn boundaries must be monotonic and must not duplicate. Two consecutive
    events sharing an identical window is a real anomaly regardless of whether
    the text is accurate — and on 2026-09-01 it was accurate, which is exactly
    the problem: the corruption was invisible without a counting test.

    Making it LOUD is the point. An intermittent stamping fault that only shows
    up under a probe nobody runs is indistinguishable from no fault at all, and
    the whole lesson of #619 is that a failure which does not announce itself
    gets discovered far too late.
    """
    if end_s < start_s:
        return f"turn ends before it starts ({start_s:.2f} > {end_s:.2f})"
    # Degenerate durations. A zero-length turn cannot come from the audio clock:
    # VAD spans real speech, so equal start and end means both were stamped from
    # one fallback value. An implausibly long one means the span belongs to a
    # different turn, or a boundary was missed. Both were live defects.
    duration = end_s - start_s
    if duration <= 0.0:
        return f"turn has zero duration at {start_s:.2f} — no speech spans zero time"
    if duration > MAX_PLAUSIBLE_TURN_SECONDS:
        return (
            f"turn spans {duration:.2f}s ({start_s:.2f}-{end_s:.2f}), longer than "
            f"{MAX_PLAUSIBLE_TURN_SECONDS:.0f}s — server VAD closes on silence, so "
            f"a span this long means the boundary belongs to another turn"
        )
    if previous_end_s is None:
        return None
    # Server VAD can open the next turn a fraction before it reports the
    # previous one's stop — measured at 0.23s on a real session, with both
    # spans otherwise correct. That is the detector's own latency, not a
    # stamping fault, so only a substantial overlap is an anomaly.
    if start_s < previous_end_s - VAD_OVERLAP_TOLERANCE_SECONDS:
        return (
            f"turn starts at {start_s:.2f}, {previous_end_s - start_s:.2f}s before "
            f"the previous turn ended at {previous_end_s:.2f} — boundaries this far "
            f"out of order mean the spans belong to different turns"
        )
    return None


def transcript_record(
    index: int,
    text: str,
    *,
    start_s: float,
    end_s: float,
    filtered: str | None = None,
    filtered_detail: str | None = None,
) -> dict:
    """One finished turn.

    ``start_s``/``end_s`` MUST come from the VAD speech boundaries the server
    reports, not from wall clock. Deriving them from arrival time produced the
    fragmentation defect of 2026-09-01: every turn was labelled exactly 5.0s
    long and consecutive turns overlapped by 2-3s, because the label was the
    constant ``(now - 5.0, now)`` rather than a measurement of anything. The
    audio boundaries are the only honest source — they index the recording.

    A turn caught by the hallucination filter keeps its text and gains
    ``filtered``; nothing is ever silently dropped, so a false positive stays
    visible and the filter's accuracy stays measurable against the JSONL.
    """
    record = {
        "chunk": index,
        "t": datetime.now(tz=UTC).isoformat(),
        "start_s": round(start_s, 2),
        "end_s": round(end_s, 2),
        "ok": True,
        "text": text,
        "source": "stream",
    }
    if filtered:
        record["filtered"] = filtered
        if filtered_detail:
            record["filtered_detail"] = filtered_detail
    return record


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


DEGRADED_VAD_MESSAGE = (
    "local VAD unavailable (pip install 'ateles[vad]', or webrtcvad directly) "
    "— input gating is running on the RMS threshold alone; loud non-speech can "
    "still reach the model and be transcribed as fabrication"
)


def degraded_vad_record(index: int) -> dict:
    """The missing-VAD notice, on the channel the operator actually watches.

    ``load_speech_detector`` already logs this to stderr, but stderr is a
    write-only channel (ateles#583): the operator reads the JSONL through the
    session Monitor. A degraded gate changes the transcript the operator is
    about to trust, so it belongs where they are looking — non-fatal, because
    the RMS gate still works and the stream continues.
    """
    return error_record(index, DEGRADED_VAD_MESSAGE)


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


def session_update_message(
    model: str = DEFAULT_MODEL,
    silence_duration_ms: int | None = None,
    prefix_padding_ms: int | None = None,
) -> dict:
    """The Realtime session config.

    `server_vad` is load-bearing for BOTH correctness and cost: it closes turns
    on silence (fixing the 42% mid-sentence truncation) and it keeps billing on
    speech rather than wall clock. With `turn_detection: null` you commit
    whatever you stream, silence included, and pay for all of it.

    Its TUNING is load-bearing too, and used not to be sent at all. Naming only
    the type left `silence_duration_ms` on the API default of 500ms, short
    enough that an ordinary mid-sentence pause closed the turn and split one
    sentence into fragments too short to decode in context. Both values are now
    explicit, so the configuration the session runs under is visible in the
    request rather than inherited from a default that can change underneath us.

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
                    "turn_detection": {
                        "type": "server_vad",
                        "silence_duration_ms": (
                            VAD_SILENCE_DURATION_MS
                            if silence_duration_ms is None
                            else silence_duration_ms
                        ),
                        "prefix_padding_ms": (
                            VAD_PREFIX_PADDING_MS
                            if prefix_padding_ms is None
                            else prefix_padding_ms
                        ),
                    },
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
    expected_language: str = DEFAULT_LANGUAGE,
    use_local_vad: bool = True,
    gate_dbfs: float = INPUT_GATE_DBFS,
    vad_silence_ms: int = VAD_SILENCE_DURATION_MS,
    vad_prefix_padding_ms: int = VAD_PREFIX_PADDING_MS,
    raw_event_log: Path | None = None,
    health_poll: float = 5.0,
) -> tuple[int, HealthMonitor]:
    """Capture once, tee to disk and socket, append transcripts to the JSONL."""
    import websockets

    monitor = HealthMonitor()
    trace_started = time.monotonic()
    gate = InputGate(
        threshold_dbfs=gate_dbfs,
        vad=load_speech_detector(enabled=use_local_vad),
    )
    log(
        f"input gate: sending only above {gate.threshold_dbfs:g} dBFS sustained"
        + (f", local VAD {gate.vad.name}" if gate.vad else ", no local VAD")
    )
    # Beside the input gate, because the two decide together which audio
    # becomes a turn: the gate picks what is SENT, server VAD picks where the
    # turns are CUT. Reading one without the other explains neither.
    log(
        f"server VAD: closing turns after {vad_silence_ms}ms silence, "
        f"keeping {vad_prefix_padding_ms}ms before onset"
    )
    index = 0

    def append(record: dict) -> None:
        with open(out_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    if use_local_vad and gate.vad is None:
        # Degraded, not broken: say so where the operator is reading.
        append(degraded_vad_record(index))
        index += 1

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
            await ws.send(
                json.dumps(
                    session_update_message(
                        model,
                        silence_duration_ms=vad_silence_ms,
                        prefix_padding_ms=vad_prefix_padding_ms,
                    )
                )
            )
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
                    # Health always sees EVERY frame: the monitor's job is to
                    # know what the capture is really carrying, which the gate
                    # must not be able to hide from it.
                    monitor.note_audio(payload)
                    if not gate.should_send(payload, time.monotonic()):
                        continue
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
                boundaries = TurnBoundaries()
                previous_end_s: float | None = None

                async for raw in ws:
                    monitor.note_socket_event()
                    try:
                        event = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    etype = event.get("type", "")

                    if raw_event_log is not None:
                        with contextlib.suppress(OSError):
                            with open(raw_event_log, "a", encoding="utf-8") as fh:
                                fh.write(json.dumps({
                                    "at": round(time.monotonic() - trace_started, 3),
                                    "streamed_s": round(
                                        monitor.bytes_streamed / BYTES_PER_SECOND, 3),
                                    "type": etype,
                                    "audio_start_ms": event.get("audio_start_ms"),
                                    "audio_end_ms": event.get("audio_end_ms"),
                                    "item_id": event.get("item_id"),
                                    "content_index": event.get("content_index"),
                                    "transcript": event.get("transcript"),
                                    "delta": event.get("delta"),
                                }, ensure_ascii=False) + "\n")

                    if boundaries.observe(event):
                        continue

                    # Only COMPLETED turns reach the JSONL. `.delta` events are
                    # partial hypotheses for the same utterance; writing them
                    # would emit each sentence several times over.
                    if etype.endswith("input_audio_transcription.completed"):
                        text = (event.get("transcript") or "").strip()
                        if text:
                            # Fall back to the audio actually streamed so far
                            # rather than to wall clock: bytes_streamed is a
                            # position on the same audio clock the VAD reports
                            # on, so a missing boundary degrades to a slightly
                            # coarse offset instead of a fabricated one.
                            streamed_s = monitor.bytes_streamed / BYTES_PER_SECOND
                            start_s, end_s, vad_closed = boundaries.claim(streamed_s)

                            anomaly = timestamp_anomaly(start_s, end_s, previous_end_s)
                            if anomaly:
                                log(f"TIMESTAMP ANOMALY: {anomaly}")
                                append(
                                    error_record(
                                        index, f"timestamp anomaly: {anomaly}"
                                    )
                                )
                                index += 1
                            previous_end_s = end_s

                            verdict = screen_transcription(
                                text,
                                expected_language=expected_language,
                                window_seconds=max(0.0, end_s - start_s),
                                vad_closed=vad_closed,
                            )
                            if verdict.filtered:
                                log(
                                    f"filtered ({verdict.reason}): {text[:60]!r}"
                                )
                            append(
                                transcript_record(
                                    index,
                                    text,
                                    start_s=start_s,
                                    end_s=end_s,
                                    filtered=verdict.reason,
                                    filtered_detail=verdict.detail,
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
        log(f"input gate: {json.dumps(gate.summary())}")

    return 0, monitor


def build_parser() -> argparse.ArgumentParser:
    """The CLI surface, separated from `main` so a test can assert that a flag
    actually reaches the socket rather than only that it parses.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", default=os.environ.get("STREAM_TRANSCRIPT_DEVICE", ":3"))
    parser.add_argument("--dir", type=Path, default=DEFAULT_DIR)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument(
        "--raw-event-log",
        type=Path,
        default=None,
        help="append every socket event to this file (diagnostics)",
    )
    parser.add_argument(
        "--no-local-vad",
        action="store_true",
        help="disable the local VAD pre-filter (RMS input gating still applies)",
    )
    parser.add_argument(
        "--input-gate-dbfs",
        type=float,
        default=INPUT_GATE_DBFS,
        help=(
            "suppress audio below this sustained dBFS instead of sending it to "
            f"the model (default: {INPUT_GATE_DBFS:g}, calibrated per device)"
        ),
    )
    parser.add_argument(
        "--vad-silence-ms",
        type=int,
        default=VAD_SILENCE_DURATION_MS,
        help=(
            "silence the server must hear before closing a turn (default: "
            f"{VAD_SILENCE_DURATION_MS}). Lower splits sentences at "
            "mid-sentence pauses; higher reports each turn later"
        ),
    )
    parser.add_argument(
        "--vad-prefix-padding-ms",
        type=int,
        default=VAD_PREFIX_PADDING_MS,
        help=(
            "audio kept before the detected speech onset, so low-energy word "
            f"beginnings are not clipped (default: {VAD_PREFIX_PADDING_MS})"
        ),
    )
    parser.add_argument(
        "--language",
        default=DEFAULT_LANGUAGE,
        help="language the session is expected to be in (drives the hallucination filter)",
    )
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
    return parser


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)

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
                expected_language=args.language,
                use_local_vad=not args.no_local_vad,
                gate_dbfs=args.input_gate_dbfs,
                vad_silence_ms=args.vad_silence_ms,
                vad_prefix_padding_ms=args.vad_prefix_padding_ms,
                raw_event_log=args.raw_event_log,
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
