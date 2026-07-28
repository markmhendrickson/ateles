"""
neotoma_mcp.py — minimal HTTP-MCP client for daemon writes to Neotoma.

Neotoma removed its REST write API (POST /observations, /create_relationship);
the write surface is now MCP-only, served at NEOTOMA_BASE_URL/mcp as JSON-RPC
over an SSE-framed HTTP response. This module gives a daemon a direct
`store` / `create_relationship` call without spawning a `claude` subprocess
(the heavyweight pattern in apis/skill_runner.py) — it speaks the transport
itself.

Flow per call session:
  1. POST initialize        → capture the Mcp-Session-Id response header
  2. POST notifications/initialized (best-effort; some servers require it)
  3. POST tools/call {name, arguments}  with the session header
  4. Parse the SSE body ("event: message\\ndata: {json}") → JSON-RPC result

Auth: sends Authorization: Bearer <token> when a token is provided; local
loopback (TRUST_PROD_LOOPBACK) answers without one, so an empty token is valid
against localhost and only rejected by the remote.

Every function is fail-soft: any transport/parse error logs and returns None
(store) / False (relationship). Callers MUST treat None as "write did not land"
and never claim success on it — see the swarm notification contract
(ent_3e307b060661e8d3a45f07f0 req 2).
"""

from __future__ import annotations

import json
import logging
from typing import Any

log = logging.getLogger("turdus.neotoma_mcp")

_MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


def _parse_sse_json(body: str) -> dict | None:
    """Extract the JSON-RPC object from an SSE 'data:' frame (or plain JSON)."""
    body = (body or "").strip()
    if not body:
        return None
    # SSE frame: one or more "data: {...}" lines. Take the last data payload.
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


async def _mcp_tools_call(
    base_url: str,
    token: str,
    tool_name: str,
    arguments: dict[str, Any],
    timeout: float = 15.0,
) -> dict | None:
    """Run one MCP tools/call and return the parsed JSON-RPC result dict, or None."""
    import httpx

    if not base_url:
        log.debug("[neotoma_mcp] no NEOTOMA_BASE_URL — skipping write")
        return None

    url = f"{base_url.rstrip('/')}/mcp"
    headers = dict(_MCP_HEADERS)
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        async with httpx.AsyncClient(headers=headers, timeout=timeout) as client:
            # 1. initialize — establishes the session and yields Mcp-Session-Id.
            init = await client.post(url, json={
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "turdus-daemon", "version": "1"},
                },
            })
            init.raise_for_status()
            session_id = init.headers.get("Mcp-Session-Id") or init.headers.get(
                "mcp-session-id"
            )
            if session_id:
                headers["Mcp-Session-Id"] = session_id
                client.headers["Mcp-Session-Id"] = session_id

            # 2. notifications/initialized — best-effort; ignore its outcome.
            try:
                await client.post(url, json={
                    "jsonrpc": "2.0", "method": "notifications/initialized",
                })
            except Exception:  # noqa: BLE001
                pass

            # 3. tools/call
            resp = await client.post(url, json={
                "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": tool_name, "arguments": arguments},
            })
            resp.raise_for_status()
            obj = _parse_sse_json(resp.text)
            if obj is None:
                log.error(f"[neotoma_mcp] {tool_name}: unparseable MCP response")
                return None
            if "error" in obj:
                log.error(f"[neotoma_mcp] {tool_name} JSON-RPC error: {obj['error']}")
                return None
            return obj.get("result") or {}
    except Exception as exc:  # noqa: BLE001
        log.error(f"[neotoma_mcp] {tool_name} transport error: {exc}")
        return None


def _extract_entity_id(result: dict | None) -> str | None:
    """Pull the first entity_id from a `store` tool result.

    The MCP tool result wraps content as [{type:'text', text:'<json>'}]; the
    inner JSON is the same store envelope the REST API returned
    ({entity_id} or {entities:[{entity_id}]}).
    """
    if not result:
        return None
    # Unwrap structured content if present, else the text block.
    payload: Any = result.get("structuredContent")
    if payload is None:
        content = result.get("content") or []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                try:
                    payload = json.loads(block.get("text") or "")
                    break
                except (ValueError, TypeError):
                    continue
    if not isinstance(payload, dict):
        return None
    eid = payload.get("entity_id")
    if eid:
        return str(eid)
    entities = payload.get("entities") or []
    if entities and isinstance(entities[0], dict):
        return entities[0].get("entity_id")
    return None


async def store_entity(
    base_url: str,
    token: str,
    entity: dict[str, Any],
    idempotency_key: str | None = None,
    timeout: float = 15.0,
) -> str | None:
    """Store one entity via the MCP `store` tool. Returns its entity_id or None."""
    args: dict[str, Any] = {"entities": [entity]}
    if idempotency_key:
        args["idempotency_key"] = idempotency_key
    result = await _mcp_tools_call(base_url, token, "store", args, timeout)
    entity_id = _extract_entity_id(result)
    if entity_id:
        log.info(f"[neotoma_mcp] stored {entity.get('entity_type')} → {entity_id}")
    return entity_id


async def create_relationship(
    base_url: str,
    token: str,
    source_entity_id: str,
    target_entity_id: str,
    relationship_type: str,
    idempotency_key: str | None = None,
    timeout: float = 15.0,
) -> bool:
    """Create one relationship via MCP. Returns True on success."""
    args: dict[str, Any] = {
        "source_entity_id": source_entity_id,
        "target_entity_id": target_entity_id,
        "relationship_type": relationship_type,
    }
    if idempotency_key:
        args["idempotency_key"] = idempotency_key
    result = await _mcp_tools_call(
        base_url, token, "create_relationship", args, timeout
    )
    return result is not None
