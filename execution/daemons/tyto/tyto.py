#!/usr/bin/env python3
"""
Tyto — Screenshot watcher + meeting recording transcription daemon.

Tyto genus: barn owls. T3 daemon in the Ateles swarm.

Tyto watches up to four directories:
  1. TYTO_SCREENSHOTS_DIR — new image files (PNG/JPG/etc.), stored as
     `screenshot` entities in Neotoma. Phase 3: OCR dispatch.
  2. TYTO_RECORDINGS_DIR — new meeting recording files (*remote*.aac/.m4a),
     auto-transcribed via transcribe_audio.py --diarize immediately on
     detection (with --no-diarize fallback). Optionally also
     TYTO_NATIVE_RECORDINGS_DIR for platform-native (Zoom/Meet/Teams)
     recordings, which carry the platform's built-in consent disclosure.
     Each transcription is stamped with capture_method for consent auditing.
  3. TYTO_VOICE_MEMOS_DIR — macOS Voice Memos. Unlike the two above these are
     single-file, single-speaker recordings the operator made alone, so there
     is no mic/remote pair to merge and no third-party consent dimension:
     they are stamped capture_method=voice_memo. A backlog guard (startup
     seeding + an mtime age window) keeps the existing memo archive from being
     transcribed on the first poll.

Lives at: launchd on the operator's machine (no external endpoint required)

AAuth sub: tyto@ateles-swarm

Environment variables:
  NEOTOMA_BEARER_TOKEN      Neotoma API auth token
  NEOTOMA_BASE_URL          Neotoma API base URL (default: https://neotoma.markmhendrickson.com)
  TELEGRAM_BOT_TOKEN        Telegram bot token
  TELEGRAM_CHAT_ID          Telegram chat ID
  TELEGRAM_TOPIC_TYTO       Telegram topic ID for Tyto notifications (optional)
  TYTO_SCREENSHOTS_DIR      Directory to watch for screenshots (default: ~/Desktop/Screenshots)
  TYTO_POLL_INTERVAL        Polling interval in seconds (default: 10)
  TYTO_AGENT_DEFINITION_ID  Neotoma entity ID for Tyto's agent_definition (optional)
  TYTO_RECORDINGS_DIR       Directory to watch for meeting recordings
                            (default: $RECORD_MEETING_DIR or ~/Documents/data/recordings).
                            Files here are stamped capture_method=audio_hijack_system
                            (local system capture, no built-in consent disclosure).
  TYTO_NATIVE_RECORDINGS_DIR  Optional second dir for platform-NATIVE recordings
                            (Zoom/Meet/Teams local recordings, which carry the
                            platform's built-in consent notice). Files here flow
                            through the identical transcribe+analyze pipeline but
                            are stamped capture_method=platform_native for
                            consent-posture auditing. Unset → not watched.
  TYTO_VOICE_MEMOS_DIR      Directory to watch for macOS Voice Memos (default: the
                            standard ~/Library/Group Containers/
                            group.com.apple.VoiceMemos.shared/Recordings path).
                            Memos are single-speaker, single-file recordings by the
                            operator alone, so they are never sent down the two-file
                            mic+remote merge path and are stamped
                            capture_method=voice_memo. Set to "" to disable.
  TYTO_VOICE_MEMO_MAX_AGE_SECS  Only memos modified within this many seconds are
                            eligible (default: 3600). Together with startup seeding
                            this is the backlog guard that stops an existing memo
                            archive from being transcribed en masse on first poll.
                            0 disables the age window (seeding still applies).
  TYTO_VOICE_MEMO_INCLUDE_QTA  Set to 0 to exclude .qta files (default: 1).
                            Current Voice Memos can arrive as .qta; the same
                            startup, age, and stable-file guards apply.
  TYTO_VOICE_MEMO_RETRY_STATE  Durable pending-retry journal (default:
                            ~/.local/state/ateles/tyto-voice-memo-retries.json).
  TYTO_VOICE_MEMO_RETRY_SECS  Delay after a failed memo transcription before
                            retrying (default: 300).
  TYTO_TRANSCRIBE_PROCESS_TIMEOUT_SECS  Outer deadline for a transcription
                            subprocess (default: 1800). A timed-out recording
                            remains eligible for the next poll/retry.
  TYTO_TRANSCRIBE_ENABLED   Set to 0 to disable auto-transcription (default: 1)
  NEOTOMA_RC_DIR            Deployed Neotoma runtime for transcription CLI calls.
                           Must support per-agent key paths; checked before STT.
  TYTO_TRANSCRIBE_SCRIPT    Path to transcribe_audio.py (auto-detected from repo root)
  ELEVENLABS_API_KEY        Enables ElevenLabs for explicit diarization and
                            multi-channel audio; key presence alone does not route.
  RECORD_MEETING_DIARIZE    Set to 1 for ElevenLabs diarization, 0 for local;
                            unset routes from the audio channel count.
  TYTO_ANALYZE_ENABLED      Set to 0 to disable post-transcription meeting analysis (default: 1)
  TYTO_ANALYZE_MEETING_SKILL  Path to analyze-meeting/SKILL.md (auto-detected from repo root)
  TYTO_ANALYZE_NEOTOMA_SKILL  Path to analyze-neotoma-feedback/SKILL.md (auto-detected)
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

# ── Load .env early so env vars are available before config reads ─────────────
def _load_env() -> None:
    """Load .env files from personal repo and ateles-private into os.environ."""
    _home = Path.home()
    candidates = [
        _home / "repos" / "personal" / ".env",
        _home / "repos" / "ateles-private" / ".env",
    ]
    for env_path in candidates:
        if not env_path.exists():
            continue
        try:
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, _, val = line.partition("=")
                    key = key.strip()
                    val = val.strip().strip('"').strip("'")
                    if key and key not in os.environ:
                        os.environ[key] = val
        except Exception:
            pass

_load_env()

# ── Path bootstrap ────────────────────────────────────────────────────────────
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from lib.daemon_runtime import (  # noqa: E402
    AAuthSigner,
    AgentLoader,
    enforce_status_or_exit,
)
from lib.daemon_runtime.neotoma_signed import NEOTOMA_RC_DIR, agent_identity  # noqa: E402
from lib.notify import Notifier, Priority  # noqa: E402

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("tyto")

# ── Config ────────────────────────────────────────────────────────────────────
DAEMON_NAME = "tyto"

NEOTOMA_BASE_URL = os.environ.get("NEOTOMA_BASE_URL", "")
NEOTOMA_BEARER_TOKEN = os.environ.get("NEOTOMA_BEARER_TOKEN", "")

SCREENSHOTS_DIR = Path(
    os.environ.get(
        "TYTO_SCREENSHOTS_DIR",
        str(Path.home() / "Desktop" / "Screenshots"),
    )
)
POLL_INTERVAL = int(os.environ.get("TYTO_POLL_INTERVAL", "10"))

SCREENSHOT_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}

# ── Recording transcription config ───────────────────────────────────────────
_default_recordings_dir = os.environ.get(
    "RECORD_MEETING_DIR",
    str(Path.home() / "Documents" / "data" / "recordings"),
)
RECORDINGS_DIR = Path(
    os.environ.get("TYTO_RECORDINGS_DIR", _default_recordings_dir)
)
# Optional second watch dir for platform-NATIVE recordings (Zoom/Meet/Teams local
# recordings, which carry the platform's built-in consent disclosure). When set,
# files here flow through the identical transcribe+analyze pipeline but are stamped
# capture_method=platform_native for consent-posture auditing. Unset → not watched.
NATIVE_RECORDINGS_DIR = (
    Path(os.environ["TYTO_NATIVE_RECORDINGS_DIR"])
    if os.environ.get("TYTO_NATIVE_RECORDINGS_DIR", "").strip()
    else None
)

# Optional third watch dir for macOS Voice Memos. These are the operator's own
# self-recorded single-speaker memos: no second party, so no third-party consent
# dimension at all — hence a distinct capture_method=voice_memo rather than
# reusing audio_hijack_system (system capture, no disclosure) or platform_native
# (Zoom/Meet/Teams, built-in disclosure).
#
# Set TYTO_VOICE_MEMOS_DIR="" to disable. The default is the fixed, documented
# macOS group-container path — a per-OS constant, not operator config, so it
# carries no portability or PII concern the way an email or calendar ID would.
_default_voice_memos_dir = str(
    Path.home()
    / "Library"
    / "Group Containers"
    / "group.com.apple.VoiceMemos.shared"
    / "Recordings"
)
_voice_memos_env = os.environ.get("TYTO_VOICE_MEMOS_DIR", _default_voice_memos_dir)
VOICE_MEMOS_DIR = Path(_voice_memos_env) if _voice_memos_env.strip() else None

# Backlog guard. The Voice Memos directory holds the operator's entire memo
# archive (hundreds of files). Without a cutoff, the first poll would fire one
# transcription + notification per archived memo. Only memos modified within
# this many seconds of daemon start are eligible; everything older is recorded
# as already-handled at startup and never transcribed. 0 disables the age
# window (startup seeding still applies).
VOICE_MEMO_MAX_AGE_SECS = int(
    os.environ.get("TYTO_VOICE_MEMO_MAX_AGE_SECS", "3600")
)

# Current Voice Memos may arrive as QTA. The existing startup and age guards
# distinguish the archive from new arrivals regardless of container format.
VOICE_MEMO_INCLUDE_QTA = os.environ.get("TYTO_VOICE_MEMO_INCLUDE_QTA", "1") == "1"
VOICE_MEMO_RETRY_STATE = Path(
    os.environ.get(
        "TYTO_VOICE_MEMO_RETRY_STATE",
        str(Path.home() / ".local" / "state" / "ateles" / "tyto-voice-memo-retries.json"),
    )
)
VOICE_MEMO_RETRY_SECS = int(os.environ.get("TYTO_VOICE_MEMO_RETRY_SECS", "300"))
TRANSCRIBE_PROCESS_TIMEOUT_SECS = max(
    1, int(os.environ.get("TYTO_TRANSCRIBE_PROCESS_TIMEOUT_SECS", "1800"))
)
TRANSCRIBE_ENABLED = os.environ.get("TYTO_TRANSCRIBE_ENABLED", "1") != "0"

# Auto-detect transcribe_audio.py — check ateles repo first, then personal repo
def _find_transcribe_script() -> str:
    candidates = [
        _REPO_ROOT / "execution" / "scripts" / "transcribe_audio.py",
        Path.home() / "repos" / "personal" / "execution" / "scripts" / "transcribe_audio.py",
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    return str(candidates[0])  # fall through to surface the error clearly

TRANSCRIBE_SCRIPT = Path(
    os.environ.get("TYTO_TRANSCRIBE_SCRIPT", _find_transcribe_script())
)

# ── Meeting analysis config ───────────────────────────────────────────────────
# Set TYTO_ANALYZE_ENABLED=0 to disable post-transcription analysis.
ANALYZE_ENABLED = os.environ.get("TYTO_ANALYZE_ENABLED", "1") != "0"

# Path to the analyze-meeting skill SKILL.md (authoritative prompt)
_default_analyze_meeting_skill = str(
    _REPO_ROOT / ".claude" / "skills" / "analyze-meeting" / "SKILL.md"
)
ANALYZE_MEETING_SKILL_PATH = Path(
    os.environ.get("TYTO_ANALYZE_MEETING_SKILL", _default_analyze_meeting_skill)
)
_default_analyze_neotoma_skill = str(
    _REPO_ROOT / ".claude" / "skills" / "analyze-neotoma-feedback" / "SKILL.md"
)
ANALYZE_NEOTOMA_SKILL_PATH = Path(
    os.environ.get("TYTO_ANALYZE_NEOTOMA_SKILL", _default_analyze_neotoma_skill)
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _sha256_file(path: Path) -> str:
    """Compute SHA-256 of a file. Returns hex digest."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _find_claude_bin() -> str | None:
    """Return path to claude CLI, or None if not found."""
    import shutil
    return shutil.which("claude")


