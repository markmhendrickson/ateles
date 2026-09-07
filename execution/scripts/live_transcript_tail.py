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
import re
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

# Remnant below this (seconds) with a stalled file is treated as empty — exit
# without a final ffmpeg/transcribe pass.
STALL_EMPTY_SECONDS = 0.05

# --- Silence gate -----------------------------------------------------------
# Whisper does not return empty on silence — it HALLUCINATES subtitle boilerplate
# ("thank you for watching", "please subscribe", full sentences in Japanese,
# Korean, Ukrainian). Gating on measured level BEFORE transcription is the only
# thing that actually stops it; post-hoc phrase filtering is a losing arms race
# against an open-ended set of fabrications in arbitrary languages.
#
# Statistic: the 95th percentile of ffmpeg's windowed RMS, NOT the median and
# NOT the peak. Measured on 39 labelled chunks of a real session:
#   - median FAILS: a speaker who pauses between sentences leaves a 35s window
#     with a median of -75 to -82 dB, indistinguishable from true silence.
#   - peak FAILS: transient clicks push silent windows to -22 dB.
#   - p95 separates: it asks "was there sustained energy in the loudest ~5% of
#     this window", which is exactly what "someone spoke at some point" means.
DEFAULT_SILENCE_THRESHOLD_DB = float(
    os.environ.get("LIVE_TRANSCRIPT_SILENCE_THRESHOLD_DB", "-50")
)
RMS_PERCENTILE = 0.95
_RMS_RE = re.compile(r"RMS_level=(-?[\d.]+)")

# --- Confidence gate (ateles#777) -------------------------------------------
# The level gate above is necessary but NOT sufficient. It answers "was this
# loud", and on a noisy microphone that is not the same question as "was this
# speech". Measured on the 2026-09-07 fixture session (mic track, ten chunks):
# a hallucination sat at -39.0 dB while real speech sat at -36.4 dB — a 2.6 dB
# separation. No threshold splits those populations, so raising the level gate
# cannot fix it; it would only start dropping real speech.
#
# So we ask Whisper what IT thinks, using the per-segment fields the
# ``verbose_json`` response format carries. This is the model's own judgement
# rather than a proxy measured outside it, and it is language-independent —
# which output-side phrase matching can never be, since the fabrications are
# open-ended across arbitrary languages (this fixture alone produced English,
# Chinese, Japanese and Ukrainian).
#
# A chunk is emitted as transcript when AT LEAST ONE segment looks like real
# speech on all three axes at once. No single axis separates the fixture:
#
#   no_speech_prob  Whisper's own "this is not speech" estimate, and the most
#                   run-to-run stable of the three. Alone it fails: real speech
#                   reached 0.370 while a fabrication sat at 0.297, so the
#                   populations overlap on this axis.
#   avg_logprob     How confident the decode was. Alone it fails too — a
#                   fabrication decoded at -0.331, better than real speech at
#                   -1.050 — but it catches the degenerate markup/garbage case,
#                   which decoded around -3.5.
#   char rate       Characters of text per second of segment. Fabricating on
#                   silence, Whisper stretches a few tokens across the whole
#                   window. This is a structural property of padding silence,
#                   not a phrase list: it never inspects WHAT was said, only how
#                   much text was claimed for how much time.
#
# Thresholds are fitted over FOUR transcriptions of each of the ten fixture
# chunks (40 samples), not one — Whisper's decode is non-deterministic, and a
# rule fitted to a single draw overfits badly. Re-transcribing one fabricated
# chunk five times produced five different fabrications in five different
# languages, with no_speech_prob ranging 0.03-0.60 and avg_logprob -0.33 to
# -4.15 on the SAME audio. Real speech, by contrast, was near-identical across
# trials — which is itself part of the signal.
#
# Over those 40 samples the defaults below keep 24/24 real-speech transcriptions
# and suppress 16/16 fabricated ones. The binding constraint is char rate: real
# speech never fell below 3.59 chars/s, while no fabrication passing the other
# two axes exceeded 2.87. Margins: nsp +0.26/-0.02, alp +0.40/-20.0,
# rate +0.55/-0.10.
DEFAULT_MAX_NO_SPEECH_PROB = float(
    os.environ.get("LIVE_TRANSCRIPT_MAX_NO_SPEECH_PROB", "0.40")
)
DEFAULT_MIN_AVG_LOGPROB = float(
    os.environ.get("LIVE_TRANSCRIPT_MIN_AVG_LOGPROB", "-1.0")
)
DEFAULT_MIN_CHAR_RATE = float(
    os.environ.get("LIVE_TRANSCRIPT_MIN_CHAR_RATE", "3.0")
)
# Set to "0" to log the verdict without acting on it — useful when calibrating
# the thresholds on a new mic or room before trusting them to suppress.
CONFIDENCE_GATE_ENABLED = os.environ.get("LIVE_TRANSCRIPT_CONFIDENCE_GATE", "1") != "0"

