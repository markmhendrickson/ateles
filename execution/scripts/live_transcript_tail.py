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
from typing import Literal

sys.path.insert(0, str(Path(__file__).resolve().parent))

from hallucination_filter import screen_transcription  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
TRANSCRIBE = REPO_ROOT / "execution" / "scripts" / "transcribe_audio.py"

# The marker transcribe_audio.py prints under `--no-store --emit-language`.
LANGUAGE_MARKER = "__TRANSCRIBE_LANGUAGE__="

# Interpreter candidates, in priority order. REPO_ROOT/execution/venv is the
# canonical one; a git worktree usually has only `.venv`, which is why resolving
# against REPO_ROOT alone silently degraded to an interpreter that cannot import
# `config` and failed EVERY chunk while reporting healthy. See
# resolve_transcriber_python().
VENV_PYTHON = REPO_ROOT / "execution" / "venv" / "bin" / "python"
DOTVENV_PYTHON = REPO_ROOT / ".venv" / "bin" / "python"

# Matches Tyto's conventions so both halves see the same files.
REMOTE_TRACK_NAMES = ("remote", "system")
RECORDING_EXTENSIONS = {".aac", ".m4a", ".mp4", ".wav"}

# Track kinds in selection priority. The mic track is the operator; the system
# track is the computer's OUTPUT. Audio Hijack writes both simultaneously, so an
# auto-detect that took whichever it found first transcribed the AGENT's own
# speech back as the operator's — plausible-looking chunks from the wrong
# source, which is worse than no chunks at all.
TRACK_PRIORITY = ("mic", "remote", "system")

# The session's expected language. A detected language other than this is the
# single highest-yield hallucination signal; see hallucination_filter.
DEFAULT_SESSION_LANGUAGE = os.environ.get("LIVE_TRANSCRIPT_LANGUAGE", "en")

# Three failed chunks in a row means the transcription path is broken, not that
# the room is quiet. On 2026-09-01 every chunk failed with
# `ModuleNotFoundError: config` across four restarts while the tailer reported
# healthy — a run that cannot transcribe anything is not tailing.
DEFAULT_MAX_CONSECUTIVE_FAILURES = int(
    os.environ.get("LIVE_TRANSCRIPT_MAX_CONSECUTIVE_FAILURES", "3")
)

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


def track_priority(path: Path) -> int:
    """Sort key: lower is preferred. Mic beats remote beats system."""
    kind = track_kind(path)
    return TRACK_PRIORITY.index(kind) if kind in TRACK_PRIORITY else len(TRACK_PRIORITY)


def is_tailable_track(path: Path) -> bool:
    """Any Audio Hijack track this tailer can follow, mic included."""
    return (
        path.is_file()
        and path.suffix.lower() in RECORDING_EXTENSIONS
        and track_kind(path) in TRACK_PRIORITY
    )


def find_growing_recording(watch_dir: Path, settle_probe: float = 3.0) -> Path | None:
    """Return the actively-growing track, preferring mic over system/remote.

    Probes ALL candidates across one sleep rather than only the newest, because
    Audio Hijack writes `<session> mic.mp4` and `<session> system.mp4`
    simultaneously and their mtimes interleave. Taking whichever happened to be
    newest is how the 2026-09-01 session ended up tailing the computer's own
    OUTPUT and feeding the agent's speech back as the operator's.
    """
    if not watch_dir.exists():
        log(f"watch dir does not exist: {watch_dir}")
        return None

    candidates = [p for p in watch_dir.iterdir() if is_tailable_track(p)]
    if not candidates:
        return None

    def size_of(p: Path) -> int | None:
        try:
            return p.stat().st_size
        except OSError:
            return None

    before = {p: size_of(p) for p in candidates}
    time.sleep(settle_probe)

    growing = []
    for p in candidates:
        start, end = before.get(p), size_of(p)
        if start is not None and end is not None and end > start:
            growing.append(p)

    if not growing:
        newest = max(candidates, key=lambda p: p.stat().st_mtime)
        log(f"no recording is growing (finished?): newest is {newest.name}")
        return None

    # Priority first, mtime only to break ties within a kind.
    growing.sort(key=lambda p: (track_priority(p), -p.stat().st_mtime))
    return growing[0]