def _find_venv_python() -> str:
    """Return venv python path if available, otherwise system python3."""
    venv_py = _REPO_ROOT / "execution" / "venv" / "bin" / "python"
    if venv_py.exists():
        return str(venv_py)
    return sys.executable


# ── Meeting analysis ─────────────────────────────────────────────────────────


def _parse_recording_timestamp(remote_path: Path) -> datetime | None:
    """
    Parse the recording start time from Audio Hijack's filename convention:
      "YYYYMMDD HHMM remote.mp4"  →  datetime (UTC assumed; local time on disk)
    Returns None if the filename doesn't match.
    """
    import re
    m = re.match(r"(\d{4})(\d{2})(\d{2})\s+(\d{2})(\d{2})", remote_path.stem)
    if not m:
        return None
    try:
        return datetime(
            int(m.group(1)), int(m.group(2)), int(m.group(3)),
            int(m.group(4)), int(m.group(5)),
            tzinfo=UTC,
        )
    except ValueError:
        return None


def _run_analysis(
    remote_path: Path,
    transcription_entity_id: str | None,
    notifier: Notifier,
) -> None:
    """
    Invoke `claude --print` to run /analyze-meeting (and /analyze-neotoma-feedback
    when the transcript is Neotoma-oriented) on the just-transcribed recording.

    Passes:
    - Precise recording timestamp (parsed from filename) for calendar lookup.
    - Back-to-back meeting detection instruction.
    - Inline /analyze-neotoma-feedback skill when meeting is Neotoma-oriented.

    Runs in a subprocess so Tyto's poll loop is not blocked.
    """
    claude_bin = _find_claude_bin()
    if not claude_bin:
        log.warning(
            f"[{DAEMON_NAME}] claude CLI not found on PATH — skipping meeting analysis. "
            "Install claude (npm i -g @anthropic-ai/claude-code) to enable."
        )
        return

    if not ANALYZE_MEETING_SKILL_PATH.exists():
        log.warning(
            f"[{DAEMON_NAME}] analyze-meeting skill not found: {ANALYZE_MEETING_SKILL_PATH} "
            "— skipping analysis."
        )
        return

    # Source reference: prefer Neotoma entity ID; fall back to file path.
    source_ref = transcription_entity_id or str(remote_path)

    # Parse recording timestamp from filename for precise calendar lookup.
    recording_dt = _parse_recording_timestamp(remote_path)
    recording_ts_str = (
        recording_dt.strftime("%Y-%m-%dT%H:%M") if recording_dt
        else datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M")
    )

    # Build the prompt: skill content + neotoma skill + invocation context
    skill_content = ANALYZE_MEETING_SKILL_PATH.read_text(encoding="utf-8")

    neotoma_skill_section = ""
    if ANALYZE_NEOTOMA_SKILL_PATH.exists():
        neotoma_content = ANALYZE_NEOTOMA_SKILL_PATH.read_text(encoding="utf-8")
        neotoma_skill_section = (
            "\n\n---\n\n"
            "# Supplementary skill: /analyze-neotoma-feedback\n\n"
            "When you classify the meeting as Neotoma-oriented (customer_call or partner_call "
            "where the primary topic is Neotoma's data model, schema, API, product behaviour, "
            "MCP, SDK, or customer development), ALSO run the following skill in the same turn, "
            "producing both a meeting_analysis AND a feedback_analysis entity linked via the "
            "shared transcription and contact entities.\n\n"
            + neotoma_content
        )

    # Additional Tyto-specific instructions injected ahead of the invocation.
    tyto_instructions = f"""
## Tyto pre-analysis instructions

These instructions are injected by the Tyto daemon and MUST be followed before
running the standard /analyze-meeting steps.

### 1. Back-to-back meeting detection (REQUIRED before Step 1)

Before treating this as a single meeting, scan the full transcript for natural
session boundaries: greetings ("hi", "hello", "good morning", "nice to meet you",
"thanks for joining"), farewells ("bye", "talk soon", "thanks everyone", "take care",
"have a good one", "see you later"), and significant topic/participant discontinuities.

Rules:
- If you detect **one clear session** with no internal boundary signals → proceed
  as a single meeting (standard flow).
- If you detect **two or more distinct sessions** (e.g. farewell followed by a new
  greeting with different or partially overlapping participants) → treat each segment
  as a **separate meeting**. Run the full /analyze-meeting pipeline independently for
  each segment, producing one `meeting_analysis` entity per segment. Number them:
  "Meeting 1 of N", "Meeting 2 of N", etc.
- For each segment, note approximate start/end timestamps from the transcript
  (use speaker turn indices or word timestamps if available) and record them in the
  `meeting_analysis` entity as `segment_start_approx` and `segment_end_approx`.
- When in doubt (ambiguous boundary), prefer splitting. A false split costs one extra
  analysis; a missed split loses a meeting's follow-ups entirely.

### 2. Google Calendar context (REQUIRED as part of Step 1)

The recording file was created at: **{recording_ts_str}** (UTC).

In Step 1 of /analyze-meeting, use `gws calendar events list --timezone Europe/Madrid`
to query events in a **±90-minute window** around this timestamp. Do this for EACH
detected meeting segment (if multiple), using the segment's approximate start time.

For each matched calendar event:
- Extract title, attendees (name + email), start/end time.
- Cross-reference attendees with speaker labels in the transcript to resolve real names
  for diarized [Speaker_0], [Speaker_1], etc. labels.
- Store a `calendar_event` entity and link it to the `meeting_analysis` via REFERS_TO.
- Use attendee emails to stage Gmail recap drafts (per Step 5 of the skill).

If no calendar event matches within the ±90-minute window, note
`_Calendar: no matching event found._` and proceed without it.
"""

    prompt = (
        f"{skill_content}"
        f"{neotoma_skill_section}"
        f"\n\n---\n\n"
        f"{tyto_instructions}"
        f"\n\n---\n\n"
        f"## Invocation\n\n"
        f"/analyze-meeting {source_ref}\n\n"
        f"Recording file: {remote_path}\n"
        f"Recording timestamp: {recording_ts_str} UTC\n"
        f"Transcription entity: {transcription_entity_id or '(not stored — use file path)'}\n"
    )

    log.info(
        f"[{DAEMON_NAME}] Running meeting analysis for {remote_path.name} "
        f"(source: {source_ref[:60]}...)"
    )

    result = subprocess.run(
        [claude_bin, "--print", "--dangerously-skip-permissions"],
        input=prompt,
        capture_output=True,
        text=True,
        timeout=600,  # analysis can take a few minutes for long meetings
        env={**os.environ},
    )

    if result.returncode != 0:
        log.error(
            f"[{DAEMON_NAME}] Meeting analysis failed (rc={result.returncode}): "
            f"{result.stderr.strip()[:500]}"
        )
        notifier.send(
            f"Meeting analysis failed for {remote_path.name}: rc={result.returncode}",
            priority=Priority.BLOCKER,
            handler=DAEMON_NAME,
        )
        return

    log.info(f"[{DAEMON_NAME}] Meeting analysis complete for {remote_path.name}.")
    # Surface a brief summary from the output (first non-empty line of stdout)
    first_line = next(
        (line.strip() for line in result.stdout.splitlines() if line.strip()), ""
    )
    notifier.send(
        f"Meeting analysis done: {first_line[:120] or remote_path.name}",
        priority=Priority.INFO,
        handler=DAEMON_NAME,
    )


