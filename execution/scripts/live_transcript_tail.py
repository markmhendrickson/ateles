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
# The gate itself now lives in ``silence_gate`` so the batch path
# (``local_whisper.py``) reuses this calibration rather than inventing a second
# threshold. See that module for the measured rationale (95th-percentile windowed
# RMS at -50 dB, calibrated against 39 labelled chunks; median and peak both
# failed). Re-exported here because this module's callers and tests refer to
# these names.
try:
    from scripts.silence_gate import (  # noqa: F401
        DEFAULT_SILENCE_THRESHOLD_DB,
        RMS_PERCENTILE,
        parse_rms_levels,
        sustained_rms_db,
    )
    from scripts.silence_gate import measure_sustained_rms_db as _measure_sustained_rms_db
except ImportError:  # pragma: no cover - path-dependent import
    from silence_gate import (  # type: ignore[no-redef]  # noqa: F401
        DEFAULT_SILENCE_THRESHOLD_DB,
        RMS_PERCENTILE,
        parse_rms_levels,
        sustained_rms_db,
    )
    from silence_gate import (  # type: ignore[no-redef]
        measure_sustained_rms_db as _measure_sustained_rms_db,
    )

# --- Hallucination filter ----------------------------------------------------
# The RMS gate above answers "was there sustained energy in this window" and
# has a structural ceiling: ateles#777 measured a fabrication ("*sad music*")
# at -44.0 dB and another ("Thank you.") at -45.8 dB, both above the -50 dB
# gate, on a session where real quiet speech also lived in that band. No
# threshold value separates the two populations (see the RMS gate section of
# this module and the SKILL doc for the math). ``hallucination_filter`` looks
# at what Whisper actually RETURNED instead of how loud the input was, so it
# is additive to the gate, not a replacement for it — the gate still saves the
# API call on true silence, and this catches what gets past it. Same module
# ``local_whisper.py`` already wires in for the batch/file path; this is the
# streaming path picking up the same defense.
try:
    from scripts.hallucination_filter import screen_transcription
    from scripts.local_whisper import NO_SPEECH_MARKER_PREFIX
except ImportError:  # pragma: no cover - path-dependent import
    from hallucination_filter import screen_transcription  # type: ignore[no-redef]
    from local_whisper import NO_SPEECH_MARKER_PREFIX  # type: ignore[no-redef]

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


def measure_slice_rms_db(wav_path: Path) -> float | None:
    """Sustained RMS (dB) of a slice, or None if the measurement failed.

    None is the caller's signal to transcribe anyway: a broken measurement must
    never silently discard audio. Delegates to the shared gate so the streaming
    and batch paths measure identically.
    """
    return _measure_sustained_rms_db(wav_path, log=log)


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


# The distinct ``skipped`` value for a hallucination-filter suppression, kept
# separate from the RMS gate's "below_threshold" so the two mechanisms are
# measurable apart in the JSONL (ateles#777 acceptance criterion).
FILTERED_HALLUCINATION = "hallucination_filtered"


def apply_hallucination_filter(
    record: dict,
    *,
    expected_language: str | None = "en",
    window_seconds: float | None = None,
) -> bool:
    """Screen a successfully-transcribed record's text; return True if filtered.

    Only called for chunks that ``apply_transcription_result`` already marked
    ``ok`` with non-empty ``text`` — silence and transcription errors are the
    RMS gate's and the failure path's business, not this filter's. A filtered
    chunk keeps the same ``{"silence": true, ...}`` shape the RMS gate uses so
    a consumer reading ``if record.get("silence"): continue`` skips both kinds
    uniformly, but ``skipped`` is set to a DIFFERENT value
    (``FILTERED_HALLUCINATION`` vs. the gate's ``"below_threshold"``) so the two
    mechanisms stay independently countable, per the module docstring in
    ``hallucination_filter.py`` and ateles#777's acceptance criteria.

    The fabricated text is preserved under ``filtered_text`` — never under
    ``text`` — so a false positive stays recoverable by eye, matching
    ``hallucination_filter``'s own "nothing is ever silently dropped" contract.

    A crash inside the filter itself fails OPEN: the chunk is left exactly as
    transcribed rather than raised or dropped, mirroring
    ``measure_slice_rms_db``'s own None-on-failure contract for the RMS gate.
    A broken filter must never take real audio down with it.

    ``window_seconds`` is accepted but DELIBERATELY NOT threaded through to
    ``screen_transcription`` from this module's own caller — see the
    hardcoded ``window_seconds=None`` below. Measured against real audio
    (ateles#777 chunk 6, "E aí E aí", 9 characters of genuine Portuguese
    speech in a 40s window): passing the chunk's true duration makes
    ``too_short_for_window`` fire on it, a false positive. That signal's
    ``MIN_CHARS_PER_LONG_WINDOW=12`` floor was calibrated for batch/memo
    transcription and for windows where nothing was said at all ("P", "you");
    it was never calibrated against this tailer's 30-40s fixed-cadence
    chunks, where a real interjection ("Yes.", "What?") is ordinary and can
    legitimately be under 12 characters. ``local_whisper.py`` already avoids
    this exact trap for the batch path for the same reason ("a short file
    yielding short text is ordinary, not suspicious"); the same restraint
    applies here until this signal is recalibrated against live-tailer
    windows specifically. The parameter stays on the signature so a future
    caller (or a recalibrated default) can opt in without a signature change.
    """
    text = record.get("text")
    if not record.get("ok") or record.get("silence") or not text:
        return False

    # The local whisper-cli backend already runs this SAME filter internally
    # (local_whisper.py's transcribe_local -> screen_transcription) before
    # transcribe_slice() ever sees the result. When it fires there, "text" is
    # not a fresh transcript to screen — it is that batch-path verdict's OWN
    # marker prose (NO_SPEECH_MARKER_PREFIX), describing what got suppressed
    # and why. Re-running screen_transcription on the marker's diagnostic
    # sentence would be screening the filter's own output, not the operator's
    # audio, and the marker's phrasing happens to accidentally trip some
    # signals and not others (observed on ateles#777 chunk 2 and 21: the
    # marker text passed every signal untouched and would otherwise have been
    # written to the JSONL's "text" field as if Whisper had said it). Treat
    # it as an already-filtered chunk directly instead.
    if text.startswith(NO_SPEECH_MARKER_PREFIX):
        record["filtered_text"] = text
        record["text"] = ""
        record["silence"] = True
        record["skipped"] = FILTERED_HALLUCINATION
        record["filtered_reason"] = "local_backend_no_speech_marker"
        return True

    del window_seconds  # accepted for a future recalibration; see docstring
    try:
        verdict = screen_transcription(
            text, expected_language=expected_language, window_seconds=None
        )
    except Exception as exc:  # noqa: BLE001 - fail open, never drop audio
        log(f"hallucination filter failed ({exc!r}) — keeping chunk as transcribed")
        return False
    if not verdict.filtered:
        return False

    record["filtered_text"] = text
    record["text"] = ""
    record["silence"] = True
    record["skipped"] = FILTERED_HALLUCINATION
    record["filtered_reason"] = verdict.reason
    if verdict.detail:
        record["filtered_detail"] = verdict.detail
    return True


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


