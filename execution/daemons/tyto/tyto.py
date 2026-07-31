#!/usr/bin/env python3
"""
Tyto — Screenshot watcher + meeting recording transcription daemon.

Tyto genus: barn owls. T3 daemon in the Ateles swarm.

Tyto watches two directories:
  1. TYTO_SCREENSHOTS_DIR — new image files (PNG/JPG/etc.), stored as
     `screenshot` entities in Neotoma. A second in-process consumer
     (OcrConsumer) then polls Neotoma for `status:"pending_ocr"` screenshots
     and dispatches OCR + entity extraction (Phase 3, this consumer).
  2. TYTO_RECORDINGS_DIR — new meeting recording files (*remote*.aac/.m4a),
     auto-transcribed via transcribe_audio.py --diarize immediately on
     detection (with --no-diarize fallback). Optionally also
     TYTO_NATIVE_RECORDINGS_DIR for platform-native (Zoom/Meet/Teams)
     recordings, which carry the platform's built-in consent disclosure.
     Each transcription is stamped with capture_method for consent auditing.

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
  TYTO_TRANSCRIBE_ENABLED   Set to 0 to disable auto-transcription (default: 1)
  TYTO_TRANSCRIBE_SCRIPT    Path to transcribe_audio.py (auto-detected from repo root)
  ELEVENLABS_API_KEY        When set, enables diarization via ElevenLabs
  RECORD_MEETING_DIARIZE    Set to 0 to force plain transcription (default: 1)
  TYTO_ANALYZE_ENABLED      Set to 0 to disable post-transcription meeting analysis (default: 1)
  TYTO_ANALYZE_MEETING_SKILL  Path to analyze-meeting/SKILL.md (auto-detected from repo root)
  TYTO_ANALYZE_NEOTOMA_SKILL  Path to analyze-neotoma-feedback/SKILL.md (auto-detected)
  TYTO_OCR_ENABLED          Set to 0 to disable the pending_ocr screenshot consumer (default: 1)
  TYTO_OCR_SKILL            Path to tyto-ocr/SKILL.md (auto-detected from repo root)
  TYTO_OCR_CLAUDE_BIN       Absolute path to `claude` binary (default: auto-detect on PATH)
  TYTO_OCR_DISPATCH_TIMEOUT Per-screenshot OCR dispatch timeout in seconds (default: 300)
  TYTO_OCR_POLL_INTERVAL    Interval (s) between pending_ocr polls (default: TYTO_POLL_INTERVAL)
  TYTO_OCR_BATCH_SIZE       Max pending_ocr screenshots processed per poll cycle (default: 10)
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

import httpx

# ── Path bootstrap ────────────────────────────────────────────────────────────
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from lib.daemon_runtime import (  # noqa: E402
    AAuthSigner,
    AgentLoader,
)
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

# Diarization: enabled by default when ELEVENLABS_API_KEY is set
def _should_diarize() -> bool:
    if os.environ.get("RECORD_MEETING_DIARIZE", "1") == "0":
        return False
    return bool(os.environ.get("ELEVENLABS_API_KEY", ""))

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

# ── OCR consumer config ───────────────────────────────────────────────────────
OCR_ENABLED = os.environ.get("TYTO_OCR_ENABLED", "1") != "0"
_default_ocr_skill = str(_REPO_ROOT / ".claude" / "skills" / "tyto-ocr" / "SKILL.md")
OCR_SKILL_PATH = Path(os.environ.get("TYTO_OCR_SKILL", _default_ocr_skill))
OCR_CLAUDE_BIN = os.environ.get("TYTO_OCR_CLAUDE_BIN") or None  # resolved lazily via _find_claude_bin()
OCR_DISPATCH_TIMEOUT_SECONDS = int(os.environ.get("TYTO_OCR_DISPATCH_TIMEOUT", "300"))
OCR_POLL_INTERVAL = int(os.environ.get("TYTO_OCR_POLL_INTERVAL", str(POLL_INTERVAL)))
OCR_BATCH_SIZE = int(os.environ.get("TYTO_OCR_BATCH_SIZE", "10"))
OCR_RESULT_PREFIX = "TYTO_OCR_RESULT="


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
        (l.strip() for l in result.stdout.splitlines() if l.strip()), ""
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


# ── OCR consumer ──────────────────────────────────────────────────────────────


class OcrConsumer:
    """
    Polls Neotoma for `screenshot` entities with status:"pending_ocr" and
    dispatches OCR + entity extraction for each, via the tyto-ocr invocable
    agent (Formica-style `claude --print` subprocess dispatch).

    Drains the backlog: every poll cycle queries Neotoma directly (not local
    filesystem state), so pre-existing pending_ocr screenshots are picked up
    exactly like newly-arrived ones. Status transitions (pending_ocr →
    ocr_complete | ocr_failed) are the idempotency backstop — a screenshot
    already at a terminal status is excluded by the query filter and never
    re-dispatched.
    """

    def __init__(self, notifier: Notifier) -> None:
        self._notifier = notifier
        self._last_run_monotonic: float | None = None

    async def poll_once(self) -> None:
        if not OCR_ENABLED:
            return
        # OCR_POLL_INTERVAL may be configured independently of the daemon's
        # base POLL_INTERVAL (e.g. to throttle OCR dispatch cost separately
        # from screenshot/recording detection, which stay on POLL_INTERVAL).
        # _last_run_monotonic starts as None (not 0.0) so the first poll always
        # runs — loop.time() is anchored to the system monotonic clock, which
        # can be under OCR_POLL_INTERVAL on a freshly-booted CI runner and would
        # otherwise wrongly gate the very first call.
        now = asyncio.get_event_loop().time()
        if self._last_run_monotonic is not None and now - self._last_run_monotonic < OCR_POLL_INTERVAL:
            return
        self._last_run_monotonic = now

        screenshots = await asyncio.to_thread(_retrieve_pending_ocr_screenshots)
        if not screenshots:
            return
        # Fetched once per poll cycle, not once per screenshot — the candidate
        # task list doesn't change mid-cycle and a batch of 10 screenshots
        # would otherwise re-fetch up to 200 tasks 10 times over.
        candidate_tasks = await asyncio.to_thread(_retrieve_candidate_tasks)
        for entity_id, snapshot in screenshots:
            await self._process_screenshot(entity_id, snapshot, candidate_tasks)

    async def _process_screenshot(
        self, entity_id: str, snapshot: dict, candidate_tasks: list[dict]
    ) -> None:
        source_path = snapshot.get("source_path", "")
        log.info(f"[{DAEMON_NAME}] OCR dispatch starting: {entity_id} ({source_path})")
        try:
            result = await asyncio.to_thread(
                self._run_ocr, entity_id, source_path, candidate_tasks
            )
        except Exception as exc:
            log.error(
                f"[{DAEMON_NAME}] OCR dispatch error for {entity_id}: {exc}",
                exc_info=True,
            )
            result = {"ok": False, "error": str(exc)}

        if not result.get("ok"):
            error = result.get("error", "unknown error")
            log.warning(f"[{DAEMON_NAME}] OCR failed for {entity_id}: {error}")
            await asyncio.to_thread(_set_screenshot_status, entity_id, "ocr_failed")
            self._notifier.send(
                f"OCR failed for screenshot {entity_id}: {error}",
                priority=Priority.WARN,
                handler=DAEMON_NAME,
            )
            return

        ocr_text = result.get("ocr_text", "") or ""
        extracted_entities = result.get("extracted_entities", []) or []
        matched_task_entity_id = result.get("matched_task_entity_id")

        stored_ok = await asyncio.to_thread(
            _store_ocr_results, entity_id, ocr_text, extracted_entities
        )
        if not stored_ok:
            log.warning(f"[{DAEMON_NAME}] Failed to store OCR results for {entity_id}")
            await asyncio.to_thread(_set_screenshot_status, entity_id, "ocr_failed")
            self._notifier.send(
                f"OCR result store failed for screenshot {entity_id}",
                priority=Priority.WARN,
                handler=DAEMON_NAME,
            )
            return

        if matched_task_entity_id:
            linked_ok = await asyncio.to_thread(
                _link_screenshot_to_task, entity_id, matched_task_entity_id
            )
            if not linked_ok:
                # A failed task link is not an OCR failure — per PM acceptance
                # criteria, screenshots still complete when no link is made.
                # Surface it (rather than only debug-logging inside
                # _link_screenshot_to_task) since it's otherwise a silent,
                # unrecoverable miss once status reaches a terminal value.
                log.warning(
                    f"[{DAEMON_NAME}] REFERS_TO link failed for {entity_id} -> "
                    f"{matched_task_entity_id}; completing without the link"
                )
                self._notifier.send(
                    f"Screenshot {entity_id} OCR'd but task link failed "
                    f"(-> {matched_task_entity_id})",
                    priority=Priority.WARN,
                    handler=DAEMON_NAME,
                )

        await asyncio.to_thread(_set_screenshot_status, entity_id, "ocr_complete")
        log.info(
            f"[{DAEMON_NAME}] OCR complete for {entity_id} "
            f"({len(extracted_entities)} entities extracted"
            + (f", linked task {matched_task_entity_id}" if matched_task_entity_id else "")
            + ")"
        )
        self._notifier.send(
            f"Screenshot OCR'd: {snapshot.get('filename', entity_id)}",
            priority=Priority.INFO,
            handler=DAEMON_NAME,
        )

    def _run_ocr(self, entity_id: str, source_path: str, candidate_tasks: list[dict]) -> dict:
        """
        Dispatch the tyto-ocr skill via `claude --print` and parse its
        TYTO_OCR_RESULT= stdout line. Returns a dict with at least {"ok": bool}.
        Raises on subprocess-level failures the caller should treat as ocr_failed.
        """
        claude_bin = OCR_CLAUDE_BIN or _find_claude_bin()
        if not claude_bin:
            return {"ok": False, "error": "claude binary not found on PATH"}
        if not OCR_SKILL_PATH.exists():
            return {"ok": False, "error": f"tyto-ocr SKILL.md not found: {OCR_SKILL_PATH}"}

        if not source_path or not Path(source_path).exists():
            return {"ok": False, "error": f"source image missing: {source_path}"}
        try:
            if Path(source_path).stat().st_size == 0:
                return {"ok": False, "error": f"source image is zero-byte: {source_path}"}
        except OSError as exc:
            return {"ok": False, "error": f"source image unreadable: {exc}"}

        skill_md = OCR_SKILL_PATH.read_text(encoding="utf-8")
        prompt = (
            f"Image path: {source_path}\n"
            f"Screenshot entity_id: {entity_id}\n"
            f"Candidate tasks (id, title) to match against:\n"
            + "\n".join(f"- {t['entity_id']}: {t['title']}" for t in candidate_tasks)
        )

        subprocess_env = _ocr_subprocess_env()

        try:
            proc = subprocess.run(
                [claude_bin, "--print", "--append-system-prompt", skill_md],
                input=prompt,
                capture_output=True,
                text=True,
                timeout=OCR_DISPATCH_TIMEOUT_SECONDS,
                env=subprocess_env,
            )
        except subprocess.TimeoutExpired:
            return {
                "ok": False,
                "error": f"OCR dispatch timed out after {OCR_DISPATCH_TIMEOUT_SECONDS}s",
            }

        if proc.returncode != 0:
            stderr_text = (proc.stderr or "").strip()[:500]
            return {
                "ok": False,
                "error": f"OCR dispatch failed (rc={proc.returncode}): {stderr_text}",
            }

        return self._parse_result(proc.stdout or "")

    @staticmethod
    def _parse_result(stdout: str) -> dict:
        for line in stdout.splitlines():
            if line.startswith(OCR_RESULT_PREFIX):
                raw = line[len(OCR_RESULT_PREFIX):].strip()
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, dict) and "ok" in parsed:
                        return parsed
                except (ValueError, TypeError):
                    pass
                return {"ok": False, "error": "malformed TYTO_OCR_RESULT JSON"}
        return {"ok": False, "error": "no TYTO_OCR_RESULT line in OCR output"}


def _ocr_subprocess_env() -> dict[str, str]:
    """
    Environment for the headless `claude --print` OCR subprocess.

    Starts from the full parent environment — matching every other `claude
    --print` dispatch site in this repo (formica.py, apis/skill_runner.py,
    phoenicurus-release/prepare.py). A narrow allowlist was tried and reverted:
    `claude` needs more than PATH/HOME to authenticate and run (config dirs,
    OAuth/API-key env vars), and phoenicurus-release's prepare.py docstring
    records a production incident from exactly this kind of narrower assumption.

    Prefer the operator's Claude subscription (CLAUDE_CODE_OAUTH_TOKEN) over a
    pay-per-token API key, matching the same precedent (both set → API key
    wins and can silently fail on an unfunded account).

    NEOTOMA_BEARER_TOKEN is explicitly dropped: the OCR skill only reads an
    image and returns structured JSON on stdout — it has no need to call
    Neotoma directly (Tyto performs all Neotoma writes itself), so the
    credential is not extended to a subprocess reading arbitrary,
    potentially-sensitive screenshot content. See ateles#302 Security/Arch review.
    """
    env = dict(os.environ)
    if env.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip():
        env.pop("ANTHROPIC_API_KEY", None)
    env.pop("NEOTOMA_BEARER_TOKEN", None)
    return env


def _neotoma_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {NEOTOMA_BEARER_TOKEN}"}


def _retrieve_pending_ocr_screenshots() -> list[tuple[str, dict]]:
    """
    Query Neotoma for screenshot entities with status:"pending_ocr", server-side
    filtered (not fetched-then-filtered), so the query stays scoped and drains
    the full backlog rather than only newly-arrived screenshots.
    """
    if not NEOTOMA_BEARER_TOKEN:
        return []
    try:
        resp = httpx.post(
            f"{NEOTOMA_BASE_URL}/entities/query",
            json={
                "entity_type": "screenshot",
                "snapshot_filters": {"status": {"op": "eq", "value": "pending_ocr"}},
                "limit": OCR_BATCH_SIZE,
                "include_snapshots": True,
            },
            headers=_neotoma_headers(),
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        log.warning(f"[{DAEMON_NAME}] pending_ocr query failed: {exc}")
        return []

    results: list[tuple[str, dict]] = []
    for entity in data.get("entities", []):
        entity_id = entity.get("entity_id") or entity.get("id")
        snapshot = (entity.get("snapshot") or {}).get("snapshot")
        if not isinstance(snapshot, dict):
            snapshot = entity.get("snapshot") if isinstance(entity.get("snapshot"), dict) else {}
        if entity_id and snapshot:
            results.append((entity_id, snapshot))
    return results


def _retrieve_candidate_tasks() -> list[dict]:
    """Fetch a bounded set of open task entities to match OCR'd text against."""
    if not NEOTOMA_BEARER_TOKEN:
        return []
    try:
        resp = httpx.post(
            f"{NEOTOMA_BASE_URL}/entities/query",
            json={"entity_type": "task", "limit": 200, "include_snapshots": True},
            headers=_neotoma_headers(),
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        log.warning(f"[{DAEMON_NAME}] candidate task query failed: {exc}")
        return []

    candidates: list[dict] = []
    for entity in data.get("entities", []):
        entity_id = entity.get("entity_id") or entity.get("id")
        snapshot = (entity.get("snapshot") or {}).get("snapshot")
        if not isinstance(snapshot, dict):
            snapshot = entity.get("snapshot") if isinstance(entity.get("snapshot"), dict) else {}
        title = snapshot.get("title") if isinstance(snapshot, dict) else None
        if entity_id and title:
            candidates.append({"entity_id": entity_id, "title": title})
    return candidates


def _set_screenshot_status(entity_id: str, status: str) -> bool:
    """Transition a screenshot entity's status via /correct. Fail-open (False)."""
    if not NEOTOMA_BEARER_TOKEN:
        return False
    try:
        resp = httpx.post(
            f"{NEOTOMA_BASE_URL}/correct",
            json={
                "entity_id": entity_id,
                "entity_type": "screenshot",
                "field": "status",
                "value": status,
                "idempotency_key": f"tyto-ocr-status-{entity_id}-{status}",
            },
            headers=_neotoma_headers(),
            timeout=15,
        )
        resp.raise_for_status()
        return True
    except Exception as exc:
        log.warning(f"[{DAEMON_NAME}] status transition to {status} failed for {entity_id}: {exc}")
        return False


def _store_ocr_results(entity_id: str, ocr_text: str, extracted_entities: list[dict]) -> bool:
    """
    Persist OCR text + extracted entities onto the screenshot entity via
    /store, keyed with a deterministic idempotency_key so a re-poll after a
    partial failure does not create a duplicate write.
    """
    if not NEOTOMA_BEARER_TOKEN:
        return False
    try:
        resp = httpx.post(
            f"{NEOTOMA_BASE_URL}/store",
            json={
                "entities": [
                    {
                        "entity_type": "screenshot",
                        "entity_id": entity_id,
                        "ocr_text": ocr_text,
                        "ocr_extracted_entities": extracted_entities,
                        "ocr_completed_at": datetime.now(tz=UTC).isoformat(),
                    }
                ],
                "idempotency_key": f"tyto-ocr-{entity_id}",
                "observation_source": "workflow_state",
            },
            headers=_neotoma_headers(),
            timeout=15,
        )
        resp.raise_for_status()
        return True
    except Exception as exc:
        log.warning(f"[{DAEMON_NAME}] OCR result store failed for {entity_id}: {exc}")
        return False


def _refers_to_exists(source_entity_id: str, target_entity_id: str) -> bool:
    """
    Check for an existing REFERS_TO edge before creating a new one (re-poll
    safety). Uses GET /entities/:id/relationships — the documented read path
    (see docs/developer/neotoma_finances_dashboard_neotoma_side_diagnostics.md)
    — rather than a standalone query endpoint, which does not exist.
    """
    if not NEOTOMA_BEARER_TOKEN:
        return False
    try:
        resp = httpx.get(
            f"{NEOTOMA_BASE_URL}/entities/{source_entity_id}/relationships",
            headers=_neotoma_headers(),
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        relationships = data.get("relationships", [])
    except Exception as exc:
        log.debug(f"[{DAEMON_NAME}] relationship existence check failed (assuming absent): {exc}")
        return False

    for rel in relationships:
        if (
            rel.get("relationship_type") == "REFERS_TO"
            and rel.get("target_entity_id") == target_entity_id
        ):
            return True
    return False


def _link_screenshot_to_task(screenshot_entity_id: str, task_entity_id: str) -> bool:
    """
    Create a REFERS_TO edge screenshot -> task, unless one already exists.
    Uses POST /create_relationship (matching the create_relationship MCP tool
    and turdus.py's existing precedent), not a fabricated /relationships path.
    """
    if not NEOTOMA_BEARER_TOKEN:
        return False
    if _refers_to_exists(screenshot_entity_id, task_entity_id):
        log.info(
            f"[{DAEMON_NAME}] REFERS_TO already exists: {screenshot_entity_id} -> {task_entity_id}"
        )
        return True
    try:
        resp = httpx.post(
            f"{NEOTOMA_BASE_URL}/create_relationship",
            json={
                "relationship_type": "REFERS_TO",
                "source_entity_id": screenshot_entity_id,
                "target_entity_id": task_entity_id,
            },
            headers=_neotoma_headers(),
            timeout=15,
        )
        resp.raise_for_status()
        return True
    except Exception as exc:
        log.warning(
            f"[{DAEMON_NAME}] REFERS_TO create failed {screenshot_entity_id} -> "
            f"{task_entity_id}: {exc}"
        )
        return False


# ── Recording watcher ─────────────────────────────────────────────────────────


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
    ) -> None:
        self._dir = watch_dir
        self._notifier = notifier
        # Consent-posture provenance stamped on every transcription this watcher
        # produces. "audio_hijack_system" = local system capture (no built-in
        # disclosure); "platform_native" = Zoom/Meet/Teams recording (carries the
        # platform's own consent notice). See record_meeting SKILL.md disclosure ladder.
        self._capture_method = capture_method
        self._seen: dict[Path, float] = {}    # path → mtime at first sight
        self._transcribed: set[Path] = set() # remote paths already transcribed

    # Audio Hijack recorder block names that represent the far-end / system audio track.
    # "remote" = original naming, "system" = after session rename to "Tyto".
    _REMOTE_TRACK_NAMES = ("remote", "system")

    def _is_remote_file(self, path: Path) -> bool:
        name_lower = path.name.lower()
        return (
            path.suffix.lower() in self.RECORDING_EXTENSIONS
            and any(t in name_lower for t in self._REMOTE_TRACK_NAMES)
            and "mic" not in name_lower  # never confuse mic track
        )

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
        prev = self._seen.get(path)
        if prev is None:
            self._seen[path] = st.st_mtime
            return False
        if st.st_mtime != prev:
            self._seen[path] = st.st_mtime
            return False
        return (now - st.st_mtime) >= self.SETTLE_SECS

    async def poll_once(self) -> None:
        if not self._dir.exists():
            log.debug(f"[{DAEMON_NAME}] Recordings dir does not exist: {self._dir}")
            return

        now = datetime.now(tz=UTC).timestamp()

        for path in sorted(self._dir.iterdir()):
            if not self._is_remote_file(path):
                continue
            if path in self._transcribed:
                continue

            # Ensure remote file is settled
            if not self._is_settled(path, now):
                if path not in self._seen:
                    log.info(f"[{DAEMON_NAME}] New recording detected (settling): {path.name}")
                continue

            # Find matching mic file
            mic_path = self._find_mic_pair(path)

            # If mic file exists, wait for it to settle too
            if mic_path is not None and not self._is_settled(mic_path, now):
                log.debug(f"[{DAEMON_NAME}] Waiting for mic file to settle: {mic_path.name}")
                continue

            # Both settled (or no mic file) — transcribe
            self._transcribed.add(path)
            log.info(
                f"[{DAEMON_NAME}] Recording settled, transcribing: {path.name}"
                + (f" + {mic_path.name}" if mic_path else " (remote only)")
            )
            await self._handle_recording(path, mic_path)

    async def _handle_recording(self, remote_path: Path, mic_path: Path | None) -> None:
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
            self._notifier.send(
                f"Transcription failed for {label}: {exc}",
                priority=Priority.BLOCKER,
                handler=DAEMON_NAME,
            )
            return  # don't attempt analysis if transcription failed

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

    def _run_transcription(self, remote_path: Path, mic_path: Path | None) -> str | None:
        """
        Run transcription and return the Neotoma transcription entity ID (or None).
        Raises RuntimeError on unrecoverable failure.
        """
        if not TRANSCRIBE_SCRIPT.exists():
            log.error(
                f"[{DAEMON_NAME}] transcribe_audio.py not found: {TRANSCRIBE_SCRIPT}"
            )
            return None

        python = _find_venv_python()
        has_elevenlabs = bool(os.environ.get("ELEVENLABS_API_KEY", "").strip())

        def _extract_entity_id(stdout: str) -> str | None:
            for line in stdout.splitlines():
                if line.startswith("NEOTOMA_TRANSCRIPTION_ENTITY_ID="):
                    return line.split("=", 1)[1].strip() or None
            return None

        # Two-file merge path: mic + remote, requires ElevenLabs for word timestamps
        if mic_path is not None and mic_path.exists() and has_elevenlabs:
            log.info(f"[{DAEMON_NAME}] Two-file merge mode: [You] + diarized remote.")
            cmd = [
                python, str(TRANSCRIBE_SCRIPT), str(remote_path),
                "--mic-file", str(mic_path),
                "--capture-method", self._capture_method,
            ]
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode == 0:
                entity_id = _extract_entity_id(result.stdout)
                log.info(
                    f"[{DAEMON_NAME}] Two-file transcription complete. "
                    f"entity_id={entity_id}"
                )
                self._notifier.send(
                    f"Transcription complete ([You]+diarized): {remote_path.name}",
                    priority=Priority.INFO,
                    handler=DAEMON_NAME,
                )
                return entity_id
            log.warning(
                f"[{DAEMON_NAME}] Two-file transcription failed "
                f"(rc={result.returncode}): {result.stderr.strip()[:300]}"
                " — falling back to remote-only diarization."
            )

        # Single-file fallback: remote only, diarized or plain
        cmd = [
            python, str(TRANSCRIBE_SCRIPT), str(remote_path),
            "--capture-method", self._capture_method,
        ]
        if _should_diarize():
            cmd.append("--diarize")
            log.info(f"[{DAEMON_NAME}] Single-file diarization mode.")
        else:
            log.info(f"[{DAEMON_NAME}] Single-file plain transcription mode.")

        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            log.warning(
                f"[{DAEMON_NAME}] Transcription failed (rc={result.returncode}): "
                f"{result.stderr.strip()[:300]}"
            )
            if _should_diarize():
                log.info(f"[{DAEMON_NAME}] Retrying without diarization...")
                cmd_fallback = [
                    python, str(TRANSCRIBE_SCRIPT), str(remote_path), "--no-diarize",
                    "--capture-method", self._capture_method,
                ]
                result2 = subprocess.run(cmd_fallback, capture_output=True, text=True)
                if result2.returncode != 0:
                    raise RuntimeError(
                        f"Fallback transcription also failed: {result2.stderr.strip()[:300]}"
                    )
                entity_id = _extract_entity_id(result2.stdout)
                log.info(f"[{DAEMON_NAME}] Fallback transcription succeeded. entity_id={entity_id}")
                self._notifier.send(
                    f"Transcription complete (no diarization): {remote_path.name}",
                    priority=Priority.INFO,
                    handler=DAEMON_NAME,
                )
                return entity_id
            else:
                raise RuntimeError(result.stderr.strip()[:300])
        else:
            entity_id = _extract_entity_id(result.stdout)
            log.info(
                f"[{DAEMON_NAME}] Transcription complete: {remote_path.name} "
                f"entity_id={entity_id}"
            )
            self._notifier.send(
                f"Transcription complete: {remote_path.name}",
                priority=Priority.INFO,
                handler=DAEMON_NAME,
            )
            return entity_id


# ── Main ──────────────────────────────────────────────────────────────────────


async def main() -> None:
    log.info(f"[{DAEMON_NAME}] Starting up...")

    # 1. Load agent_definition from Neotoma
    agent_def = AgentLoader(DAEMON_NAME).load()
    log.info(
        f"[{DAEMON_NAME}] agent_definition: status={agent_def.status} "
        f"grant={agent_def.agent_grant} sub={agent_def.aauth_sub}"
    )

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
    ocr_consumer = OcrConsumer(notifier)
    recording_watchers: list[RecordingWatcher] = []
    if TRANSCRIBE_ENABLED:
        recording_watchers.append(
            RecordingWatcher(RECORDINGS_DIR, notifier, capture_method="audio_hijack_system")
        )
        if NATIVE_RECORDINGS_DIR is not None:
            recording_watchers.append(
                RecordingWatcher(
                    NATIVE_RECORDINGS_DIR, notifier, capture_method="platform_native"
                )
            )
    log.info(f"[{DAEMON_NAME}] Poll interval: {POLL_INTERVAL}s")

    while True:
        try:
            await screenshot_watcher.poll_once()
        except Exception as exc:
            log.error(f"[{DAEMON_NAME}] Screenshot poll error: {exc}", exc_info=True)
        try:
            await ocr_consumer.poll_once()
        except Exception as exc:
            log.error(f"[{DAEMON_NAME}] OCR consumer poll error: {exc}", exc_info=True)
        for rw in recording_watchers:
            try:
                await rw.poll_once()
            except Exception as exc:
                log.error(f"[{DAEMON_NAME}] Recording poll error: {exc}", exc_info=True)
        await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info(f"[{DAEMON_NAME}] Stopped by operator.")
