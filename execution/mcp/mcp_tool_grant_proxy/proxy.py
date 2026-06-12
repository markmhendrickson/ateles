#!/usr/bin/env python3
"""
mcp_tool_grant_proxy — generic MCP tool-call grant enforcement (ateles#26).

Sits between `claude --print` and a downstream MCP server, speaking the MCP
stdio JSON-RPC protocol on both sides. It transparently forwards every message
EXCEPT `tools/call`, which it intercepts:

  1. Read the agent's identity + grant id from the environment:
       ATELES_AGENT_SUB        e.g. "monedula@ateles-swarm"  (required)
       ATELES_AGENT_GRANT_ID   optional explicit grant entity id (else looked
                               up by sub)
       MCP_GRANT_SERVER_NAME   logical server name used as the "<server>" half
                               of the "<server>:<tool>" grant key (required)
  2. Look up the agent_grant in Neotoma via lib/daemon_runtime.GrantChecker.
  3. Enforce `capabilities.tools` (op == "tool:<server>:<tool>") plus any
     param_constraints against the call arguments.
  4. Allowed → forward to the downstream server and relay its response.
     Denied  → synthesize a JSON-RPC error result (isError) WITHOUT forwarding,
               so the side-effecting tool is never reached.
  5. Either way, emit a `tool_call_observation` to Neotoma (result allowed |
     denied) for a unified cross-MCP audit trail.

Permissive fallback: if Neotoma is unreachable, or the agent has no grant that
declares ANY tool capabilities, calls are allowed (advisory mode) — matching
GrantChecker.check_tool semantics. This lets un-migrated agents keep working
while migrated agents get hard enforcement.

Launch pattern (in .mcp.json or Anthus dispatch config):

    {
      "command": "python",
      "args": [
        ".../mcp_tool_grant_proxy/proxy.py",
        "--server-name", "parquet",
        "--", "python", ".../parquet_mcp/server.py"
      ]
    }

Everything after `--` is the downstream server command, launched as a child
subprocess whose stdio this proxy bridges.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional

# ── Path bootstrap ──────────────────────────────────────────────────────────────
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from lib.daemon_runtime.grant_checker import (  # noqa: E402
    GrantChecker,
    check_param_constraints,
)

# Session-integrity (ateles#6 layer 2): observe-only invariant riding the same
# tools/call chokepoint. Soft-imported so a refactor of this sibling module can
# never break the grant-enforcement path.
try:
    from session_integrity import SessionIntegrityTracker  # noqa: E402
except Exception:  # noqa: BLE001
    try:
        from execution.mcp.mcp_tool_grant_proxy.session_integrity import (  # noqa: E402
            SessionIntegrityTracker,
        )
    except Exception:  # noqa: BLE001
        SessionIntegrityTracker = None  # type: ignore

DEBUG = os.environ.get("MCP_GRANT_PROXY_DEBUG") == "1"
log = logging.getLogger("mcp_tool_grant_proxy")
logging.basicConfig(
    level=logging.DEBUG if DEBUG else logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    stream=sys.stderr,
)

NEOTOMA_BASE_URL = os.environ.get(
    "NEOTOMA_BASE_URL", "https://neotoma.markmhendrickson.com"
).rstrip("/")
NEOTOMA_BEARER_TOKEN = os.environ.get("NEOTOMA_BEARER_TOKEN", "")


class GrantEnforcer:
    """Caches a GrantChecker for the agent sub and enforces tool calls."""

    def __init__(self, agent_sub: str, server_name: str) -> None:
        self.agent_sub = agent_sub
        self.server_name = server_name
        self._checker: Optional[GrantChecker] = None

    def _checker_loaded(self) -> GrantChecker:
        if self._checker is None:
            self._checker = GrantChecker(self.agent_sub).load()
        return self._checker

    def enforce(self, tool: str, arguments: dict) -> tuple[bool, str]:
        """Return (allowed, reason). reason is '' when allowed."""
        if not self.agent_sub:
            # No identity configured → advisory passthrough.
            return True, ""
        checker = self._checker_loaded()
        allowed, constraints = checker.check_tool(self.server_name, tool)
        if not allowed:
            return False, f"no grant for tool {self.server_name}:{tool}"
        if constraints:
            ok, reason = check_param_constraints(constraints, arguments or {})
            if not ok:
                return False, f"param constraint failed: {reason}"
        return True, ""


def _emit_tool_call_observation(
    agent_sub: str,
    server_name: str,
    tool: str,
    result: str,
    reason: str = "",
) -> None:
    """Best-effort audit write to Neotoma. Never raises into the proxy path."""
    if not NEOTOMA_BEARER_TOKEN:
        return
    try:
        import httpx

        entity = {
            "entity_type": "tool_call_observation",
            "agent_sub": agent_sub,
            "mcp_server": server_name,
            "tool_name": tool,
            "result": result,  # "allowed" | "denied"
            "reason": reason,
            "observed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        httpx.post(
            f"{NEOTOMA_BASE_URL}/store",
            json={
                "entities": [entity],
                "idempotency_key": f"toolcall-{agent_sub}-{server_name}-{tool}-{time.time_ns()}",
                "observation_source": "workflow_state",
            },
            headers={"Authorization": f"Bearer {NEOTOMA_BEARER_TOKEN}"},
            timeout=5,
        )
    except Exception as exc:  # noqa: BLE001
        log.debug(f"tool_call_observation emit failed (non-fatal): {exc}")


def _deny_response(request_id: Any, reason: str) -> dict:
    """Build an MCP tools/call result that signals denial without side effects."""
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {
            "content": [
                {
                    "type": "text",
                    "text": f"DENIED by mcp_tool_grant_proxy: {reason}",
                }
            ],
            "isError": True,
        },
    }


async def _pump_downstream_to_client(
    downstream_stdout: asyncio.StreamReader,
    client_writer,
) -> None:
    """Relay raw bytes from the downstream server's stdout to our stdout."""
    while True:
        line = await downstream_stdout.readline()
        if not line:
            break
        client_writer.write(line)
        await client_writer.drain()


