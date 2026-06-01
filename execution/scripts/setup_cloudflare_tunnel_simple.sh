#!/bin/bash
# Simple Cloudflare Tunnel setup - no domain required
# Creates a quick tunnel URL (like ngrok) without DNS setup

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
WEBHOOK_PORT=${1:-8080}

echo "Starting Cloudflare Tunnel (quick tunnel mode)..."
echo "This will create a temporary tunnel URL (no domain required)"
echo ""
echo "Webhook server should be running on port $WEBHOOK_PORT"
echo ""

# Check if cloudflared is installed
if ! command -v cloudflared &> /dev/null; then
    echo "cloudflared is not installed."
    echo ""
    echo "Install it with:"
    echo "  brew install cloudflared"
    echo ""
    echo "Or download from: https://developers.cloudflare.com/cloudflare-one/connections/connect-apps/install-and-setup/installation/"
    exit 1
fi

# Check if webhook server is running
if ! lsof -Pi :$WEBHOOK_PORT -sTCP:LISTEN -t >/dev/null ; then
    echo "Warning: No service detected on port $WEBHOOK_PORT"
    echo "Make sure the webhook server is running:"
    echo "  python scripts/asana_webhook_server.py --port $WEBHOOK_PORT"
    echo ""
    read -p "Continue anyway? (y/N): " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        exit 1
    fi
fi

echo "Starting tunnel..."
echo "The tunnel URL will be displayed below. Use it to register webhooks."
echo ""
echo "Press Ctrl+C to stop the tunnel"
echo ""

# Run tunnel in quick mode (no config needed)
cloudflared tunnel --url http://localhost:$WEBHOOK_PORT