# ── Screenshot watcher ────────────────────────────────────────────────────────


class ScreenshotWatcher:
    """
    Polls a directory for new image files and stores them in Neotoma.

    State: tracks seen file paths + mtimes to avoid double-processing.
    """

    def __init__(self, watch_dir: Path, notifier: Notifier) -> None:
        self._dir = watch_dir
        self._notifier = notifier
        self._seen: dict[Path, float] = {}  # path → mtime

    async def poll_once(self) -> None:
        """Check the directory for new or modified screenshots."""
        if not self._dir.exists():
            log.debug(f"[{DAEMON_NAME}] Watch dir does not exist: {self._dir}")
            return

        for path in sorted(self._dir.iterdir()):
            if path.suffix.lower() not in SCREENSHOT_EXTENSIONS:
                continue
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue

            if self._seen.get(path) == mtime:
                continue  # already processed

            self._seen[path] = mtime
            log.info(f"[{DAEMON_NAME}] New screenshot: {path.name}")
            await self._handle_screenshot(path)

    async def _handle_screenshot(self, path: Path) -> None:
        """
        Store screenshot as a Neotoma entity and queue for OCR.

        Phase 3 will add: dispatching an OCR invocable agent, extracting
        entities from the screenshot content, and linking to related tasks.
        """
        entity_id = await asyncio.to_thread(self._store_screenshot_entity, path)
        if entity_id:
            self._notifier.send(
                f"Screenshot captured: {path.name}",
                priority=Priority.INFO,
                handler=DAEMON_NAME,
            )
            log.info(
                f"[{DAEMON_NAME}] Stored screenshot entity: {entity_id} ({path.name})"
            )
        else:
            log.warning(f"[{DAEMON_NAME}] Could not store screenshot entity for {path}")

    def _store_screenshot_entity(self, path: Path) -> str | None:
        """
        Store a screenshot entity in Neotoma via the HTTP API.
        Returns the entity_id on success, None on failure.
        """
        if not NEOTOMA_BEARER_TOKEN:
            log.debug(
                f"[{DAEMON_NAME}] NEOTOMA_BEARER_TOKEN not set — skipping entity store"
            )
            return None

        try:
            file_hash = _sha256_file(path)
            captured_at = datetime.fromtimestamp(
                path.stat().st_mtime, tz=UTC
            ).isoformat()

            payload: dict[str, Any] = {
                "entities": [
                    {
                        "entity_type": "screenshot",
                        "filename": path.name,
                        "file_hash": file_hash,
                        "captured_at": captured_at,
                        "source_path": str(path),
                        "daemon": DAEMON_NAME,
                        "status": "pending_ocr",
                    }
                ]
            }
            resp = httpx.post(
                f"{NEOTOMA_BASE_URL}/store",
                json=payload,
                headers={"Authorization": f"Bearer {NEOTOMA_BEARER_TOKEN}"},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            entities = data.get("entities", [])
            if entities:
                return entities[0].get("entity_id", "")
        except Exception as exc:
            log.warning(f"[{DAEMON_NAME}] Store error for {path.name}: {exc}")
        return None


# ── Recording watcher ─────────────────────────────────────────────────────────

# Neotoma store/ingest error codes that are deterministic rejections of the
# call shape, not transient service trouble — retrying them changes nothing
# and only spins the loop (ateles#1083: 22,878 retries of
# ERR_FILE_PATH_IS_SERVER_LOCAL rate-limited the daemon's own Telegram
# notification channel, hiding the failure and starving every other daemon's
# alerts on the same channel). A memo that fails with one of these codes is
# recorded as permanently failed after one attempt and never retried again;
# an unrecognized error stays transient so recoverable work is preserved.
PERMANENT_STORE_ERROR_CODES = ("ERR_FILE_PATH_IS_SERVER_LOCAL",)


def _classify_store_error(exc: BaseException) -> str | None:
    """Return the permanent error code found in ``exc``'s text, or ``None``."""
    text = str(exc)
    for code in PERMANENT_STORE_ERROR_CODES:
        if code in text:
            return code
    return None


class RecordingWatcher:
    """
    Polls RECORDINGS_DIR for new *remote* AAC/M4A/MP4 files produced by Audio Hijack.

    Audio Hijack saves paired files with matching timestamp prefixes:
      "YYYYMMDD HHMM remote.mp4" — system-wide audio (far end, all remote speakers)
      "YYYYMMDD HHMM mic.mp4"    — microphone only (you)

    On detection: waits for BOTH files to settle (mtime stable for SETTLE_SECS),
    then runs transcribe_audio.py with --mic-file for a merged [You]/[Speaker_N]
    transcript. Falls back to remote-only diarization if mic file is absent or
    ELEVENLABS_API_KEY is not set.

    State: tracks seen file paths + mtimes to avoid double-processing.
    """

    RECORDING_EXTENSIONS = {".aac", ".m4a", ".mp4", ".wav"}
    SETTLE_SECS = int(os.environ.get("TYTO_RECORDING_SETTLE_SECS", "8"))

    def __init__(
        self,
        watch_dir: Path,
        notifier: Notifier,
        capture_method: str = "audio_hijack_system",
        *,
        paired: bool = True,
        extensions: set[str] | None = None,
        max_age_secs: int = 0,
        seed_existing: bool = False,
        retry_state_path: Path | None = None,
        retry_secs: int = 300,
        max_files_per_poll: int | None = None,
    ) -> None:
        self._dir = watch_dir
        self._notifier = notifier
        # Consent-posture provenance stamped on every transcription this watcher
        # produces. "audio_hijack_system" = local system capture (no built-in
        # disclosure); "platform_native" = Zoom/Meet/Teams recording (carries the
        # platform's own consent notice); "voice_memo" = the operator's own
        # single-speaker memo (no second party at all). See record_meeting
        # SKILL.md disclosure ladder.
        self._capture_method = capture_method
        # Paired watchers use the Audio Hijack "<prefix> remote|system|mic"
        # convention: only the remote/system track is eligible, and its mic
        # counterpart is merged in. Unpaired watchers (Voice Memos) have no such
        # convention — every audio file is a self-contained single-speaker
        # recording, so the filename predicate is extension-only and the
        # two-file merge path is never attempted.
        self._paired = paired
        self._extensions = (
            {e.lower() for e in extensions}
            if extensions is not None
            else set(self.RECORDING_EXTENSIONS)
        )
        # Only files whose mtime is within this many seconds of "now" are
        # eligible. 0 = no age limit (the meeting-recording watchers' behavior).
        self._max_age_secs = max_age_secs
        self._retry_state_path = retry_state_path
        self._retry_secs = max(0, retry_secs)
        self._max_files_per_poll = (
            None if max_files_per_poll is None else max(1, max_files_per_poll)
        )
        self._pending_retries: dict[Path, float] = {}
        # Permanent failures: path → {"error_code", "at", "notified"}. A path
        # in here is never retried again and is skipped by every later poll
        # (ateles#1083 — a deterministic rejection retried forever, which
        # generated the error volume that rate-limited Telegram and hid the
        # failure). Only populated for watchers with a durable journal.
        self._hard_failed: dict[Path, dict[str, Any]] = {}
        self._retry_state_available = True
        if self._retry_state_path is not None:
            self._load_retry_state()
        # A path is settled only after both its timestamp and byte length stay
        # unchanged across polls.  Watching mtime alone is insufficient: a
        # writer can append more audio while preserving/restoring mtime.
        self._seen: dict[Path, tuple[int, int]] = {}  # path → (mtime_ns, size)
        self._transcribed: set[Path] = set() # remote paths already transcribed
        # Backlog guard: record everything already on disk at construction time
        # as handled, so an existing archive is never transcribed. Belt and
        # braces with _max_age_secs — seeding covers a file whose mtime is
        # touched after startup, the age window covers a file that appears
        # later bearing an old mtime (e.g. an iCloud sync landing an archive).
        if seed_existing:
            self._seed_existing()

    def _load_retry_state(self) -> None:
        """Load durable pending retries, failing closed if the journal is unreadable.

        Accepts v1 (``pending`` only) and v2 (``pending`` + ``hard_failed``).
        A v1 journal is read as-is in memory; the next save writes it back as
        v2. Older readers of a v2 file are not a concern — this journal has
        exactly one reader/writer (this watcher instance).
        """
        assert self._retry_state_path is not None
        if not self._retry_state_path.exists():
            return
        try:
            payload = json.loads(self._retry_state_path.read_text())
            if not isinstance(payload, dict) or payload.get("version") not in (1, 2):
                raise ValueError("unsupported retry-state format")
            if not isinstance(payload.get("pending"), list):
                raise ValueError("unsupported retry-state format")
            for item in payload["pending"]:
                if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                    raise ValueError("invalid pending retry entry")
                self._pending_retries[Path(item["path"])] = float(
                    item.get("retry_after", 0)
                )
            for item in payload.get("hard_failed", []) or []:
                if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                    raise ValueError("invalid hard-failed entry")
                self._hard_failed[Path(item["path"])] = {
                    "error_code": item.get("error_code", "unknown"),
                    "at": item.get("at", ""),
                    "notified": bool(item.get("notified", False)),
                }
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            self._retry_state_available = False
            log.error(
                f"[{DAEMON_NAME}] Voice Memo retry state is unreadable: "
                f"{self._retry_state_path}: {exc}. Memo processing is disabled "
                "until the journal is repaired; existing files will not be seeded "
                "as handled."
            )

    def _save_retry_state(self) -> bool:
        """Atomically persist pending retries and hard failures (journal v2)."""
        if self._retry_state_path is None:
            return True
        payload = {
            "version": 2,
            "pending": [
                {"path": str(path), "retry_after": retry_after}
                for path, retry_after in sorted(
                    self._pending_retries.items(), key=lambda item: str(item[0])
                )
            ],
            "hard_failed": [
                {"path": str(path), **info}
                for path, info in sorted(
                    self._hard_failed.items(), key=lambda item: str(item[0])
                )
            ],
        }
        tmp_path = self._retry_state_path.with_suffix(
            self._retry_state_path.suffix + ".tmp"
        )
        try:
            self._retry_state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path.write_text(json.dumps(payload, indent=2) + "\n")
            os.chmod(tmp_path, 0o600)
            tmp_path.replace(self._retry_state_path)
            return True
        except OSError as exc:
            self._retry_state_available = False
            log.error(
                f"[{DAEMON_NAME}] Cannot persist Voice Memo retry state at "
                f"{self._retry_state_path}: {exc}. Memo processing is disabled "
                "rather than risking a lost retry."
            )
            return False

    def _mark_pending(self, path: Path, retry_after: float = 0) -> bool:
        self._pending_retries[path] = retry_after
        return self._save_retry_state()

    def _clear_pending(self, path: Path) -> bool:
        self._pending_retries.pop(path, None)
        return self._save_retry_state()

    def _mark_hard_failed(self, path: Path, error_code: str) -> bool:
        """Record a permanent failure once and stop this path from ever retrying.

        Idempotent: a path already hard-failed keeps its original ``notified``
        state rather than resetting it, so a later poll cannot re-trigger a
        notification for the same permanent failure.
        """
        self._pending_retries.pop(path, None)
        existing = self._hard_failed.get(path)
        if existing is None:
            self._hard_failed[path] = {
                "error_code": error_code,
                "at": datetime.now(tz=UTC).isoformat(),
                "notified": False,
            }
        return self._save_retry_state()

    def _mark_hard_failure_notified(self, path: Path) -> bool:
        entry = self._hard_failed.get(path)
        if entry is None or entry.get("notified"):
            return True
        entry["notified"] = True
        return self._save_retry_state()

    def _seed_existing(self) -> None:
        """Mark every currently-present eligible file as already handled."""
        if not self._retry_state_available:
            return
        try:
            existing = [p for p in self._dir.iterdir() if self._is_eligible_file(p)]
        except OSError:
            return
        for path in existing:
            if path not in self._pending_retries:
                self._transcribed.add(path)
        seeded_count = len(existing) - sum(
            path in self._pending_retries for path in existing
        )
        if seeded_count:
            log.info(
                f"[{DAEMON_NAME}] Backlog guard: {seeded_count} pre-existing "
                f"file(s) in {self._dir} marked as handled; they will not be "
                "transcribed."
            )
        if self._pending_retries:
            log.info(
                f"[{DAEMON_NAME}] Restored {len(self._pending_retries)} pending "
                "Voice Memo retry/retries from durable state."
            )

    # Audio Hijack recorder block names that represent the far-end / system audio track.
    # "remote" = original naming, "system" = after session rename to "Tyto".
    _REMOTE_TRACK_NAMES = ("remote", "system")

    def _is_remote_file(self, path: Path) -> bool:
        name_lower = path.name.lower()
        return (
            path.suffix.lower() in self._extensions
            and any(t in name_lower for t in self._REMOTE_TRACK_NAMES)
            and "mic" not in name_lower  # never confuse mic track
        )

    def _is_eligible_file(self, path: Path) -> bool:
        """Per-watcher file-matching test.

        Paired watchers (Audio Hijack, platform-native) keep the original
        predicate: the filename must name the remote/system track. Unpaired
        watchers (Voice Memos) match on extension alone — memo filenames carry
        no track name, so the paired predicate would reject every one of them,
        and the non-audio sidecars the directory also holds (.waveform,
        .composition, .db, and the extensionless CaptureRecovery/Capture
        directories) fall out because their suffixes are not in _extensions.
        """
        if self._paired:
            return self._is_remote_file(path)
        if not path.is_file():
            return False
        return path.suffix.lower() in self._extensions

    def _is_within_age_window(self, path: Path, now: float) -> bool:
        """Return True when path is new enough to be eligible.

        With _max_age_secs == 0 every file passes (meeting-recording behavior).
        """
        if path in self._pending_retries or self._max_age_secs <= 0:
            return True
        try:
            st = path.stat()
        except OSError:
            return False
        return (now - st.st_mtime) <= self._max_age_secs

    def _find_mic_pair(self, remote_path: Path) -> Path | None:
        """
        Find the matching mic file for a remote/system file.
        Audio Hijack naming: "YYYYMMDD HHMM remote.ext" / "YYYYMMDD HHMM system.ext"
        → "YYYYMMDD HHMM mic.ext"
        Tries same extension first, then any supported extension.
        """
        stem_lower = remote_path.stem.lower()
        # Strip the track-name suffix to get the timestamp prefix
        for track in self._REMOTE_TRACK_NAMES:
            if stem_lower.endswith(track):
                prefix = stem_lower[: -len(track)].rstrip()
                break
        else:
            prefix = stem_lower.rstrip()

        for ext in [remote_path.suffix] + list(self.RECORDING_EXTENSIONS - {remote_path.suffix}):
            for candidate in self._dir.iterdir():
                if (
                    "mic" in candidate.name.lower()
                    and candidate.suffix.lower() == ext.lower()
                    and candidate.stem.lower().replace("mic", "").rstrip() == prefix
                ):
                    return candidate
        return None

    def _is_settled(self, path: Path, now: float) -> bool:
        """Return True if path exists, has size > 0, and mtime has been stable."""
        try:
            st = path.stat()
        except OSError:
            return False
        if st.st_size == 0:
            return False
        signature = (st.st_mtime_ns, st.st_size)
        prev = self._seen.get(path)
        if prev is None:
            self._seen[path] = signature
            return False
        if signature != prev:
            self._seen[path] = signature
            return False
        return (now - st.st_mtime) >= self.SETTLE_SECS

    async def poll_once(self) -> None:
        if not self._retry_state_available:
            return
        if not self._dir.exists():
            log.debug(f"[{DAEMON_NAME}] Recordings dir does not exist: {self._dir}")
            return

        now = datetime.now(tz=UTC).timestamp()

        processed = 0
        for path in sorted(self._dir.iterdir()):
            if not self._is_eligible_file(path):
                continue
            if path in self._transcribed:
                continue
            if path in self._hard_failed:
                # Permanent, deterministic rejection (e.g.
                # ERR_FILE_PATH_IS_SERVER_LOCAL) — retrying would not change
                # the outcome, only regenerate the alert storm this guards
                # against (ateles#1083). Notify exactly once, then skip on
                # every later poll.
                if not self._hard_failed[path].get("notified"):
                    self._notifier.send(
                        f"Transcription permanently failed for {path.name}: "
                        f"{self._hard_failed[path].get('error_code', 'unknown')} "
                        "(will not retry; see retry journal)",
                        priority=Priority.BLOCKER,
                        handler=DAEMON_NAME,
                    )
                    self._mark_hard_failure_notified(path)
                continue
            if self._pending_retries.get(path, 0) > now:
                continue
            # Backlog guard: an old file is never a fresh recording. Checked
            # before settling so a stale archive file is not even logged.
            if not self._is_within_age_window(path, now):
                continue

            # Ensure remote file is settled
            if not self._is_settled(path, now):
                if path not in self._seen:
                    log.info(f"[{DAEMON_NAME}] New recording detected (settling): {path.name}")
                continue

            # Find matching mic file. Unpaired watchers (Voice Memos) are
            # always single-file — never attempt the two-file merge path.
            mic_path = self._find_mic_pair(path) if self._paired else None

            # If mic file exists, wait for it to settle too
            if mic_path is not None and not self._is_settled(mic_path, now):
                log.debug(f"[{DAEMON_NAME}] Waiting for mic file to settle: {mic_path.name}")
                continue

            # Voice Memos record retry eligibility durably before invoking a
            # backend, then clear it only after successful transcription.
            durable_retry = self._retry_state_path is not None
            if durable_retry:
                if path not in self._pending_retries and not self._mark_pending(path):
                    continue
            log.info(
                f"[{DAEMON_NAME}] Recording settled, transcribing: {path.name}"
                + (f" + {mic_path.name}" if mic_path else " (remote only)")
            )
            outcome, error_code = await self._handle_recording(path, mic_path)
            if outcome == "success":
                self._transcribed.add(path)
                if durable_retry:
                    if not self._clear_pending(path):
                        self._transcribed.discard(path)
                else:
                    self._pending_retries.pop(path, None)
            elif outcome == "permanent" and durable_retry:
                # Only the durably-journaled watcher (Voice Memos) can make a
                # failure permanent; other watchers have no journal to record
                # it in and fall through to the transient path below.
                self._mark_hard_failed(path, error_code or "unknown")
                self._notifier.send(
                    f"Transcription permanently failed for {path.name}: "
                    f"{error_code} (will not retry)",
                    priority=Priority.BLOCKER,
                    handler=DAEMON_NAME,
                )
                self._mark_hard_failure_notified(path)
            else:
                retry_after = datetime.now(tz=UTC).timestamp() + self._retry_secs
                if durable_retry:
                    self._mark_pending(path, retry_after)
                else:
                    self._pending_retries[path] = retry_after
            processed += 1
            if (
                self._max_files_per_poll is not None
                and processed >= self._max_files_per_poll
            ):
                break

    async def _handle_recording(
        self, remote_path: Path, mic_path: Path | None
    ) -> tuple[str, str | None]:
        """Transcribe one recording.

        Returns ``(outcome, error_code)`` where ``outcome`` is one of
        ``"success"``, ``"permanent"`` (deterministic rejection — caller
        should stop retrying and journal it), or ``"transient"`` (worth
        retrying later). ``error_code`` is set only for ``"permanent"``.
        """
        label = remote_path.name + (f" + {mic_path.name}" if mic_path else "")
        self._notifier.send(
            f"Auto-transcribing: {label}",
            priority=Priority.INFO,
            handler=DAEMON_NAME,
        )
        transcription_entity_id: str | None = None
        try:
            transcription_entity_id = await asyncio.to_thread(
                self._run_transcription, remote_path, mic_path
            )
        except Exception as exc:
            log.error(
                f"[{DAEMON_NAME}] Transcription error for {label}: {exc}",
                exc_info=True,
            )
            error_code = _classify_store_error(exc)
            if error_code is None:
                # Only alert here for transient failures; a permanent one is
                # reported once by the caller after it journals the failure,
                # so this path must not also alert (ateles#1083 — duplicate
                # per-attempt alerts are exactly what rate-limited Telegram).
                self._notifier.send(
                    f"Transcription failed for {label}: {exc}",
                    priority=Priority.BLOCKER,
                    handler=DAEMON_NAME,
                )
                return "transient", None
            return "permanent", error_code

        if ANALYZE_ENABLED:
            try:
                await asyncio.to_thread(
                    _run_analysis, remote_path, transcription_entity_id, self._notifier
                )
            except Exception as exc:
                log.error(
                    f"[{DAEMON_NAME}] Analysis error for {label}: {exc}",
                    exc_info=True,
                )
                self._notifier.send(
                    f"Meeting analysis failed for {label}: {exc}",
                    priority=Priority.BLOCKER,
                    handler=DAEMON_NAME,
                )
        return "success", None

    def _run_transcription(self, remote_path: Path, mic_path: Path | None) -> str | None:
        """
        Run transcription and return the Neotoma transcription entity ID (or None).
        Raises RuntimeError on unrecoverable failure.
        """
        if not TRANSCRIBE_SCRIPT.exists():
            raise RuntimeError(
                f"transcribe_audio.py not found: {TRANSCRIBE_SCRIPT}"
            )

        python = _find_venv_python()
        identity = agent_identity(DAEMON_NAME)
        if identity is None or identity["sub"] != f"{DAEMON_NAME}@ateles-swarm":
            raise RuntimeError(
                "Tyto transcription requires its own AAuth identity; configure "
                "ATELES_AAUTH_KEYS_DIR and remove a conflicting NEOTOMA_AAUTH_SUB."
            )
        # The CLI's shared default identity may belong to an interactive agent.
        # Pin all nested transcription queries and stores to this daemon's grant.
        transcription_env = dict(
            os.environ,
            NEOTOMA_AAUTH_PRIVATE_JWK_PATH=identity["key"],
            NEOTOMA_AAUTH_SUB=identity["sub"],
            NEOTOMA_AAUTH_KID=identity["kid"],
            NEOTOMA_CLI_SCRIPT=str(Path(NEOTOMA_RC_DIR) / "dist" / "cli" / "bootstrap.js"),
        )
        _verify_transcription_cli_runtime(transcription_env)
        has_elevenlabs = bool(os.environ.get("ELEVENLABS_API_KEY", "").strip())

        def _extract_entity_id(stdout: str) -> str | None:
            for line in stdout.splitlines():
                if line.startswith("NEOTOMA_TRANSCRIPTION_ENTITY_ID="):
                    return line.split("=", 1)[1].strip() or None
            return None

        def _extract_backend(stdout: str, fallback: str) -> str:
            backend_to_engine = {
                "local": "local_whisper_cpp",
                "elevenlabs": "elevenlabs_stt",
                "openai": "openai_whisper_api",
            }
            for line in stdout.splitlines():
                if line.startswith("TRANSCRIPTION_ENGINE="):
                    return line.split("=", 1)[1].strip() or fallback
                if line.startswith("TRANSCRIPTION_BACKEND_SELECTED="):
                    selected = line.split("=", 1)[1].strip()
                    return backend_to_engine.get(selected, selected) or fallback
            return fallback

        def _run_command(cmd: list[str], fallback_backend: str) -> subprocess.CompletedProcess[str]:
            try:
                return subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=TRANSCRIBE_PROCESS_TIMEOUT_SECS,
                    env=transcription_env,
                )
            except subprocess.TimeoutExpired as exc:
                stdout = exc.stdout or ""
                if isinstance(stdout, bytes):
                    stdout = stdout.decode(errors="replace")
                backend = _extract_backend(stdout, fallback_backend)
                raise RuntimeError(
                    f"backend={backend}: transcription timed out after "
                    f"{TRANSCRIBE_PROCESS_TIMEOUT_SECS}s"
                ) from exc

        backend_override = os.environ.get("TRANSCRIBE_BACKEND", "").strip().lower()
        diarization_override = os.environ.get("RECORD_MEETING_DIARIZE", "").strip()
        paired_elevenlabs = backend_override == "elevenlabs" or (
            not backend_override and diarization_override != "0"
        )

        # Two-file merge path: mic + remote, requires ElevenLabs for word timestamps
        if (
            mic_path is not None
            and mic_path.exists()
            and has_elevenlabs
            and paired_elevenlabs
        ):
            log.info(f"[{DAEMON_NAME}] Two-file merge mode: [You] + diarized remote.")
            cmd = [
                python, str(TRANSCRIBE_SCRIPT), str(remote_path),
                "--mic-file", str(mic_path),
                "--capture-method", self._capture_method,
            ]
            result = _run_command(cmd, "elevenlabs_stt")
            if result.returncode == 0:
                entity_id = _extract_entity_id(result.stdout)
                backend = _extract_backend(result.stdout, "elevenlabs_stt")
                log.info(
                    f"[{DAEMON_NAME}] Two-file transcription complete. "
                    f"entity_id={entity_id}"
                )
                self._notifier.send(
                    f"Transcription complete (backend={backend}, "
                    f"[You]+diarized): {remote_path.name}",
                    priority=Priority.INFO,
                    handler=DAEMON_NAME,
                )
                return entity_id
            log.warning(
                f"[{DAEMON_NAME}] Two-file transcription failed "
                f"(rc={result.returncode}): {result.stderr.strip()[:300]}"
                " — falling back to remote-only diarization."
            )
        elif mic_path is not None and mic_path.exists() and not paired_elevenlabs:
            log.info(
                f"[{DAEMON_NAME}] Explicit backend/diarization override disables "
                "the two-file ElevenLabs path; transcribing the system track only."
            )

        # Single-file fallback: remote only, diarized or plain
        cmd = [
            python, str(TRANSCRIBE_SCRIPT), str(remote_path),
            "--capture-method", self._capture_method,
        ]
        if self._capture_method == "voice_memo":
            cmd.extend(["--backend", "local"])
            log.info(f"[{DAEMON_NAME}] Voice Memo local transcription mode.")
        elif not backend_override and diarization_override == "1":
            cmd.append("--diarize")
            log.info(f"[{DAEMON_NAME}] Explicit single-file diarization mode.")
        elif not backend_override and diarization_override == "0":
            cmd.append("--no-diarize")
            log.info(f"[{DAEMON_NAME}] Explicit single-file local mode.")
        else:
            log.info(f"[{DAEMON_NAME}] Single-file audio-directed transcription mode.")

        fallback = {
            "local": "local_whisper_cpp",
            "elevenlabs": "elevenlabs_stt",
            "openai": "openai_whisper_api",
        }.get(
            backend_override,
            "local_whisper_cpp" if self._capture_method == "voice_memo" else "unknown",
        )
        result = _run_command(cmd, fallback)
        if result.returncode != 0:
            log.warning(
                f"[{DAEMON_NAME}] Transcription failed (rc={result.returncode}): "
                f"{result.stderr.strip()[:300]}"
            )
            if "--diarize" in cmd:
                log.info(f"[{DAEMON_NAME}] Retrying without diarization...")
                cmd_fallback = [
                    python, str(TRANSCRIBE_SCRIPT), str(remote_path), "--no-diarize",
                    "--capture-method", self._capture_method,
                ]
                result2 = _run_command(cmd_fallback, "local_whisper_cpp")
                if result2.returncode != 0:
                    backend = _extract_backend(
                        result2.stdout, "local_whisper_cpp"
                    )
                    raise RuntimeError(
                        f"backend={backend}: fallback transcription also failed: "
                        f"{result2.stderr.strip()[:300]}"
                    )
                entity_id = _extract_entity_id(result2.stdout)
                backend = _extract_backend(result2.stdout, "local_whisper_cpp")
                log.info(f"[{DAEMON_NAME}] Fallback transcription succeeded. entity_id={entity_id}")
                self._notifier.send(
                    f"Transcription complete (backend={backend}, no diarization): "
                    f"{remote_path.name}",
                    priority=Priority.INFO,
                    handler=DAEMON_NAME,
                )
                return entity_id
            else:
                backend = _extract_backend(result.stdout, fallback)
                raise RuntimeError(
                    f"backend={backend}: {result.stderr.strip()[:300]}"
                )
        else:
            entity_id = _extract_entity_id(result.stdout)
            backend = _extract_backend(result.stdout, fallback)
            log.info(
                f"[{DAEMON_NAME}] Transcription complete: {remote_path.name} "
                f"entity_id={entity_id}"
            )
            self._notifier.send(
                f"Transcription complete (backend={backend}): {remote_path.name}",
                priority=Priority.INFO,
                handler=DAEMON_NAME,
            )
            return entity_id


