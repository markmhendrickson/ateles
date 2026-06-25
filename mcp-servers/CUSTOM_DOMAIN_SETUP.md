# Custom Domain Setup for MCP Remote Access

This guide explains how to set up a custom domain like `dev.neotoma.io/mcp` for your MCP servers using Cloudflare Tunnel.

## Overview

Instead of using random `trycloudflare.com` URLs, you can use a custom domain with a persistent Cloudflare tunnel. This provides:
- **Stable URL**: `https://dev.neotoma.io/mcp` (doesn't change)
- **Professional appearance**: Custom domain instead of random subdomain
- **Path-based routing**: Multiple services on same domain (e.g., `/mcp`, `/api`, `/webhooks`)

## Prerequisites

1. **Cloudflare account** with `neotoma.io` domain added
2. **cloudflared installed**: `brew install cloudflared`
3. **DNS access**: Ability to create DNS records in Cloudflare

## Setup Steps

### Step 1: Authenticate with Cloudflare

```bash
cloudflared tunnel login
```

This opens a browser to authenticate and grants tunnel access to your Cloudflare account.

### Step 2: Run Setup Script

```bash
./execution/scripts/setup_mcp_custom_domain_tunnel.sh [port] [domain] [path]
```

**Parameters:**
- `port`: Local port where MCP proxy is running (default: 8080)
- `domain`: Custom domain (default: `dev.neotoma.io`)
- `path`: Path prefix (default: `mcp`)

**Example:**
```bash
./execution/scripts/setup_mcp_custom_domain_tunnel.sh 8080 dev.neotoma.io mcp
```

This will:
1. Create a named tunnel called `mcp-servers`
2. Configure it to route `dev.neotoma.io/mcp/*` to `localhost:8080`
3. Provide DNS setup instructions

### Step 3: Create DNS Record

In Cloudflare Dashboard:
1. Go to your domain (`neotoma.io`)
2. Navigate to **DNS** → **Records**
3. Click **Add record**:
   - **Type**: `CNAME`
   - **Name**: `dev`
   - **Target**: `<tunnel-uuid>.cfargotunnel.com` (provided by script)
   - **Proxy status**: Proxied (orange cloud)
4. Click **Save**

### Step 4: Start the Tunnel

```bash
cloudflared tunnel run mcp-servers
```

Or run as a service (see below).

### Step 5: Update OAuth Redirect URI

Update your OAuth redirect URI to use the custom domain:

```bash
./execution/scripts/setup_mcp_server_tunnel.sh \
    parquet \
    mcp/parquet/parquet_mcp_server.py \
    8080 \
    "" \
    --oauth-client-id "$CLIENT_ID" \
    --oauth-client-secret "$CLIENT_SECRET" \
    --oauth-redirect-uri "https://dev.neotoma.io/mcp/oauth/callback"
```

## Configuration File

The tunnel configuration is stored at `~/.cloudflared/config.yml`:

```yaml
tunnel: <tunnel-uuid>
credentials-file: ~/.cloudflared/<tunnel-uuid>.json

ingress:
  # MCP servers (with path prefix)
  - hostname: dev.neotoma.io
    path: /mcp/*
    service: http://localhost:8080
  
  # Catch-all rule (must be last)
  - service: http_status:404
```

## Running as Service (macOS)

Create a LaunchAgent to run the tunnel automatically:

```bash
cat > ~/Library/LaunchAgents/com.cloudflare.mcp-servers.plist <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.cloudflare.mcp-servers</string>
    <key>ProgramArguments</key>
    <array>
        <string>/opt/homebrew/bin/cloudflared</string>
        <string>tunnel</string>
        <string>run</string>
        <string>mcp-servers</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$HOME/Library/Logs/cloudflare-mcp-servers.log</string>
    <key>StandardErrorPath</key>
    <string>$HOME/Library/Logs/cloudflare-mcp-servers.error.log</string>
</dict>
</plist>
EOF

# Load the service
launchctl load ~/Library/LaunchAgents/com.cloudflare.mcp-servers.plist
```

## Using the Custom Domain

Once set up, your MCP server is accessible at:

```
https://dev.neotoma.io/mcp
```

**For OAuth:**
- **Token endpoint**: `https://dev.neotoma.io/mcp/oauth/token`
- **Authorization endpoint**: `https://dev.neotoma.io/mcp/oauth/authorize`
- **Callback endpoint**: `https://dev.neotoma.io/mcp/oauth/callback`

**For Custom Connector:**
- **URL**: `https://dev.neotoma.io/mcp`
- **OAuth Client ID**: Your client ID
- **OAuth Client Secret**: Your client secret

## Multiple Services on Same Domain

You can route multiple services on the same domain using different paths:

```yaml
ingress:
  # MCP servers
  - hostname: dev.neotoma.io
    path: /mcp/*
    service: http://localhost:8080
  
  # API server
  - hostname: dev.neotoma.io
    path: /api/*
    service: http://localhost:3000
  
  # Webhooks
  - hostname: dev.neotoma.io
    path: /webhooks/*
    service: http://localhost:8081
  
  # Catch-all (must be last)
  - service: http_status:404
```

## Troubleshooting

### Tunnel Not Starting

1. **Check authentication:**
   ```bash
   cloudflared tunnel list
   ```
   If this fails, re-authenticate:
   ```bash
   cloudflared tunnel login
   ```

2. **Check config file:**
   ```bash
   cat ~/.cloudflared/config.yml
   ```

3. **Check tunnel status:**
   ```bash
   cloudflared tunnel info mcp-servers
   ```

### DNS Not Resolving

1. **Verify DNS record exists** in Cloudflare Dashboard
2. **Check proxy status** (should be "Proxied" - orange cloud)
3. **Wait for DNS propagation** (can take a few minutes)
4. **Test DNS:**
   ```bash
   dig dev.neotoma.io
   # Should return CNAME to <tunnel-uuid>.cfargotunnel.com
   ```

### Path Not Working

- Ensure the path in config matches: `/mcp/*` (with wildcard)
- Check that your MCP proxy is running on the correct port
- Verify ingress rules are in correct order (specific paths before catch-all)

## Management

### List Tunnels
```bash
cloudflared tunnel list
```

### Check Tunnel Status
```bash
cloudflared tunnel info mcp-servers
```

### View Logs
```bash
tail -f ~/Library/Logs/cloudflare-mcp-servers.log
```

### Restart Service
```bash
launchctl unload ~/Library/LaunchAgents/com.cloudflare.mcp-servers.plist
launchctl load ~/Library/LaunchAgents/com.cloudflare.mcp-servers.plist
```
