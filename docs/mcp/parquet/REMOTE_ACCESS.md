# Remote Access for Parquet MCP Server

This guide explains how to expose the parquet MCP server for remote usage via tunnel with MCP standard Bearer token authentication.

**Note:** This server can also be exposed using the generic MCP proxy. See `../REMOTE_ACCESS.md` for the generic approach that works for all MCP servers.

## Overview

The parquet MCP server uses **stdio transport** by default (local only). To expose it remotely with authentication, use the **authenticated proxy bridge** (Option 1).

## Option 1: Authenticated Proxy Bridge (Recommended)

Use the authenticated proxy wrapper that adds API key authentication to the MCP proxy bridge.

### Quick Start

```bash
# Make script executable
chmod +x execution/scripts/setup_parquet_mcp_tunnel.sh

# Start with auto-generated API key
./execution/scripts/setup_parquet_mcp_tunnel.sh 8080

# Or provide your own API key
./execution/scripts/setup_parquet_mcp_tunnel.sh 8080 "your-secret-api-key"
```

### Generate Auth Token

```bash
# Generate a new secure MCP auth token
python3 execution/scripts/parquet_mcp_authenticated_proxy.py --generate-key
```

This will output a secure random token. Save it securely.

### Auth Token Storage Options

**Option A: Environment Variable (MCP Standard)**
```bash
export MCP_AUTH_TOKEN="your-secret-token"
./execution/scripts/setup_parquet_mcp_tunnel.sh 8080
```

**Option B: 1Password**
1. Create a 1Password item titled "Parquet MCP Proxy"
2. Add a field labeled "MCP_AUTH_TOKEN" with your token
3. The script will automatically retrieve it

**Option C: Command Line**
```bash
./execution/scripts/setup_parquet_mcp_tunnel.sh 8080 "your-secret-token"
```

**Backward Compatibility:** The old `MCP_PROXY_API_KEY` environment variable still works but is deprecated.

### Manual Setup

```bash
# Install dependencies
pip install aiohttp

# Start authenticated proxy
python3 execution/scripts/parquet_mcp_authenticated_proxy.py \
    --port 8080 \
    --api-key "your-secret-api-key"
```

### Client Configuration

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
    }
  }
}
```

**Note:** Uses MCP standard `Authorization: Bearer <token>` header (OAuth 2.1 style), not custom API key headers.

**For Cursor/Claude Desktop:**
```json
{
  "mcpServers": {
    "parquet": {
      "command": "curl",
      "args": [
        "-X", "POST",
        "-H", "X-API-Key: your-secret-api-key",
        "http://your-server-ip:8080"
      ]
    }
  }
}
```

**Note:** Some MCP clients may require different configuration. Check your client's documentation for HTTP transport with custom headers.

## Option 2: Cloudflare Tunnel (Public Access with Authentication)

Expose the server publicly via Cloudflare tunnel with authentication:

### Setup

1. Start authenticated proxy locally:
```bash
./execution/scripts/setup_parquet_mcp_tunnel.sh 8080 "your-api-key"
```

2. In another terminal, create Cloudflare tunnel:
```bash
cloudflared tunnel --url http://localhost:8080
```

3. Use the public URL with auth token in client configuration (MCP standard):
```json
{
  "mcpServers": {
    "parquet": {
      "type": "streamable-http",
      "url": "https://your-tunnel-url.trycloudflare.com",
      "headers": {
        "Authorization": "Bearer your-secret-token"
      }
    }
  }
}
```

### Enhanced Security with Cloudflare Access

For additional security, use Cloudflare Access:

```bash
cloudflared tunnel --url http://localhost:8080 \
    --access-protected-url true
```

This adds Cloudflare's authentication layer on top of your API key.

## Option 3: SSH Tunnel (Most Secure)

For secure access without public exposure:

### Server Side

```bash
# Start authenticated proxy (localhost only)
python3 execution/scripts/parquet_mcp_authenticated_proxy.py \
    --port 8080 \
    --host 127.0.0.1 \
    --api-key "your-secret-api-key"
```

### Client Side

```bash
# Create SSH tunnel
ssh -L 8080:localhost:8080 user@your-server-ip

# Configure client to use localhost with API key
```

### Client Configuration

```json
{
  "mcpServers": {
    "parquet": {
      "type": "streamable-http",
      "url": "http://localhost:8080",
      "headers": {
        "Authorization": "Bearer your-secret-token"
      }
    }
  }
}
```

## Security Best Practices

### Auth Token Management

1. **Generate Strong Tokens**: Use the `--generate-key` option to create cryptographically secure tokens
2. **Rotate Regularly**: Change auth tokens periodically
3. **Never Commit Tokens**: Keep tokens out of version control
4. **Use Environment Variables**: Prefer `MCP_AUTH_TOKEN` (MCP standard) over command-line arguments
5. **1Password Integration**: Store tokens in 1Password for secure access

### Network Security

1. **Use HTTPS**: Always use HTTPS in production (via Cloudflare tunnel or reverse proxy)
2. **Restrict Network Access**: Use firewall rules to limit access
3. **VPN Access**: Consider VPN for additional security layer
4. **Rate Limiting**: Consider adding rate limiting to prevent abuse

### Data Security

- The parquet MCP server has full read/write access to your data directory
- Ensure `DATA_DIR` environment variable is set correctly
- Review audit logs regularly: `$DATA_DIR/logs/audit_log.parquet`
- Consider read-only mode for remote access (future enhancement)

## Architecture

The authenticated proxy works as follows:

```
Remote MCP Client
    ↓ (HTTP + X-API-Key header)
Authenticated Proxy (parquet_mcp_authenticated_proxy.py)
    ↓ (validates API key)
    ↓ (forwards to stdio proxy)
Stdio-to-HTTP Proxy (mcp-proxy)
    ↓ (converts HTTP to stdio)
Parquet MCP Server (parquet_mcp_server.py)
    ↓ (stdio communication)
Data Directory ($DATA_DIR)
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

- Verify proxy is running: `curl http://localhost:8080` (should return 401 without key)
- Check firewall rules
- Verify `--host` setting (use `0.0.0.0` for remote access)

### MCP Client Can't Connect

- Verify transport type: `"type": "streamable-http"`
- Check URL format: `http://` or `https://`
- Verify headers are included in client configuration
- Test with curl first to isolate client issues

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

## Future Enhancements

1. **Native HTTP Support**: Modify server to support HTTP transport directly (no proxy needed)
2. **OAuth/JWT**: Add token-based authentication
3. **Read-Only Mode**: Separate read-only endpoint for safer remote access
4. **Rate Limiting**: Prevent abuse of remote endpoints
5. **Multiple API Keys**: Support multiple keys with different permissions

## References

- [MCP Transport Specification](https://modelcontextprotocol.io/specification/2024-11-05/basic/transports)
- [MCP Proxy Tools](https://github.com/modelcontextprotocol/proxy)
- [Cloudflare Tunnel Docs](https://developers.cloudflare.com/cloudflare-one/connections/connect-apps/)
