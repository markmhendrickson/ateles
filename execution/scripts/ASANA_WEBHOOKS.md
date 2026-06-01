# Asana Webhooks Setup

Webhooks provide instant sync when tasks change in Asana, eliminating the need for polling.

## Architecture

1. **Webhook Server** - Receives events from Asana
2. **Webhook Registration** - Registers webhooks for projects in both workspaces
3. **Event Handler** - Processes events and triggers immediate sync

## Quick Start

### 1. Start Webhook Server

```bash
# Local development (requires ngrok for public access)
python scripts/asana_webhook_server.py --port 8080

# Production (with public domain)
python scripts/asana_webhook_server.py --port 8080 --host 0.0.0.0
```

### 2. Expose Server Publicly (Local Development)

You have two options for exposing your local server:

#### Option A: Cloudflare Tunnel (Recommended)

Cloudflare Tunnel provides free HTTPS tunnels with better reliability than ngrok:

```bash
# Install cloudflared (if not installed)
brew install cloudflared

# Quick tunnel (no domain required, like ngrok)
./scripts/setup_cloudflare_tunnel_simple.sh 8080

# Or for persistent tunnel with custom domain
./scripts/setup_cloudflare_tunnel.sh
```

The quick tunnel will display a URL like `https://abc123.trycloudflare.com` - use this for webhook registration.

#### Option B: ngrok (Alternative)

```bash
# Install ngrok (if not installed)
brew install ngrok

# Start ngrok tunnel
ngrok http 8080
```

Copy the HTTPS URL (e.g., `https://abc123.ngrok.io`)

### 3. Register Webhooks

```bash
# Register webhooks for both workspaces
# Use Cloudflare Tunnel URL (from step 2)
python scripts/register_asana_webhooks.py \
  --webhook-url https://abc123.trycloudflare.com/webhook/asana \
  --workspace both

# Or use ngrok URL
python scripts/register_asana_webhooks.py \
  --webhook-url https://abc123.ngrok.io/webhook/asana \
  --workspace both

# Or use your custom domain (production)
python scripts/register_asana_webhooks.py \
  --webhook-url https://asana-webhook.yourdomain.com/webhook/asana \
  --workspace both
```

### 4. Verify Webhooks

```bash
# List registered webhooks
python scripts/register_asana_webhooks.py --list

# Check webhook server health
curl http://localhost:8080/health
```

## Production Deployment

### Option 1: Cloudflare Tunnel (Recommended for Local)

Run webhook server locally with Cloudflare Tunnel for public access:

```bash
# Terminal 1: Start webhook server
python scripts/asana_webhook_server.py --port 8080

# Terminal 2: Start Cloudflare Tunnel (persistent)
cloudflared tunnel run asana-webhook

# Or use quick tunnel
./scripts/setup_cloudflare_tunnel_simple.sh 8080
```

**Benefits:**
- Free HTTPS tunnel
- No domain required (quick tunnel mode)
- More reliable than ngrok free tier
- Can use custom domain if desired

### Option 2: Cloud Service

Deploy webhook server to a cloud service with public HTTPS:

- **Heroku**: `git push heroku main`
- **Railway**: Connect GitHub repo
- **Fly.io**: `fly deploy`
- **AWS Lambda**: Use API Gateway + Lambda

### Option 3: VPS with Reverse Proxy

1. Deploy server to VPS
2. Set up nginx/Caddy reverse proxy with SSL
3. Point domain to webhook endpoint
4. Register webhooks with domain URL

### Option 4: Keep Polling

If you can't set up a public endpoint, continue using polling:

```bash
python scripts/sync_asana_tasks.py --daemon --interval 60
```

## Webhook Server as Service

### macOS LaunchAgent

Create `scripts/com.finances.asana-webhook-server.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.finances.asana-webhook-server</string>
    
    <key>ProgramArguments</key>
    <array>
        <string>/path/to/execution/venv/bin/python</string>
        <string>$REPO_ROOT/execution/scripts/asana_webhook_server.py</string>
        <string>--port</string>
        <string>8080</string>
    </array>
    
    <key>WorkingDirectory</key>
    <string>$REPO_ROOT</string>
    
    <key>RunAtLoad</key>
    <true/>
    
    <key>KeepAlive</key>
    <true/>
    
    <key>StandardOutPath</key>
    <string>$REPO_ROOT/data/logs/webhook_server.log</string>
    
    <key>StandardErrorPath</key>
    <string>$REPO_ROOT/data/logs/webhook_server.error.log</string>
</dict>
</plist>
```

