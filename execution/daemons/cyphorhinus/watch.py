#!/usr/bin/env python3
"""
Cyphorhinus — Audio Import & Meeting Recording Daemon

Watches two sources for new audio to process:

1. macOS Voice Memos (Recordings dir) — polls for new files, imports,
   transcribes, and extracts entities via Claude.
2. Meeting recording imports (imports/audio/) — watches for WAV files
   produced by mic-recorder / meeting-recording-control.sh, then reports
   transcription and analysis progress via Telegram.

Named after Cyphorhinus, a wren genus known for exceptionally complex song.
Runs as a launchd agent — see com.markmhendrickson.cyphorhinus.plist.
"""

import json
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path

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


AUDIO_EXTENSIONS = {".m4a", ".qta"}
# Meeting recordings from mic-recorder are WAV files.
MEETING_AUDIO_EXTENSIONS = {".wav"}

POLL_INTERVAL_SECONDS = 60  # check every minute; launchd keeps it alive

# Pattern for timestamped import filenames produced by import_audio_from_desktop.py:
# e.g. "20251226_175827_voicememo_20250528 214608-1C87EB64.m4a"
# The original recording name follows the "_voicememo_" prefix.
_IMPORTED_NAME_RE = re.compile(r"^\d{8}_\d{6}_voicememo_(.+)$")

PROJECT_ROOT = Path(__file__).parent.parent.parent.parent  # ateles repo root
IMPORT_SCRIPT = PROJECT_ROOT / "execution" / "scripts" / "import_audio_from_desktop.py"

LOG_DIR = Path.home() / "Library" / "Logs" / "ateles"
LOG_FILE = LOG_DIR / "cyphorhinus.log"

# Local state files — track filenames already processed.
STATE_FILE = Path(__file__).parent / "seen_files.json"
MEETING_STATE_FILE = Path(__file__).parent / "seen_meeting_files.json"

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

# 2. Refresh NEOTOMA_BEARER_TOKEN from 1Password (best-effort).
#    This ensures the token is always current without needing `neotoma auth login`.
#    Requires `op` CLI to be installed and an active 1Password session.
_OP_REF = "op://Private/Neotoma local bearer token/bearer_token"
try:
    _op_result = subprocess.run(
        ["op", "read", _OP_REF],
        capture_output=True, text=True, timeout=10,
    )
    if _op_result.returncode == 0:
        _token = _op_result.stdout.strip()
        if _token:
            os.environ["NEOTOMA_BEARER_TOKEN"] = _token
            # Write back to .env so the neotoma CLI picks it up too
            if _NEOTOMA_ENV_FILE.exists():
                _env_text = _NEOTOMA_ENV_FILE.read_text()
                import re as _re
                _new_line = f'NEOTOMA_BEARER_TOKEN="{_token}"'
                if "NEOTOMA_BEARER_TOKEN" in _env_text:
                    _env_text = _re.sub(
                        r'^NEOTOMA_BEARER_TOKEN=.*$', _new_line,
                        _env_text, flags=_re.MULTILINE
                    )
                else:
                    _env_text += f"\n{_new_line}\n"
                _NEOTOMA_ENV_FILE.write_text(_env_text)
except Exception:
    pass  # op not available or session expired — proceed with existing token

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOG_DIR.mkdir(parents=True, exist_ok=True)


class _FlushingFileHandler(logging.FileHandler):
    """FileHandler that flushes after every record — required when stdout is
    piped by launchd and Python's default block-buffering would delay log lines."""

    def emit(self, record: logging.LogRecord) -> None:
        super().emit(record)
        self.flush()


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [cyphorhinus] %(levelname)s %(message)s",
    handlers=[
        _FlushingFileHandler(LOG_FILE),
        # stdout is captured by launchd to the same log file — no StreamHandler
        # here to avoid every line appearing twice in the log.
    ],
)
log = logging.getLogger(__name__)


# Dedup state for Telegram alerts — avoids repeat messages for persistent errors.
# Maps alert text → (first_sent_time, send_count). Cleared when the error resolves.
_telegram_alert_state: dict[str, tuple[float, int]] = {}
# Re-notify for the same persistent error after this many seconds (hourly reminder).
_TELEGRAM_REPEAT_INTERVAL = 3600


def _telegram(message: str) -> None:
    """Send a Telegram message via telegram-send (best-effort, no raise)."""
    import shutil

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


def _telegram_deduped(message: str) -> None:
    """
    Send a Telegram alert only if the message is new or hasn't been sent
    recently. Suppresses repeated identical alerts within _TELEGRAM_REPEAT_INTERVAL,
    but sends a reminder when the interval lapses so persistent issues stay visible.
    Call _telegram_clear(message) when the condition resolves.
    """
    now = time.monotonic()
    state = _telegram_alert_state.get(message)
    if state is None:
        # New alert — send immediately
        _telegram_alert_state[message] = (now, 1)
        _telegram(message)
    else:
        first_sent, count = state
        elapsed = now - first_sent
        if elapsed >= _TELEGRAM_REPEAT_INTERVAL * count:
            # Remind once per hour for persistent issues
            count += 1
            _telegram_alert_state[message] = (first_sent, count)
            _telegram(f"{message} (still ongoing, {int(elapsed / 60)}m)")


