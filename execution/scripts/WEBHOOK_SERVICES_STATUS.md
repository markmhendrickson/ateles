# Asana Webhook Services Status

## Services Installed

✅ **Webhook Server** - Running on port 8080
✅ **Cloudflare Tunnel** - Running and connecting

## Service Status

Check service status:
```bash
launchctl list | grep -E '(asana-webhook|cloudflare)'
```

## Get Tunnel URL

The tunnel URL appears in the logs once the connection is established:

```bash
# Watch logs in real-time
tail -f data/logs/cloudflare_tunnel.log

# Extract URL from logs
grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' data/logs/cloudflare_tunnel.log | tail -1

# Or use helper script
./scripts/get_tunnel_url.sh
```

The URL format will be: `https://abc123-def456-ghi789.trycloudflare.com`

## Register Webhooks

Once you have the tunnel URL:

```bash
python scripts/register_asana_webhooks.py \
  --webhook-url https://YOUR-TUNNEL-URL.trycloudflare.com/webhook/asana \
  --workspace both
```

## Log Files

- **Webhook Server**: `data/logs/webhook_server.log`
- **Webhook Errors**: `data/logs/webhook_server.error.log`
- **Tunnel**: `data/logs/cloudflare_tunnel.log`
- **Tunnel Errors**: `data/logs/cloudflare_tunnel.error.log`

## Service Management

### Stop Services
```bash
launchctl unload ~/Library/LaunchAgents/com.finances.asana-webhook-server.plist
launchctl unload ~/Library/LaunchAgents/com.cloudflare.asana-webhook-tunnel.plist
```

### Start Services
```bash
launchctl load ~/Library/LaunchAgents/com.finances.asana-webhook-server.plist
launchctl load ~/Library/LaunchAgents/com.cloudflare.asana-webhook-tunnel.plist
```

### Restart Services
```bash
./scripts/setup-asana-webhook-services.sh
```

## Troubleshooting

### Tunnel URL Not Appearing

1. Check tunnel is running:
   ```bash
   ps aux | grep cloudflared
   ```

2. Check tunnel logs:
   ```bash
   tail -f data/logs/cloudflare_tunnel.log
   ```

3. Restart tunnel:
   ```bash
   launchctl unload ~/Library/LaunchAgents/com.cloudflare.asana-webhook-tunnel.plist
   launchctl load ~/Library/LaunchAgents/com.cloudflare.asana-webhook-tunnel.plist
   ```

### Webhook Server Not Running

1. Check if port 8080 is in use:
   ```bash
   lsof -i :8080
   ```

2. Check webhook server logs:
   ```bash
   tail -f data/logs/webhook_server.error.log
   ```

3. Restart webhook server:
   ```bash
   launchctl unload ~/Library/LaunchAgents/com.finances.asana-webhook-server.plist
   launchctl load ~/Library/LaunchAgents/com.finances.asana-webhook-server.plist
   ```

## Next Steps

1. ✅ Services are running
2. ⏳ Wait for tunnel URL (check logs)
3. 📝 Register webhooks with tunnel URL
4. ✅ Webhooks will sync tasks instantly








