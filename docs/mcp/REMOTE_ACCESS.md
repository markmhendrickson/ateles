# Remote Access for MCP Servers

This guide explains how to expose any MCP server for remote usage via tunnel with MCP standard Bearer token authentication.

## Overview

All MCP servers in this repository use **stdio transport** by default (local only). To expose them remotely with authentication, use the **generic authenticated proxy bridge**.

## Quick Start

### For Any MCP Server

```bash
# Make script executable
chmod +x execution/scripts/setup_mcp_server_tunnel.sh

# Start authenticated proxy for any server
./execution/scripts/setup_mcp_server_tunnel.sh \
    <server-name> \
    <server-script-path> \
    [port] \
    [auth-token]

# Examples:
./execution/scripts/setup_mcp_server_tunnel.sh \
    parquet \
    mcp/parquet/parquet_mcp_server.py \
    8080

./execution/scripts/setup_mcp_server_tunnel.sh \
    dnsimple \
    mcp/dnsimple/dnsimple_mcp_server.py \
    8081

./execution/scripts/setup_mcp_server_tunnel.sh \
    gmail \
    mcp/gmail/run-gmail-mcp.sh \
    8082
```

## Available MCP Servers

### Truth Layer Servers

- **parquet**: `mcp/parquet/parquet_mcp_server.py`
- **dnsimple**: `mcp/dnsimple/dnsimple_mcp_server.py`

### Execution Layer Servers

- **gmail**: `mcp/gmail/run-gmail-mcp.sh`
- **google-calendar**: `mcp/google-calendar/run-google-calendar-mcp.sh`
- **instagram**: `mcp/instagram/src/instagram_mcp_server.py`
- **minted**: `mcp/minted/minted_mcp_server.py`
- **asana**: `mcp/asana/asana_mcp_server.py`
- **whatsapp**: `mcp/whatsapp/whatsapp_mcp_server.py`
- **homekit**: `mcp/homekit/homekit_mcp_server.py`

## Generate Auth Token

```bash
# Generate a new secure MCP auth token for a specific server
python3 execution/scripts/mcp_authenticated_proxy.py \
    --server-name <server-name> \
    --server-script <server-script-path> \
    --generate-token
```

This will output a secure random token. Save it securely.

## Auth Token Storage Options

### Option A: Shared Token (MCP Standard)

Use the same token for all servers:

```bash
export MCP_AUTH_TOKEN="your-secret-token"
./execution/scripts/setup_mcp_server_tunnel.sh parquet mcp/parquet/parquet_mcp_server.py 8080
```

### Option B: Server-Specific Token

Use different tokens per server:

```bash
export MCP_PARQUET_AUTH_TOKEN="token-for-parquet"
export MCP_DNSIMPLE_AUTH_TOKEN="token-for-dnsimple"
./execution/scripts/setup_mcp_server_tunnel.sh parquet mcp/parquet/parquet_mcp_server.py 8080
```

### Option C: 1Password

1. Create a 1Password item titled `{Server Name} MCP Proxy` (e.g., "Parquet MCP Proxy")
2. Add a field labeled `MCP_AUTH_TOKEN` with your token
3. Or create a shared item "MCP Proxy" with `MCP_AUTH_TOKEN` for all servers
4. The script will automatically retrieve it

### Option D: Command Line

```bash
./execution/scripts/setup_mcp_server_tunnel.sh \
    parquet \
    mcp/parquet/parquet_mcp_server.py \
    8080 \
    "your-secret-token"
```

## Client Configuration

Configure your remote MCP client to connect via HTTP with MCP standard Bearer token:

```json
{
  "mcpServers": {
    "parquet": {
      "type": "streamable-http",
      "url": "http://your-server-ip:8080",
      "headers": {
        "Authorization": "Bearer your-secret-token"
      }
    },
    "dnsimple": {
      "type": "streamable-http",
      "url": "http://your-server-ip:8081",
      "headers": {
        "Authorization": "Bearer your-secret-token"
      }
    }
  }
}
```

**Note:** Uses MCP standard `Authorization: Bearer <token>` header (OAuth 2.1 style).

## Cloudflare Tunnel (Public Access)

Expose servers publicly via Cloudflare tunnel:

