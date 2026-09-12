#!/usr/bin/env python3
"""
End-to-end smoke test for the ateles MCP server (ateles#267).

Spawns the real server.py subprocess over stdio (no mocking) and drives the
actual MCP JSON-RPC protocol to confirm:

  - initialize succeeds and the server advertises SERVER_INSTRUCTIONS
  - tools/list returns exactly the 4 documented tools with valid schemas
  - tools/call against a real tool, with no NEOTOMA_BEARER_TOKEN set,
    degrades gracefully (structured error, not a crash/exception)

This is the effect-level counterpart to test_server.py's in-process unit
tests (which exercise the pure _route_task/_resolve_checkpoint/_get_swarm_roster
logic via monkeypatching) — it verifies the server actually speaks MCP
correctly end-to-end, matching the sibling pattern in
execution/mcp/mcp_tool_grant_proxy/test_proxy_smoke.py.

Run: python3 execution/mcp/ateles/test_server_smoke.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

_HERE = Path(__file__).resolve().parent
SERVER = _HERE / "server.py"

TIMEOUT_SECONDS = 10.0


def _send_and_collect(proc: subprocess.Popen, messages: list[dict], expect_ids: set[int]) -> dict[int, dict]:
    """Write messages, then read responses with a wall-clock timeout.

    A plain readline() loop can hang forever if the server wedges — this
    kills the process and gives up rather than blocking the test run.
    """
    for msg in messages:
        proc.stdin.write(json.dumps(msg) + "\n")
    proc.stdin.flush()

    responses: dict[int, dict] = {}
    timed_out = threading.Event()
    timer = threading.Timer(TIMEOUT_SECONDS, lambda: (timed_out.set(), proc.kill()))
    timer.start()
    try:
        while len(responses) < len(expect_ids):
            line = proc.stdout.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            rid = parsed.get("id")
            if rid in expect_ids:
                responses[rid] = parsed
    finally:
        timer.cancel()
    if timed_out.is_set():
        raise AssertionError(f"timed out after {TIMEOUT_SECONDS}s waiting for responses {expect_ids}")
    return responses


def _stderr_tail(proc: subprocess.Popen) -> str:
    """Best-effort stderr read for diagnostics — never blocks on a live pipe."""
    if proc.poll() is None:
        return "(process still running, stderr not read to avoid blocking)"
    try:
        return proc.stderr.read()
    except Exception:
        return "(stderr unavailable)"


def _start_server() -> subprocess.Popen:
    env = {**os.environ, "NEOTOMA_BEARER_TOKEN": ""}
    return subprocess.Popen(
        [sys.executable, str(SERVER)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        text=True,
        bufsize=1,
    )


def test_initialize_and_tools_list():
    proc = _start_server()
    try:
        responses = _send_and_collect(
            proc,
            [
                {
                    "jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "smoke-test", "version": "0.0.1"},
                    },
                },
            ],
            expect_ids={1},
        )
        assert 1 in responses, f"no initialize response, stderr={_stderr_tail(proc)}"
        result = responses[1].get("result", {})
        assert result.get("serverInfo", {}).get("name") == "ateles", f"got {result}"
        assert "connected to Ateles" in result.get("instructions", ""), f"got {result}"

        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}) + "\n")
        proc.stdin.flush()

        responses = _send_and_collect(
            proc,
            [{"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}],
            expect_ids={2},
        )
        assert 2 in responses, f"no tools/list response, stderr={_stderr_tail(proc)}"
        tools = responses[2]["result"]["tools"]
        names = {t["name"] for t in tools}
        assert names == {
            "get_swarm_roster", "route_task", "list_checkpoints", "resolve_checkpoint",
            # Read-only swarm observability (see server.py).
            "get_gate_status", "list_pipeline_queue", "get_dispatch_health",
            "check_swarm_fact",
        }, names
        route_task = next(t for t in tools if t["name"] == "route_task")
        assert route_task["inputSchema"]["required"] == ["task_description"]
    finally:
        proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def test_tool_call_degrades_gracefully_without_token():
    proc = _start_server()
    try:
        _send_and_collect(
            proc,
            [
                {
                    "jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "smoke-test", "version": "0.0.1"},
                    },
                },
            ],
            expect_ids={1},
        )
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}) + "\n")
        proc.stdin.flush()

        responses = _send_and_collect(
            proc,
            [
                {
                    "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                    "params": {"name": "get_swarm_roster", "arguments": {}},
                },
            ],
            expect_ids={2},
        )
        assert 2 in responses, f"no tools/call response, stderr={_stderr_tail(proc)}"
        content = responses[2]["result"]["content"][0]["text"]
        payload = json.loads(content)
        assert "error" in payload, f"expected graceful error, got {payload}"
    finally:
        proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def _run_all() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"  PASS {t.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"  FAIL {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_run_all())
