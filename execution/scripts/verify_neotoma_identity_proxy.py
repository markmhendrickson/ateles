#!/usr/bin/env python3
"""End-to-end verification for the Neotoma identity proxy.

Spawns `run_neotoma_identity_proxy.sh`, speaks MCP JSON-RPC over stdio,
completes `initialize`, calls the Neotoma MCP tool `get_session_identity`,
and asserts:

- the downstream returns a session payload
- attribution tier is NOT `anonymous` (allowed floor: `unverified_client`)
- `eligible_for_trusted_writes` is boolean and present

Mirrors the verification phase in
`/Users/markmhendrickson/.cursor/plans/cursor_mcp_proxy_4a68cb45.plan.md`.

Usage:

    python3 execution/scripts/verify_neotoma_identity_proxy.py

Exits non-zero with a human-readable failure on any failed assertion.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
LAUNCHER = SCRIPT_DIR / "run_neotoma_identity_proxy.sh"


ACCEPTABLE_TIERS = {"unverified_client", "software", "hardware"}


async def _read_json_line(
    stdout: asyncio.StreamReader, timeout: float = 30.0
) -> dict[str, Any]:
    line = await asyncio.wait_for(stdout.readline(), timeout=timeout)
    if not line:
        raise RuntimeError("proxy closed stdout before responding")
    raw = line.decode("utf-8", errors="replace").strip()
    if not raw:
        return await _read_json_line(stdout, timeout=timeout)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"proxy produced non-JSON stdout line: {raw[:200]} ({exc})"
        ) from exc


async def _drain_stderr(stderr: asyncio.StreamReader) -> None:
    while True:
        chunk = await stderr.readline()
        if not chunk:
            return
        sys.stderr.write(f"[proxy] {chunk.decode('utf-8', errors='replace')}")


def _write_json(stdin: asyncio.StreamWriter, payload: dict[str, Any]) -> None:
    line = json.dumps(payload, ensure_ascii=False) + "\n"
    stdin.write(line.encode("utf-8"))


async def _run() -> int:
    if not LAUNCHER.exists():
        print(f"FAIL: launcher missing at {LAUNCHER}", file=sys.stderr)
        return 2

    env = os.environ.copy()
    env.setdefault("MCP_PROXY_SESSION_PREFLIGHT", "1")

    process = await asyncio.create_subprocess_exec(
        str(LAUNCHER),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    assert process.stdin and process.stdout and process.stderr

    stderr_task = asyncio.create_task(_drain_stderr(process.stderr))

    try:
        initialize = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {},
            },
        }
        _write_json(process.stdin, initialize)
        await process.stdin.drain()
        init_response = await _read_json_line(process.stdout)
        if "error" in init_response:
            print(
                f"FAIL: initialize returned error: {init_response['error']}",
                file=sys.stderr,
            )
            return 1

        initialized_notification = {
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
            "params": {},
        }
        _write_json(process.stdin, initialized_notification)
        await process.stdin.drain()

        tool_call = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "get_session_identity", "arguments": {}},
        }
        _write_json(process.stdin, tool_call)
        await process.stdin.drain()
        session_response = await _read_json_line(process.stdout)

        if "error" in session_response:
            print(
                f"FAIL: get_session_identity returned error: {session_response['error']}",
                file=sys.stderr,
            )
            return 1

        result = session_response.get("result", {})
        content_items = result.get("content") or []
        session_payload: Optional[dict[str, Any]] = None
        for item in content_items:
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if isinstance(text, str):
                try:
                    session_payload = json.loads(text)
                    break
                except json.JSONDecodeError:
                    continue
        if session_payload is None and isinstance(
            result.get("structuredContent"), dict
        ):
            session_payload = result["structuredContent"]

        if session_payload is None:
            print(
                "FAIL: could not parse session payload from get_session_identity result",
                file=sys.stderr,
            )
            print(json.dumps(session_response, indent=2), file=sys.stderr)
            return 1

        attribution = session_payload.get("attribution", {})
        tier = attribution.get("tier")
        eligible = session_payload.get("eligible_for_trusted_writes")
        thumbprint = attribution.get("agent_thumbprint")

        print(
            f"PASS candidate: tier={tier} thumbprint={thumbprint or '<none>'} eligible={eligible}"
        )

        if tier not in ACCEPTABLE_TIERS:
            print(
                f"FAIL: attribution tier {tier!r} is not in acceptable set {sorted(ACCEPTABLE_TIERS)}",
                file=sys.stderr,
            )
            return 1
        if not isinstance(eligible, bool):
            print(
                f"FAIL: eligible_for_trusted_writes missing or not bool: {eligible!r}",
                file=sys.stderr,
            )
            return 1
        print("PASS: proxy preserved session and reported non-anonymous attribution")
        return 0
    finally:
        try:
            process.stdin.close()
        except Exception:
            pass
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
        stderr_task.cancel()
        try:
            await stderr_task
        except asyncio.CancelledError:
            pass


def main() -> None:
    try:
        exit_code = asyncio.run(_run())
    except Exception as exc:
        print(f"FAIL: verification raised: {exc}", file=sys.stderr)
        sys.exit(3)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
