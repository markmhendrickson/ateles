"""One-field Neotoma correction over REST, with a loud failure path.

The correction route is ``POST /correct`` with one field per call. There is no
``/corrections`` or ``/entities/<id>/corrections`` route: calling them returns
404, and callers that swallowed that lost every correction silently.

``correct_field`` returns True only when the write got a 2xx AND the field reads
back with the value written. Anything else is logged at ERROR and returns False.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

import httpx

log = logging.getLogger("neotoma_correct")


def _describe(value: Any) -> str:
    """Log form of a written value: long strings by length only (notes can be large)."""
    if isinstance(value, str) and len(value) > 60:
        return f"<str len={len(value)}>"
    return repr(value)


def unwrap_snapshot(body: Any) -> dict | None:
    """The entity's snapshot dict from a GET /entities response, or None.

    Whenever a ``snapshot`` key exists (at the root, or under ``entity``) ONLY
    that value is used: if it is empty, null or not a mapping the result is
    None, never the surrounding object. A root that carries ``entity_type`` but
    an empty ``snapshot`` therefore reads as unreadable, not as a flat entity
    with no fields. A nested ``{snapshot: {snapshot: ...}}`` is unwrapped one
    level with the same rule. The flat-entity reading applies only when no
    ``snapshot`` key exists anywhere.
    """
    if not isinstance(body, dict):
        return None
    entity = body.get("entity")
    if "snapshot" in body:
        snap = body["snapshot"]
    elif isinstance(entity, dict):
        snap = entity["snapshot"] if "snapshot" in entity else entity
    else:
        snap = body
    if isinstance(snap, dict) and "snapshot" in snap:
        snap = snap["snapshot"]
    if not isinstance(snap, dict) or not snap:
        return None
    return snap


def _readback_matches(
    body: Any, field: str, value: Any, same: Callable[[Any, Any], bool] | None = None
) -> bool:
    inner = unwrap_snapshot(body)
    if inner is None:
        return False
    actual = inner.get(field)
    if same is not None:
        try:
            return bool(same(actual, value))
        except Exception:  # noqa: BLE001
            return False
    return actual == value


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
    same: Callable[[Any, Any], bool] | None = None,
) -> bool:
    """POST /correct for one field, then read it back. Never raises.

    ``same(actual, written)`` overrides the exact-equality read-back check (e.g.
    to compare normalised dates).
    """
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
        ok = _readback_matches(back.json(), field, value, same)
    except Exception as exc:  # noqa: BLE001
        log.error("correction readback error for %s: %s", where, exc)
        return False
    if not ok:
        log.error("correction readback MISMATCH for %s: wrote %s", where, _describe(value))
    return ok
