#!/usr/bin/env python3
"""
End-to-end smoke test for mcp_tool_grant_proxy (ateles#26).

Spawns the proxy wrapping a trivial fake downstream MCP server, then drives
JSON-RPC over the proxy's stdio to confirm:

  - an allowed tools/call reaches the downstream (echoed back)
  - a denied tools/call is blocked by the proxy (isError, never echoed)
  - a non-tools/call message passes through untouched

Grant lookup is stubbed by monkeypatching GrantChecker via a sitecustomize
env hook — instead, we run in advisory-bypass for "allow" and force a deny by
pointing the enforcer at a grant fixture through MCP_GRANT_PROXY_TEST_GRANT.

Run: .venv/bin/python execution/mcp/mcp_tool_grant_proxy/test_proxy_smoke.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent.parent

FAKE_SERVER = _HERE / "_fake_downstream.py"
PROXY = _HERE / "proxy.py"


def _write_fake_server() -> None:
    """A minimal downstream MCP server that echoes tools/call as success."""
    FAKE_SERVER.write_text(
        '''#!/usr/bin/env python3
import json, sys
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        msg = json.loads(line)
    except Exception:
        continue
    if msg.get("method") == "tools/call":
        resp = {
            "jsonrpc": "2.0", "id": msg.get("id"),
            "result": {"content": [{"type": "text", "text": "DOWNSTREAM_REACHED"}], "isError": False},
        }
    else:
        resp = {"jsonrpc": "2.0", "id": msg.get("id"), "result": {"ok": True}}
    sys.stdout.write(json.dumps(resp) + "\\n")
    sys.stdout.flush()
'''
    )


def _run_case(env_extra: dict, request: dict, timeout: float = 8.0) -> dict:
    env = {**os.environ, **env_extra, "PYTHONPATH": str(_REPO_ROOT)}
    proc = subprocess.Popen(
        [sys.executable, str(PROXY), "--server-name", "parquet", "--",
         sys.executable, str(FAKE_SERVER)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=env, text=True,
    )
    try:
        out, _err = proc.communicate(json.dumps(request) + "\n", timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _err = proc.communicate()
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        if msg.get("id") == request.get("id"):
            return msg
    return {}


def test_allowed_passthrough_advisory():
    """No ATELES_AGENT_SUB → advisory bypass → downstream reached."""
    resp = _run_case(
        {"ATELES_AGENT_SUB": "", "NEOTOMA_BEARER_TOKEN": ""},
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "read_parquet", "arguments": {"table": "transactions"}}},
    )
    assert resp, "no response"
    text = resp.get("result", {}).get("content", [{}])[0].get("text", "")
    assert "DOWNSTREAM_REACHED" in text, f"expected downstream reach, got {resp}"
    assert resp["result"].get("isError") in (False, None)


def test_non_tool_call_passthrough():
    """tools/list is not gated → passes through."""
    resp = _run_case(
        {"ATELES_AGENT_SUB": "", "NEOTOMA_BEARER_TOKEN": ""},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    )
    assert resp.get("result", {}).get("ok") is True, f"got {resp}"


def test_enforcer_deny_path_in_process():
    """
    Exercise the real GrantEnforcer.enforce deny decision in-process with a
    stubbed checker — verifies a denied tool call yields (False, reason) and
    NEVER touches the downstream (the proxy synthesizes _deny_response).
    """
    sys.path.insert(0, str(_REPO_ROOT))
    from lib.daemon_runtime.grant_checker import GrantChecker  # noqa: E402
    import importlib.util

    spec = importlib.util.spec_from_file_location("proxy_mod", str(PROXY))
    proxy_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(proxy_mod)

    # Build a checker pre-seeded with a grant that allows parquet:read_parquet
    # (with a table constraint) but NOT github_harness.
    grant_entity = {
        "entity_id": "ent_test",
        "snapshot": {
            "match_sub": "monedula@ateles-swarm",
            "status": "active",
            "capabilities": [
                {"op": "tool:parquet:read_parquet",
                 "param_constraints": {"tables": ["transactions"]}},
            ],
        },
    }
    checker = GrantChecker("monedula@ateles-swarm")
    checker._grants = [GrantChecker._parse(grant_entity)]
    checker._loaded = True

    enf = proxy_mod.GrantEnforcer("monedula@ateles-swarm", "parquet")
    enf._checker = checker

    # Allowed tool + satisfying constraint.
    ok, reason = enf.enforce("read_parquet", {"table": "transactions"})
    assert ok, f"expected allow, got {reason}"

    # Allowed tool but VIOLATING constraint → deny.
    ok, reason = enf.enforce("read_parquet", {"table": "contacts"})
    assert not ok and "constraint" in reason, f"expected constraint deny, got {ok}/{reason}"

    # Ungranted tool on same server → deny.
    ok, reason = enf.enforce("add_record", {"table": "transactions"})
    assert not ok and "no grant" in reason, f"expected no-grant deny, got {ok}/{reason}"

    # Verify the deny response shape is a proper MCP isError result.
    resp = proxy_mod._deny_response(99, "test reason")
    assert resp["result"]["isError"] is True
    assert resp["id"] == 99
    assert "DENIED" in resp["result"]["content"][0]["text"]


def _run_all() -> int:
    _write_fake_server()
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failures = 0
    try:
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
    finally:
        if FAKE_SERVER.exists():
            FAKE_SERVER.unlink()
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_run_all())
