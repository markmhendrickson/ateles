#!/usr/bin/env python3
"""
Morning Brief — Ateles morning digest dispatcher.

Runs at 05:30 Madrid time via launchd. Reads checkpoint_briefs stored by
Cotinga (which ran at 05:00), composes a polished Ateles-voice digest,
and sends it to Telegram.

If checkpoint_briefs are not yet in Neotoma (Cotinga still running), waits
up to WAIT_MINUTES before falling back to a raw calendar summary.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# Env bootstrap
# ---------------------------------------------------------------------------

_NEOTOMA_ENV_FILE = Path.home() / ".config" / "neotoma" / ".env"
if _NEOTOMA_ENV_FILE.exists():
    for _line in _NEOTOMA_ENV_FILE.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

LOG_DIR = Path.home() / "Library" / "Logs" / "ateles"
LOG_FILE = LOG_DIR / "morning-brief.log"
STATE_FILE = Path(__file__).parent / ".morning_brief_last_run"

MADRID_TZ = ZoneInfo("Europe/Madrid")
NEOTOMA_BEARER_TOKEN = os.environ.get("NEOTOMA_BEARER_TOKEN", "")
NEOTOMA_BASE_URL = os.environ.get("NEOTOMA_BASE_URL", "https://neotoma.markmhendrickson.com")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
TELEGRAM_TOPIC_COTINGA = os.environ.get("TELEGRAM_TOPIC_COTINGA", "")

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent

# How long to wait for Cotinga's briefs to appear before giving up
WAIT_MINUTES = 20
WAIT_POLL_SECONDS = 60

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
    format="%(asctime)s [morning-brief] %(levelname)s %(message)s",
    handlers=[
        _FlushingFileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


LOCK_FILE = Path(__file__).parent / ".morning_brief_lock"


def _acquire_lock() -> bool:
    if LOCK_FILE.exists():
        try:
            pid = int(LOCK_FILE.read_text().strip())
            import signal
            os.kill(pid, 0)
            log.warning(f"Another morning-brief instance is running (pid {pid}) — exiting.")
            return False
        except (ProcessLookupError, ValueError):
            pass
    LOCK_FILE.write_text(str(os.getpid()))
    return True


def _release_lock() -> None:
    LOCK_FILE.unlink(missing_ok=True)


def _already_ran_today() -> bool:
    if STATE_FILE.exists():
        return STATE_FILE.read_text().strip() == date.today().isoformat()
    return False


def _mark_ran_today() -> None:
    STATE_FILE.write_text(date.today().isoformat())


# ---------------------------------------------------------------------------
# Neotoma
# ---------------------------------------------------------------------------


def _neotoma_get(path: str) -> dict | list | None:
    if not NEOTOMA_BEARER_TOKEN:
        return None
    try:
        url = f"{NEOTOMA_BASE_URL.rstrip('/')}{path}"
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {NEOTOMA_BEARER_TOKEN}",
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except Exception as exc:
        log.debug(f"Neotoma GET {path} failed: {exc}")
        return None


def fetch_todays_checkpoint_briefs() -> list[dict]:
    """Retrieve checkpoint_brief entities created today by Cotinga."""
    today = date.today().isoformat()
    data = _neotoma_get(
        f"/api/entities?entity_type=checkpoint_brief"
        f"&search={urllib.parse.quote(today)}&limit=20"
    )
    if not data:
        return []
    entities = data.get("entities") or []
    briefs = []
    for e in entities:
        snap = e.get("snapshot") or {}
        # Only include briefs from today
        created = e.get("created_at") or ""
        if today in created or today in (snap.get("date") or ""):
            briefs.append(snap)
    return briefs


def wait_for_briefs() -> list[dict]:
    """Poll Neotoma for checkpoint_briefs, waiting up to WAIT_MINUTES."""
    deadline = time.time() + WAIT_MINUTES * 60
    while time.time() < deadline:
        briefs = fetch_todays_checkpoint_briefs()
        if briefs:
            log.info(f"Found {len(briefs)} checkpoint_brief(s) from Cotinga.")
            return briefs
        log.info("No checkpoint_briefs yet — waiting for Cotinga...")
        time.sleep(WAIT_POLL_SECONDS)
    log.warning(f"No checkpoint_briefs after {WAIT_MINUTES}min wait. Proceeding without them.")
    return []


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------


def telegram_send(text: str) -> None:
    import shutil
    node = shutil.which("node")
    send_script = PROJECT_ROOT / "execution" / "lib" / "telegram" / "send.mjs"
    if node and send_script.exists():
        try:
            args = [node, str(send_script), "--text", text]
            if TELEGRAM_TOPIC_COTINGA:
                args += ["--thread-id", TELEGRAM_TOPIC_COTINGA]
            subprocess.run(args, timeout=15, capture_output=True, env=os.environ)
            return
        except Exception as exc:
            log.warning(f"send.mjs failed: {exc}")

    telegram_cmd = shutil.which("telegram-send")
    if telegram_cmd:
        try:
            subprocess.run([telegram_cmd, text], timeout=15, capture_output=True, env=os.environ)
        except Exception as exc:
            log.warning(f"telegram-send fallback failed: {exc}")


# ---------------------------------------------------------------------------
# Ateles digest via Claude
# ---------------------------------------------------------------------------


def build_ateles_digest(briefs: list[dict]) -> str:
    """
    Spawn a one-shot Claude agent with Ateles persona to compose the
    morning digest from the checkpoint_briefs, return the text.
    Falls back to a raw brief dump if Claude is unavailable.
    """
    import shutil

    claude = shutil.which("claude")
    soul_path = PROJECT_ROOT / ".claude" / "skills" / "ateles" / "SKILL.md"

    if not claude or not soul_path.exists():
        return _fallback_digest(briefs)

    today = date.today().strftime("%A %-d %B %Y")
    briefs_json = json.dumps(briefs, indent=2)

    prompt = f"""You are Ateles. Today is {today}.

