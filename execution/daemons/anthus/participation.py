"""
Persistence layer for orchestrator gate state.

Each (work_entity_id, gate_name) pair is stored as a `participation_record`
entity in Neotoma. On Anthus restart, the daemon fetches all records for
in-flight work entities and rebuilds its in-memory state.

This is the tactical Phase 5 implementation of ateles#9. The schema is
also used by the Phase 6 emergent-participation model (ateles#4).
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from typing import Any

import httpx

log = logging.getLogger("anthus.participation")

_BEARER_ENV = "NEOTOMA_BEARER_TOKEN"  # gitleaks:allow
NEOTOMA_BASE_URL = os.environ.get(
    "NEOTOMA_BASE_URL", "https://neotoma.markmhendrickson.com"
).rstrip("/")


def _bearer() -> str | None:
    return os.environ.get(_BEARER_ENV)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


async def record_dispatched(
    work_entity_id: str,
    workflow_definition_id: str,
    gate_name: str,
    agent: str,
) -> None:
    """Write/update a participation_record with status=dispatched."""
    await _upsert(
        {
            "work_entity_id": work_entity_id,
            "workflow_definition_id": workflow_definition_id,
            "gate_name": gate_name,
            "agent": agent,
            "status": "dispatched",
            "dispatched_at": _now_iso(),
        },
        idempotency_key=f"dispatch-{work_entity_id}-{gate_name}",
    )


async def record_satisfied(
    work_entity_id: str,
    gate_name: str,
    artifact_ref: str,
) -> None:
    """Write/update a participation_record with status=satisfied."""
    await _upsert(
        {
            "work_entity_id": work_entity_id,
            "gate_name": gate_name,
            "status": "satisfied",
            "satisfied_at": _now_iso(),
            "artifact_refs": [artifact_ref],
        },
        idempotency_key=f"satisfied-{work_entity_id}-{gate_name}",
    )


async def record_skipped(work_entity_id: str, gate_name: str, reason: str) -> None:
    """Write/update a participation_record with status=skipped."""
    await _upsert(
        {
            "work_entity_id": work_entity_id,
            "gate_name": gate_name,
            "status": "skipped",
            "satisfied_at": _now_iso(),
            "error": reason if reason else None,
        },
        idempotency_key=f"skipped-{work_entity_id}-{gate_name}",
    )


async def load_state_for(work_entity_id: str) -> dict[str, dict[str, Any]]:
    """
    Fetch all participation_record entities for a work entity and return them
    as a dict keyed by gate_name, ready to seed orchestrator in-memory state.

    Each value is a plain dict with keys: gate_name, status, dispatched_at,
    satisfied_at, artifact_refs, error.
    """
    bearer = _bearer()
    if not bearer:
        log.warning(f"{_BEARER_ENV} not set; cannot load participation_records.")
        return {}

    headers = {
        "Authorization": f"Bearer {bearer}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(headers=headers, timeout=15) as client:
            resp = await client.post(
                f"{NEOTOMA_BASE_URL}/retrieve_entities",
                json={
                    "entity_type": "participation_record",
                    "limit": 200,
                    "include_snapshots": True,
                },
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        log.warning(f"load_state_for({work_entity_id}) failed: {exc}")
        return {}

    out: dict[str, dict[str, Any]] = {}
    for e in data.get("entities", []):
        snap = e.get("snapshot") or {}
        if snap.get("work_entity_id") != work_entity_id:
            continue
        gate_name = snap.get("gate_name")
        if not gate_name:
            continue
        out[gate_name] = {
            "gate_name": gate_name,
            "status": snap.get("status", "pending"),
            "dispatched_at": snap.get("dispatched_at"),
            "satisfied_at": snap.get("satisfied_at"),
            "artifact_refs": list(snap.get("artifact_refs", [])),
            "error": snap.get("error"),
        }
    return out


async def _upsert(payload: dict[str, Any], idempotency_key: str) -> None:
    """Send a store request with idempotency_key. Fire-and-forget on error."""
    bearer = _bearer()
    if not bearer:
        log.debug("Skipping participation_record write — no bearer token.")
        return

    headers = {
        "Authorization": f"Bearer {bearer}",
        "Content-Type": "application/json",
    }
    body = {
        "entities": [{"entity_type": "participation_record", **payload}],
        "idempotency_key": idempotency_key,
    }
    try:
        async with httpx.AsyncClient(headers=headers, timeout=15) as client:
            resp = await client.post(f"{NEOTOMA_BASE_URL}/store", json=body)
            if resp.status_code >= 400:
                log.warning(
                    f"participation_record upsert {idempotency_key} -> "
                    f"HTTP {resp.status_code}: {resp.text[:200]}"
                )
    except Exception as exc:
        log.warning(f"participation_record upsert {idempotency_key} failed: {exc}")
