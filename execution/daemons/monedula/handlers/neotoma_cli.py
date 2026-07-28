"""
handlers/neotoma_cli.py — one place that knows how to write a field to Neotoma.

History: this used to shell out to `neotoma --api-only corrections create`, but
Neotoma removed its REST correction endpoint — POST /corrections and
/entities/<id>/corrections both 404 on the bare HTTP server, so the CLI's
`corrections create` fails at runtime ("Failed to create correction") and no
field ever lands. The write surface is now MCP-only, served at
NEOTOMA_BASE_URL/mcp. This helper calls the MCP `correct` tool directly.

Verified 2026-07-27 against prod 9180: the CLI path 404s; the MCP `correct` tool
lands the field (read-back confirms). Callers still read the value back
(_verify_task_field in monedula.py) because a correction can be dropped as an
idempotency replay (neotoma#1991), so a payment-gating field is never assumed.

Every write returns True/False and logs the real outcome, so a caller can never
report success for a write that did not land. Fail-open: never raises.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

log = logging.getLogger(__name__)

_MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


def _base_url() -> str:
    return (os.environ.get("NEOTOMA_BASE_URL", "") or "http://localhost:9180").rstrip("/")


def _token() -> str:
    return os.environ.get("NEOTOMA_BEARER_TOKEN", "")


def _parse_sse_json(body: str) -> dict | None:
    """Extract the JSON-RPC object from an SSE 'data:' frame (or plain JSON)."""
    body = (body or "").strip()
    if not body:
        return None
    data_payload: str | None = None
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            data_payload = line[len("data:"):].strip()
    raw = data_payload if data_payload is not None else body
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


async def _mcp_correct(
    entity_id: str,
    field: str,
    value: str,
    entity_type: str,
    idempotency_key: str | None,
    timeout: float,
) -> bool:
    import httpx

    url = f"{_base_url()}/mcp"
    headers = dict(_MCP_HEADERS)
    token = _token()
    is_loopback = "localhost" in url or "127.0.0.1" in url
    if token and not is_loopback:
        headers["Authorization"] = f"Bearer {token}"

    # idempotency_key is REQUIRED by the MCP `correct` tool. For a payment-gating
    # field that alternates (e.g. payment_approved true/false) the key must vary
    # by value so a re-approval is not dropped as a replay (neotoma#1991) — the
    # caller still reads the value back, but a value-scoped key avoids the replay
    # in the first place. Derive a stable one when the caller doesn't supply it.
    if not idempotency_key:
        idempotency_key = f"monedula-correct-{entity_id}-{field}-{value}"
    args: dict = {
        "entity_id": entity_id,
        "entity_type": entity_type,
        "field": field,
        "value": str(value),
        "idempotency_key": idempotency_key,
    }

    try:
        async with httpx.AsyncClient(headers=headers, timeout=timeout) as client:
            init = await client.post(url, json={
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "monedula-daemon", "version": "1"},
                },
            })
            init.raise_for_status()
            session_id = init.headers.get("Mcp-Session-Id") or init.headers.get(
                "mcp-session-id"
            )
            if session_id:
                client.headers["Mcp-Session-Id"] = session_id
            try:
                await client.post(url, json={
                    "jsonrpc": "2.0", "method": "notifications/initialized",
                })
            except Exception:  # noqa: BLE001
                pass
            resp = await client.post(url, json={
                "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "correct", "arguments": args},
            })
            resp.raise_for_status()
            obj = _parse_sse_json(resp.text)
            if obj is None or "error" in (obj or {}):
                log.warning(
                    f"[monedula] correct {field} MCP error: "
                    f"{(obj or {}).get('error') if obj else 'unparseable'}"
                )
                return False
            return True
    except Exception as exc:  # noqa: BLE001 — never crash a payment run
        log.warning(f"[monedula] correct {field} MCP transport error: {exc}")
        return False


def correct_field(
    entity_id: str,
    field: str,
    value: str,
    *,
    entity_type: str = "task",
    label: str = "monedula",
    idempotency_key: str | None = None,
    timeout: int = 30,
) -> bool:
    """Write one field to a Neotoma entity via the MCP `correct` tool.

    Returns True only when the MCP call succeeds. Fail-open: never raises.
    Callers that gate money on the write MUST still read the value back
    (idempotency replays can succeed without writing — neotoma#1991).
    """
    if not entity_id or not field:
        return False
    ok = asyncio.run(
        _mcp_correct(entity_id, field, value, entity_type, idempotency_key, float(timeout))
    )
    if ok:
        log.info(f"[{label}] Neotoma {field} correction submitted on {entity_id}.")
    return ok