Cotinga has already run and stored checkpoint_briefs for today's meetings in Neotoma.
Here are the briefs:

{briefs_json}

Compose a morning digest to send to Mark via Telegram. Use your voice: direct, no filler, short.

Format:
---
Good morning. Here's your day.

[For each meeting, 3-5 lines:
  - Time + title
  - Who's there (one line, role/company)
  - Key goal or what to watch for
  - Any pre-event tasks due today]

[If no meetings: "Clear calendar today."]

[Close with 1 sentence: any open blocker or priority task for the day if one stands out from the briefs.]
---

Output only the Telegram message text. Nothing else."""

    try:
        result = subprocess.run(
            [claude, "--print", "--dangerously-skip-permissions", prompt],
            capture_output=True,
            text=True,
            timeout=120,
            env=os.environ,
        )
        text = result.stdout.strip()
        if text:
            return text
        log.warning(f"Claude returned empty output: {result.stderr[:200]}")
    except Exception as exc:
        log.warning(f"Claude digest failed: {exc}")

    return _fallback_digest(briefs)


def _fallback_digest(briefs: list[dict]) -> str:
    today = date.today().strftime("%A %-d %B %Y")
    if not briefs:
        return f"☀️ Morning brief — {today}\n\nNo checkpoint briefs available. Check Cotinga logs."
    lines = [f"☀️ Morning brief — {today}", ""]
    for b in briefs:
        title = b.get("title") or b.get("subject") or "(untitled)"
        body = (b.get("body") or "")[:300]
        lines.append(f"📅 {title}")
        if body:
            lines.append(body)
        lines.append("")
    return "\n".join(lines).strip()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    log.info("Morning Brief starting.")

    if not _acquire_lock():
        return

    try:
        _main()
    finally:
        _release_lock()


def _main() -> None:
    if _already_ran_today():
        log.info("Already ran today — exiting.")
        return

    today_str = date.today().isoformat()
    log.info(f"Composing morning brief for {today_str}")

    briefs = wait_for_briefs()
    digest = build_ateles_digest(briefs)

    log.info("Sending morning digest via Telegram...")
    telegram_send(digest)
    log.info("Morning digest sent.")

    _mark_ran_today()


if __name__ == "__main__":
    main()
