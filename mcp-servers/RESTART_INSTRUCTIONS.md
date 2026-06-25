# Restart Instructions for MCP Proxy and Tunnel

## Quick Restart Commands

### Restart MCP Proxy

```bash
# Stop current proxy
pkill -f "mcp_authenticated_proxy.py"

# Restart (will auto-detect OAuth credentials from .env)
./execution/scripts/setup_mcp_server_tunnel.sh parquet mcp/parquet/parquet_mcp_server.py 8080
```

### Restart Cloudflare Tunnel

```bash
# Restart via LaunchAgent
launchctl unload ~/Library/LaunchAgents/com.cloudflare.mcp-servers-tunnel.plist
launchctl load ~/Library/LaunchAgents/com.cloudflare.mcp-servers-tunnel.plist
```

### Restart Both (Full Restart)

```bash
# 1. Stop proxy
pkill -f "mcp_authenticated_proxy.py"

# 2. Restart tunnel
launchctl unload ~/Library/LaunchAgents/com.cloudflare.mcp-servers-tunnel.plist
launchctl load ~/Library/LaunchAgents/com.cloudflare.mcp-servers-tunnel.plist

# 3. Wait a few seconds for tunnel to connect
sleep 3

# 4. Start proxy (OAuth credentials from .env)
./execution/scripts/setup_mcp_server_tunnel.sh parquet mcp/parquet/parquet_mcp_server.py 8080
```

## Verify Status

### Check Proxy Status

```bash
# Check if proxy is running
lsof -i :8080

# Check proxy process
ps aux | grep mcp_authenticated_proxy | grep -v grep
```

### Check Tunnel Status

```bash
# Check LaunchAgent status
launchctl list | grep mcp-servers-tunnel

# Check tunnel logs
tail -f data/logs/cloudflare_mcp_tunnel.log
```

### Test Endpoints

```bash
# Test OAuth token endpoint
curl -X POST https://dev.neotoma.io/oauth/token \
    -H "Content-Type: application/json" \
    -d '{
        "grant_type": "client_credentials",
        "client_id": "test",
        "client_secret": "test"
    }'

# Should return: {"error": "invalid_client", ...}
# (This confirms endpoint is accessible)
```

## Common Issues

### Proxy Not Starting

- **Check OAuth credentials in .env:**
  ```bash
  grep MCP_OAUTH .env
  ```
  
- **If missing, sync from 1Password:**
  ```bash
  op signin
  python3 scripts/op_sync_env_from_1password.py
  ```

### Tunnel Not Connecting

- **Check tunnel configuration:**
  ```bash
  cat ~/.cloudflared/config.yml
  ```

- **Check tunnel logs:**
  ```bash
  tail -20 data/logs/cloudflare_mcp_tunnel.error.log
  ```

- **Verify tunnel is running:**
  ```bash
  cloudflared tunnel info mcp-servers
  ```

### Port Already in Use

```bash
# Kill process on port 8080
lsof -ti :8080 | xargs kill -9

# Kill process on port 8081 (stdio proxy)
lsof -ti :8081 | xargs kill -9
```

## Automatic Restart on System Startup

**Tunnel:** Automatically starts via LaunchAgent (already configured)

**Proxy:** Currently requires manual start. To auto-start proxy:

1. Create LaunchAgent for proxy (similar to tunnel)
2. Or use a startup script that starts both

See `mcp/TUNNEL_AUTOSTART.md` for tunnel auto-start details.
