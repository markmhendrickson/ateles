#!/usr/bin/env python3
"""
Piculet — Audio Import & Meeting Recording Daemon

Watches two sources for new audio to process:

1. macOS Voice Memos (Recordings dir) — polls for new files, imports,
   transcribes, and extracts entities via Claude.
2. Meeting recording imports (imports/audio/) — watches for WAV files
   produced by mic-recorder / meeting-recording-control.sh, then reports
   transcription and analysis progress via Telegram.

Named after Piculet, a small woodpecker genus known for its rapid drumming.
Runs as a launchd agent — see com.ateles.piculet.plist.

Clarity gate (ateles#747): a voice memo whose transcript fails a measured
clarity check (lib/transcript_clarity.py) is held rather than processed.
Operator commands, run once and exit (no args starts the watcher):

    python3 watch.py --list-held              List memos currently held.
    python3 watch.py --release '<filename>'   Release a held memo for entity
                                               extraction (exact match, or a
                                               distinctive substring).
    python3 watch.py --discard '<filename>'   Drop a held memo without
                                               processing it.

The exact release command for each held memo is also printed in its hold
notification over the existing Telegram path.
"""

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

# Cloudflare fronts the hosted Neotoma instance and blocks urllib's default
# User-Agent with a 1010 "browser signature" 403. Any explicit UA passes.
NEOTOMA_USER_AGENT = "ateles-neotoma-sync/1.0"

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

RECORDINGS_DIR = (
    Path.home()
    / "Library"
    / "Group Containers"
    / "group.com.apple.VoiceMemos.shared"
    / "Recordings"
)


# Resolved at runtime from DATA_DIR env or the iCloud default path.
def _audio_imports_dir() -> Path:
    data_dir = os.environ.get("DATA_DIR")
    if data_dir:
        return Path(data_dir) / "imports" / "audio"
    return (
        Path.home()
        / "Library"
        / "Mobile Documents"
        / "com~apple~CloudDocs"
        / "Documents"
        / "data"
        / "imports"
        / "audio"
    )


# Formats Voice Memos and the import path actually produce in
# RECORDINGS_DIR (ateles#748: .qta was silently skipped everywhere this set
# wasn't consulted — the imports scan, the clarity-gate duration lookup in
# assess_memo, etc.). Single source of truth: every audio-file enumeration or
# suffix check in this module must consult this constant, never repeat the
# literal, so the two cannot diverge again.
AUDIO_EXTENSIONS = {".m4a", ".qta"}
# Meeting recordings from mic-recorder are WAV files, written to a different
# directory (imports/audio/) that also holds archived .m4a/.qta copies of
# already-processed Voice Memos — MEETING_AUDIO_EXTENSIONS stays WAV-only so
# those archived copies are never misreported as meeting recordings.
MEETING_AUDIO_EXTENSIONS = {".wav"}

POLL_INTERVAL_SECONDS = 60  # check every minute; launchd keeps it alive

# Pattern for timestamped import filenames produced by import_audio_from_desktop.py:
# e.g. "20251226_175827_voicememo_20250528 214608-1C87EB64.m4a"
# The original recording name follows the "_voicememo_" prefix.
_IMPORTED_NAME_RE = re.compile(r"^\d{8}_\d{6}_voicememo_(.+)$")

PROJECT_ROOT = Path(__file__).parent.parent.parent.parent  # ateles repo root
IMPORT_SCRIPT = PROJECT_ROOT / "execution" / "scripts" / "import_audio_from_desktop.py"

LOG_DIR = Path.home() / "Library" / "Logs" / "ateles"
LOG_FILE = LOG_DIR / "piculet.log"

# Local state files — track filenames already processed.
STATE_FILE = Path(__file__).parent / "seen_files.json"
MEETING_STATE_FILE = Path(__file__).parent / "seen_meeting_files.json"
# Memos held by the clarity gate, awaiting operator confirmation.
# A held memo is NOT in the seen-set, so it is never silently dropped: it stays
# listed here, and is re-surfaced to the operator on the reminder interval
# until it is explicitly released or discarded.
HELD_STATE_FILE = Path(__file__).parent / "held_memos.json"

# Re-surface the held-memo backlog on this interval so an unconfirmed memo
# cannot fall silently out of view. Matches the operator-alert reminder cadence.
_HELD_REMINDER_INTERVAL = 6 * 3600  # 6 hours

# The command the operator pastes to release a held memo.
RELEASE_COMMAND = "python3 execution/daemons/piculet/watch.py --release"

# ---------------------------------------------------------------------------
# Env bootstrap — runs at import time before anything else
# ---------------------------------------------------------------------------

# 1. Load ~/.config/neotoma/.env so launchd picks up all Neotoma vars.
#    Use os.environ[] (not setdefault) so daemon restarts always pick up a
#    refreshed token written by the 1Password sync below.
_NEOTOMA_ENV_FILE = Path.home() / ".config" / "neotoma" / ".env"
if _NEOTOMA_ENV_FILE.exists():
    for _line in _NEOTOMA_ENV_FILE.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ[_k.strip()] = _v.strip().strip('"').strip("'")

# 2. Refresh NEOTOMA_BEARER_TOKEN — prefer an OFFLINE SOPS decrypt (no live
#    1Password session needed), falling back to live `op read` during migration.
#    The canonical value lives in 1Password; the age-encrypted snapshot lives in
#    the PRIVATE ateles-private repo (ateles is public), decryptable with the
#    machine-local age key.
_OP_REF = "op://Private/Neotoma local bearer token/bearer_token"
_SOPS_CANDIDATES = [
    Path.home() / "repos" / "ateles-private" / "secrets" / "neotoma.sops.enc",
    Path.home() / ".config" / "neotoma" / "secrets" / "neotoma.sops.enc",
]


