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
NEOTOMA_BASE_URL = os.environ.get("NEOTOMA_BASE_URL", "").rstrip("/")


def _bearer() -> str | None:
    return os.environ.get(_BEARER_ENV)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


async def record_dispatched(
    work_entity_id: str,
    workflow_definition_id: str,
    gate_name: str,
    agent: str,
    agent_definition_ref: str = "",
    agent_definition_observation_id: str = "",
    agent_strategy_ref: str = "",
) -> None:
    """Write/update a participation_record with status=dispatched."""
    payload: dict = {
        "work_entity_id": work_entity_id,
        "workflow_definition_id": workflow_definition_id,
        "gate_name": gate_name,
        "agent": agent,
        "status": "dispatched",
        "dispatched_at": _now_iso(),
    }
    if agent_definition_ref:
        payload["agent_definition_ref"] = agent_definition_ref
    if agent_definition_observation_id:
        payload["agent_definition_observation_id"] = agent_definition_observation_id
    if agent_strategy_ref:
        payload["agent_strategy_ref"] = agent_strategy_ref
    await _upsert(payload, idempotency_key=f"dispatch-{work_entity_id}-{gate_name}")


async def record_satisfied(
    work_entity_id: str,
    gate_name: str,
    artifact_ref: str,
    agent: str,
) -> None:
    """
    Write/update a participation_record with status=satisfied.

    `agent` is REQUIRED by the participation_record schema. It used to be
    absent from this signature entirely, so every terminal write produced an
    incomplete row (ateles#682). Neotoma ACCEPTS such a write — HTTP 200 with
    a `MISSING_REQUIRED_FIELD` store_warning — so nothing raised and nothing
    logged, and the gate looked satisfied to the writer while the stored row
    carried neither the agent nor the artifact.
    """
    payload: dict[str, Any] = {
        "work_entity_id": work_entity_id,
        "gate_name": gate_name,
        "agent": agent,
        "status": "satisfied",
        "satisfied_at": _now_iso(),
    }
    # Schema field is the singular `artifact_ref` (string). Writing the plural
    # `artifact_refs` list landed it in raw_fragments, never the snapshot —
    # which is why the two pre-fix satisfied rows in prod carry no artifact.
    if artifact_ref:
        payload["artifact_ref"] = artifact_ref
    await _upsert(payload, idempotency_key=f"satisfied-{work_entity_id}-{gate_name}")


async def record_skipped(
    work_entity_id: str,
    gate_name: str,
    reason: str,
    agent: str,
) -> None:
    """Write/update a participation_record with status=skipped."""
    payload: dict[str, Any] = {
        "work_entity_id": work_entity_id,
        "gate_name": gate_name,
        "agent": agent,
        "status": "skipped",
        # Schema declares `skipped_at` for this transition; the old code wrote
        # `satisfied_at`, conflating a skip with a completion.
        "skipped_at": _now_iso(),
    }
    # Schema field is `skip_reason`; `error` is not declared on this schema.
    if reason:
        payload["skip_reason"] = reason
    await _upsert(payload, idempotency_key=f"skipped-{work_entity_id}-{gate_name}")


class ParticipationStateUnavailable(RuntimeError):
    """
    Raised when participation state could not be read from Neotoma.

    This is deliberately distinct from "the read succeeded and there are no
    records yet". Both used to collapse into an empty dict, which made a failed
    read indistinguishable from a genuinely fresh work entity — so the caller
    hydrated nothing and dispatched every gate as if it had never run. See
    ateles#584: a 404 on this read was logged as a WARNING while the dispatch
    proceeded, so Anthus looked healthy while its gate bookkeeping was blind.

    Callers must treat this as "hold the dispatch", never as "no prior state".
    """


