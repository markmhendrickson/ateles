#!/usr/bin/env python3
"""
Mimus — Conversation-Corpus Content-Idea Daemon

Named after Mimus polyglottos (the northern mockingbird), which echoes back
the songs it hears — fitting for a daemon that listens to the conversation
corpus and re-voices its best material as content ideas.

See SPEC.md for the full design. Runs once daily (06:00 Madrid via launchd,
after Cotinga's 05:30 briefing).

Design (thin-scheduler + delegated-judgment, mirroring Cotinga):
  Phase 1 (Python): acquire lock, enforce once-per-day idempotency, read the
    backlog cursor, and decide which page(s) of the conversation corpus to
    process this run.
  Phase 2 (delegated): for each batch, spawn a blocking `claude --print` agent
    that retrieves the page from Neotoma via MCP, extracts candidate content
    ideas, dedupes them against the existing post_idea entities, stores the
    survivors as post_idea (status="idea", source="conversation_sweep") linked
    REFERS_TO the source conversation/analysis and PART_OF the Ateles plan, and
    sends a ranked Telegram digest. The agent prints a MIMUS_RESULT sentinel;
    Python parses it to advance the cursor only on a clean run.

The cursor is offset-based over conversation entities ordered oldest-first, so
the first runs drain the full backlog deterministically (operator decision
2026-06-19); once drained, the daemon tails newly-updated conversations.

Usage:
  python3 mimus.py
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# Env bootstrap (launchd does not source shell profiles)
# ---------------------------------------------------------------------------

_NEOTOMA_ENV_FILE = Path.home() / ".config" / "neotoma" / ".env"
if _NEOTOMA_ENV_FILE.exists():
    for _line in _NEOTOMA_ENV_FILE.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

# ---------------------------------------------------------------------------
# lib/notify integration
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

try:
    from lib.notify import Notifier
    _notifier: "Notifier | None" = Notifier.from_neotoma()
except Exception:
    _notifier = None


def _notify(message: str, priority: str = "info") -> None:
    if _notifier is None:
        return
    try:
        from lib.notify import Priority
        p = getattr(Priority, priority.upper(), Priority.INFO)
        _notifier.send(message, priority=p, handler="mimus")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Constants & tunables
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).parent.parent.parent.parent
LOG_DIR = Path.home() / "Library" / "Logs" / "ateles"
LOG_FILE = LOG_DIR / "mimus.log"

STATE_FILE = Path(__file__).parent / ".mimus_last_run"   # once-per-day guard
CURSOR_FILE = Path(__file__).parent / ".mimus_cursor"    # backlog cursor (JSON)
LOCK_FILE = Path(__file__).parent / ".mimus_lock"

MADRID_TZ = ZoneInfo("Europe/Madrid")

PLAN_ENTITY_ID = "ent_99ace4dd6673aa36ed08b1fe"  # Ateles plan

# Tunables (env-overridable). Defaults: top-5 digest, 100/batch, 5 batches/run
# → ~500 conversations/day, draining ~5,701 in roughly 12 days, then tailing.
BATCH_SIZE = int(os.environ.get("MIMUS_BATCH_SIZE", "100"))
MAX_BATCHES_PER_RUN = int(os.environ.get("MIMUS_MAX_BATCHES_PER_RUN", "5"))
DIGEST_LIMIT = int(os.environ.get("MIMUS_DIGEST_LIMIT", "5"))
AGENT_TIMEOUT_SEC = int(os.environ.get("MIMUS_AGENT_TIMEOUT_SEC", "1800"))

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
TELEGRAM_TOPIC_CONTENT = os.environ.get("TELEGRAM_TOPIC_CONTENT", "")

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOG_DIR.mkdir(parents=True, exist_ok=True)


class _FlushingFileHandler(logging.FileHandler):
    def emit(self, record: logging.LogRecord) -> None:
        super().emit(record)
        self.flush()


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [mimus] %(levelname)s %(message)s",
    handlers=[_FlushingFileHandler(LOG_FILE)],
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Locking & idempotency
# ---------------------------------------------------------------------------


def _acquire_lock() -> bool:
    """Return True if we got the lock, False if another instance is running."""
    if LOCK_FILE.exists():
        try:
            pid = int(LOCK_FILE.read_text().strip())
            os.kill(pid, 0)
            log.warning(f"Another Mimus instance is running (pid {pid}) — exiting.")
            return False
        except (ProcessLookupError, ValueError):
            pass  # stale lock
    LOCK_FILE.write_text(str(os.getpid()))
    return True


def _release_lock() -> None:
    LOCK_FILE.unlink(missing_ok=True)


def _check_already_ran_today() -> bool:
    if STATE_FILE.exists():
        return STATE_FILE.read_text().strip() == date.today().isoformat()
    return False


def _mark_ran_today() -> None:
    STATE_FILE.write_text(date.today().isoformat())


# ---------------------------------------------------------------------------
# Backlog cursor
# ---------------------------------------------------------------------------


def _read_cursor() -> dict:
    """Cursor shape: {"offset": int, "drained": bool, "updated_at": iso}.

    offset   — number of oldest-first conversations already swept.
    drained  — True once the full backlog has been processed; later runs tail
               newly-updated conversations (offset still advances as the corpus
               grows, which is the desired behaviour for an oldest-first sweep).
    """
    if CURSOR_FILE.exists():
        try:
            return json.loads(CURSOR_FILE.read_text())
        except (json.JSONDecodeError, ValueError):
            log.warning("Cursor file corrupt — resetting to offset 0.")
    return {"offset": 0, "drained": False, "updated_at": None}


def _write_cursor(offset: int, drained: bool) -> None:
    CURSOR_FILE.write_text(json.dumps({
        "offset": offset,
        "drained": drained,
        "updated_at": datetime.now(tz=MADRID_TZ).isoformat(),
    }))


# ---------------------------------------------------------------------------
# Extraction agent
# ---------------------------------------------------------------------------


def _build_agent_prompt(offset: int, batch_size: int) -> str:
    """Prompt for one batch. The agent does all retrieval/extraction/storage via
    the Neotoma MCP server and sends the Telegram digest itself; it must print a
    single MIMUS_RESULT JSON line so the scheduler can advance the cursor."""
    topic_flag = f"--topic {TELEGRAM_TOPIC_CONTENT}" if TELEGRAM_TOPIC_CONTENT else ""
    return f"""You are Mimus, the conversation-corpus content-idea daemon for the Ateles