def _persist_bearer_token(_token: str) -> None:
    """Set the token in-process and write it back to .env (best-effort)."""
    if not _token:
        return
    os.environ["NEOTOMA_BEARER_TOKEN"] = _token
    if _NEOTOMA_ENV_FILE.exists():
        import re as _re
        _env_text = _NEOTOMA_ENV_FILE.read_text()
        _new_line = f'NEOTOMA_BEARER_TOKEN="{_token}"'
        if "NEOTOMA_BEARER_TOKEN" in _env_text:
            _env_text = _re.sub(
                r'^NEOTOMA_BEARER_TOKEN=.*$', _new_line,
                _env_text, flags=_re.MULTILINE,
            )
        else:
            _env_text += f"\n{_new_line}\n"
        _NEOTOMA_ENV_FILE.write_text(_env_text)


_bearer = ""
# sops' default age-key path is OS-specific; point it at the standard location
# explicitly so launchd daemons (minimal env) can decrypt.
_AGE_KEY_FILE = Path.home() / ".config" / "sops" / "age" / "keys.txt"
if (not os.environ.get("SOPS_AGE_KEY_FILE") and not os.environ.get("SOPS_AGE_KEY")
        and _AGE_KEY_FILE.exists()):
    os.environ["SOPS_AGE_KEY_FILE"] = str(_AGE_KEY_FILE)
# (a) Offline: decrypt the SOPS snapshot with the local age key.
for _snap in _SOPS_CANDIDATES:
    if not _snap.exists():
        continue
    try:
        _r = subprocess.run(
            ["sops", "--decrypt", "--input-type", "dotenv",
             "--output-type", "dotenv", str(_snap)],
            capture_output=True, text=True, timeout=15,
        )
        if _r.returncode == 0:
            for _l in _r.stdout.splitlines():
                _l = _l.strip()
                if _l.startswith("NEOTOMA_BEARER_TOKEN="):
                    _bearer = _l.partition("=")[2].strip().strip('"').strip("'")
                    break
        if _bearer:
            break
    except Exception:
        pass  # sops/age not available — fall through to op read
# (b) Fallback: live `op read` (requires an active 1Password session).
if not _bearer:
    try:
        _r = subprocess.run(
            ["op", "read", _OP_REF],
            capture_output=True, text=True, timeout=10,
        )
        if _r.returncode == 0:
            _bearer = _r.stdout.strip()
    except Exception:
        pass  # op not available or session expired — keep existing token
_persist_bearer_token(_bearer)

# ---------------------------------------------------------------------------
# lib/notify integration
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from lib.daemon_runtime.logging_setup import configure_daemon_logging  # noqa: E402
from lib.transcript_clarity import assess_transcript  # noqa: E402

try:
    from lib.notify import Notifier  # noqa: E402

    _lib_notifier: "Notifier | None" = Notifier.from_neotoma()
except Exception:
    _lib_notifier = None


def _notify_lib(message: str, priority: str = "info") -> None:
    """Forward a notification through lib/notify if available (best-effort)."""
    if _lib_notifier is None:
        return
    try:
        from lib.notify import Priority

        p = getattr(Priority, priority.upper(), Priority.INFO)
        _lib_notifier.send(message, priority=p, handler="piculet")
    except Exception:
        pass


# Activity-log channel (CyphorhinusBot observation feed).
try:
    from lib.activity import ActivityLogger  # noqa: E402

    _activity: "ActivityLogger | None" = ActivityLogger(agent="piculet")
except Exception:
    _activity = None


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOG_DIR.mkdir(parents=True, exist_ok=True)


# Rotating + repeat-suppressing (lib/daemon_runtime/logging_setup.py):
# unbounded retry logging filled a 926 GB disk on 2026-08-18.
log = configure_daemon_logging("piculet")


# Dedup state for operator alerts — avoids repeat messages for persistent errors.
# Maps alert text → (first_sent_time, send_count). Cleared when the error resolves.
# Shared across every channel (Telegram + lib/notify) so they agree on which
# occurrences are "new" vs "suppressed" for the same underlying condition.
_telegram_alert_state: dict[str, tuple[float, int]] = {}
# Re-notify for the same persistent error after this many seconds (hourly reminder).
_TELEGRAM_REPEAT_INTERVAL = 3600


def _telegram(message: str) -> None:
    """Send a Telegram message, trying the shared lib first then falling back to
    telegram-send (best-effort, no raise)."""
    import shutil

    # Try the shared Node.js lib first.
    node = shutil.which("node")
    send_script = PROJECT_ROOT / "execution" / "lib" / "telegram" / "send.mjs"
    if node and send_script.exists():
        try:
            args = [node, str(send_script), "--text", message]
            thread_id = os.environ.get("TELEGRAM_TOPIC_PICULET", "").strip()
            if thread_id:
                args += ["--thread-id", thread_id]
            subprocess.run(
                args,
                timeout=10,
                capture_output=True,
                env=os.environ,
            )
            return
        except Exception:
            pass  # fall through to telegram-send

    # Fallback: telegram-send CLI.
    telegram = shutil.which("telegram-send")
    if not telegram:
        return
    try:
        subprocess.run(
            [telegram, message],
            timeout=10,
            capture_output=True,
            env=os.environ,
        )
    except Exception:
        pass