# --- Follow mode ------------------------------------------------------------
DEFAULT_FOLLOW = os.environ.get("LIVE_TRANSCRIPT_FOLLOW", "") == "1"
DEFAULT_FOLLOW_TIMEOUT_MIN = float(
    os.environ.get("LIVE_TRANSCRIPT_FOLLOW_TIMEOUT_MIN", "30")
)
# Resume is polled far faster than the chunk interval: it is a directory listing,
# not a transcription. Detection latency costs nothing in lost audio (resume
# starts at cursor 0) but it does delay the operator's first words reaching the
# session, so keep it short.
FOLLOW_POLL_SECONDS = 4.0


def log(msg: str) -> None:
    print(f"[live-tail] {msg}", file=sys.stderr, flush=True)


def slice_decision(
    available: float,
    stalled: bool,
    *,
    min_slice: float = MIN_SLICE_SECONDS,
    stall_empty: float = STALL_EMPTY_SECONDS,
) -> str:
    """Decide what to do when evaluating the next live chunk.

    Returns one of:
      - ``"transcribe"`` — enough new audio; take a normal slice
      - ``"wait"`` — sub-threshold and still growing; sleep another cycle
      - ``"exit_clean"`` — stalled with essentially no remnant
      - ``"flush_final"`` — stalled with a usable remnant; one last slice then exit
    """
    if available >= min_slice:
        return "transcribe"
    if not stalled:
        return "wait"
    if available <= stall_empty:
        return "exit_clean"
    return "flush_final"


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


def parse_rms_levels(stderr: str) -> list[float]:
    """Extract finite windowed RMS_level values (dB) from ffmpeg astats output."""
    return [
        float(m) for m in _RMS_RE.findall(stderr or "")
        if "inf" not in m.lower()
    ]


def sustained_rms_db(values: list[float], percentile: float = RMS_PERCENTILE) -> float | None:
    """Representative *sustained* level: the ``percentile`` of windowed RMS.

    Deliberately not the mean/median (a pausing speaker drags those down to
    silence levels) and not the max (a single click lifts silence to speech
    levels). See DEFAULT_SILENCE_THRESHOLD_DB for the measured rationale.
    """
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(percentile * len(ordered)))
    return ordered[idx]


def measure_slice_rms_db(wav_path: Path) -> float | None:
    """Sustained RMS (dB) of a slice, or None if the measurement failed.

    None is the caller's signal to transcribe anyway: a broken measurement must
    never silently discard audio.
    """
    try:
        proc = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-i", str(wav_path),
                "-af",
                "astats=metadata=1:reset=1:length=3,"
                "ametadata=print:key=lavfi.astats.Overall.RMS_level",
                "-f", "null", "/dev/null",
            ],
            capture_output=True, text=True, timeout=60,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        log(f"RMS measurement failed ({exc}) — transcribing anyway")
        return None

    level = sustained_rms_db(parse_rms_levels(proc.stderr))
    if level is None:
        log("RMS measurement returned no usable values — transcribing anyway")
    return level


def segment_char_rate(segment: dict) -> float:
    """Characters of transcript per second of segment wall-clock.

    Whisper hallucinating on near-silence stretches a handful of tokens across
    the whole window, so this collapses toward zero; real speech bursts well
    above it. Measures only how much text was claimed for how much time — it
    never looks at what the text says, in any language.
    """
    span = float(segment.get("end", 0.0)) - float(segment.get("start", 0.0))
    if span <= 0:
        return 0.0
    return len((segment.get("text") or "").strip()) / span


def segment_is_speech(
    segment: dict,
    *,
    max_no_speech_prob: float = DEFAULT_MAX_NO_SPEECH_PROB,
    min_avg_logprob: float = DEFAULT_MIN_AVG_LOGPROB,
    min_char_rate: float = DEFAULT_MIN_CHAR_RATE,
) -> bool:
    """True when one segment clears all three confidence axes at once."""
    return (
        float(segment.get("no_speech_prob", 0.0)) <= max_no_speech_prob
        and float(segment.get("avg_logprob", 0.0)) >= min_avg_logprob
        and segment_char_rate(segment) >= min_char_rate
    )