def _telegram_clear(message: str) -> None:
    """Mark an alert as resolved so it fires fresh if it recurs."""
    _telegram_alert_state.pop(message, None)


def log_error(message: str) -> None:
    """Log at ERROR level and send a deduplicated Telegram alert."""
    log.error(message)
    _telegram_deduped(f"🔴 [cyphorhinus] ERROR: {message}")


def log_warning(message: str) -> None:
    """Log at WARNING level and send a deduplicated Telegram alert."""
    log.warning(message)
    _telegram_deduped(f"🟡 [cyphorhinus] WARNING: {message}")


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
    """Raised when Neotoma is not usable (CLI missing, server down, auth failure, etc.)."""


# Fixed prod port — the launchagent always binds here; port file can go stale.
_NEOTOMA_PROD_PORT = 3180
# Candidate ports to probe when the configured base URL is unreachable.
# Dev port (3080) is intentionally excluded: falling back to dev would silently
# write imports to the wrong database. We only ever want the prod server.
_NEOTOMA_FALLBACK_PORTS = [3180]
# Project root for reading the port file as a last resort.
_NEOTOMA_PROJECT_ROOT = Path(
    os.environ.get("NEOTOMA_PROJECT_ROOT", Path.home() / "repos" / "neotoma")
)


def _probe_tcp(host: str, port: int, timeout: float = 1.0) -> bool:
    """Return True if a TCP connection to host:port succeeds within timeout."""
    import socket
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _find_neotoma_base_url() -> str | None:
    """
    Self-repair: find the prod server's actual URL when the configured one is
    unreachable.

    Strategy (in order):
    1. Port file  — NEOTOMA_PROJECT_ROOT/.dev-serve/local_http_port_prod
    2. Fixed port — 3180 (launchagent default)
    3. Dev port   — 3080 (fallback, in case prod is somehow on dev)

    Returns the first URL that has a listening TCP port, or None if none found.
    """
    candidates: list[int] = []

    # 1. Port file
    port_file = _NEOTOMA_PROJECT_ROOT / ".dev-serve" / "local_http_port_prod"
    try:
        raw = port_file.read_text().strip()
        if raw.isdigit():
            candidates.append(int(raw))
    except Exception:
        pass

    # 2 & 3. Fixed fallbacks (deduped, preserve order)
    for p in _NEOTOMA_FALLBACK_PORTS:
        if p not in candidates:
            candidates.append(p)

    for port in candidates:
        if _probe_tcp("localhost", port):
            return f"http://localhost:{port}"

    return None


def _neotoma_cmd(extra_args: list[str], timeout: int = 15) -> subprocess.CompletedProcess:
    """Run a neotoma CLI command, returning the CompletedProcess."""
    return subprocess.run(
        ["neotoma", "--json", "--api-only"] + extra_args,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=os.environ,
    )


