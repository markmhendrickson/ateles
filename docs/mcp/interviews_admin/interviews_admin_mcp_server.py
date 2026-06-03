#!/usr/bin/env python3
"""
MCP wrapper for interviews admin API.

Uses Authorization: Bearer <ADMIN_PASSPHRASE> against the interviews admin API.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any
from urllib.parse import urlencode, urlparse

import httpx

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

DEFAULT_BASE_URL = "https://interviews.markmhendrickson.com"
ADMIN_API_PATH = "/api/admin"
REQUEST_TIMEOUT_SECONDS = 30.0

app = Server("interviews-admin")


def _error_response(message: str) -> list[TextContent]:
    return [
        TextContent(type="text", text=json.dumps({"success": False, "error": message}))
    ]


def _base_url() -> str:
    return os.getenv("INTERVIEWS_ADMIN_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


def _admin_passphrase() -> str:
    value = os.getenv("INTERVIEWS_ADMIN_PASSPHRASE") or os.getenv(
        "ADMIN_PASSPHRASE", ""
    )
    return value.strip()


def _enforce_env_alignment() -> str | None:
    enforce = os.getenv("INTERVIEWS_ADMIN_ENFORCE_NEOTOMA_ENV", "1").strip().lower()
    if enforce in {"0", "false", "no", "off"}:
        return None

    app_env = (os.getenv("INTERVIEWS_ADMIN_ENV") or "dev").strip().lower()
    if app_env not in {"dev", "prod"}:
        parsed = urlparse(_base_url())
        host = (parsed.hostname or "").lower()
        if host in {"localhost", "127.0.0.1"} or host.endswith(".local"):
            app_env = "dev"
        elif "dev" in host:
            app_env = "dev"
        else:
            app_env = "prod"

    neotoma_env = (
        (
            os.getenv("INTERVIEWS_ADMIN_NEOTOMA_ENV")
            or os.getenv("NEOTOMA_ENV")
            or os.getenv("NEOTOMA_TARGET_ENV")
            or "dev"
        )
        .strip()
        .lower()
    )
    if neotoma_env not in {"dev", "prod"}:
        neotoma_env = "dev"

    if app_env != neotoma_env:
        return (
            "Environment mismatch: interviews app env is "
            f"{app_env}, but Neotoma env is {neotoma_env}. Refusing request."
        )

    return None


async def _request_admin_api(
    method: str,
    *,
    query: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    passphrase = _admin_passphrase()
    if not passphrase:
        return {
            "success": False,
            "error": "Missing INTERVIEWS_ADMIN_PASSPHRASE (or ADMIN_PASSPHRASE) env var",
        }

    env_mismatch_error = _enforce_env_alignment()
    if env_mismatch_error:
        return {
            "success": False,
            "error": env_mismatch_error,
        }

    params = query or {}
    url = f"{_base_url()}{ADMIN_API_PATH}"
    if params:
        url = f"{url}?{urlencode(params, doseq=True)}"

    headers = {
        "Authorization": f"Bearer {passphrase}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
        response = await client.request(method, url, headers=headers, json=body)
        payload: Any = None
        try:
            payload = response.json()
        except json.JSONDecodeError:
            payload = {"raw": response.text}

    if response.status_code >= 400:
        return {
            "success": False,
            "status": response.status_code,
            "error": payload.get("error", f"HTTP {response.status_code}")
            if isinstance(payload, dict)
            else f"HTTP {response.status_code}",
            "payload": payload,
        }

    return {"success": True, "status": response.status_code, "data": payload}


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="interviews_admin_get_overview",
            description="Get admin overview: results + contacts for an interview slug.",
            inputSchema={
                "type": "object",
                "properties": {
                    "interviewSlug": {
                        "type": "string",
                        "description": "Interview slug (default: ai)",
                    }
                },
            },
        ),
        Tool(
            name="interviews_admin_list_results",
            description="List interview results for an interview slug.",
            inputSchema={
                "type": "object",
                "properties": {
                    "interviewSlug": {
                        "type": "string",
                        "description": "Interview slug (default: ai)",
                    }
                },
            },
        ),
        Tool(
            name="interviews_admin_get_result",
            description="Fetch a single interview result by sessionId.",
            inputSchema={
                "type": "object",
                "properties": {
                    "sessionId": {"type": "string"},
                    "interviewSlug": {"type": "string"},
                },
                "required": ["sessionId"],
            },
        ),
        Tool(
            name="interviews_admin_delete_result",
            description="Delete a stored interview result by sessionId.",
            inputSchema={
                "type": "object",
                "properties": {
                    "sessionId": {"type": "string"},
                    "interviewSlug": {"type": "string"},
                },
                "required": ["sessionId"],
            },
        ),
        Tool(
            name="interviews_admin_list_contacts",
            description="List all contact codes for an interview slug.",
            inputSchema={
                "type": "object",
                "properties": {
                    "interviewSlug": {
                        "type": "string",
                        "description": "Interview slug (default: ai)",
                    }
                },
            },
        ),
        Tool(
            name="interviews_admin_list_events",
            description="List interview lifecycle events, optionally filtered by code or sessionId.",
            inputSchema={
                "type": "object",
                "properties": {
                    "interviewSlug": {
                        "type": "string",
                        "description": "Interview slug (default: ai)",
                    },
                    "code": {"type": "string"},
                    "sessionId": {"type": "string"},
                },
            },
        ),
        Tool(
            name="interviews_admin_upsert_contact",
            description="Create or update an interview contact code.",
            inputSchema={
                "type": "object",
                "properties": {
                    "code": {"type": "string"},
                    "name": {"type": "string"},
                    "email": {"type": "string"},
                    "context": {"type": "string"},
                    "source": {"type": "string"},
                    "interviewSlug": {"type": "string"},
                },
                "required": ["code", "name"],
            },
        ),
        Tool(
            name="interviews_admin_delete_contact",
            description="Delete a contact code by code value.",
            inputSchema={
                "type": "object",
                "properties": {
                    "code": {"type": "string"},
                    "interviewSlug": {"type": "string"},
                },
                "required": ["code"],
            },
        ),
        Tool(
            name="interviews_admin_send_invite",
            description="Send interview invite email via SendGrid for a contact code.",
            inputSchema={
                "type": "object",
                "properties": {
                    "code": {"type": "string"},
                    "interviewSlug": {"type": "string"},
                    "senderName": {"type": "string"},
                },
                "required": ["code"],
            },
        ),
        Tool(
            name="interviews_admin_get_text_invite",
            description="Generate text invite copy for manual sending.",
            inputSchema={
                "type": "object",
                "properties": {
                    "code": {"type": "string"},
                    "interviewSlug": {"type": "string"},
                    "senderName": {"type": "string"},
                },
                "required": ["code"],
            },
        ),
        Tool(
            name="interviews_admin_confirm_text_invite",
            description="Confirm manual text invite delivery for lifecycle tracking.",
            inputSchema={
                "type": "object",
                "properties": {
                    "code": {"type": "string"},
                    "interviewSlug": {"type": "string"},
                },
                "required": ["code"],
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: Any) -> list[TextContent]:
    if not isinstance(arguments, dict):
        return _error_response("Invalid arguments. Expected an object.")

    interview_slug = str(arguments.get("interviewSlug", "ai")).strip() or "ai"

    try:
        if name == "interviews_admin_get_overview":
            result = await _request_admin_api(
                "GET", query={"resource": "overview", "interview": interview_slug}
            )
            return [TextContent(type="text", text=json.dumps(result))]

        if name == "interviews_admin_list_results":
            result = await _request_admin_api(
                "GET", query={"resource": "results", "interview": interview_slug}
            )
            return [TextContent(type="text", text=json.dumps(result))]

        if name == "interviews_admin_get_result":
            session_id = str(arguments.get("sessionId", "")).strip()
            if not session_id:
                return _error_response("sessionId is required")
            result = await _request_admin_api(
                "GET",
                query={
                    "resource": "results",
                    "sessionId": session_id,
                    "interview": interview_slug,
                },
            )
            return [TextContent(type="text", text=json.dumps(result))]

        if name == "interviews_admin_delete_result":
            session_id = str(arguments.get("sessionId", "")).strip()
            if not session_id:
                return _error_response("sessionId is required")
            result = await _request_admin_api(
                "DELETE",
                query={
                    "resource": "results",
                    "sessionId": session_id,
                    "interview": interview_slug,
                },
            )
            return [TextContent(type="text", text=json.dumps(result))]

        if name == "interviews_admin_list_contacts":
            result = await _request_admin_api(
                "GET", query={"resource": "contacts", "interview": interview_slug}
            )
            return [TextContent(type="text", text=json.dumps(result))]

        if name == "interviews_admin_list_events":
            query: dict[str, Any] = {"resource": "events", "interview": interview_slug}
            code = str(arguments.get("code", "")).strip()
            session_id = str(arguments.get("sessionId", "")).strip()
            if code:
                query["code"] = code
            if session_id:
                query["sessionId"] = session_id
            result = await _request_admin_api("GET", query=query)
            return [TextContent(type="text", text=json.dumps(result))]

        if name == "interviews_admin_upsert_contact":
            code = str(arguments.get("code", "")).strip()
            name_value = str(arguments.get("name", "")).strip()
            if not code or not name_value:
                return _error_response("code and name are required")
            body = {
                "code": code,
                "name": name_value,
                "email": arguments.get("email"),
                "context": arguments.get("context"),
                "source": arguments.get("source"),
                "interviewSlug": interview_slug,
            }
            result = await _request_admin_api(
                "POST",
                query={"resource": "contacts", "interview": interview_slug},
                body=body,
            )
            return [TextContent(type="text", text=json.dumps(result))]

        if name == "interviews_admin_delete_contact":
            code = str(arguments.get("code", "")).strip()
            if not code:
                return _error_response("code is required")
            result = await _request_admin_api(
                "DELETE",
                query={
                    "resource": "contacts",
                    "code": code,
                    "interview": interview_slug,
                },
            )
            return [TextContent(type="text", text=json.dumps(result))]

        if name == "interviews_admin_send_invite":
            code = str(arguments.get("code", "")).strip()
            if not code:
                return _error_response("code is required")
            body = {
                "code": code,
                "interviewSlug": interview_slug,
                "method": "email",
                "senderName": arguments.get("senderName"),
            }
            result = await _request_admin_api(
                "POST",
                query={"resource": "invite", "interview": interview_slug},
                body=body,
            )
            return [TextContent(type="text", text=json.dumps(result))]

        if name == "interviews_admin_get_text_invite":
            code = str(arguments.get("code", "")).strip()
            if not code:
                return _error_response("code is required")
            body = {
                "code": code,
                "interviewSlug": interview_slug,
                "method": "text",
                "senderName": arguments.get("senderName"),
            }
            result = await _request_admin_api(
                "POST",
                query={"resource": "invite", "interview": interview_slug},
                body=body,
            )
            return [TextContent(type="text", text=json.dumps(result))]

        if name == "interviews_admin_confirm_text_invite":
            code = str(arguments.get("code", "")).strip()
            if not code:
                return _error_response("code is required")
            body = {
                "code": code,
                "interviewSlug": interview_slug,
                "method": "text_confirm",
            }
            result = await _request_admin_api(
                "POST",
                query={"resource": "invite", "interview": interview_slug},
                body=body,
            )
            return [TextContent(type="text", text=json.dumps(result))]

        return _error_response(f"Unknown tool: {name}")
    except Exception as exc:  # noqa: BLE001
        return _error_response(str(exc))


async def main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