def classify_transcription(
    segments: list[dict] | None,
    *,
    max_no_speech_prob: float = DEFAULT_MAX_NO_SPEECH_PROB,
    min_avg_logprob: float = DEFAULT_MIN_AVG_LOGPROB,
    min_char_rate: float = DEFAULT_MIN_CHAR_RATE,
) -> tuple[str, dict]:
    """Judge a transcribed chunk from Whisper's own per-segment confidence.

    Returns ``(verdict, evidence)`` where verdict is one of:

      - ``"speech"``      — at least one segment clears all three axes; emit it
      - ``"hallucinated"``— segments exist but none does; suppress it
      - ``"unknown"``     — no segments came back at all, so there is no signal
                            to judge on. Emitted rather than suppressed: a
                            missing measurement must never silently discard
                            audio, exactly as a failed RMS read falls through
                            to transcription.

    ``evidence`` carries the best segment's three numbers so the JSONL records
    WHY a chunk was suppressed, and so thresholds can be re-derived later from
    the log alone without re-billing the API.
    """
    if not segments:
        return "unknown", {}

    scored = [
        (
            float(s.get("no_speech_prob", 0.0)),
            float(s.get("avg_logprob", 0.0)),
            segment_char_rate(s),
            s,
        )
        for s in segments
    ]
    speechy = [
        t for t in scored
        if segment_is_speech(
            t[3],
            max_no_speech_prob=max_no_speech_prob,
            min_avg_logprob=min_avg_logprob,
            min_char_rate=min_char_rate,
        )
    ]
    # Report the segment that carried the decision: the best speech-looking one
    # when we keep, the closest near-miss (densest) when we suppress.
    ref = max(speechy or scored, key=lambda t: t[2])
    evidence = {
        "no_speech_prob": round(ref[0], 4),
        "avg_logprob": round(ref[1], 4),
        "char_rate": round(ref[2], 2),
        "segments": len(segments),
    }
    return ("speech" if speechy else "hallucinated"), evidence


def track_kind(path: Path) -> str:
    """Which Audio Hijack track a file belongs to ('mic', 'remote'/'system', …).

    Used on resume so a paused *mic* recording resumes on the new *mic* file
    rather than jumping tracks mid-session.
    """
    name = path.name.lower()
    if "mic" in name:
        return "mic"
    for t in REMOTE_TRACK_NAMES:
        if t in name:
            return t
    return ""


def matches_track(path: Path, kind: str) -> bool:
    return (
        path.is_file()
        and path.suffix.lower() in RECORDING_EXTENSIONS
        and track_kind(path) == kind
    )


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


def wait_for_resume(
    watch_dir: Path,
    kind: str,
    known: set[Path],
    timeout_s: float,
    *,
    poll: float = FOLLOW_POLL_SECONDS,
) -> Path | None:
    """Block until a NEW recording of the same track appears; None on timeout.

    Detection is on *file appearance*, not on confirmed growth. Audio Hijack
    creates the file the moment recording starts, and the 3s growth probe used
    at startup is known to false-negative on its buffered writes. Because resume
    always re-slices from second zero, detecting a hair early costs nothing —
    whereas waiting to confirm growth costs the operator's first sentence.
    """
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        time.sleep(poll)
        try:
            current = {p for p in watch_dir.iterdir() if matches_track(p, kind)}
        except OSError:
            continue

        fresh = sorted(current - known, key=lambda p: p.stat().st_mtime)
        if not fresh:
            continue

        candidate = fresh[-1]
        # A just-created file may not be a readable container yet. Keep polling
        # rather than erroring out — the next pass usually succeeds.
        if probe_duration(candidate) is None:
            continue
        return candidate

    return None


# Returned in place of an error when a slice transcribes to nothing. Silence is
# an ordinary meeting state (a pause, a break, someone reading), NOT a failure —
# counting it toward the failure kill switch would stop the tailer mid-meeting.
SILENCE_SENTINEL = "__silence__"


