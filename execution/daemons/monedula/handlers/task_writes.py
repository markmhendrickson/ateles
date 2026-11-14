"""Neotoma task writes for payment handlers, over REST ``POST /correct``.

The handlers used to shell out to ``neotoma entities update``. The installed
CLI has no such command, so every post-payment task update failed with
"unknown command" and was logged as a warning: a settled payment left its task
reading awaiting_approval and its due date unrolled. Writes now go through
``lib.daemon_runtime.neotoma_correct.correct_field`` (one field per call, read
back after write, ERROR on any failure).

Credentials come from the daemon environment (NEOTOMA_BASE_URL,
NEOTOMA_BEARER_TOKEN) and are never logged.
"""

from __future__ import annotations

import hashlib
import logging
import os
import sys
from pathlib import Path
from typing import Any

import httpx

_REPO_ROOT = Path(__file__).resolve().parents[4]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from lib.daemon_runtime.neotoma_correct import correct_field  # noqa: E402

log = logging.getLogger(__name__)

USER_AGENT = "ateles-monedula/1.0"  # Cloudflare 1010-blocks urllib's default UA


def make_client() -> tuple[httpx.Client, str] | None:
    """Return (client, base_url), or None (after an ERROR) when env is missing."""
    base = os.environ.get("NEOTOMA_BASE_URL", "").strip().rstrip("/")
    token = os.environ.get("NEOTOMA_BEARER_TOKEN", "").strip()
    if not base or not token:
        log.error(
            "NEOTOMA_BASE_URL / NEOTOMA_BEARER_TOKEN not set - cannot write to Neotoma"
        )
        return None
    client = httpx.Client(
        headers={"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT}
    )
    return client, base


def _idem(entity_id: str, field: str, value: Any) -> str:
    digest = hashlib.sha256(repr(value).encode()).hexdigest()[:12]
    return f"monedula-{field}-{entity_id}-{digest}"


def _snapshot(client: httpx.Client, base: str, entity_id: str) -> dict | None:
    """The entity snapshot dict, or None when the read failed (logged ERROR)."""
    try:
        resp = client.get(f"{base}/entities/{entity_id}", timeout=15)
        resp.raise_for_status()
        body = resp.json()
    except Exception as exc:  # noqa: BLE001
        log.error("read of %s failed: %s", entity_id, exc)
        return None
    snap = body.get("snapshot") if isinstance(body, dict) else None
    if not isinstance(snap, dict):
        snap = body if isinstance(body, dict) else {}
    return snap.get("snapshot") if isinstance(snap.get("snapshot"), dict) else snap


def write_field(
    client: httpx.Client,
    base: str,
    entity_id: str,
    entity_type: str,
    field: str,
    value: Any,
) -> bool:
    """Correct one field and read it back. Logs ERROR and returns False on failure."""
    return correct_field(
        client, base, entity_id, entity_type, field, value, _idem(entity_id, field, value)
    )


def append_note(client: httpx.Client, base: str, task_id: str, note: str) -> bool:
    """Append ``note`` to the task's notes.

    The notes field replaces on write, so the existing text is read first and
    merged. If that read fails nothing is written: a blind write could erase
    the operator's notes.
    """
    snap = _snapshot(client, base, task_id)
    if snap is None:
        return False
    existing = str(snap.get("notes") or "")
    if note in existing:
        return True
    merged = f"{existing.rstrip()}\n{note}" if existing.strip() else note
    return write_field(client, base, task_id, "task", "notes", merged)
