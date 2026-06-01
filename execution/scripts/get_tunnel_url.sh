#!/bin/bash
# Get the Cloudflare Tunnel URL from logs

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Require DATA_DIR environment variable
if [ -z "$DATA_DIR" ]; then
    echo "Error: DATA_DIR environment variable is not set" >&2
    exit 1
fi

LOG_FILE="$DATA_DIR/logs/cloudflare_tunnel.log"

if [ ! -f "$LOG_FILE" ]; then
    echo "Tunnel log file not found. Tunnel may still be starting..."
    echo "Check status: launchctl list | grep cloudflare"
    exit 1
fi

# Try to extract URL from logs
URL=$(grep -oE 'https://[a-z0-9-]+\.(trycloudflare\.com|cfargotunnel\.com)' "$LOG_FILE" 2>/dev/null | tail -1)

if [ -z "$URL" ]; then
    echo "Tunnel URL not found in logs yet."
    echo ""
    echo "The tunnel may still be connecting. Check logs:"
    echo "  tail -f $LOG_FILE"
    echo ""
    echo "Or check if tunnel is running:"
    echo "  ps aux | grep cloudflared"
    exit 1
fi

echo "$URL"