def check_neotoma() -> None:
    """
    Verify that Neotoma is ready: CLI present, server reachable, auth valid.
    Raises NeotomaUnavailableError with a descriptive message on any failure.

    Self-repair behaviours:
    - "Server not reachable": probes candidate ports, updates NEOTOMA_BASE_URL
      in-process, and retries once.
    - "401 Unauthorized": raises with a clear message distinguishing auth
      failure from connectivity failure (token rotation required).
    Call this before any operation that depends on Neotoma.
    """
    import shutil

    neotoma = shutil.which("neotoma")
    if not neotoma:
        raise NeotomaUnavailableError("neotoma CLI not found in PATH")

    health_args = ["entities", "list", "--entity-type", "transcription", "--limit", "1"]

    try:
        result = _neotoma_cmd(health_args)
    except subprocess.TimeoutExpired:
        raise NeotomaUnavailableError("Neotoma health check timed out")
    except Exception as exc:
        raise NeotomaUnavailableError(f"Neotoma health check failed: {exc}") from exc

    if result.returncode != 0:
        raw_err = (result.stderr.strip() or result.stdout.strip())[:300]

        # --- Self-repair: server not reachable ---
        if "not reachable" in raw_err.lower() or "server not reachable" in raw_err.lower():
            recovered_url = _find_neotoma_base_url()
            if recovered_url:
                current_url = os.environ.get("NEOTOMA_BASE_URL", "")
                if recovered_url != current_url:
                    log.warning(
                        f"Prod server moved: {current_url or '(none)'} → {recovered_url}. "
                        "Updating NEOTOMA_BASE_URL and retrying."
                    )
                    os.environ["NEOTOMA_BASE_URL"] = recovered_url
                    # Persist to .env so the neotoma CLI subprocess picks it up
                    try:
                        env_text = _NEOTOMA_ENV_FILE.read_text()
                        new_line = f'NEOTOMA_BASE_URL="{recovered_url}"'
                        import re as _re2
                        if "NEOTOMA_BASE_URL" in env_text:
                            env_text = _re2.sub(
                                r"^NEOTOMA_BASE_URL=.*$", new_line,
                                env_text, flags=_re2.MULTILINE,
                            )
                        else:
                            env_text += f"\n{new_line}\n"
                        _NEOTOMA_ENV_FILE.write_text(env_text)
                    except Exception:
                        pass
                # Retry once with the (possibly updated) URL
                try:
                    result = _neotoma_cmd(health_args)
                except subprocess.TimeoutExpired:
                    raise NeotomaUnavailableError("Neotoma health check timed out (retry)")
                except Exception as exc:
                    raise NeotomaUnavailableError(f"Neotoma health check failed (retry): {exc}") from exc
                if result.returncode != 0:
                    raw_err2 = (result.stderr.strip() or result.stdout.strip())[:300]
                    raise NeotomaUnavailableError(
                        f"Neotoma still unreachable after URL repair ({recovered_url}): {raw_err2}"
                    )
            else:
                raise NeotomaUnavailableError(
                    f"Neotoma server not reachable on any known port: {raw_err}"
                )

        # --- Auth failure: distinguish from connectivity ---
        elif "401" in raw_err or "unauthorized" in raw_err.lower() or "invalid authentication" in raw_err.lower():
            raise NeotomaUnavailableError(
                "Neotoma auth rejected (401) — bearer token may need rotation in 1Password"
            )

        else:
            raise NeotomaUnavailableError(
                f"Neotoma returned non-zero exit code {result.returncode}: {raw_err}"
            )

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise NeotomaUnavailableError(
            f"Neotoma returned unparseable response: {result.stdout[:200]}"
        ) from exc

    if "error" in data:
        # One more layer of 401 detection (some versions embed in JSON)
        err_msg = data["error"]
        if "401" in err_msg or "unauthorized" in err_msg.lower():
            raise NeotomaUnavailableError(
                "Neotoma auth rejected (401) — bearer token may need rotation in 1Password"
            )
        raise NeotomaUnavailableError(f"Neotoma API error: {err_msg}")


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
        result = subprocess.run(
            [
                "neotoma",
                "--json",
                "--api-only",
                "entities",
                "list",
                "--entity-type",
                "transcription",
                "--limit",
                str(limit),
                "--offset",
                str(offset),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            env=os.environ,
        )
        if result.returncode != 0 or not result.stdout.strip():
            raise NeotomaUnavailableError(
                f"Neotoma hydration failed at offset {offset}: "
                f"{(result.stderr or result.stdout).strip()[:200]}"
            )
        data = json.loads(result.stdout)
        if "error" in data:
            raise NeotomaUnavailableError(f"Neotoma hydration error: {data['error']}")
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
        result = subprocess.run(
            [
                sys.executable,
                str(IMPORT_SCRIPT),
                "--source",
                str(RECORDINGS_DIR),
                "--analyze",
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


def notify(title: str, message: str) -> None:
    """Send a Telegram message via telegram-send (best-effort; logs on failure)."""
    import shutil

    telegram = shutil.which("telegram-send")
    if not telegram:
        log.warning("telegram-send not found in PATH — notification skipped")
        return
    try:
        result = subprocess.run(
            [telegram, f"[{title}] {message}"],
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
   - Anything about Neotoma the product: relate to ent_44835c5b0047ce26ffbe40bc (Neotoma, Inc.)
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
                    _telegram(f"✅ [cyphorhinus] Neotoma available — startup resumed after {duration}")
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
                    _telegram(f"✅ [cyphorhinus] Neotoma back online (was down {duration})")

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
                run_import()
                notify(
                    "Voice Memos",
                    f"Transcription complete for {n} memo{'s' if n != 1 else ''}. Extracting entities…",
                )
                entity_summary = run_entity_extraction(new_files)
                done_msg = (
                    f"Done — {n} memo{'s' if n != 1 else ''} imported & transcribed."
                )
                if entity_summary and entity_summary != "none":
                    done_msg += f"\nEntities: {entity_summary}"
                notify("Voice Memos", done_msg)
                for f in new_files:
                    seen.add(f.name)
                save_seen(seen)
            else:
                log.debug("No new Voice Memos.")

            # --- Meeting recordings (mic-recorder) ---
            new_recordings = find_new_meeting_recordings(seen_meetings)
            if new_recordings:
                log.info(f"Found {len(new_recordings)} new meeting recording(s).")
            reported = set()
            for rec in new_recordings:
                report_meeting_recording(rec)
                # report_meeting_recording only marks ready files; track which
                # we attempted so we don't retry until next poll.
                stem = rec.stem
                parent = rec.parent
                if (parent / f"{stem}.txt").exists():
                    reported.add(rec.name)
            if reported:
                seen_meetings.update(reported)
                save_seen_meetings(seen_meetings)

        except NeotomaUnavailableError as exc:
            log_error(f"Neotoma/pipeline unavailable — aborting this cycle: {exc}")
        except Exception as exc:
            log_error(f"Watcher loop error: {exc}")

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
