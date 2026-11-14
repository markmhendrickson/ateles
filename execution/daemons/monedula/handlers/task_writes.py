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

from lib.daemon_runtime.neotoma_correct import (  # noqa: E402
    correct_field,
    unwrap_snapshot,
)

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
    """Deterministic per (entity, field, value).

    An identical replay is a true replay. A later write of the *same* value to
    the same field (e.g. a task reopened by the operator, then closed again with
    the same status) dedupes to a no-op and the read-back then reports a
    mismatch at ERROR: loud, not silent.
    """
    digest = hashlib.sha256(repr(value).encode()).hexdigest()[:12]
    return f"monedula-{field}-{entity_id}-{digest}"


_IDENTITY_FIELDS = ("title", "entity_type", "canonical_name", "name")


def _has_snapshot_key(body: Any) -> bool:
    """True only when a ``snapshot`` key is present at the root or under ``entity``."""
    if not isinstance(body, dict):
        return False
    if "snapshot" in body:
        return True
    entity = body.get("entity")
    return isinstance(entity, dict) and "snapshot" in entity


def _extract_snapshot(body: Any) -> dict | None:
    """Unwrap a GET /entities response; None unless it is a usable task snapshot.

    A response is readable only when a ``snapshot`` mapping is present (root or
    under ``entity``). There is no flat-entity reading: ``entity_type``,
    ``canonical_name`` and ``entity_id`` are entity metadata, so a response
    carrying only those is unreadable, never "a task with no notes". The
    snapshot itself must also carry an identity field.
    """
    if not _has_snapshot_key(body):
        return None
    snap = unwrap_snapshot(body)
    if snap is None or not any(snap.get(k) for k in _IDENTITY_FIELDS):
        return None
    return snap


def _snapshot(client: httpx.Client, base: str, entity_id: str) -> dict | None:
    """The entity snapshot dict, or None when the read failed (logged ERROR)."""
    try:
        resp = client.get(f"{base}/entities/{entity_id}", timeout=15)
        resp.raise_for_status()
        body = resp.json()
    except Exception as exc:  # noqa: BLE001
        log.error("read of %s failed: %s", entity_id, exc)
        return None
    snap = _extract_snapshot(body)
    if snap is None:
        log.error(
            "read of %s returned an empty or unrecognised snapshot - treating as a "
            "failed read so nothing is overwritten",
            entity_id,
        )
    return snap


def _same_date(actual: Any, written: Any) -> bool:
    def day(v: Any) -> str:
        return str(v or "").strip()[:10]

    return bool(day(actual)) and day(actual) == day(written)


def write_field(
    client: httpx.Client,
    base: str,
    entity_id: str,
    entity_type: str,
    field: str,
    value: Any,
) -> bool:
    """Correct one field and read it back. Logs ERROR and returns False on failure.

    ``due_date`` is compared as a date, so a server that normalises it (adds a
    time or timezone) does not make a good write read as a mismatch.
    """
    return correct_field(
        client,
        base,
        entity_id,
        entity_type,
        field,
        value,
        _idem(entity_id, field, value),
        same=_same_date if field == "due_date" else None,
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
    raw = snap.get("notes")
    if raw is not None and not isinstance(raw, str):
        log.error("notes on %s is not text - refusing to overwrite it", task_id)
        return False
    existing = raw or ""
    if note in existing:
        return True
    merged = f"{existing.rstrip()}\n{note}" if existing.strip() else note
    return write_field(client, base, task_id, "task", "notes", merged)