async def load_state_for(work_entity_id: str) -> dict[str, dict[str, Any]]:
    """
    Fetch all participation_record entities for a work entity and return them
    as a dict keyed by gate_name, ready to seed orchestrator in-memory state.

    Each value is a plain dict with keys: gate_name, status, dispatched_at,
    satisfied_at, skipped_at, artifact_refs, error.

    Returns an empty dict ONLY when the read succeeded and no records exist.
    Raises ParticipationStateUnavailable when the state could not be read at
    all — the caller must hold the dispatch rather than assume a clean slate.
    """
    bearer = _bearer()
    if not bearer:
        raise ParticipationStateUnavailable(
            f"{_BEARER_ENV} not set; cannot load participation_records."
        )

    headers = {
        "Authorization": f"Bearer {bearer}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(headers=headers, timeout=15) as client:
            # NOTE: the entity-read route is POST /entities/query. The prod
            # HTTP surface does NOT expose /retrieve_entities (404) — that path
            # only exists behind the MCP layer. /entities/query returns the
            # same {entities, total, limit, offset} shape with the entity_type
            # filter applied server-side and snapshots included. Same gotcha as
            # orchestrator.fetch_workflow_definitions and apis/issue_spec.py.
            resp = await client.post(
                f"{NEOTOMA_BASE_URL}/entities/query",
                json={
                    "entity_type": "participation_record",
                    "limit": 200,
                    "include_snapshots": True,
                },
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        log.error(f"load_state_for({work_entity_id}) failed: {exc}")
        raise ParticipationStateUnavailable(
            f"could not read participation state for {work_entity_id}: {exc}"
        ) from exc

    out: dict[str, dict[str, Any]] = {}
    for e in data.get("entities", []):
        snap = e.get("snapshot") or {}
        if snap.get("work_entity_id") != work_entity_id:
            continue
        gate_name = snap.get("gate_name")
        if not gate_name:
            continue
        # Read the schema's singular `artifact_ref` / `skip_reason`. The old
        # code read `artifact_refs` / `error`, which are not declared on this
        # schema and so were never present in a snapshot — the read silently
        # produced an empty list and a None for every record.
        artifact_ref = snap.get("artifact_ref")
        out[gate_name] = {
            "gate_name": gate_name,
            "status": snap.get("status", "pending"),
            "dispatched_at": snap.get("dispatched_at"),
            "satisfied_at": snap.get("satisfied_at"),
            "skipped_at": snap.get("skipped_at"),
            "artifact_refs": [artifact_ref] if artifact_ref else [],
            "error": snap.get("skip_reason"),
        }
    return out


class ParticipationWriteFailed(RuntimeError):
    """
    Raised when a participation_record write did not durably land.

    Covers three cases that were previously indistinguishable from success:
    transport failure, an HTTP error status, and — the one that produced the
    stranded population in ateles#682 — a 200 response carrying
    `store_warnings` / `required_fields_missing`, meaning Neotoma accepted the
    write but stored an incomplete row.
    """


def _write_defects(data: Any) -> list[str]:
    """
    Return human-readable defects reported inside a 200 store response.

    Neotoma does NOT reject a write that omits a schema-required field. It
    returns HTTP 200 with `required_fields_missing` and a `MISSING_REQUIRED_FIELD`
    entry in `store_warnings`. A caller that only checks the status code sees
    success and stores a row that can never be read back correctly.
    """
    if not isinstance(data, dict):
        return []
    defects: list[str] = []
    for missing in data.get("required_fields_missing") or []:
        if isinstance(missing, dict):
            defects.append(
                f"missing required field "
                f"{missing.get('entity_type', 'participation_record')}."
                f"{missing.get('field')}"
            )
        else:
            defects.append(f"missing required field {missing}")
    for warning in data.get("store_warnings") or []:
        if isinstance(warning, dict):
            code = warning.get("code", "STORE_WARNING")
            # Already reported via required_fields_missing; don't double-count.
            if code == "MISSING_REQUIRED_FIELD" and defects:
                continue
            defects.append(f"{code}: {warning.get('message', '')}".strip())
        else:
            defects.append(str(warning))
    for unknown in data.get("unknown_fields") or []:
        defects.append(f"unknown field {unknown}")
    return defects


async def _upsert(payload: dict[str, Any], idempotency_key: str) -> None:
    """
    Send a store request with idempotency_key.

    Raises ParticipationWriteFailed when the write did not land cleanly. This
    used to swallow every failure mode and return None, so a gate that failed
    to record its terminal state was indistinguishable from one that recorded
    it — the row stayed at `dispatched` forever and nothing said why
    (ateles#682). The caller decides what to do; silence is not an option.
    """
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
    except Exception as exc:
        log.error(f"participation_record upsert {idempotency_key} failed: {exc}")
        raise ParticipationWriteFailed(
            f"{idempotency_key}: transport failure: {exc}"
        ) from exc

    if resp.status_code >= 400:
        log.error(
            f"participation_record upsert {idempotency_key} -> "
            f"HTTP {resp.status_code}: {resp.text[:200]}"
        )
        raise ParticipationWriteFailed(
            f"{idempotency_key}: HTTP {resp.status_code}: {resp.text[:200]}"
        )

    try:
        data = resp.json()
    except Exception:  # noqa: BLE001 - a non-JSON 200 is still a defect
        data = None

    defects = _write_defects(data)
    if defects:
        log.error(
            f"participation_record upsert {idempotency_key} stored an "
            f"INCOMPLETE row: {'; '.join(defects)}"
        )
        raise ParticipationWriteFailed(f"{idempotency_key}: {'; '.join(defects)}")