def _alert_is_due(key: str) -> bool:
    """
    Return True if an alert for `key` should fire now: first sighting, or
    the persistent-condition reminder interval has elapsed. Mutates
    _telegram_alert_state (records send) as a side effect on True.

    Shared rate-limit gate for every operator channel (Telegram + lib/notify)
    so a single evaluation per condition decides both channels' fate — the
    email path used to bypass this gate entirely and fire on every failed
    poll, emailing the operator once per poll interval indefinitely.
    """
    now = time.monotonic()
    state = _telegram_alert_state.get(key)
    if state is None:
        _telegram_alert_state[key] = (now, 1)
        return True
    first_sent, count = state
    if now - first_sent >= _TELEGRAM_REPEAT_INTERVAL * count:
        _telegram_alert_state[key] = (first_sent, count + 1)
        return True
    return False


def _telegram_send_due(key: str, text: str) -> bool:
    """
    Send `text` over Telegram if `key` is due per _alert_is_due, appending the
    "still ongoing" reminder suffix when this is a repeat (not first) send.
    Returns whether the alert was due (i.e. whether anything was sent).
    """
    if not _alert_is_due(key):
        return False
    first_sent, count = _telegram_alert_state[key]
    if count == 1:
        _telegram(text)
    else:
        elapsed = time.monotonic() - first_sent
        _telegram(f"{text} (still ongoing, {int(elapsed / 60)}m)")
    return True


def _telegram_deduped(message: str) -> None:
    """
    Send a Telegram alert only if the message is new or hasn't been sent
    recently. Suppresses repeated identical alerts within _TELEGRAM_REPEAT_INTERVAL,
    but sends a reminder when the interval lapses so persistent issues stay visible.
    Call _telegram_clear(message) when the condition resolves.
    """
    _telegram_send_due(message, message)


def _telegram_clear(message: str) -> None:
    """Mark an alert as resolved so it fires fresh if it recurs."""
    _telegram_alert_state.pop(message, None)


def log_error(message: str) -> None:
    """Log at ERROR level and alert the operator, deduplicated per channel.

    A single _alert_is_due(message) evaluation (via _telegram_send_due) drives
    both the Telegram send and the lib/notify (email) send, so they agree on
    new-vs-suppressed for the same underlying condition.
    """
    log.error(message)
    if _telegram_send_due(message, f"🔴 [piculet] ERROR: {message}"):
        _notify_lib(f"piculet error: {message}", priority="blocker")


def log_warning(message: str) -> None:
    """Log at WARNING level and alert the operator, deduplicated per channel.

    A single _alert_is_due(message) evaluation (via _telegram_send_due) drives
    both the Telegram send and the lib/notify (email) send, so they agree on
    new-vs-suppressed for the same underlying condition.
    """
    log.warning(message)
    if _telegram_send_due(message, f"🟡 [piculet] WARNING: {message}"):
        _notify_lib(f"piculet warning: {message}", priority="info")


# ---------------------------------------------------------------------------
# Local state — tracks seen filenames so polls are O(1) filesystem ops
# ---------------------------------------------------------------------------


def original_recording_name(imported_name: str) -> str:
    """
    Convert an imported filename back to the original recording filename.

    import_audio_from_desktop.py renames files like:
      "20251226_175827_voicememo_20250528 214608-1C87EB64.m4a"
    The original Recordings-dir filename is the part after "_voicememo_".
    If the name doesn't match the pattern (e.g. a Desktop import), return as-is.
    """
    m = _IMPORTED_NAME_RE.match(imported_name)
    return m.group(1) if m else imported_name


def load_seen() -> set[str]:
    """Load the set of already-processed filenames from local state."""
    if STATE_FILE.exists():
        try:
            return set(json.loads(STATE_FILE.read_text()))
        except Exception:
            pass
    return set()


def save_seen(seen: set[str]) -> None:
    STATE_FILE.write_text(json.dumps(sorted(seen), indent=2))


def load_seen_meetings() -> set[str]:
    if MEETING_STATE_FILE.exists():
        try:
            return set(json.loads(MEETING_STATE_FILE.read_text()))
        except Exception:
            pass
    return set()


def save_seen_meetings(seen: set[str]) -> None:
    MEETING_STATE_FILE.write_text(json.dumps(sorted(seen), indent=2))


def find_new_meeting_recordings(seen: set[str]) -> list[Path]:
    """Return WAV files in the audio imports dir not yet reported."""
    imports_dir = _audio_imports_dir()
    if not imports_dir.exists():
        return []
    return sorted(
        p
        for p in imports_dir.iterdir()
        if p.is_file()
        and p.suffix.lower() in MEETING_AUDIO_EXTENSIONS
        and p.name not in seen
    )


def partition_meeting_recordings(
    candidates: list[Path], acknowledged: set[str]
) -> tuple[list[Path], list[Path]]:
    """
    Split not-yet-reported recordings into (ready, pending).

    A recording is *ready* once its transcript sidecar exists; only then is
    there anything to report. Everything else is *pending* — waiting on a
    transcript that may never arrive.

    This distinction is why the watermark never advanced (ateles#421): the old
    loop logged every unreported WAV as "new" on every poll, but only ever
    added the ready ones to the seen-set. The 88 recordings with no transcript
    were therefore re-found and re-announced once a minute forever. Pending
    files are still re-examined each poll (a transcript can land later), but
    they are counted and logged as pending, never as new.
    """
    ready: list[Path] = []
    pending: list[Path] = []
    for rec in candidates:
        if (rec.parent / f"{rec.stem}.txt").exists():
            ready.append(rec)
        else:
            pending.append(rec)
    return ready, pending


