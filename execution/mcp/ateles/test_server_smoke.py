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
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
SERVER = _HERE / "server.py"

# The reply to the first `initialize` also covers process startup (interpreter
# start, imports, and the server's agent_policy reachability probe), so it gets
# a separate, realistic allowance. Every later request stays under the request
# limit, so a slow or wedged request still fails fast.
STARTUP_TIMEOUT_SECONDS = 60.0
REQUEST_TIMEOUT_SECONDS = 10.0


def _closed_local_url() -> tuple[str, socket.socket]:
    """A loopback URL nothing listens on, so connections are refused at once.

    server.py probes Neotoma for agent_policy before it answers `initialize`.
    Left at its default it reaches the hosted instance over the network, and a
    slow or unresponsive host holds the first reply for as long as the probe's
    own timeouts and retries allow. A refused loopback connection keeps that
    probe on its existing failure path without depending on the network.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    return f"http://127.0.0.1:{sock.getsockname()[1]}", sock


def _send_and_collect(
    proc: subprocess.Popen,
    messages: list[dict],
    expect_ids: set[int],
    timeout: float = REQUEST_TIMEOUT_SECONDS,
) -> dict[int, dict]:
    """Write messages, then read responses with a wall-clock timeout.

    A plain readline() loop can hang forever if the server wedges — this
    kills the process and gives up rather than blocking the test run.
    Pass `timeout=STARTUP_TIMEOUT_SECONDS` only for the first `initialize`.
    """
    for msg in messages:
        proc.stdin.write(json.dumps(msg) + "\n")
    proc.stdin.flush()

    responses: dict[int, dict] = {}
    timed_out = threading.Event()
    timer = threading.Timer(timeout, lambda: (timed_out.set(), proc.kill()))
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
        raise AssertionError(f"timed out after {timeout}s waiting for responses {expect_ids}")
    return responses


def _drain_stderr(proc: subprocess.Popen) -> None:
    """Read stderr continuously so the server can never block on a full pipe."""
    try:
        for line in proc.stderr:
            proc._stderr_lines.append(line)
    except Exception:  # noqa: BLE001 — diagnostics only
        pass


def _stderr_tail(proc: subprocess.Popen) -> str:
    """The server's stderr collected so far — never blocks on a live pipe."""
    return "".join(proc._stderr_lines[-50:]) or "(no stderr output)"


def _start_server() -> subprocess.Popen:
    denial_store = tempfile.TemporaryDirectory(prefix="ateles-mcp-denials-")
    closed_url, reserved_socket = _closed_local_url()
    env = {
        **os.environ,
        "NEOTOMA_BEARER_TOKEN": "",
        "APIS_CHECKPOINT_DENIAL_DIR": denial_store.name,
        "NEOTOMA_BASE_URL": closed_url,
    }
    try:
        proc = subprocess.Popen(
            [sys.executable, str(SERVER)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            text=True,
            bufsize=1,
        )
    except Exception:
        reserved_socket.close()
        denial_store.cleanup()
        raise
    proc._checkpoint_denial_store = denial_store
    proc._reserved_socket = reserved_socket
    proc._stderr_lines = []
    threading.Thread(target=_drain_stderr, args=(proc,), daemon=True).start()
    return proc


def _stop_server(proc: subprocess.Popen) -> None:
    proc.kill()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    proc._reserved_socket.close()
    proc._checkpoint_denial_store.cleanup()


def test_startup_allowance_covers_a_slow_start():
    """A delayed initialize reply fits the startup limit, not the request limit."""
    assert STARTUP_TIMEOUT_SECONDS > REQUEST_TIMEOUT_SECONDS
    child_code = (
        "import json, sys, time\n"
        "for line in sys.stdin:\n"
        "    request = json.loads(line)\n"
        "    time.sleep(0.4)\n"
        "    print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], "
        "'result': {'ready': True}}), flush=True)\n"
    )
    message = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}

    def collect_with_limit(limit: float) -> dict[int, dict]:
        proc = subprocess.Popen(
            [sys.executable, "-u", "-c", child_code],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        try:
            return _send_and_collect(proc, [message], expect_ids={1}, timeout=limit)
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)
            proc.stdin.close()
            proc.stdout.close()
            proc.stderr.close()

    # Scaled limits exercise both sides without waiting for the production
    # 10-second request timeout or 60-second startup allowance.
    request_limit = 0.1
    startup_allowance = 1.0
    assert request_limit < 0.4 < startup_allowance
    try:
        collect_with_limit(request_limit)
    except AssertionError as exc:
        assert f"timed out after {request_limit}s" in str(exc), str(exc)
    else:
        raise AssertionError("slow initialize unexpectedly passed the request limit")
    responses = collect_with_limit(startup_allowance)
    assert responses[1]["result"]["ready"] is True


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
            timeout=STARTUP_TIMEOUT_SECONDS,
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
            "get_task_timeline", "watch_swarm",
        }, names
        route_task = next(t for t in tools if t["name"] == "route_task")
        assert route_task["inputSchema"]["required"] == ["task_description"]
    finally:
        _stop_server(proc)


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
            timeout=STARTUP_TIMEOUT_SECONDS,
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
        _stop_server(proc)


def test_startup_allowance_does_not_widen_request_limit():
    """A later request that never answers fails at the request limit.

    The startup allowance is for the first `initialize` only. If it leaked
    into later requests, a wedged request would hang for the full allowance
    instead of failing at REQUEST_TIMEOUT_SECONDS.
    """
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
            timeout=STARTUP_TIMEOUT_SECONDS,
        )
        assert 1 in responses, f"no initialize response, stderr={_stderr_tail(proc)}"

        # A notification gets no reply, so waiting for id 3 after it is a
        # request that never answers.
        started = time.monotonic()
        try:
            _send_and_collect(
                proc,
                [{"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}],
                expect_ids={3},
            )
        except AssertionError as exc:
            elapsed = time.monotonic() - started
            assert f"timed out after {REQUEST_TIMEOUT_SECONDS}s" in str(exc), str(exc)
        else:
            raise AssertionError("a request that never answers did not time out")
        assert REQUEST_TIMEOUT_SECONDS <= elapsed < STARTUP_TIMEOUT_SECONDS, (
            f"unanswered request failed after {elapsed:.1f}s, expected about "
            f"{REQUEST_TIMEOUT_SECONDS}s and well under {STARTUP_TIMEOUT_SECONDS}s"
        )
    finally:
        _stop_server(proc)


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