## How It Works

### Webhook Flow

1. **Task Changed in Asana** → Asana sends webhook event
2. **Webhook Server Receives Event** → Verifies signature
3. **Event Handler Processes** → Fetches updated task from Asana
4. **Immediate Sync** → Updates local parquet and syncs to other workspace
5. **Response Sent** → Confirms receipt to Asana

### Security

- **Signature Verification**: All webhooks verified using HMAC-SHA256
- **Secret Storage**: Webhook secrets stored during handshake
- **Workspace Validation**: Events validated against registered workspaces

### Event Types

Webhooks trigger on:
- Task created
- Task updated
- Task deleted
- Task moved between projects

## Management Commands

```bash
# List registered webhooks
python scripts/register_asana_webhooks.py --list

# Delete all webhooks
python scripts/register_asana_webhooks.py --delete-all

# Re-register webhooks (after server restart)
python scripts/register_asana_webhooks.py \
  --webhook-url https://your-domain.com/webhook/asana \
  --workspace both
```

## Troubleshooting

### Webhooks Not Receiving Events

1. **Check server is running**: `curl http://localhost:8080/health`
2. **Verify webhooks registered**: `python scripts/register_asana_webhooks.py --list`
3. **Check server logs**: `tail -f data/logs/webhook_server.log`
4. **Verify public accessibility**: Test webhook URL from external network

### Signature Verification Fails

- Webhook secret may have changed
- Re-register webhooks to get new secret
- Check server logs for signature errors

### Events Not Syncing

- Check webhook server logs for errors
- Verify Asana API credentials are correct
- Ensure sync lock isn't blocking (check logs)

## Hybrid Approach

You can use both webhooks and polling:

- **Webhooks**: Instant sync for active workspaces
- **Polling**: Backup sync every 5-10 minutes for missed events

Run both services simultaneously:

```bash
# Terminal 1: Webhook server
python scripts/asana_webhook_server.py

# Terminal 2: Polling backup (longer interval)
python scripts/sync_asana_tasks.py --daemon --interval 600
```

## Cloudflare Tunnel Setup

### Quick Tunnel (No Domain Required)

Simplest option - creates a temporary tunnel URL:

```bash
# Start webhook server in one terminal
python scripts/asana_webhook_server.py --port 8080

# Start Cloudflare Tunnel in another terminal
./scripts/setup_cloudflare_tunnel_simple.sh 8080
```

The tunnel will display a URL like `https://abc123.trycloudflare.com` - use this for webhook registration.

### Persistent Tunnel (With Custom Domain)

For a stable URL with your own domain:

1. **Install cloudflared**:
   ```bash
   brew install cloudflared
   ```

2. **Login to Cloudflare**:
   ```bash
   cloudflared tunnel login
   ```

3. **Create tunnel**:
   ```bash
   ./scripts/setup_cloudflare_tunnel.sh
   ```

4. **Edit config** (replace YOUR_DOMAIN.com):
   ```bash
   nano ~/.cloudflared/config.yml
   ```

5. **Run tunnel**:
   ```bash
   cloudflared tunnel run asana-webhook
   ```

### Running Tunnel as Service

Create a LaunchAgent to run tunnel automatically:

```bash
# Create plist file
cat > ~/Library/LaunchAgents/com.cloudflare.asana-webhook.plist <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.cloudflare.asana-webhook</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/local/bin/cloudflared</string>
        <string>tunnel</string>
        <string>run</string>
        <string>asana-webhook</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$REPO_ROOT/data/logs/cloudflare_tunnel.log</string>
    <key>StandardErrorPath</key>
    <string>$REPO_ROOT/data/logs/cloudflare_tunnel.error.log</string>
</dict>
</plist>
EOF

# Load the service
launchctl load ~/Library/LaunchAgents/com.cloudflare.asana-webhook.plist
```

## Comparison: Webhooks vs Polling

| Feature | Webhooks | Polling |
|---------|----------|---------|
| **Latency** | Instant (< 1s) | 60s+ (configurable) |
| **API Calls** | Only on changes | Every interval |
| **Setup Complexity** | Requires public endpoint | Simple |
| **Reliability** | Requires server uptime | More resilient |
| **Cost** | Free (Cloudflare Tunnel) | API rate limits |

**Recommendation**: Use webhooks with Cloudflare Tunnel for instant sync, polling as backup.