# ── Main ──────────────────────────────────────────────────────────────────────


def _verify_transcription_cli_runtime(env: dict[str, str]) -> None:
    """Fail before STT if the selected CLI cannot honor a per-agent key path."""
    script = Path(env["NEOTOMA_CLI_SCRIPT"])
    if not script.is_file():
        raise RuntimeError("Tyto transcription requires a deployed Neotoma CLI bootstrap.")
    signer = script.with_name("aauth_signer.js")
    check = """
        const { pathToFileURL } = await import('node:url');
        const { resolve } = await import('node:path');
        const signer = await import(pathToFileURL(process.argv[1]).href);
        if (typeof signer.resolveCliPrivateJwkPath !== 'function' ||
            resolve(signer.resolveCliPrivateJwkPath()) !==
            resolve(process.env.NEOTOMA_AAUTH_PRIVATE_JWK_PATH)) process.exit(1);
    """
    result = subprocess.run(
        [env.get("NODE_BIN", "node"), "--input-type=module", "-e", check, str(signer)],
        env=env, capture_output=True, text=True, timeout=20,
    )
    if result.returncode:
        raise RuntimeError(
            "Tyto transcription requires a deployed Neotoma CLI supporting "
            "per-agent key paths; update NEOTOMA_RC_DIR before retrying."
        )