def warn_if_not_mic(recording: Path) -> None:
    """Say loudly when the selected track is the computer's output, not the mic.

    Silence here is what made the wrong-track failure invisible: a system-track
    tail produces perfectly plausible chunks, they are just the wrong person's.
    """
    kind = track_kind(recording)
    log(f"selected track: {kind or 'unknown'} ({recording.name})")
    if kind in ("system", "remote"):
        log("=" * 68)
        log("WARNING: SYSTEM/REMOTE TRACK SELECTED — this is the computer's")
        log("OUTPUT, not the microphone. Transcribed speech will be whatever")
        log("was PLAYED, not what you said.")
        log(f"  selected: {recording}")
        log("  pass --file '<session> mic.mp4' to tail the microphone instead.")
        log("=" * 68)


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


LivenessVerdict = Literal["healthy", "dead", "stalled", "paused_ok"]


def _pid_alive(pid: int | None) -> bool:
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _last_jsonl_record(jsonl_path: Path) -> dict | None:
    if not jsonl_path.exists():
        return None
    last: dict | None = None
    for line in jsonl_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            last = json.loads(line)
        except json.JSONDecodeError:
            continue
    return last


def effective_recording_path(
    jsonl_path: Path,
    recording_path: Path | None,
) -> Path | None:
    """Return the active recording file, following ``resumed`` events in JSONL."""
    if not jsonl_path.exists():
        return recording_path

    active = recording_path
    for line in jsonl_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("event") == "resumed" and record.get("file"):
            active = Path(record["file"])
    return active


def assess_tailer_liveness(
    *,
    tailer_pid: int | None,
    jsonl_path: Path,
    recording_path: Path | None,
    interval_s: float,
    now: float | None = None,
    prev_recording_size: int | None = None,
    prev_jsonl_mtime: float | None = None,
) -> tuple[LivenessVerdict, str]:
    """Assess whether the live tailer is healthy, paused legitimately, or stuck.

    Called from the ``/stream-transcript`` skill's staleness watch (§4c), not
    from the tailer's main loop. ``prev_recording_size`` and ``prev_jsonl_mtime``
    come from the prior probe so growth-vs-staleness can be detected across the
    ~60s supervisor cadence.
    """
    now = time.time() if now is None else now
    alive = _pid_alive(tailer_pid)
    last = _last_jsonl_record(jsonl_path)
    paused = last is not None and last.get("event") == "paused"

    if paused:
        if alive:
            return "paused_ok", "tailer alive during follow break — not an alert"
        return (
            "dead",
            "tailer process gone during follow break — relaunch before resume",
        )

    if tailer_pid is not None and not alive:
        return "dead", "tailer process gone — relaunch tailer"

    recording_path = effective_recording_path(jsonl_path, recording_path)

    jsonl_mtime: float | None = None
    if jsonl_path.exists():
        try:
            jsonl_mtime = jsonl_path.stat().st_mtime
        except OSError:
            jsonl_mtime = None

    stall_threshold = 2 * interval_s + 30

    if (
        prev_jsonl_mtime is not None
        and jsonl_mtime is not None
        and jsonl_mtime > prev_jsonl_mtime
    ):
        return "healthy", "JSONL advancing"

    recording_growing = False
    if recording_path is not None and recording_path.exists():
        try:
            st = recording_path.stat()
            if prev_recording_size is not None:
                recording_growing = st.st_size > prev_recording_size
            else:
                recording_growing = (now - st.st_mtime) <= interval_s
        except OSError:
            recording_growing = False

    if (
        recording_growing
        and jsonl_mtime is not None
        and (now - jsonl_mtime) > stall_threshold
    ):
        stale_s = now - jsonl_mtime
        return (
            "stalled",
            f"recording growing but JSONL silent for {stale_s:.0f}s — "
            "tailer may be stuck",
        )

    if jsonl_mtime is not None and (now - jsonl_mtime) <= stall_threshold:
        return "healthy", "JSONL recently updated"

    return "healthy", "tailer appears healthy"


# Returned in place of an error when a slice transcribes to nothing. Silence is
# an ordinary meeting state (a pause, a break, someone reading), NOT a failure —
# counting it toward the failure kill switch would stop the tailer mid-meeting.
SILENCE_SENTINEL = "__silence__"


