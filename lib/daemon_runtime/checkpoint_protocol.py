"""Canonical wire contract shared by checkpoint resolvers and verifiers."""

from __future__ import annotations

import json
from typing import Any


def checkpoint_resolution_body(checkpoint_id: str, action: str) -> dict[str, Any]:
    normalized_action = str(action or "").strip().lower()
    if normalized_action not in {"approve", "reject"}:
        raise ValueError("action must be 'approve' or 'reject'")
    status = "approved" if normalized_action == "approve" else "rejected"
    entity_id = str(checkpoint_id or "").strip()
    if not entity_id:
        raise ValueError("checkpoint_id is required")
    return {
        "entity_id": entity_id,
        "entity_type": "checkpoint_" + "brief",
        "field": "status",
        "value": status,
        "idempotency_key": f"resolve-checkpoint-{entity_id}-{status}",
    }


def canonical_json_bytes(body: dict[str, Any]) -> bytes:
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