async def _poll_watcher_forever(
    name: str,
    poll_once: Any,
    *,
    interval: float = POLL_INTERVAL,
) -> None:
    """Poll one source independently so another source cannot starve it."""
    while True:
        try:
            await poll_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error(f"[{DAEMON_NAME}] {name} poll error: {exc}", exc_info=True)
        await asyncio.sleep(interval)


async def main() -> None:
    log.info(f"[{DAEMON_NAME}] Starting up...")

    # 1. Load agent_definition from Neotoma
    agent_def = AgentLoader(DAEMON_NAME).load()
    log.info(
        f"[{DAEMON_NAME}] agent_definition: status={agent_def.status} "
        f"grant={agent_def.agent_grant} sub={agent_def.aauth_sub}"
    )

    # Enforce agent_definition.status (ateles#562). Previously this value was
    # logged and then ignored, so a "retired" agent ran normally.
    enforce_status_or_exit(agent_def, DAEMON_NAME)

    # 2. Load AAuth signer
    signer = AAuthSigner.from_key_file(DAEMON_NAME)
    if signer.is_stub:
        log.warning(
            f"[{DAEMON_NAME}] AAuth keypair not minted yet — "
            "observations attributed to operator token"
        )

    # 3. Load notification rubric — route to TELEGRAM_TOPIC_TYTO (Cyphorhinus bot thread)
    notifier = Notifier.from_neotoma(telegram_topic_env="TELEGRAM_TOPIC_TYTO")

    # 4. Validate watch dirs
    if not SCREENSHOTS_DIR.exists():
        log.warning(
            f"[{DAEMON_NAME}] Screenshots dir does not exist: {SCREENSHOTS_DIR} "
            "— will retry on each poll"
        )
    else:
        log.info(f"[{DAEMON_NAME}] Watching screenshots: {SCREENSHOTS_DIR}")

    if TRANSCRIBE_ENABLED:
        if not RECORDINGS_DIR.exists():
            log.warning(
                f"[{DAEMON_NAME}] Recordings dir does not exist: {RECORDINGS_DIR} "
                "— will retry on each poll"
            )
        else:
            log.info(f"[{DAEMON_NAME}] Watching recordings: {RECORDINGS_DIR}")
        if NATIVE_RECORDINGS_DIR is not None:
            if not NATIVE_RECORDINGS_DIR.exists():
                log.warning(
                    f"[{DAEMON_NAME}] Native recordings dir does not exist: "
                    f"{NATIVE_RECORDINGS_DIR} — will retry on each poll"
                )
            else:
                log.info(
                    f"[{DAEMON_NAME}] Watching native recordings (platform_native): "
                    f"{NATIVE_RECORDINGS_DIR}"
                )
        if VOICE_MEMOS_DIR is not None:
            if not VOICE_MEMOS_DIR.exists():
                log.warning(
                    f"[{DAEMON_NAME}] Voice Memos dir does not exist: "
                    f"{VOICE_MEMOS_DIR} — will retry on each poll"
                )
            else:
                log.info(
                    f"[{DAEMON_NAME}] Watching voice memos (voice_memo): "
                    f"{VOICE_MEMOS_DIR} "
                    f"(max age {VOICE_MEMO_MAX_AGE_SECS}s, "
                    f"qta={'on' if VOICE_MEMO_INCLUDE_QTA else 'off'})"
                )
    else:
        log.info(f"[{DAEMON_NAME}] Auto-transcription disabled (TYTO_TRANSCRIBE_ENABLED=0)")

    # 5. Notify startup
    notifier.send(
        f"{DAEMON_NAME} started — screenshots: {SCREENSHOTS_DIR}, "
        f"recordings: {RECORDINGS_DIR if TRANSCRIBE_ENABLED else 'disabled'}, "
        f"poll: {POLL_INTERVAL}s",
        priority=Priority.INFO,
        handler=DAEMON_NAME,
    )

    # 6. Poll loop
    screenshot_watcher = ScreenshotWatcher(SCREENSHOTS_DIR, notifier)
    recording_watchers: list[RecordingWatcher] = []
    if TRANSCRIBE_ENABLED:
        recording_watchers.append(
            RecordingWatcher(
                RECORDINGS_DIR,
                notifier,
                capture_method="audio_hijack_system",
                max_files_per_poll=1,
            )
        )
        if NATIVE_RECORDINGS_DIR is not None:
            recording_watchers.append(
                RecordingWatcher(
                    NATIVE_RECORDINGS_DIR,
                    notifier,
                    capture_method="platform_native",
                    max_files_per_poll=1,
                )
            )
        if VOICE_MEMOS_DIR is not None:
            memo_exts = {".m4a", ".wav"} | ({".qta"} if VOICE_MEMO_INCLUDE_QTA else set())
            recording_watchers.append(
                RecordingWatcher(
                    VOICE_MEMOS_DIR,
                    notifier,
                    capture_method="voice_memo",
                    paired=False,
                    extensions=memo_exts,
                    max_age_secs=VOICE_MEMO_MAX_AGE_SECS,
                    seed_existing=True,
                    retry_state_path=VOICE_MEMO_RETRY_STATE,
                    retry_secs=VOICE_MEMO_RETRY_SECS,
                )
            )
    log.info(f"[{DAEMON_NAME}] Poll interval: {POLL_INTERVAL}s")

    loops = [
        _poll_watcher_forever("screenshots", screenshot_watcher.poll_once),
        *(
            _poll_watcher_forever(
                f"recordings:{watcher._capture_method}", watcher.poll_once
            )
            for watcher in recording_watchers
        ),
    ]
    await asyncio.gather(*loops)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info(f"[{DAEMON_NAME}] Stopped by operator.")
