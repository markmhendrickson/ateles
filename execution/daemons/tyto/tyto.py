#!/usr/bin/env python3
"""
Tyto — Screenshot watcher daemon.

Tyto genus: barn owls. T3 daemon in the Ateles swarm.

Tyto watches a configured screenshots directory for new image files (PNG/JPG),
stores each as a `screenshot` entity in Neotoma, then dispatches an invocable
agent (OCR + entity extraction) if the priority_rubric permits.

Lives at: launchd on the operator's machine (no external endpoint required)

AAuth sub: tyto@ateles-swarm
Phase 2: skeleton with directory polling + Neotoma entity store.
Full OCR dispatch logic deferred to Phase 3.

Environment variables:
  NEOTOMA_BEARER_TOKEN      Neotoma API auth token
  NEOTOMA_BASE_URL          Neotoma API base URL (default: https://neotoma.markmhendrickson.com)
  TELEGRAM_BOT_TOKEN        Telegram bot token
  TELEGRAM_CHAT_ID          Telegram chat ID
  TELEGRAM_TOPIC_TYTO       Telegram topic ID for Tyto notifications (optional)
  TYTO_SCREENSHOTS_DIR      Directory to watch (default: ~/Desktop/Screenshots)
  TYTO_POLL_INTERVAL        Polling interval in seconds (default: 10)
  TYTO_AGENT_DEFINITION_ID  Neotoma entity ID for Tyto's agent_definition (optional)
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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

NEOTOMA_BASE_URL = os.environ.get(
    "NEOTOMA_BASE_URL", "https://neotoma.markmhendrickson.com"
)
NEOTOMA_BEARER_TOKEN = os.environ.get("NEOTOMA_BEARER_TOKEN", "")

SCREENSHOTS_DIR = Path(
    os.environ.get(
        "TYTO_SCREENSHOTS_DIR",
        str(Path.home() / "Desktop" / "Screenshots"),
    )
)
POLL_INTERVAL = int(os.environ.get("TYTO_POLL_INTERVAL", "10"))

SCREENSHOT_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}


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


def _sha256_file(path: Path) -> str:
    """Compute SHA-256 of a file. Returns hex digest."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


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

    # 3. Load notification rubric
    notifier = Notifier.from_neotoma()

    # 4. Validate watch dir
    if not SCREENSHOTS_DIR.exists():
        log.warning(
            f"[{DAEMON_NAME}] Screenshots dir does not exist: {SCREENSHOTS_DIR} "
            "— will retry on each poll"
        )
    else:
        log.info(f"[{DAEMON_NAME}] Watching: {SCREENSHOTS_DIR}")

    # 5. Notify startup
    notifier.send(
        f"{DAEMON_NAME} started — polling {SCREENSHOTS_DIR} every {POLL_INTERVAL}s",
        priority=Priority.INFO,
        handler=DAEMON_NAME,
    )

    # 6. Poll loop
    watcher = ScreenshotWatcher(SCREENSHOTS_DIR, notifier)
    log.info(f"[{DAEMON_NAME}] Poll interval: {POLL_INTERVAL}s")

    while True:
        try:
            await watcher.poll_once()
        except Exception as exc:
            log.error(f"[{DAEMON_NAME}] Poll error: {exc}", exc_info=True)
        await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info(f"[{DAEMON_NAME}] Stopped by operator.")
