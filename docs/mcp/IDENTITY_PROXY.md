# MCP Identity Proxy (Cursor → Neotoma)

This directory ships a reusable MCP identity proxy designed to sit between
Cursor (stdio MCP client) and downstream HTTP MCP servers. The first
target is Neotoma's HTTP `/mcp` endpoint so writes carry attribution
provenance instead of degrading to `anonymous`.

## Why

Cursor does not currently expose settable `initialize.clientInfo` or
AAuth signing. When Cursor launches Neotoma as a direct stdio subprocess,
Neotoma's AAuth middleware never runs (it only attaches to HTTP `/mcp`),
and `clientInfo` is whatever Cursor sends — which Neotoma normalises away
as generic. Result: `anonymous` attribution on every write.

Routing through this proxy makes the proxy itself the stable agent-identity
boundary. The proxy speaks stdio upstream (Cursor), HTTP downstream
(Neotoma), and injects fallback `clientInfo` today with an AAuth signing
hook for later phases.

## Related plans

- ateles-side: `/Users/markmhendrickson/.cursor/plans/cursor_mcp_proxy_4a68cb45.plan.md`
- Neotoma-side: `/Users/markmhendrickson/.cursor/plans/proxy_identity_enhancements_4c5ecc8e.plan.md`

The Neotoma plan owns session introspection (`GET /session`,
`get_session_identity` MCP tool), attribution policy, and diagnostics.
This proxy consumes those surfaces rather than redefining them.

## Pieces

- `execution/scripts/mcp_identity_proxy.py` — reusable proxy core
  (upstream stdio adapter, downstream HTTP adapter, identity middleware,
  session manager, optional `/session` preflight).
- `execution/scripts/run_neotoma_identity_proxy.sh` — Neotoma-specific
  launcher suitable for `mcp.json` command slots.
- `execution/scripts/verify_neotoma_identity_proxy.py` — end-to-end check
  that runs the launcher, completes MCP `initialize`, calls
  `get_session_identity`, and verifies the returned tier is not
  `anonymous`.

## Architecture

```
Cursor (stdio MCP client)
    │
    │  JSON-RPC (newline-delimited) on stdin/stdout
    ▼
run_neotoma_identity_proxy.sh
    │
    │  spawns
    ▼
mcp_identity_proxy.py
    │
    │  HTTP POST /mcp (JSON body, SSE or JSON response)
    │  Mcp-Session-Id preserved across calls
    │  clientInfo injected on initialize
    │  (AAuth signing hook reserved for Phase 2)
    ▼
Neotoma HTTP /mcp  ──►  AAuth middleware ──►  write path with provenance
```

Neotoma's session-introspection surface (`GET /session`, MCP tool
`get_session_identity`) is the canonical way to verify the resolved
attribution tier from the proxy.

## Cursor integration

Point Cursor's MCP config at the launcher:

```json
{
  "mcpServers": {
    "neotoma-proxy": {
      "command": "/Users/markmhendrickson/repos/ateles/execution/scripts/run_neotoma_identity_proxy.sh",
      "env": {
        "MCP_PROXY_AGENT_LABEL": "ateles",
        "MCP_PROXY_SESSION_PREFLIGHT": "1",
        "MCP_PROXY_AUTOSTART_NEOTOMA": "1",
        "MCP_PROXY_NEOTOMA_REPO": "/Users/markmhendrickson/repos/neotoma"
      }
    }
  }
}
```

While rolling out, the existing direct-stdio `neotoma` entry can stay in
parallel. Once the proxy is stable, remove the direct entry so every
Cursor-originated write flows through the identity boundary.

## Configuration

All knobs read from environment variables (preferred for launcher use) or
CLI flags on the proxy. Defaults work against a local Neotoma dev server
on `http://localhost:3080`.