def apply_transcription_result(
    record: dict,
    ok: bool,
    payload: str,
    consecutive_failures: int,
) -> int:
    """Update ``record`` from a ``transcribe_slice`` result; return new failure streak.

    Silence is a normal meeting state: it neither increments nor resets the
    consecutive-failure kill switch.
    """
    if ok:
        record["text"] = payload
        return 0
    if payload == SILENCE_SENTINEL:
        record["ok"] = True
        record["text"] = ""
        record["silence"] = True
        return consecutive_failures
    record["error"] = payload
    return consecutive_failures + 1


#: The only secret transcribe_audio.py needs. The materialized dotenv holds
#: many unrelated credentials (GitHub PATs, Telegram and Wise tokens, the
#: Neotoma bearer token and mnemonic); none of them belong in the environment
#: of a transcription subprocess.
SUBPROCESS_SECRET_KEYS = ("OPENAI_API_KEY",)

def materialized_env_path() -> Path:
    """Where SOPS materializes the operator's dotenv.

    Resolved on each call, not at import, so ``Path.home`` stays patchable in
    tests and a changed HOME is honoured at runtime.
    """
    return Path.home() / ".config" / "neotoma" / ".env"


def build_subprocess_env(
    materialized: Path | None = None,
    base_env: dict | None = None,
) -> dict:
    """Build the environment handed to the transcription subprocess.

    Inherits the current environment, then fills in ONLY the keys in
    ``SUBPROCESS_SECRET_KEYS`` from the SOPS-materialized dotenv. That file
    also holds unrelated secrets, so we never load it wholesale — mirroring
    the same restraint the task dashboard's ``neotomaProxy.ts`` documents.

    An already-set key in the real environment wins, so an operator can
    override without editing the dotenv.
    """
    env = {**(os.environ if base_env is None else base_env)}
    path = materialized_env_path() if materialized is None else materialized
    if not path.exists():
        return env

    wanted = set(SUBPROCESS_SECRET_KEYS)
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        if k not in wanted:
            continue
        env.setdefault(k, v.strip().strip('"').strip("'"))
    return env