def transcribe_slice(wav_path: Path, env: dict) -> tuple[bool, str]:
    """Transcribe one slice.

    Returns (ok, payload). On failure the payload is an error string, EXCEPT for
    an empty transcript, which returns SILENCE_SENTINEL so the caller can tell a
    quiet interval apart from a broken transcription path.
    """
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

    text = _extract_transcript_text(result.stdout or "")
    if not text:
        return False, SILENCE_SENTINEL
    return True, text


# transcribe_audio.py's main() always calls transcribe_audio_file(verbose=True)
# (main.py has no non-verbose path), so its stdout carries progress banners
# ahead of the transcript on EVERY call, not only occasionally. On the OpenAI
# backend that was one line ("Transcribing audio file: ..."). The local
# whisper-cli backend that commit 32c99c10 made the default prints several more:
# "TRANSCRIPTION_BACKEND_SELECTED=local", "TRANSCRIPTION_ENGINE=...", and a
# run of indented progress lines from local_whisper.py ("    Local
# whisper-cli: ...", "    Model: ...", "    Converting to 16 kHz mono WAV:
# ...", "    Running whisper-cli on ..."). Stripping only the first banner
# line (as this function did before) leaves the rest glued onto the front of
# the real transcript — corrupting every chunk's text on the now-default
# backend, and specifically corrupting the text the hallucination filter
# judges. Two independent line shapes distinguish a banner from a transcript:
# a bare KEY=VALUE line (all-caps key, no leading space, no spaces around
# "="), and any line with leading whitespace, which is how every progress
# line above is printed and how whisper-cli's own transcript output never is.
_BANNER_LINE_RE = re.compile(r"^[A-Z][A-Z0-9_]*=\S")


def _extract_transcript_text(stdout: str) -> str:
    """Strip transcribe_audio.py's verbose progress banners from its stdout.

    Keeps only lines that are neither indented progress output nor a bare
    KEY=VALUE status line, then joins what remains as the transcript.
    """
    lines = []
    for raw_line in stdout.splitlines():
        if raw_line.startswith("Transcribing audio file:"):
            continue
        if raw_line.startswith("Warning: File extension"):
            continue
        stripped = raw_line.strip()
        if not stripped:
            continue
        if raw_line[0].isspace():
            continue  # indented progress line, e.g. "    Model: ..."
        if _BANNER_LINE_RE.match(stripped):
            continue  # bare KEY=VALUE status line
        lines.append(stripped)
    return " ".join(lines).strip()


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
    ap.add_argument("--expected-language", type=str,
                    default=os.environ.get("LIVE_TRANSCRIPT_EXPECTED_LANGUAGE", "en"),
                    help="Session's spoken language, for the hallucination "
                         "filter's language/script checks (default: en, env "
                         "LIVE_TRANSCRIPT_EXPECTED_LANGUAGE)")
    ap.add_argument("--no-hallucination-filter", action="store_true",
                    default=os.environ.get("LIVE_TRANSCRIPT_NO_HALLUCINATION_FILTER", "") == "1",
                    help="Disable the post-transcription hallucination filter "
                         "(env LIVE_TRANSCRIPT_NO_HALLUCINATION_FILTER=1). The "
                         "RMS silence gate still runs either way.")
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
    if args.no_hallucination_filter:
        log("hallucination filter: OFF (--no-hallucination-filter)")
    else:
        log(f"hallucination filter: on — expected language {args.expected_language!r}")
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
                streak_before = consecutive_failures
                consecutive_failures = apply_transcription_result(
                    record, ok, payload, consecutive_failures
                )
                # Second, independent screen: the RMS gate judged loudness
                # before transcribing; this judges what Whisper actually
                # returned. A chunk it catches must not count toward the
                # failure streak either — it is a normal (if noisy) meeting
                # state, not a broken transcription path. apply_transcription_
                # result already reset the streak to 0 on this chunk's `ok`
                # success (it had no way to know a filter would still reject
                # it), so a filter hit must UNDO that reset and hold the
                # streak at its pre-chunk value — same "neither increments nor
                # resets" contract silence already gets.
                if not args.no_hallucination_filter:
                    if apply_hallucination_filter(
                        record,
                        expected_language=args.expected_language,
                        window_seconds=available,
                    ):
                        consecutive_failures = streak_before

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
