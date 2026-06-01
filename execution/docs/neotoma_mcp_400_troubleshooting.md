# Neotoma MCP HTTP 400 (`neotoma-mcp-proxy downstream error (400)`)

When the stdio bridge (`neotoma mcp proxy` / signed shim) forwards JSON-RPC to **`POST /mcp`**, Neotoma can return **400** before your tool runs. The proxy surfaces that as MCP error **-32000** with `data.detail` truncated to ~500 characters—decode that JSON to see the real message.

## Common causes (by error text)

### `Bad Request: No valid session ID provided`

Returned from `src/actions.ts` when the request is **not** an `initialize` call and there is **no** active Streamable HTTP transport **and** the client sent **no** `mcp-session-id` header (empty / absent). The message was tightened to tell the client to run `initialize` first, then attach the header.

Typical situations:

- **`Mcp-Session-Id` missing** on a non-initialize POST (stdio proxy did not attach a captured session id).
- **Wrong downstream URL** (e.g. proxy aimed at `3080` while HTTP Actions bound another port); `initialize` might hit a different instance than later `tools/call`.

**What to do:** Restart the Neotoma MCP server in Cursor so a fresh `initialize` runs; confirm stderr from the proxy logs `downstream=` and matches a live API. If you use `NEOTOMA_MCP_USE_LOCAL_PORT_FILE`, ensure `.dev-serve/local_http_port` matches the running server.

### `Service Unavailable: MCP session is unknown on this API instance` (HTTP **503**)

When a **`mcp-session-id` header is present** but that id is not in this process’s `mcpTransports` map, Neotoma returns **503** (not 400) with guidance on **sticky sessions** and restarts. Typical causes:

- **Load-balanced replicas** without session affinity: `initialize` landed on instance A, `tools/call` on B.
- **Stale id** after API restart (in-memory map cleared) while the stdio proxy still holds the old header.

**What to do:** Enable sticky routing for `POST /mcp`, run a single MCP-capable API instance for that path, or restart the MCP client after restarts. See [`../../../neotoma/docs/developer/mcp/proxy.md`](../../../neotoma/docs/developer/mcp/proxy.md) § “HTTP 400 / 503 from POST /mcp”.

### `Bad Request: Mcp-Session-Id header is required`

From the MCP SDK (`WebStandardStreamableHTTPServerTransport.validateSession`): the transport is initialized, but the **incoming POST** had no `mcp-session-id` header. The stdio proxies are supposed to **capture** the id from the `initialize` response and **attach** it on every subsequent request.

**What to do:** Check proxy version (Neotoma-packaged TS proxy vs ateles `mcp_identity_proxy.py`); inspect whether the first successful `initialize` returned `Mcp-Session-Id` on the HTTP response headers. If Cursor ever sends tool calls on a code path that skips the shared proxy process, open an issue (that would be client-side).

### `Bad Request: Server not initialized`

A non-initialize request reached a transport that has not finished MCP initialization ordering.

**What to do:** Ensure the client sends `initialize` (and usually `notifications/initialized`) before `tools/call`; restart MCP.

### `Bad Request: Unsupported protocol version`

`MCP-Protocol-Version` header mismatch.

**What to do:** Align Cursor / SDK with supported versions Neotoma advertises.

### Parse / validation errors

Rare for `tools/call`; JSON body issues can yield 400 from the transport with a parse-style message.

## Quick checks

1. Re-run one **`store_structured`** from chat; if it succeeds, earlier 400 was likely **session or server restart**.
2. Read **`error.data.detail`** from the MCP response (proxy copies upstream body).
3. Compare Neotoma server logs around **`[MCP HTTP]`** for the same timestamp.

## Related

- [`../../../neotoma/docs/developer/mcp/proxy.md`](../../../neotoma/docs/developer/mcp/proxy.md) — env vars and session behavior (sibling `neotoma` checkout)
- [`neotoma_cursor_context.md`](neotoma_cursor_context.md) — MCP toggle and compact instructions
