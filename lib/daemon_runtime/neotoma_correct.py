"""One-field Neotoma correction over REST, with a loud failure path.

The correction route is ``POST /correct`` with one field per call. There is no
``/corrections`` or ``/entities/<id>/corrections`` route: calling them returns
404, and callers that swallowed that lost every correction silently.

``correct_field`` returns True only when the write got a 2xx AND the field reads
back with the value written. Anything else is logged at ERROR and returns False.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger("neotoma_correct")


def _readback_matches(body: Any, field: str, value: Any) -> bool:
    if not isinstance(body, dict):
        return False
    snap = body.get("snapshot")
    if not isinstance(snap, dict):
        snap = body
    inner = snap.get("snapshot") if isinstance(snap.get("snapshot"), dict) else snap
    return inner.get(field) == value


def correct_field(
    client: httpx.Client,
    base_url: str,
    entity_id: str,
    entity_type: str,
    field: str,
    value: Any,
    idempotency_key: str,
    *,
    timeout: float = 15,
) -> bool:
    """POST /correct for one field, then read it back. Never raises."""
    where = f"{entity_type} {entity_id} field={field}"
    try:
        resp = client.post(
            f"{base_url}/correct",
            json={
                "entity_id": entity_id,
                "entity_type": entity_type,
                "field": field,
                "value": value,
                "idempotency_key": idempotency_key,
            },
            timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001
        log.error("correction error for %s: %s", where, exc)
        return False
    if not resp.is_success:
        log.error(
            "correction FAILED (HTTP %s) for %s: %s",
            resp.status_code,
            where,
            resp.text[:200],
        )
        return False
    try:
        back = client.get(f"{base_url}/entities/{entity_id}", timeout=timeout)
        back.raise_for_status()
        ok = _readback_matches(back.json(), field, value)
    except Exception as exc:  # noqa: BLE001
        log.error("correction readback error for %s: %s", where, exc)
        return False
    if not ok:
        log.error("correction readback MISMATCH for %s: wrote %r", where, value)
    return ok
