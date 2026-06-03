# MCP Tunnel Auto-Start Setup

The Cloudflare tunnel for MCP servers (`mcp-servers`) is configured to start automatically on system startup via macOS LaunchAgent.

## Current Status

**Tunnel Name:** `mcp-servers`  
**Tunnel UUID:** `64cffaf9-7704-4d12-9b35-436c31be34f6`  
**Custom Domain:** `https://dev.neotoma.io/mcp`  
**Local Service:** `http://localhost:8080`

## Auto-Start Configuration

The tunnel is managed by a LaunchAgent that:
- ✅ Starts automatically on system startup (`RunAtLoad: true`)
- ✅ Restarts automatically if it crashes (`KeepAlive: true`)
- ✅ Logs to `data/logs/cloudflare_mcp_tunnel.log`

**LaunchAgent:** `com.cloudflare.mcp-servers-tunnel`  
**Plist Location:** `~/Library/LaunchAgents/com.cloudflare.mcp-servers-tunnel.plist`

## Setup

To install the auto-start LaunchAgent:

```bash
./execution/scripts/setup_mcp_tunnel_autostart.sh
```

This script:
1. Verifies the tunnel exists
2. Creates log directory
3. Installs LaunchAgent plist
4. Loads the service

## Management Commands

### Check Status

```bash
# Check if LaunchAgent is loaded
launchctl list | grep mcp-servers-tunnel

# View tunnel logs
tail -f data/logs/cloudflare_mcp_tunnel.log

# View error logs
tail -f data/logs/cloudflare_mcp_tunnel.error.log
```

### Manual Control

```bash
# Stop tunnel
launchctl unload ~/Library/LaunchAgents/com.cloudflare.mcp-servers-tunnel.plist

# Start tunnel
launchctl load ~/Library/LaunchAgents/com.cloudflare.mcp-servers-tunnel.plist

# Restart tunnel
launchctl unload ~/Library/LaunchAgents/com.cloudflare.mcp-servers-tunnel.plist
launchctl load ~/Library/LaunchAgents/com.cloudflare.mcp-servers-tunnel.plist
```

### Verify Tunnel is Running

```bash
# Check tunnel status via cloudflared
cloudflared tunnel info mcp-servers

# Test tunnel endpoint
curl -I https://dev.neotoma.io/mcp/oauth/token
```

## Important Note

**The tunnel connects to `localhost:8080`**, which means the MCP authenticated proxy must also be running.

The tunnel will start automatically, but you still need to start the MCP proxy:

```bash
./execution/scripts/setup_mcp_server_tunnel.sh parquet mcp/parquet/parquet_mcp_server.py 8080 \
    --oauth-client-id <your-client-id> \
    --oauth-client-secret <your-client-secret> \
    --oauth-redirect-uri https://dev.neotoma.io/mcp/oauth/callback
```

**Future Enhancement:** Consider creating a LaunchAgent for the MCP proxy as well, or a combined service that starts both.

## Troubleshooting

### Tunnel Not Starting

1. **Check LaunchAgent status:**
   ```bash
   launchctl list | grep mcp-servers-tunnel
   ```
   If not listed, it may have failed to start.

2. **Check error logs:**
   ```bash
   cat data/logs/cloudflare_mcp_tunnel.error.log
   ```

3. **Verify tunnel exists:**
   ```bash
   cloudflared tunnel list | grep mcp-servers
   ```

4. **Check cloudflared authentication:**
   ```bash
   cloudflared tunnel list
   ```
   If this fails, re-authenticate: `cloudflared tunnel login`

### Tunnel Starts But Connection Fails

- **Check if MCP proxy is running:**
  ```bash
  lsof -i :8080
  ```
  If nothing is listening, start the MCP proxy.

- **Check tunnel configuration:**
  ```bash
  cat ~/.cloudflared/config.yml
  ```
  Verify ingress rules point to `http://localhost:8080`.

### Logs Location

- **Standard output:** `data/logs/cloudflare_mcp_tunnel.log`
- **Standard error:** `data/logs/cloudflare_mcp_tunnel.error.log`

## Related Documentation

- `mcp/CUSTOM_DOMAIN_SETUP.md` - Custom domain setup
- `mcp/TUNNEL_TROUBLESHOOTING.md` - Troubleshooting guide
- `mcp/OAUTH_QUICK_TEST.md` - OAuth testing
- `execution/scripts/setup_mcp_server_tunnel.sh` - MCP proxy setup script