def report_meeting_recording(recording: Path) -> None:
    """
    Check whether transcription and analysis for a meeting recording are
    complete (by looking for sidecar files), and send Telegram updates.

    meeting-recording-control.sh produces:
      <stem>.wav          — the raw audio
      <stem>.txt          — transcript sidecar (written by transcribe_audio.py)
      <stem>_meeting_analysis.md  — analysis report (written by /analyze-meeting)
    """
    stem = recording.stem
    parent = recording.parent

    transcript = parent / f"{stem}.txt"
    analysis = parent / f"{stem}_meeting_analysis.md"

    if transcript.exists() and analysis.exists():
        notify(
            "Meeting recording",
            f"✅ Transcription + analysis complete: {recording.name}",
        )
    elif transcript.exists():
        notify(
            "Meeting recording",
            f"📝 Transcription complete, analysis pending: {recording.name}",
        )
    else:
        # Not ready yet — leave it in the unseen set so we check again next poll.
        return

    # Mark as reported regardless of whether analysis is done — we won't
    # re-report the transcription step once the transcript exists.
    log.info(f"Meeting recording reported: {recording.name}")


class NeotomaUnavailableError(Exception):
    """Raised when Neotoma is not usable (server down, auth failure, etc.)."""


# ---------------------------------------------------------------------------
# Neotoma HTTP helpers — direct API calls, no CLI subprocess
# ---------------------------------------------------------------------------

# Base URL resolved from env at startup. The plist also sets NEOTOMA_BASE_URL
# so this will always resolve to prod (3180), never auto-detect dev (3080).
_NEOTOMA_BASE_URL: str = os.environ.get("NEOTOMA_BASE_URL", "")

# Canonical "Neotoma, Inc." company entity, referenced in the extraction prompt
# so product mentions get related to it. Env-overridable so a different graph /
# fork can point at its own company entity instead of a baked-in literal.
NEOTOMA_COMPANY_ENTITY_ID: str = os.environ.get(
    "NEOTOMA_COMPANY_ENTITY_ID", "ent_44835c5b0047ce26ffbe40bc"
)