async def run_proxy(server_name: str, downstream_cmd: list[str]) -> int:
    agent_sub = os.environ.get("ATELES_AGENT_SUB", "")
    enforcer = GrantEnforcer(agent_sub, server_name)
    tracker = (
        SessionIntegrityTracker(agent_sub, server_name)
        if SessionIntegrityTracker is not None
        else None
    )

    log.info(
        f"[proxy] server={server_name} agent_sub={agent_sub or '<none/advisory>'} "
        f"downstream={' '.join(downstream_cmd)}"
    )

    # Launch the downstream MCP server.
    downstream = await asyncio.create_subprocess_exec(
        *downstream_cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=None,  # inherit — downstream logs go to our stderr
    )

    # Bridge our stdin/stdout to the asyncio world.
    loop = asyncio.get_event_loop()
    client_reader = asyncio.StreamReader()
    await loop.connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(client_reader), sys.stdin
    )
    # stdout writer
    w_transport, w_protocol = await loop.connect_write_pipe(
        asyncio.streams.FlowControlMixin, sys.stdout
    )
    client_writer = asyncio.StreamWriter(w_transport, w_protocol, None, loop)

    # Relay downstream → client continuously.
    relay_task = asyncio.ensure_future(
        _pump_downstream_to_client(downstream.stdout, client_writer)
    )

    # Read client → (maybe intercept) → downstream.
    try:
        while True:
            line = await client_reader.readline()
            if not line:
                break
            forwarded = await _handle_client_line(
                line, enforcer, server_name, agent_sub, downstream, client_writer, tracker
            )
            if forwarded:
                downstream.stdin.write(line)
                await downstream.stdin.drain()
    finally:
        # Session-integrity audit (observe-only) — emit before teardown.
        if tracker is not None:
            try:
                tracker.finalize()
            except Exception as exc:  # noqa: BLE001 — never block teardown
                log.debug(f"session-integrity finalize error (ignored): {exc}")
        # Client stdin closed: signal EOF to downstream so it can finish
        # emitting any in-flight responses, then drain the relay before exit.
        try:
            if downstream.stdin and not downstream.stdin.is_closing():
                downstream.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            # Let the downstream flush remaining output (bounded wait).
            await asyncio.wait_for(relay_task, timeout=5.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            relay_task.cancel()
        if downstream.returncode is None:
            downstream.terminate()
        await downstream.wait()

    return downstream.returncode or 0


async def _handle_client_line(
    line: bytes,
    enforcer: GrantEnforcer,
    server_name: str,
    agent_sub: str,
    downstream,
    client_writer,
    tracker=None,
) -> bool:
    """
    Decide whether to forward a client line to the downstream server.

    Returns True to forward the original bytes unchanged; False if the proxy
    handled it (e.g. denied a tools/call and wrote its own response).
    """
    try:
        msg = json.loads(line)
    except (ValueError, TypeError):
        return True  # not JSON we understand — forward verbatim

    if not isinstance(msg, dict) or msg.get("method") != "tools/call":
        return True  # only tools/call is gated

    params = msg.get("params") or {}
    tool = params.get("name", "")
    arguments = params.get("arguments") or {}
    request_id = msg.get("id")

    # Session-integrity (observe-only): record the write signal regardless of
    # the grant decision. Never affects whether the call is forwarded.
    if tracker is not None:
        tracker.observe(tool, arguments)

    allowed, reason = enforcer.enforce(tool, arguments)

    if allowed:
        log.debug(f"[proxy] ALLOW {server_name}:{tool}")
        _emit_tool_call_observation(agent_sub, server_name, tool, "allowed")
        return True

    log.warning(f"[proxy] DENY {server_name}:{tool} — {reason}")
    _emit_tool_call_observation(agent_sub, server_name, tool, "denied", reason)
    resp = _deny_response(request_id, reason)
    client_writer.write((json.dumps(resp) + "\n").encode())
    await client_writer.drain()
    return False  # do NOT forward to downstream


def main() -> None:
    parser = argparse.ArgumentParser(
        description="MCP tool-call grant enforcement proxy (ateles#26)."
    )
    parser.add_argument(
        "--server-name",
        required=True,
        help="Logical MCP server name for grant keys (<server>:<tool>)",
    )
    parser.add_argument(
        "downstream",
        nargs=argparse.REMAINDER,
        help="-- followed by the downstream server command",
    )
    args = parser.parse_args()

    downstream_cmd = args.downstream
    if downstream_cmd and downstream_cmd[0] == "--":
        downstream_cmd = downstream_cmd[1:]
    if not downstream_cmd:
        parser.error("missing downstream command after --")

    rc = asyncio.run(run_proxy(args.server_name, downstream_cmd))
    sys.exit(rc)


if __name__ == "__main__":
    main()