swarm. Process exactly ONE batch of the conversation backlog this run, then stop.

## Batch
- Retrieve `conversation` entities ordered oldest-first (by created_at ascending),
  with `limit={batch_size}` and `offset={offset}`, via
  `mcp__mcpsrv_neotoma__retrieve_entities`. Prefer any linked `analysis` entity for
  a conversation when present (it is already-distilled signal); only read the raw
  conversation/agent_message content when there is no analysis.
- Let N = the number of conversation entities actually returned. If N == 0, store
  nothing, send nothing, and skip to the RESULT line with retrieved=0.

## Extract (per conversation in the batch)
Identify candidate CONTENT IDEAS worth publishing — angles, theses, explainers,
or series the operator could write. For each candidate capture:
  title, summary (1-2 sentences), pillar, format (post/thread/long-form/etc),
  platforms, series (optional), tags, confidence (0.0-1.0).
Use the content-marketing-ideas / memetic-ideas lens from the
`analyze-neotoma-feedback` skill. Not every conversation yields an idea — skip
thin or purely operational ones.

## Dedupe
For each candidate, `retrieve_entities(entity_type="post_idea", search=<title/summary>)`
and DROP any that closely duplicate an existing post_idea or another candidate in
this batch.

## Store (survivors only)
Store each surviving idea as a `post_idea` entity with:
  - status: "idea"
  - source: "conversation_sweep"
Then link it:
  - REFERS_TO the source conversation (and/or its analysis) entity
  - PART_OF the Ateles plan ({PLAN_ENTITY_ID})

## Hard constraints (see project CLAUDE.md)
- RGPD Art. 6(1)(f) minimization: capture themes/angles only. NEVER persist
  incidental sensitive disclosures (health, finances, family, political/religious
  views — Art. 9 categories) into an idea.
- PII scrubbing: strip usernames, worktree names, internal platform names, and
  private identifiers from every stored idea — these feed outward-facing content.
- This daemon NEVER drafts or publishes content. It only proposes ideas at
  status="idea" for operator approval.

## Notify
Send ONE ranked Telegram digest of the ideas stored THIS batch (top {DIGEST_LIMIT}
by confidence), via:
  node {PROJECT_ROOT}/execution/lib/telegram/send.mjs --text "<digest_html>" --html {topic_flag}
