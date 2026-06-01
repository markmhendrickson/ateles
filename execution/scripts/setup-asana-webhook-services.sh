#!/bin/bash
# Setup script for Asana webhook server and Cloudflare Tunnel LaunchAgents
# This installs both services to run automatically on system startup

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"

WEBHOOK_PLIST="$SCRIPT_DIR/com.finances.asana-webhook-server.plist"
TUNNEL_PLIST="$SCRIPT_DIR/com.cloudflare.asana-webhook-tunnel.plist"

WEBHOOK_INSTALLED="$LAUNCH_AGENTS_DIR/com.finances.asana-webhook-server.plist"
TUNNEL_INSTALLED="$LAUNCH_AGENTS_DIR/com.cloudflare.asana-webhook-tunnel.plist"

echo "Setting up Asana webhook services..."
echo ""

# Check if cloudflared is installed
if ! command -v cloudflared &> /dev/null; then
    echo "cloudflared is not installed."
    echo ""
    echo "Installing cloudflared..."
    if command -v brew &> /dev/null; then
        brew install cloudflared
    else
        echo "Error: Homebrew not found. Please install cloudflared manually:"
        echo "  https://developers.cloudflare.com/cloudflare-one/connections/connect-apps/install-and-setup/installation/"
        exit 1
    fi
fi

# Check if wrapper script exists and is executable
WRAPPER_SCRIPT="$SCRIPT_DIR/asana_webhook_wrapper.sh"
if [ ! -f "$WRAPPER_SCRIPT" ]; then
    echo "Error: Wrapper script not found: $WRAPPER_SCRIPT" >&2
    exit 1
fi
chmod +x "$WRAPPER_SCRIPT"

# Create LaunchAgents directory if it doesn't exist
mkdir -p "$LAUNCH_AGENTS_DIR"

# Setup webhook server service
echo "Setting up webhook server service..."

# Unload existing service if running
if [ -f "$WEBHOOK_INSTALLED" ]; then
    echo "Unloading existing webhook server service..."
    launchctl unload "$WEBHOOK_INSTALLED" 2>/dev/null || true
fi

# Generate plist file with correct paths
echo "Installing webhook server LaunchAgent..."
sed "s|/Users/markmhendrickson/Projects/personal|$PROJECT_ROOT|g" "$WEBHOOK_PLIST" > "$WEBHOOK_INSTALLED"

# Load the LaunchAgent
echo "Loading webhook server LaunchAgent..."
launchctl load "$WEBHOOK_INSTALLED"

# Setup Cloudflare Tunnel service
echo ""
echo "Setting up Cloudflare Tunnel service..."

# Unload existing service if running
if [ -f "$TUNNEL_INSTALLED" ]; then
    echo "Unloading existing tunnel service..."
    launchctl unload "$TUNNEL_INSTALLED" 2>/dev/null || true
fi

# Generate plist file with correct paths
echo "Installing tunnel LaunchAgent..."
sed "s|/Users/markmhendrickson/Projects/personal|$PROJECT_ROOT|g" "$TUNNEL_PLIST" > "$TUNNEL_INSTALLED"

# Load the LaunchAgent
echo "Loading tunnel LaunchAgent..."
launchctl load "$TUNNEL_INSTALLED"

# Wait a moment for services to start
sleep 2

echo ""
echo "=== Setup Complete ==="
echo ""
echo "Services installed and started:"
echo "  - Webhook server: com.finances.asana-webhook-server"
echo "  - Cloudflare Tunnel: com.cloudflare.asana-webhook-tunnel"
echo ""
echo "Useful commands:"
echo ""
echo "Check service status:"
echo "  launchctl list | grep -E '(asana-webhook|cloudflare)'"
echo ""
echo "View webhook server logs:"
echo "  tail -f $PROJECT_ROOT/data/logs/webhook_server.log"
echo ""
echo "View tunnel logs:"
echo "  tail -f $PROJECT_ROOT/data/logs/cloudflare_tunnel.log"
echo ""
echo "Get tunnel URL:"
echo "  grep -i 'https://' $PROJECT_ROOT/data/logs/cloudflare_tunnel.log | tail -1"
echo ""
echo "Stop services:"
echo "  launchctl unload $WEBHOOK_INSTALLED"
echo "  launchctl unload $TUNNEL_INSTALLED"
echo ""
echo "Start services:"
echo "  launchctl load $WEBHOOK_INSTALLED"
echo "  launchctl load $TUNNEL_INSTALLED"
echo ""
echo "Next steps:"
echo "  1. Wait for tunnel URL to appear in logs (may take 10-30 seconds)"
echo "  2. Get tunnel URL: tail -f $PROJECT_ROOT/data/logs/cloudflare_tunnel.log"
echo "  3. Register webhooks: python scripts/register_asana_webhooks.py --webhook-url <tunnel-url>/webhook/asana --workspace both"
echo ""