```bash
# Terminal 1: Start authenticated proxy
./execution/scripts/setup_mcp_server_tunnel.sh \
    parquet \
    mcp/parquet/parquet_mcp_server.py \
    8080

# Terminal 2: Create Cloudflare tunnel
cloudflared tunnel --url http://localhost:8080
```

Use the public URL with auth token in client configuration.

## SSH Tunnel (Most Secure)

For secure access without public exposure:

```bash
# Server side: Start authenticated proxy (localhost only)
python3 execution/scripts/mcp_authenticated_proxy.py \
    --server-name parquet \
    --server-script mcp/parquet/parquet_mcp_server.py \
    --port 8080 \
    --host 127.0.0.1 \
    --auth-token "your-token"

# Client side: Create SSH tunnel
ssh -L 8080:localhost:8080 user@your-server-ip
```

## Security Best Practices

### Token Management

1. **Generate Strong Tokens**: Use `--generate-token` to create cryptographically secure tokens
2. **Rotate Regularly**: Change auth tokens periodically
3. **Never Commit Tokens**: Keep tokens out of version control
4. **Use Environment Variables**: Prefer `MCP_AUTH_TOKEN` (shared) or server-specific tokens
5. **1Password Integration**: Store tokens in 1Password for secure access

### Network Security

1. **Use HTTPS**: Always use HTTPS in production (via Cloudflare tunnel or reverse proxy)
2. **Restrict Network Access**: Use firewall rules to limit access
3. **VPN Access**: Consider VPN for additional security layer
4. **Rate Limiting**: Consider adding rate limiting to prevent abuse

## Architecture

The authenticated proxy works as follows:

```
Remote MCP Client
    ↓ (HTTP + Authorization: Bearer <token>)
Generic Authenticated Proxy (mcp_authenticated_proxy.py)
    ↓ (validates Bearer token)
    ↓ (forwards to stdio proxy)
Stdio-to-HTTP Proxy (mcp-proxy)
    ↓ (converts HTTP to stdio)
MCP Server (any stdio server)
    ↓ (stdio communication)
External APIs / Data Sources
```

## Troubleshooting

### Proxy Not Starting

```bash
# Check dependencies
pip install aiohttp
node --version  # Should be installed for npx

# Check if port is in use
lsof -i :8080
```

### Authentication Failing

1. Verify auth token matches between server and client
2. Check header format: `Authorization: Bearer <token>` (MCP standard)
3. Test with curl:
```bash
curl -X POST http://localhost:8080 \
    -H "Authorization: Bearer your-token" \
    -H "Content-Type: application/json" \
    -d '{"jsonrpc":"2.0","method":"initialize","id":1}'
```

### Connection Refused

- Verify proxy is running: `curl http://localhost:8080` (should return 401 without token)
- Check firewall rules
- Verify `--host` setting (use `0.0.0.0` for remote access)

## Dependencies

**Required:**
- Python 3.8+
- `aiohttp` (for authenticated proxy)
- Node.js and `npx` (for mcp-proxy bridge)

**Install:**
```bash
pip install aiohttp
# Node.js: brew install node (macOS) or download from nodejs.org
```

## Examples

### Parquet Server

```bash
# Generate token
python3 execution/scripts/mcp_authenticated_proxy.py \
    --server-name parquet \
    --server-script mcp/parquet/parquet_mcp_server.py \
    --generate-token

# Start proxy
./execution/scripts/setup_mcp_server_tunnel.sh \
    parquet \
    mcp/parquet/parquet_mcp_server.py \
    8080
```

### DNSimple Server

```bash
# Start proxy with existing token
export MCP_AUTH_TOKEN="your-token"
./execution/scripts/setup_mcp_server_tunnel.sh \
    dnsimple \
    mcp/dnsimple/dnsimple_mcp_server.py \
    8081
```

### Gmail Server

```bash
# Start proxy
./execution/scripts/setup_mcp_server_tunnel.sh \
    gmail \
    mcp/gmail/run-gmail-mcp.sh \
    8082
```

## References

- [MCP Transport Specification](https://modelcontextprotocol.io/specification/2024-11-05/basic/transports)
- [MCP Authorization Specification](https://modelcontextprotocol.io/specification/2025-06-18/basic/authorization)
- [MCP Proxy Tools](https://github.com/modelcontextprotocol/proxy)
- [Cloudflare Tunnel Docs](https://developers.cloudflare.com/cloudflare-one/connections/connect-apps/)