Each line: title — one-line summary (format/platform). Omit the digest entirely if
zero ideas were stored. Do not @-mention or tag anyone.

## Final line (REQUIRED, last line of your output, nothing after it)
MIMUS_RESULT {{"retrieved": <N>, "stored": <count>, "skipped_dupes": <count>}}
"""


def _run_batch(offset: int, batch_size: int) -> dict | None:
    """Spawn a blocking extraction agent for one batch. Returns the parsed
    MIMUS_RESULT dict, or None on failure/timeout."""
    claude = shutil.which("claude")
    if not claude:
        log.error("claude CLI not found in PATH — cannot run extraction batch.")
        return None

    prompt = _build_agent_prompt(offset, batch_size)
    log.info(f"Running extraction batch: offset={offset} size={batch_size}")
    try:
        result = subprocess.run(
            [claude, "--print", "--dangerously-skip-permissions", prompt],
            capture_output=True,
            text=True,
            timeout=AGENT_TIMEOUT_SEC,
            env=os.environ,
        )
    except subprocess.TimeoutExpired:
        log.error(f"Extraction batch timed out after {AGENT_TIMEOUT_SEC}s (offset={offset}).")
        return None
    except Exception as exc:
        log.error(f"Extraction batch failed to run (offset={offset}): {exc}")
        return None

    if result.returncode != 0:
        log.error(
            f"Extraction agent exited {result.returncode} (offset={offset}): "
            f"{result.stderr.strip()[:300]}"
        )
        return None

    return _parse_result(result.stdout)


def _parse_result(stdout: str) -> dict | None:
    """Extract the trailing MIMUS_RESULT JSON sentinel from agent output."""
    matches = re.findall(r"MIMUS_RESULT\s+(\{.*\})", stdout)
    if not matches:
        log.error("No MIMUS_RESULT sentinel in agent output — treating as failure.")
        return None
    try:
        parsed = json.loads(matches[-1])
        parsed["retrieved"] = int(parsed.get("retrieved", 0))
        parsed["stored"] = int(parsed.get("stored", 0))
        parsed["skipped_dupes"] = int(parsed.get("skipped_dupes", 0))
        return parsed
    except (json.JSONDecodeError, ValueError) as exc:
        log.error(f"Could not parse MIMUS_RESULT JSON: {exc}")
        return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    log.info("Mimus starting.")
    if not _acquire_lock():
        return
    try:
        _main()
    finally:
        _release_lock()


def _main() -> None:
    if _check_already_ran_today():
        log.info("Already ran today — exiting.")
        return
    _mark_ran_today()

    cursor = _read_cursor()
    offset = int(cursor.get("offset", 0))
    drained = bool(cursor.get("drained", False))

    if drained:
        log.info(f"Backlog already drained; tailing from offset {offset}.")

    total_stored = 0
    total_skipped = 0
    batches_done = 0

    for _ in range(MAX_BATCHES_PER_RUN):
        result = _run_batch(offset, BATCH_SIZE)
        if result is None:
            # Fail-open: leave the cursor where it is and retry next run.
            log.warning(f"Batch at offset {offset} failed — leaving cursor unmoved.")
            break

        retrieved = result["retrieved"]
        total_stored += result["stored"]
        total_skipped += result["skipped_dupes"]
        batches_done += 1

        if retrieved == 0:
            # Reached the end of the corpus for this sweep.
            if not drained:
                drained = True
                log.info("Backlog fully drained — switching to tail mode next run.")
            _write_cursor(offset, drained)
            break

        offset += retrieved
        # A short page means we hit the end of the corpus.
        page_was_full = retrieved >= BATCH_SIZE
        if not page_was_full and not drained:
            drained = True
            log.info("Reached end of corpus (short page) — backlog drained.")
        _write_cursor(offset, drained)
        if not page_was_full:
            break

    log.info(
        f"Mimus run complete: {batches_done} batch(es), {total_stored} idea(s) stored, "
        f"{total_skipped} duplicate(s) skipped, cursor now at offset {offset} "
        f"(drained={drained})."
    )


if __name__ == "__main__":
    _notify("mimus started", priority="info")
    try:
        main()
        _notify("mimus run complete", priority="info")
    except Exception as exc:
        log.exception(f"Mimus fatal error: {exc}")
        _notify(f"mimus fatal error: {exc}", priority="blocker")
        sys.exit(1)
