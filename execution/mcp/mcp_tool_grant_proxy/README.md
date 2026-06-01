# mcp_tool_grant_proxy

Generic MCP tool-call grant enforcement (ateles#26).

Sits between `claude --print` and a downstream MCP server, speaking MCP stdio
JSON-RPC on both sides. Forwards everything except `tools/call`, which it gates
against the agent's `agent_grant` in Neotoma.

## Why

`agent_grant` historically gated only Neotoma entity operations. This proxy
extends the same grant model to **any** MCP tool, on **any** server — so an
agent invoked with a server connected can still be blocked from calling its
tools if the grant doesn't authorize them. Parameter constraints
(`max_amount_sats`, `tables`, …) are enforced by the grant, not the prompt, so
a compromised prompt cannot exceed them.

## Grant shape

Tool capabilities are entries in the grant's `capabilities` array:

```jsonc
{ "op": "tool:<server>:<tool>", "param_constraints": { ... } }
```

- Absent = denied. `{}` = allowed, unconstrained.
- Wildcards: `tool:<server>:*`, `tool:*`.
- Constraint keys: `tables`, `max_amount_sats`, `to_allowlist`, `max_<field>`,
  `allowed_<field>` (unknown keys ignored — forward-compatible).

See `docs/aauth.md` → "Tool-level authorization (issue #26)".

## Usage

```jsonc
{
  "command": "python",
  "args": [
    "execution/mcp/mcp_tool_grant_proxy/proxy.py",
    "--server-name", "parquet",
    "--", "python", "path/to/parquet_mcp/server.py"
  ]
}
```

Everything after `--` is the downstream server command.

### Environment

| Var | Required | Meaning |
|-----|----------|---------|
| `ATELES_AGENT_SUB` | yes (for enforcement) | Agent identity, e.g. `monedula@ateles-swarm`. Empty → advisory passthrough. |
| `ATELES_AGENT_GRANT_ID` | no | Explicit grant entity id (else looked up by sub). |
| `NEOTOMA_BASE_URL` / `NEOTOMA_BEARER_TOKEN` | for grant lookup + audit | Neotoma API. |
| `MCP_GRANT_PROXY_DEBUG` | no | `1` → verbose stderr logging. |

## Enforcement semantics

- **Allowed** → call forwarded to downstream unchanged.
- **Denied** → proxy returns an MCP `isError` result; downstream never sees it.
- Every decision emits a `tool_call_observation` to Neotoma (`result: allowed | denied`).
- **Permissive fallback:** Neotoma unreachable, or no grant declares any tool
  capability → passthrough. Enforcement tightens per-agent as `tool:` caps are
  added.

## Tests

```
.venv/bin/python execution/mcp/mcp_tool_grant_proxy/test_proxy_smoke.py
```

Covers: advisory passthrough, non-`tools/call` passthrough, and the in-process
deny path (ungranted tool + constraint violation + deny-response shape).