def transcribe_slice(wav_path: Path, env: dict) -> tuple[bool, str, list[dict]]:
    """Transcribe one slice.

    Returns ``(ok, payload, segments)``. On failure the payload is an error
    string, EXCEPT for an empty transcript, which returns SILENCE_SENTINEL so
    the caller can tell a quiet interval apart from a broken transcription path.

    ``segments`` carries Whisper's per-segment ``no_speech_prob`` /
    ``avg_logprob`` for the confidence gate, and is empty whenever that sidecar
    could not be read — which the gate reads as "no signal", never as silence.
    """
    python_bin = str(VENV_PYTHON) if VENV_PYTHON.exists() else sys.executable
    seg_path = wav_path.with_suffix(".segments.json")
    try:
        result = subprocess.run(
            [
                python_bin, str(TRANSCRIBE), str(wav_path),
                "--no-store", "--no-diarize",
                "--segments-json", str(seg_path),
            ],
            capture_output=True, text=True, env=env, timeout=300,
        )
    except subprocess.TimeoutExpired:
        return False, "transcription timed out after 300s", []
    except OSError as exc:
        return False, f"failed to run transcribe_audio.py: {exc}", []
    finally:
        pass

    segments: list[dict] = []
    try:
        if seg_path.exists():
            payload = json.loads(seg_path.read_text(encoding="utf-8"))
            segments = payload.get("segments") or []
    except (OSError, ValueError, AttributeError) as exc:
        # A missing or malformed sidecar disables the confidence gate for this
        # chunk rather than suppressing it. Never drop audio because a
        # measurement broke.
        log(f"could not read segment confidences ({exc}) — gate skipped for this chunk")
    finally:
        seg_path.unlink(missing_ok=True)

    if result.returncode != 0:
        tail = (result.stderr or "").strip().splitlines()
        return False, (tail[-1] if tail else f"exit {result.returncode}"), segments

    # transcribe_audio.py prints a "Transcribing audio file: ..." banner ahead of
    # the transcript; drop it so the JSONL carries only spoken text.
    lines = [ln for ln in (result.stdout or "").splitlines()
             if ln.strip() and not ln.startswith("Transcribing audio file:")]
    text = " ".join(ln.strip() for ln in lines).strip()
    if not text:
        return False, SILENCE_SENTINEL, segments
    return True, text, segments


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
    ap.add_argument("--follow", action="store_true", default=DEFAULT_FOLLOW,
                    help="On stop, pause and wait for the recording to resume "
                         "instead of exiting (env LIVE_TRANSCRIPT_FOLLOW=1)")
    ap.add_argument("--follow-timeout-min", type=float,
                    default=DEFAULT_FOLLOW_TIMEOUT_MIN,
                    help=f"Minutes to wait for a resume before exiting "
                         f"(default: {DEFAULT_FOLLOW_TIMEOUT_MIN:g})")
    ap.add_argument("--silence-threshold-db", type=float,
                    default=DEFAULT_SILENCE_THRESHOLD_DB,
                    help=f"Skip transcription below this sustained RMS in dB "
                         f"(default: {DEFAULT_SILENCE_THRESHOLD_DB:g})")
    ap.add_argument("--max-no-speech-prob", type=float,
                    default=DEFAULT_MAX_NO_SPEECH_PROB,
                    help=f"Confidence gate: a segment counts as speech only at or "
                         f"below this Whisper no_speech_prob "
                         f"(default: {DEFAULT_MAX_NO_SPEECH_PROB:g})")
    ap.add_argument("--min-avg-logprob", type=float,
                    default=DEFAULT_MIN_AVG_LOGPROB,
                    help=f"Confidence gate: a segment counts as speech only at or "
                         f"above this Whisper avg_logprob "
                         f"(default: {DEFAULT_MIN_AVG_LOGPROB:g})")
    ap.add_argument("--min-char-rate", type=float,
                    default=DEFAULT_MIN_CHAR_RATE,
                    help=f"Confidence gate: a segment counts as speech only at or "
                         f"above this many transcript characters per second "
                         f"(default: {DEFAULT_MIN_CHAR_RATE:g})")
    ap.add_argument("--no-confidence-gate", dest="confidence_gate",
                    action="store_false", default=CONFIDENCE_GATE_ENABLED,
                    help="Report the confidence verdict in the JSONL but do not "
                         "suppress — for calibrating on a new mic or room "
                         "(env LIVE_TRANSCRIPT_CONFIDENCE_GATE=0)")
    args = ap.parse_args(argv)

    if not TRANSCRIBE.exists():
        log(f"transcribe_audio.py not found at {TRANSCRIBE}")
        return 1

    env = build_subprocess_env()
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
    log(f"silence gate: skip below {args.silence_threshold_db:g} dB sustained RMS")
    log(f"confidence gate: {'on' if args.confidence_gate else 'report-only'} — "
        f"segment must have no_speech_prob <= {args.max_no_speech_prob:g}, "
        f"avg_logprob >= {args.min_avg_logprob:g}, "
        f"chars/s >= {args.min_char_rate:g}")
    if args.follow:
        log(f"follow mode: on — pausing (not exiting) on stop, up to "
            f"{args.follow_timeout_min:g} min per break")
    log(f"JSONL: {out_path}")
    print(str(out_path), flush=True)  # stdout: the path, for scripting

    chunk_index = 0
    consecutive_failures = 0

    def append(record: dict) -> None:
        with out_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    # Track kind + directory census, so a resume picks the same track (a paused
    # *mic* recording resumes on the new *mic* file) and only counts files that
    # did not already exist when the pause began.
    kind = track_kind(recording)
    watch_dir = args.dir

    def pause_and_resume() -> Path | None:
        """Emit a pause marker and block for a resume. None means give up."""
        log("recording stopped — pausing, watching for resume")
        append({"event": "paused", "t": datetime.now(tz=UTC).isoformat()})

        try:
            known = {p for p in watch_dir.iterdir() if matches_track(p, kind)}
        except OSError:
            known = set()
        known.add(recording)

        resumed = wait_for_resume(
            watch_dir, kind, known, args.follow_timeout_min * 60.0
        )
        if resumed is None:
            log(f"no resume within {args.follow_timeout_min:g} min — exiting")
            return None

        log(f"recording resumed — following {resumed.name}")
        append({
            "event": "resumed",
            "t": datetime.now(tz=UTC).isoformat(),
            "file": str(resumed),
        })
        return resumed

    try:
        while True:
            time.sleep(args.interval)

            duration = probe_duration(recording)
            if duration is None:
                log("could not probe duration — recording ended?")
                if args.follow:
                    resumed = pause_and_resume()
                    if resumed is None:
                        break
                    recording, cursor = resumed, 0.0
                    continue
                break

            available = duration - cursor
            final_slice = False
            # Too little new audio to be worth a chunk yet — but this is also
            # what a finished recording looks like. Check for a stall across
            # the WHOLE sub-threshold range: a recording that stops with a
            # remnant in (0, MIN_SLICE_SECONDS) leaves `available` frozen
            # there, so gating this on `available <= 0.05` would loop forever
            # and never fire the caller's lifecycle watch.
            if available < MIN_SLICE_SECONDS:
                try:
                    stalled = (time.time() - recording.stat().st_mtime) > (args.interval * 2)
                except OSError:
                    stalled = True
                decision = slice_decision(available, stalled)
                if decision == "wait":
                    continue
                if decision == "exit_clean":
                    if args.follow:
                        resumed = pause_and_resume()
                        if resumed is None:
                            break
                        # Resume at second ZERO of the new file, not at its
                        # current duration. Detection takes a few seconds and the
                        # operator starts talking the instant they hit record —
                        # starting at the cursor would drop exactly those words.
                        # A longer-than-interval first chunk is the intended
                        # cost of losing nothing.
                        recording, cursor = resumed, 0.0
                        continue
                    log("recording appears to have stopped — exiting")
                    break
                # flush_final: stop with a usable remnant — transcribe it so the
                # meeting's final words are not dropped, then exit.
                log(
                    f"recording appears to have stopped — flushing final "
                    f"{available:.1f}s slice"
                )
                final_slice = True

            tmp = tempfile.NamedTemporaryFile(
                suffix=f"_live{chunk_index:04d}.wav", delete=False, prefix="livetail_"
            )
            tmp.close()
            tmp_path = Path(tmp.name)

            rms_db: float | None = None
            skipped_silent = False
            segments: list[dict] = []
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
                    # Gate BEFORE transcribing. A measurement failure returns
                    # None and falls through to transcription — never drop audio
                    # because the meter broke.
                    rms_db = measure_slice_rms_db(tmp_path)
                    if rms_db is not None and rms_db < args.silence_threshold_db:
                        skipped_silent = True
                        ok, payload = True, ""
                    else:
                        ok, payload, segments = transcribe_slice(tmp_path, env)
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
            if skipped_silent:
                # Same shape as post-hoc silence, plus the measurement that
                # caused the skip. Not a failure: does not touch the streak.
                record["text"] = ""
                record["silence"] = True
                record["skipped"] = "below_threshold"
                record["rms_db"] = round(rms_db, 1)
            else:
                if rms_db is not None:
                    record["rms_db"] = round(rms_db, 1)
                consecutive_failures = apply_transcription_result(
                    record, ok, payload, consecutive_failures
                )
                # Confidence gate — only meaningful on a chunk that actually
                # produced text. A silent or failed chunk has nothing to judge.
                if ok and record.get("text"):
                    verdict, evidence = classify_transcription(
                        segments,
                        max_no_speech_prob=args.max_no_speech_prob,
                        min_avg_logprob=args.min_avg_logprob,
                        min_char_rate=args.min_char_rate,
                    )
                    if evidence:
                        record["confidence"] = evidence
                    if verdict == "hallucinated":
                        # Deliberately distinct from the level gate's
                        # "below_threshold": the two mechanisms suppress
                        # different populations and must be measurable apart.
                        record["suppressed"] = "low_confidence"
                        if args.confidence_gate:
                            # Keep the text under a separate key so the chunk
                            # is auditable, but never under "text" — a consumer
                            # reading "text" must never see a fabrication.
                            record["suppressed_text"] = record.pop("text")
                            record["text"] = ""
                        else:
                            # Report-only calibration mode: flag it, emit it.
                            record["suppressed"] = "low_confidence_reported_only"
                    elif verdict == "unknown":
                        # No confidence signal came back. Emit, but say so, so a
                        # consumer can render it with the uncertainty visible
                        # rather than as vouched-for transcript.
                        record["confidence_unavailable"] = True

            append(record)

            # Advance regardless of transcription success: a failed chunk must not
            # re-slice the same audio forever.
            cursor += available
            chunk_index += 1

            if final_slice:
                # The remnant is flushed either way — the operator's last words
                # reach the session before the pause marker.
                if args.follow:
                    log("final slice written")
                    resumed = pause_and_resume()
                    if resumed is None:
                        break
                    recording, cursor = resumed, 0.0
                    continue
                log("final slice written — exiting")
                break

            if consecutive_failures >= 5:
                log("5 consecutive failures — stopping (check the JSONL error lines)")
                break

    except KeyboardInterrupt:
        log("interrupted — stopping")

    log(f"done. {chunk_index} chunk(s) written to {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