| Variable                             | Purpose                                             | Default                         |
|--------------------------------------|-----------------------------------------------------|---------------------------------|
| `NEOTOMA_HTTP_URL`                   | Downstream Neotoma `/mcp` URL                       | `http://localhost:3080/mcp`     |
| `MCP_PROXY_CLIENT_NAME`              | `clientInfo.name` injected on `initialize`          | `cursor-neotoma-proxy`          |
| `MCP_PROXY_CLIENT_VERSION`           | `clientInfo.version` injected on `initialize`       | `0.1.0`                         |
| `MCP_PROXY_AGENT_LABEL`              | Repo/env label appended as `<name>+<label>`         | unset                           |
| `MCP_PROXY_BEARER_TOKEN`             | Forwarded as `Authorization: Bearer`                | unset                           |
| `MCP_PROXY_CONNECTION_ID`            | Forwarded as `X-Connection-Id`                      | unset                           |
| `MCP_PROXY_SESSION_PREFLIGHT`        | `1` to call `/session` on startup                   | unset                           |
| `MCP_PROXY_SESSION_PREFLIGHT_BASE`   | Alternate base URL for `/session`                   | derived from downstream URL     |
| `MCP_PROXY_FAIL_CLOSED`              | `1` to abort on anonymous or unreachable preflight  | unset                           |
| `MCP_PROXY_LOG_FILE`                 | Proxy diagnostics log file                          | `/tmp/mcp_identity_proxy.log`   |
| `MCP_PROXY_AUTOSTART_NEOTOMA`        | `1` to launch local Neotoma HTTP when health fails  | unset                           |
| `MCP_PROXY_NEOTOMA_REPO`             | Local Neotoma repo used by launcher autostart       | `$HOME/repos/neotoma`           |
| `MCP_PROXY_NEOTOMA_START_CMD`        | Override command used to start Neotoma HTTP         | derived from repo + `actions`   |
| `MCP_PROXY_NEOTOMA_HEALTH_URL`       | Override health URL checked before proxy starts     | derived as `<base>/session`     |
| `MCP_PROXY_NEOTOMA_START_TIMEOUT`    | Seconds to wait for autostarted Neotoma health      | `30`                            |
| `MCP_PROXY_NEOTOMA_LOG_FILE`         | Log file for launcher-started Neotoma               | `/tmp/neotoma_identity_proxy_autostart.log` |

The launcher also sources `$REPO_ROOT/.env` before invoking the proxy, so
standard ateles env conventions apply.

When `MCP_PROXY_AUTOSTART_NEOTOMA=1`, the launcher first checks
`/session` on the downstream base URL. If Neotoma is not reachable, it
starts the local Neotoma HTTP server (`dist/actions.js` when available,
else `npx tsx src/actions.ts`), waits until the health URL responds, and
then launches the proxy. If the launcher started Neotoma, it also stops
that child process when the proxy exits.

## Verification

With a separately running Neotoma HTTP server, or with
`MCP_PROXY_AUTOSTART_NEOTOMA=1`, run:

```bash
python3 execution/scripts/verify_neotoma_identity_proxy.py
```

Expected behaviour:

- `initialize` round-trips without a JSON-RPC error
- `tools/call get_session_identity` returns a session payload
- `attribution.tier` is one of `unverified_client`, `software`, or
  `hardware` (never `anonymous`)
- `eligible_for_trusted_writes` is present and boolean

On failure the script prints a `FAIL:` line describing what broke and
exits non-zero.

## Rollout sequence

1. Start Neotoma in HTTP mode on port 3080, or enable `MCP_PROXY_AUTOSTART_NEOTOMA=1`.
2. Switch Cursor's `mcp.json` to the `neotoma-proxy` entry.
3. Run `verify_neotoma_identity_proxy.py` to confirm non-anonymous tier.
4. Enable `MCP_PROXY_SESSION_PREFLIGHT=1` for startup trust check.
5. Later, when Neotoma ships the AAuth JWKS path the proxy adapter can
   sign requests; existing `clientInfo` fallback behaviour stays as a
   tier-2 safety net.

## Reuse across repos

The proxy is not Neotoma-specific: the downstream URL, `clientInfo`, and
agent label are all configurable. To use from another repo, copy or
symlink `mcp_identity_proxy.py`, write a launcher script with that repo's
`MCP_PROXY_CLIENT_NAME` and `MCP_PROXY_AGENT_LABEL`, and point the
repo-local Cursor `mcp.json` at it.