def _neotoma_query(
    entity_type: str,
    limit: int = 1,
    offset: int = 0,
    timeout: float = 15.0,
) -> dict:
    """
    Call POST /entities/query on the Neotoma HTTP API.

    Returns the parsed JSON response dict (keys: entities, total, limit, offset).
    Raises NeotomaUnavailableError on connection failure, timeout, or non-2xx status.
    """
    import urllib.error
    import urllib.request

    token = os.environ.get("NEOTOMA_BEARER_TOKEN", "")
    if not token:
        raise NeotomaUnavailableError("NEOTOMA_BEARER_TOKEN not set")

    url = f"{_NEOTOMA_BASE_URL.rstrip('/')}/entities/query"
    body_bytes = json.dumps(
        {"entity_type": entity_type, "limit": limit, "offset": offset}
    ).encode()

    req = urllib.request.Request(
        url,
        data=body_bytes,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    req.add_header("User-Agent", NEOTOMA_USER_AGENT)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode()[:300]
        if exc.code == 401:
            raise NeotomaUnavailableError(
                "Neotoma auth rejected (401) — bearer token may need rotation in 1Password"
            )
        raise NeotomaUnavailableError(f"Neotoma HTTP {exc.code}: {body}")
    except urllib.error.URLError as exc:
        raise NeotomaUnavailableError(
            f"Neotoma server not reachable at {_NEOTOMA_BASE_URL}: {exc.reason}"
        )
    except TimeoutError:
        raise NeotomaUnavailableError(f"Neotoma request timed out ({timeout}s)")

    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise NeotomaUnavailableError(
            f"Neotoma returned unparseable response: {body[:200]}"
        ) from exc

    if isinstance(data, dict) and "error" in data:
        err_msg = data["error"]
        if "401" in str(err_msg) or "unauthorized" in str(err_msg).lower():
            raise NeotomaUnavailableError(
                "Neotoma auth rejected (401) — bearer token may need rotation in 1Password"
            )
        raise NeotomaUnavailableError(f"Neotoma API error: {err_msg}")

    return data


def check_neotoma() -> None:
    """
    Verify that Neotoma is ready: server reachable and auth valid.
    Raises NeotomaUnavailableError with a descriptive message on any failure.
    Call this before any operation that depends on Neotoma.
    """
    _neotoma_query("transcription", limit=1)


def hydrate_seen_from_neotoma() -> set[str]:
    """
    On first run (empty state file), query Neotoma once to build the initial
    seen-set from existing transcription entities. This avoids re-importing
    everything on first launch.

    Assumes check_neotoma() has already passed.
    """
    seen: set[str] = set()

    log.info("Hydrating seen-set from Neotoma (first run)...")
    offset = 0
    limit = 200
    while True:
        data = _neotoma_query("transcription", limit=limit, offset=offset, timeout=30.0)
        entities = data.get("entities") or data.get("results") or []
        if not entities:
            break
        for e in entities:
            snap = e.get("snapshot") or {}
            for field in ("original_source_file", "audio_file_name"):
                val = snap.get(field)
                if val:
                    # Store as the original recording filename so it
                    # matches what find_new_files() sees in Recordings/.
                    seen.add(original_recording_name(Path(val).name))
        if len(entities) < limit:
            break
        offset += limit

    log.info(f"Hydrated {len(seen)} filenames from Neotoma.")
    return seen


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def find_new_files(seen: set[str]) -> list[Path]:
    """Return audio files in Recordings dir not present in the seen-set."""
    if not RECORDINGS_DIR.exists():
        return []
    return sorted(
        p
        for p in RECORDINGS_DIR.iterdir()
        if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS and p.name not in seen
    )


def run_import() -> None:
    """Invoke the import script, which handles dedup, transcription, and Neotoma storage."""
    log.info("New Voice Memos detected — running import pipeline...")
    try:
        # No --analyze/--no-analyze flag here: import_audio_from_desktop.py
        # analyses by default and only declares --no-analyze as an opt-out.
        # --analyze was never a declared argument, so passing it made
        # argparse exit 2 on every run (ateles#747). Do not restore it.
        result = subprocess.run(
            [
                sys.executable,
                str(IMPORT_SCRIPT),
                "--source",
                str(RECORDINGS_DIR),
            ],
            timeout=7200,  # 2 hours max
            env=os.environ,
        )
        if result.returncode == 0:
            log.info("Import pipeline completed successfully.")
        else:
            log_warning(f"Import pipeline exited with code {result.returncode}.")
    except subprocess.TimeoutExpired:
        log_error("Import pipeline timed out after 2 hours.")
    except Exception as e:
        log_error(f"Import pipeline error: {e}")


# ---------------------------------------------------------------------------
# Clarity gate — process automatically when clear, pause for confirmation
# when not. See lib/transcript_clarity.py for the checks and their calibration.
# ---------------------------------------------------------------------------


def load_held() -> dict:
    """Load the held-memo table: memo filename → why it was held."""
    if HELD_STATE_FILE.exists():
        try:
            return json.loads(HELD_STATE_FILE.read_text())
        except Exception as e:
            log.warning(f"Could not read held state ({e}) — starting empty.")
    return {}


def save_held(held: dict) -> None:
    HELD_STATE_FILE.write_text(json.dumps(held, indent=2, sort_keys=True))


def _audio_duration_seconds(audio_path: Path) -> float | None:
    """Best-effort duration via ffprobe. None when it cannot be determined."""
    import shutil

    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    try:
        result = subprocess.run(
            [
                ffprobe, "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", str(audio_path),
            ],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            return float(result.stdout.strip())
    except Exception:
        pass
    return None


def find_transcript_for(memo_name: str) -> Path | None:
    """
    Locate the transcript sidecar for an imported memo.

    import_audio_from_desktop.py renames files to
    ``<timestamp>_voicememo_<original name>`` in the imports dir and writes a
    ``.txt`` beside them, so we match on the original name suffix.
    """
    imports_dir = _audio_imports_dir()
    if not imports_dir.exists():
        return None
    stem = Path(memo_name).stem
    candidates = sorted(
        p
        for p in imports_dir.glob("*.txt")
        if p.stem.endswith(stem) or stem in p.stem
    )
    return candidates[-1] if candidates else None


def assess_memo(memo: Path) -> tuple[object | None, Path | None]:
    """
    Measure a freshly imported memo's transcript.

    Returns (report, transcript_path). A None report means we could not find a
    transcript to judge — the caller treats that as "hold", never as "clear",
    because an unmeasured memo has not been shown to be safe to process.
    """
    transcript = find_transcript_for(memo.name)
    if transcript is None:
        return None, None
    try:
        text = transcript.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        log.warning(f"Could not read transcript {transcript.name}: {e}")
        return None, transcript

    # The transcript sidecar shares a stem with its source audio file, but the
    # extension can be any format Voice Memos or the import path produces
    # (ateles#748 missed .qta here even after AUDIO_EXTENSIONS gained it,
    # because this lookup carried its own hardcoded ".m4a" fallback instead of
    # using the shared constant — duration silently came back None for every
    # .qta memo, degrading the clarity gate's measurement without an error).
    audio = None
    for ext in (".wav", *sorted(AUDIO_EXTENSIONS)):
        candidate = transcript.with_suffix(ext)
        if candidate.exists():
            audio = candidate
            break
    duration = _audio_duration_seconds(audio) if audio is not None else None
    return assess_transcript(text, duration_seconds=duration), transcript


def format_hold_notification(memo_name: str, report, transcript: Path | None) -> str:
    """
    Build the operator's message for a held memo: which memo, which check
    failed and why, the flagged span (not the whole transcript), and the exact
    command to release it.
    """
    lines = [
        f"⏸ Held for your confirmation: {memo_name}",
        "",
        "This memo was NOT processed — the transcription may not be reliable.",
        "",
    ]
    for f in report.findings:
        lines.append(f"• Check {f.check} ({f.name}): {f.reason}")
        if f.excerpt:
            lines.append(f"    flagged text: “{f.excerpt.strip()}”")
    m = report.metrics
    measured = f"Measured: {m.get('words')} words"
    if m.get("duration_seconds"):
        measured += f", {m['duration_seconds']:.0f}s audio"
    if m.get("words_per_second"):
        measured += f", {m['words_per_second']} words/sec"
    lines += ["", measured]
    if transcript:
        lines.append(f"Transcript: {transcript}")
    lines += [
        "",
        "To process it anyway (transcript is good enough):",
        f"  {RELEASE_COMMAND} {memo_name!r}",
        "",
        "Or correct the transcript file above, then run the same command.",
        f"Held memos are re-listed every {int(_HELD_REMINDER_INTERVAL / 3600)}h "
        "until released or discarded.",
    ]
    return "\n".join(lines)


def notify(title: str, message: str) -> None:
    """Send a Telegram message (best-effort; logs on failure)."""
    import shutil

    full_message = f"[{title}] {message}"

    # Try the shared Node.js lib first.
    node = shutil.which("node")
    send_script = PROJECT_ROOT / "execution" / "lib" / "telegram" / "send.mjs"
    if node and send_script.exists():
        try:
            args = [node, str(send_script), "--text", full_message]
            thread_id = os.environ.get("TELEGRAM_TOPIC_PICULET", "").strip()
            if thread_id:
                args += ["--thread-id", thread_id]
            result = subprocess.run(
                args,
                timeout=10,
                capture_output=True,
                text=True,
                env=os.environ,
            )
            if result.returncode != 0:
                log.warning(f"send.mjs failed: {result.stderr.strip()[:200]}")
            return
        except Exception as e:
            log.warning(
                f"send.mjs notification failed: {e}, falling back to telegram-send"
            )

    # Fallback: telegram-send CLI.
    telegram = shutil.which("telegram-send")
    if not telegram:
        log.warning("telegram-send not found in PATH — notification skipped")
        return
    try:
        result = subprocess.run(
            [telegram, full_message],
            timeout=10,
            capture_output=True,
            text=True,
            env=os.environ,
        )
        if result.returncode != 0:
            log.warning(f"telegram-send failed: {result.stderr.strip()[:200]}")
    except Exception as e:
        log.warning(f"Telegram notification failed: {e}")


def run_entity_extraction(new_files: list[Path]) -> str:
    """
    Invoke a Claude agent to extract entities and relationships from the
    newly imported transcriptions and store them in Neotoma.

    Returns a human-readable summary of extracted entities for use in
    notifications (e.g. "buy milk (task), Rebecca (person)").

    Raises NeotomaUnavailableError if the claude CLI is not found (entity
    extraction requires it to interact with Neotoma).
    """
    import shutil

    claude = shutil.which("claude")
    if not claude:
        raise NeotomaUnavailableError(
            "claude CLI not found in PATH — cannot run entity extraction"
        )

    filenames = "\n".join(f"  - {f.name}" for f in new_files)
    prompt = f"""You are running the post-transcription steps of the import-audio skill for newly imported Voice Memos.

The following files were just imported and transcribed into Neotoma:
{filenames}

For each of these transcriptions, perform the following steps using the Neotoma MCP tools (always use mcp__mcpsrv_neotoma__* prod instance):

1. Find the transcription entity in Neotoma by searching for the filename via retrieve_entities or retrieve_entity_by_identifier.

2. Extract entities from the transcription text:
   - People (type: person)
   - Feedback about products/features (type: feedback)
   - Actionable to-dos (type: task, status: open)
   - Decisions (type: decision)
   - Named places (type: place)
   - Topics/themes (type: topic)
   Search Neotoma first before creating — update existing entities rather than duplicating.

3. Relate each transcription to every entity it produced/updated via REFERS_TO (predicate: mentions).

4. Relate all created/updated entities to relevant existing Neotoma entities:
   - Anything about Neotoma the product: relate to {NEOTOMA_COMPANY_ENTITY_ID} (Neotoma, Inc.)
   - People to companies they work at, topics to related plans, etc.
   - Be thorough — check for any existing entities that are clearly connected.

5. Detect continuations: if multiple files were recorded within ~5 minutes of each other, or a transcript begins mid-thought or references a prior memo, create a REFERS_TO (predicate: continues) from earlier to later transcription.

Skip transcriptions whose text is only noise (e.g. "[background noise]", "[clears throat]") — no entity extraction needed, but still check for continuation relationships.

Work through all files, then stop.

After completing all files, output a final summary line in exactly this format (no other text after it):
ENTITY_SUMMARY: <comma-separated list of "name (type)" for every entity created or updated, e.g. "buy milk (task), Rebecca (person), Neotoma sync (topic)">
If no entities were extracted, output: ENTITY_SUMMARY: none
"""

    log.info(f"Running entity extraction for {len(new_files)} file(s)...")
    try:
        result = subprocess.run(
            [claude, "--print", "--dangerously-skip-permissions", prompt],
            timeout=3600,
            env=os.environ,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            log.info("Entity extraction completed successfully.")
            # Parse the structured summary line from Claude's output
            for line in reversed(result.stdout.splitlines()):
                if line.startswith("ENTITY_SUMMARY:"):
                    summary = line.removeprefix("ENTITY_SUMMARY:").strip()
                    log.info(f"Entities extracted: {summary}")
                    return summary
            return ""
        else:
            log_warning(
                f"Entity extraction exited with code {result.returncode}: {result.stderr[:500]}"
            )
            return ""
    except subprocess.TimeoutExpired:
        log_error("Entity extraction timed out after 1 hour.")
        return ""
    except Exception as e:
        log_error(f"Entity extraction error: {e}")
        return ""


def main() -> None:
    _notify_lib(
        f"piculet started — polling every {POLL_INTERVAL_SECONDS}s", priority="info"
    )
    log.info(f"Watcher started. Polling every {POLL_INTERVAL_SECONDS}s.")
    log.info(f"Watching Voice Memos: {RECORDINGS_DIR}")
    log.info(f"Watching meeting recordings: {_audio_imports_dir()}")

    # Build initial seen-set: load from state file, or hydrate from Neotoma
    # on first ever run. Block until Neotoma is usable before proceeding.
    # Grace period: suppress Telegram alerts for the first 2 startup failures
    # so transient races (daemon starts while server is restarting) are silent.
    seen = load_seen()
    if not seen:
        _startup_failures = 0
        _STARTUP_GRACE_ATTEMPTS = 2
        _startup_alerted = False
        _startup_down_since: float | None = None
        while True:
            try:
                check_neotoma()
                seen = hydrate_seen_from_neotoma()
                save_seen(seen)
                if _startup_alerted and _startup_down_since is not None:
                    elapsed = int(time.monotonic() - _startup_down_since)
                    mins, secs = divmod(elapsed, 60)
                    duration = f"{mins}m {secs}s" if mins else f"{secs}s"
                    log.info(f"Neotoma available at startup after {duration}.")
                    _telegram(f"✅ [piculet] Neotoma available — startup resumed after {duration}")
                break
            except NeotomaUnavailableError as exc:
                if _startup_failures == 0:
                    _startup_down_since = time.monotonic()
                _startup_failures += 1
                if _startup_failures <= _STARTUP_GRACE_ATTEMPTS:
                    log.warning(
                        f"Neotoma unavailable at startup (attempt {_startup_failures}/"
                        f"{_STARTUP_GRACE_ATTEMPTS} grace period — Telegram suppressed): {exc}"
                    )
                else:
                    _startup_alerted = True
                    log_error(
                        f"Neotoma unavailable at startup — will retry in {POLL_INTERVAL_SECONDS}s: {exc}"
                    )
                time.sleep(POLL_INTERVAL_SECONDS)

    seen_meetings = load_seen_meetings()
    # Size of the awaiting-transcript backlog as last logged, so the daemon
    # reports the backlog when it *changes* instead of every poll.
    pending_backlog_size = -1

    # Memos held by the clarity gate, awaiting operator confirmation.
    held = load_held()
    _held_reminded_at = time.monotonic()
    if held:
        log.info(f"{len(held)} memo(s) held awaiting confirmation at startup.")

    # Track consecutive poll failures to apply the same grace period in the
    # main loop (e.g. after a daemon restart mid-session).
    _consecutive_neotoma_failures = 0
    _POLL_GRACE_ATTEMPTS = 2  # silent retries before Telegram fires
    _neotoma_alerted = False   # True once Telegram fired for this outage
    _neotoma_down_since: float | None = None  # monotonic time of first failure

    while True:
        try:
            # Guard: verify Neotoma is reachable before doing any work.
            try:
                check_neotoma()

                # --- Recovery notification ---
                if _neotoma_alerted and _neotoma_down_since is not None:
                    elapsed = int(time.monotonic() - _neotoma_down_since)
                    mins, secs = divmod(elapsed, 60)
                    duration = f"{mins}m {secs}s" if mins else f"{secs}s"
                    log.info(f"Neotoma back online after {duration}.")
                    _telegram(f"✅ [piculet] Neotoma back online (was down {duration})")

                # Reset failure tracking on success
                _consecutive_neotoma_failures = 0
                _neotoma_alerted = False
                _neotoma_down_since = None

            except NeotomaUnavailableError as exc:
                if _consecutive_neotoma_failures == 0:
                    _neotoma_down_since = time.monotonic()
                _consecutive_neotoma_failures += 1
                if _consecutive_neotoma_failures <= _POLL_GRACE_ATTEMPTS:
                    log.warning(
                        f"Neotoma unavailable — skipping poll (attempt "
                        f"{_consecutive_neotoma_failures}/{_POLL_GRACE_ATTEMPTS} "
                        f"grace, Telegram suppressed): {exc}"
                    )
                else:
                    _neotoma_alerted = True
                    log_error(f"Neotoma unavailable — skipping poll: {exc}")
                time.sleep(POLL_INTERVAL_SECONDS)
                continue

            for _key in list(_telegram_alert_state):
                if "Neotoma unavailable" in _key:
                    _telegram_clear(_key)

            # --- Voice Memos ---
            new_files = find_new_files(seen)
            if new_files:
                n = len(new_files)
                log.info(f"Found {n} new Voice Memo(s): {[f.name for f in new_files]}")
                notify(
                    "Voice Memos",
                    f"Importing {n} new memo{'s' if n != 1 else ''}…",
                )
                _job = _activity.started(f"importing {n} new voice memo{'s' if n != 1 else ''}") if _activity else None
                try:
                    run_import()

                    # --- Clarity gate ---------------------------------------
                    # Transcription has run; decide per memo whether the
                    # transcript is measurably good enough to process without
                    # the operator. Unclear memos are held, not processed and
                    # not dropped.
                    clear_files: list[Path] = []
                    for memo in new_files:
                        report, transcript = assess_memo(memo)
                        if report is None:
                            held[memo.name] = {
                                "reason": "no transcript found to assess",
                                "checks": [],
                                "held_at": time.time(),
                                "transcript": str(transcript) if transcript else None,
                            }
                            log_warning(
                                f"Held {memo.name}: no transcript found to assess."
                            )
                            continue
                        if report.clear:
                            clear_files.append(memo)
                            continue
                        held[memo.name] = {
                            "reason": report.summary,
                            "checks": [f.check for f in report.findings],
                            "metrics": report.metrics,
                            "held_at": time.time(),
                            "transcript": str(transcript) if transcript else None,
                        }
                        log.info(f"Held {memo.name} for confirmation: {report.summary}")
                        notify(
                            "Voice Memos",
                            format_hold_notification(memo.name, report, transcript),
                        )
                    if held:
                        save_held(held)

                    if clear_files:
                        c = len(clear_files)
                        notify(
                            "Voice Memos",
                            f"Transcription complete for {c} memo{'s' if c != 1 else ''}. Extracting entities…",
                        )
                        entity_summary = run_entity_extraction(clear_files)
                        done_msg = (
                            f"Done — {c} memo{'s' if c != 1 else ''} imported & transcribed."
                        )
                        if entity_summary and entity_summary != "none":
                            done_msg += f"\nEntities: {entity_summary}"
                        if held:
                            done_msg += (
                                f"\n{len(held)} memo(s) held for your confirmation."
                            )
                        notify("Voice Memos", done_msg)
                    else:
                        entity_summary = ""
                        log.info(
                            "No memos passed the clarity gate this cycle — "
                            "nothing extracted."
                        )
                    if _job:
                        _summary = (
                            f"{len(clear_files)} memo(s) processed, "
                            f"{len(new_files) - len(clear_files)} held"
                        )
                        if entity_summary and entity_summary != "none":
                            _summary += f"; entities: {entity_summary[:80]}"
                        _job.finished(_summary)
                except Exception as _exc:
                    if _job:
                        _job.failed(f"import pipeline error: {type(_exc).__name__}")
                    raise
                # Mark every memo we handled as seen — including held ones.
                # A held memo must NOT be re-imported on the next poll (that
                # was the hot-loop failure), but it is also not lost: it stays
                # in held_memos.json and is re-surfaced on the reminder
                # interval until released or discarded.
                for f in new_files:
                    seen.add(f.name)
                save_seen(seen)
            else:
                log.debug("No new Voice Memos.")

            # --- Meeting recordings (mic-recorder) ---
            candidates = find_new_meeting_recordings(seen_meetings)
            ready, pending = partition_meeting_recordings(candidates, seen_meetings)

            # Report only what is genuinely actionable. "New" means a recording
            # whose transcript has just appeared — not every file we have never
            # managed to report, which is what produced "Found 88 new meeting
            # recording(s)" once a minute indefinitely.
            if ready:
                log.info(
                    f"Found {len(ready)} meeting recording(s) with new transcripts."
                )
            if pending:
                # Pending is a steady-state fact, not an event. Log it at debug
                # so it stays diagnosable without growing the log every minute,
                # and summarise the backlog only when it changes size.
                if pending_backlog_size != len(pending):
                    log.info(
                        f"{len(pending)} meeting recording(s) awaiting transcripts "
                        f"(was {pending_backlog_size}); not re-announcing each poll."
                    )
                    pending_backlog_size = len(pending)
                else:
                    log.debug(
                        f"{len(pending)} meeting recording(s) still awaiting transcripts."
                    )
            elif pending_backlog_size:
                log.info("Meeting-recording transcript backlog cleared.")
                pending_backlog_size = 0

            reported = set()
            for rec in ready:
                report_meeting_recording(rec)
                reported.add(rec.name)
            if reported:
                seen_meetings.update(reported)
                save_seen_meetings(seen_meetings)

            # --- Held-memo backlog reminder ---
            # An unconfirmed memo must not fade out of view. Re-list the
            # backlog on the reminder interval, through the same Telegram path
            # as every other notification.
            held = load_held()  # re-read: --release may have run externally
            if held and time.monotonic() - _held_reminded_at >= _HELD_REMINDER_INTERVAL:
                _held_reminded_at = time.monotonic()
                lines = [
                    f"⏸ {len(held)} voice memo(s) still awaiting your confirmation:",
                    "",
                ]
                for name, info in sorted(held.items()):
                    lines.append(f"• {name} — {info.get('reason', 'unknown')}")
                lines += ["", f"Release one with:  {RELEASE_COMMAND} '<filename>'"]
                notify("Voice Memos", "\n".join(lines))
                log.info(f"Re-surfaced {len(held)} held memo(s) to the operator.")

        except NeotomaUnavailableError as exc:
            log_error(f"Neotoma/pipeline unavailable — aborting this cycle: {exc}")
        except Exception as exc:
            log_error(f"Watcher loop error: {exc}")

        time.sleep(POLL_INTERVAL_SECONDS)


def release_held(memo_name: str, discard: bool = False) -> int:
    """
    Release a held memo so its entities are extracted, or discard it.

    This is the operator's resume path: he pastes the command from the hold
    notification. Matching is exact-then-substring so he can paste a short
    distinctive fragment instead of the full filename.

    Returns a process exit code.
    """
    held = load_held()
    if not held:
        print("No memos are currently held.")
        return 0

    matches = [k for k in held if k == memo_name] or [
        k for k in held if memo_name in k
    ]
    if not matches:
        print(f"No held memo matches {memo_name!r}. Currently held:")
        for name, info in sorted(held.items()):
            print(f"  {name} — {info.get('reason', 'unknown')}")
        return 1
    if len(matches) > 1:
        print(f"{memo_name!r} matches {len(matches)} held memos — be more specific:")
        for name in sorted(matches):
            print(f"  {name}")
        return 1

    name = matches[0]
    info = held.pop(name)

    if discard:
        save_held(held)
        print(f"Discarded {name} (was held: {info.get('reason', 'unknown')}).")
        log.info(f"Operator discarded held memo {name}.")
        notify("Voice Memos", f"🗑 Discarded held memo (operator): {name}")
        return 0

    print(f"Releasing {name} — running entity extraction…")
    try:
        summary = run_entity_extraction([Path(name)])
    except Exception as exc:
        # Keep it held: a failed release must not lose the memo.
        print(f"Entity extraction failed: {exc}\n{name} remains held.")
        log_error(f"Release of held memo {name} failed: {exc}")
        return 1

    save_held(held)
    msg = f"✅ Released by operator and processed: {name}"
    if summary and summary != "none":
        msg += f"\nEntities: {summary}"
    print(msg)
    log.info(f"Operator released held memo {name}.")
    notify("Voice Memos", msg)
    return 0


def list_held() -> int:
    """Print the held-memo backlog."""
    held = load_held()
    if not held:
        print("No memos are currently held.")
        return 0
    print(f"{len(held)} memo(s) held awaiting confirmation:\n")
    for name, info in sorted(held.items()):
        print(f"  {name}")
        print(f"    reason: {info.get('reason', 'unknown')}")
        if info.get("transcript"):
            print(f"    transcript: {info['transcript']}")
    print(f"\nRelease with:  {RELEASE_COMMAND} '<filename>'")
    return 0


if __name__ == "__main__":
    # Operator subcommands run once and exit; no args starts the watcher.
    if len(sys.argv) > 1 and sys.argv[1] in ("--release", "--discard", "--list-held"):
        cmd = sys.argv[1]
        if cmd == "--list-held":
            sys.exit(list_held())
        if len(sys.argv) < 3:
            print(f"usage: watch.py {cmd} '<memo filename>'")
            sys.exit(2)
        sys.exit(release_held(sys.argv[2], discard=(cmd == "--discard")))
    main()
