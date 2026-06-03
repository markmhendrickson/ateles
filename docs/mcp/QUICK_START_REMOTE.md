# Quick Start: Remote Access for MCP Servers

Quick reference for exposing any MCP server remotely with authentication.

## One-Liner Setup

```bash
# For any server
./execution/scripts/setup_mcp_server_tunnel.sh \
    <server-name> \
    <server-script-path> \
    [port] \
    [auth-token]
```

## Server-Specific Examples

### Parquet Server
```bash
./execution/scripts/setup_mcp_server_tunnel.sh \
    parquet \
    mcp/parquet/parquet_mcp_server.py \
    8080
```

### DNSimple Server
```bash
./execution/scripts/setup_mcp_server_tunnel.sh \
    dnsimple \
    mcp/dnsimple/dnsimple_mcp_server.py \
    8081
```

### Gmail Server
```bash
./execution/scripts/setup_mcp_server_tunnel.sh \
    gmail \
    mcp/gmail/run-gmail-mcp.sh \
    8082
```

### Google Calendar Server
```bash
./execution/scripts/setup_mcp_server_tunnel.sh \
    google-calendar \
    mcp/google-calendar/run-google-calendar-mcp.sh \
    8083
```

### Instagram Server
```bash
./execution/scripts/setup_mcp_server_tunnel.sh \
    instagram \
    mcp/instagram/src/instagram_mcp_server.py \
    8084
```

### Minted Server
```bash
./execution/scripts/setup_mcp_server_tunnel.sh \
    minted \
    mcp/minted/minted_mcp_server.py \
    8085
```

### Asana Server
```bash
./execution/scripts/setup_mcp_server_tunnel.sh \
    asana \
    mcp/asana/asana_mcp_server.py \
    8086
```

### WhatsApp Server
```bash
./execution/scripts/setup_mcp_server_tunnel.sh \
    whatsapp \
    mcp/whatsapp/whatsapp_mcp_server.py \
    8087
```

### HomeKit Server
```bash
./execution/scripts/setup_mcp_server_tunnel.sh \
    homekit \
    mcp/homekit/homekit_mcp_server.py \
    8088
```

## Generate Auth Token

```bash
# For a specific server
python3 execution/scripts/mcp_authenticated_proxy.py \
    --server-name <server-name> \
    --server-script <server-script-path> \
    --generate-token
```

## Client Configuration

All servers use the same MCP standard authentication:

```json
{
  "mcpServers": {
    "<server-name>": {
      "type": "streamable-http",
      "url": "http://your-server-ip:<port>",
      "headers": {
        "Authorization": "Bearer your-secret-token"
      }
    }
  }
}
```

## Environment Variables

**Shared token (recommended):**
```bash
export MCP_AUTH_TOKEN="your-secret-token"
```

**Server-specific token:**
```bash
export MCP_PARQUET_AUTH_TOKEN="token-for-parquet"
export MCP_DNSIMPLE_AUTH_TOKEN="token-for-dnsimple"
```

## See Also

- Full documentation: `REMOTE_ACCESS.md`
- Parquet-specific: `parquet/REMOTE_ACCESS.md`