def apply_transcription_result(
    record: dict,
    ok: bool,
    payload: str,
    consecutive_failures: int,
    *,
    detected_language: str | None = None,
    expected_language: str | None = None,
    window_seconds: float | None = None,
    screen: bool = True,
) -> int:
    """Update ``record`` from a ``transcribe_slice`` result; return new failure streak.

    Silence is a normal meeting state: it neither increments nor resets the
    consecutive-failure kill switch.

    A successful transcription is additionally screened for hallucination
    signatures. A caught chunk KEEPS ITS TEXT and gains ``filtered`` plus a
    reason — it is never dropped. Dropping it would make a false positive
    invisible and unrecoverable, which is precisely the silent-failure class
    this whole issue is about. A filtered chunk is also not a failure: the
    transcription path worked, it just produced a fabrication, so the streak
    resets exactly as a clean success would.
    """
    if ok:
        record["text"] = payload
        if detected_language:
            record["language"] = detected_language
        if not screen:
            return 0
        verdict = screen_transcription(
            payload,
            expected_language=expected_language,
            detected_language=detected_language,
            window_seconds=window_seconds,
        )
        if verdict.filtered:
            record["filtered"] = verdict.reason
            if verdict.detail:
                record["filtered_detail"] = verdict.detail
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


def preflight_interpreter(python_bin: Path, env: dict) -> str | None:
    """Return None if ``python_bin`` can import what transcribe_audio.py needs.

    Otherwise returns why it cannot. Existence is not enough: the interpreter
    that broke the 2026-09-01 session existed and ran, it just could not import
    `config`. Only an actual import proves the dependency path is intact.
    """
    if not python_bin.exists():
        return "does not exist"
    probe = (
        "import sys; sys.path.insert(0, %r); "
        "import config, openai, dotenv, requests" % str(TRANSCRIBE.parent)
    )
    try:
        proc = subprocess.run(
            [str(python_bin), "-c", probe],
            capture_output=True, text=True, env=env, timeout=60,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        return f"could not be run ({exc})"
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()
        return tail[-1] if tail else f"import probe exited {proc.returncode}"
    return None


def resolve_transcriber_python(env: dict) -> tuple[Path | None, list[str]]:
    """Pick an interpreter that can actually run transcribe_audio.py.

    Returns ``(interpreter, attempts)``. A None interpreter means the caller
    must REFUSE TO START — the old behaviour of falling back to
    ``sys.executable`` produced a tailer that ran forever, wrote a chunk every
    30 seconds, and failed every single one.

    ``LIVE_TRANSCRIPT_PYTHON`` overrides the search, but is preflighted like any
    other candidate: an explicit override that cannot import the dependencies is
    the same silent degradation under a different name.
    """
    candidates: list[Path] = []
    override = env.get("LIVE_TRANSCRIPT_PYTHON", "").strip()
    if override:
        candidates.append(Path(override))
    candidates += [VENV_PYTHON, DOTVENV_PYTHON]

    attempts: list[str] = []
    for candidate in candidates:
        problem = preflight_interpreter(candidate, env)
        if problem is None:
            return candidate, attempts
        attempts.append(f"{candidate}: {problem}")
    return None, attempts


def transcribe_slice(
    wav_path: Path,
    env: dict,
    python_bin: Path,
    *,
    language: str | None = None,
) -> tuple[bool, str, str | None]:
    """Transcribe one slice.

    Returns ``(ok, payload, detected_language)``. On failure the payload is an
    error string, EXCEPT for an empty transcript, which returns SILENCE_SENTINEL
    so the caller can tell a quiet interval apart from a broken transcription
    path.

    ``python_bin`` is resolved once at startup and passed in — never re-derived
    here, so there is no code path left that can quietly pick a different
    interpreter mid-run.
    """
    cmd = [
        str(python_bin), str(TRANSCRIBE), str(wav_path),
        "--no-store", "--no-diarize", "--emit-language",
    ]
    if language:
        cmd += ["--language", language]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, env=env, timeout=300,
        )
    except subprocess.TimeoutExpired:
        return False, "transcription timed out after 300s", None
    except OSError as exc:
        return False, f"failed to run transcribe_audio.py: {exc}", None

    if result.returncode != 0:
        tail = (result.stderr or "").strip().splitlines()
        return False, (tail[-1] if tail else f"exit {result.returncode}"), None

    # transcribe_audio.py prints a "Transcribing audio file: ..." banner ahead of
    # the transcript; drop it so the JSONL carries only spoken text. The language
    # marker rides on its own trailing line.
    detected: str | None = None
    lines = []
    for ln in (result.stdout or "").splitlines():
        if not ln.strip():
            continue
        if ln.startswith(LANGUAGE_MARKER):
            detected = ln[len(LANGUAGE_MARKER):].strip() or None
            continue
        if ln.startswith("Transcribing audio file:"):
            continue
        lines.append(ln.strip())

    text = " ".join(lines).strip()
    if not text:
        return False, SILENCE_SENTINEL, detected
    return True, text, detected


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
    ap.add_argument("--max-consecutive-failures", type=int,
                    default=DEFAULT_MAX_CONSECUTIVE_FAILURES,
                    help=f"Stop after this many consecutive failed chunks "
                         f"(default: {DEFAULT_MAX_CONSECUTIVE_FAILURES})")
    ap.add_argument("--language", default=DEFAULT_SESSION_LANGUAGE,
                    help=f"Expected session language, for the hallucination "
                         f"filter (default: {DEFAULT_SESSION_LANGUAGE!r}); "
                         f"empty disables the language check")
    ap.add_argument("--no-hallucination-filter", action="store_true",
                    help="Record chunks without screening them (the JSONL keeps "
                         "filtered text either way; this only stops the marking)")
    args = ap.parse_args(argv)

    if not TRANSCRIBE.exists():
        log(f"transcribe_audio.py not found at {TRANSCRIBE}")
        return 1

    env = build_subprocess_env()
    if not env.get("OPENAI_API_KEY"):
        log("OPENAI_API_KEY not set (checked env and ~/.config/neotoma/.env)")
        log("Refusing to start: every chunk would fail. Materialize the secret")
        log("first (see docs/secrets_management.md), then re-run.")
        return 1

    # Fail closed on the interpreter BEFORE anything is written. The old code
    # fell back to sys.executable, which exists and runs but cannot import
    # `config` from a worktree — so the tailer ran for hours writing nothing but
    # errors, indistinguishable at a glance from a tailer sitting through a
    # quiet room.
    python_bin, attempts = resolve_transcriber_python(env)
    if python_bin is None:
        log("no usable Python interpreter for transcribe_audio.py — refusing to start.")
        for attempt in attempts:
            log(f"  tried {attempt}")
        log("Create execution/venv, or point LIVE_TRANSCRIPT_PYTHON at an")
        log("interpreter that can import config/openai/dotenv/requests.")
        return 1
    log(f"transcriber interpreter: {python_bin}")

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

    # Applies to --file too: an explicit system track is just as wrong a source
    # as an auto-detected one, and just as invisible in the output.
    warn_if_not_mic(recording)

    out_path = args.out or recording.with_name(f"{recording.stem}_live.jsonl")

    cursor = args.start_at
    if cursor is None:
        cursor = probe_duration(recording) or 0.0

    log(f"tailing: {recording.name}")
    log(f"chunk interval: {args.interval}s   starting at: {cursor:.0f}s")
    log(f"silence gate: skip below {args.silence_threshold_db:g} dB sustained RMS")
    if args.no_hallucination_filter:
        log("hallucination filter: OFF")
    else:
        log(f"hallucination filter: on (session language "
            f"{args.language or 'unset'}) — caught chunks are MARKED, not dropped")
    log(f"failure kill switch: stop after {args.max_consecutive_failures} "
        f"consecutive failed chunks")
    if args.follow:
        log(f"follow mode: on — pausing (not exiting) on stop, up to "
            f"{args.follow_timeout_min:g} min per break")
    log(f"JSONL: {out_path}")
    print(str(out_path), flush=True)  # stdout: the path, for scripting

    chunk_index = 0
    consecutive_failures = 0
    fatal = False

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
            detected_language: str | None = None
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
                        ok, payload, detected_language = transcribe_slice(
                            tmp_path, env, python_bin,
                        )
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
                    record, ok, payload, consecutive_failures,
                    detected_language=detected_language,
                    expected_language=args.language or None,
                    window_seconds=available,
                    screen=not args.no_hallucination_filter,
                )
                if record.get("filtered"):
                    log(
                        f"chunk {chunk_index}: filtered as "
                        f"{record['filtered']} ({record.get('filtered_detail', '')}) "
                        f"— kept in the JSONL, not surfaced as speech"
                    )

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

            if consecutive_failures >= args.max_consecutive_failures:
                # A run that cannot transcribe anything is not tailing. Say so
                # in the JSONL as well as on stderr, so a supervising session
                # sees it without having to read the log.
                append({
                    "event": "fatal_transcription_failures",
                    "ok": False,
                    "consecutive_failures": consecutive_failures,
                    "last_error": record.get("error"),
                    "t": datetime.now(tz=UTC).isoformat(),
                })
                log(
                    f"{consecutive_failures} consecutive transcription failures "
                    f"— STOPPING. This is a broken transcription path, not a "
                    f"quiet room."
                )
                log(f"last error: {record.get('error')}")
                fatal = True
                break

    except KeyboardInterrupt:
        log("interrupted — stopping")

    log(f"done. {chunk_index} chunk(s) written to {out_path}")
    return 1 if fatal else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
