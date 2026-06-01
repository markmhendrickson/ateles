#!/bin/bash
# Setup script for Twilio SMS webhook server, Cloudflare Tunnel, and polling LaunchAgents
# This installs all services to run automatically on system startup

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"

WEBHOOK_PLIST="$SCRIPT_DIR/com.finances.twilio-sms-webhook-server.plist"
TUNNEL_PLIST="$SCRIPT_DIR/com.cloudflare.twilio-sms-webhook-tunnel.plist"
POLLING_PLIST="$SCRIPT_DIR/com.finances.twilio-sms-polling.plist"

WEBHOOK_INSTALLED="$LAUNCH_AGENTS_DIR/com.finances.twilio-sms-webhook-server.plist"
TUNNEL_INSTALLED="$LAUNCH_AGENTS_DIR/com.cloudflare.twilio-sms-webhook-tunnel.plist"
POLLING_INSTALLED="$LAUNCH_AGENTS_DIR/com.finances.twilio-sms-polling.plist"

echo "Setting up Twilio SMS services..."
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

# Check if wrapper scripts exist and are executable
WRAPPER_SCRIPTS=(
    "$SCRIPT_DIR/twilio_sms_webhook_wrapper.sh"
    "$SCRIPT_DIR/twilio_sms_polling_wrapper.sh"
)

for WRAPPER_SCRIPT in "${WRAPPER_SCRIPTS[@]}"; do
    if [ ! -f "$WRAPPER_SCRIPT" ]; then
        echo "Error: Wrapper script not found: $WRAPPER_SCRIPT" >&2
        exit 1
    fi
    chmod +x "$WRAPPER_SCRIPT"
done

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

# Check if named tunnel is configured
if ! cloudflared tunnel list 2>/dev/null | grep -q "twilio-sms-webhook"; then
    echo ""
    echo "⚠️  Named tunnel 'twilio-sms-webhook' not found!"
    echo ""
    echo "Please run the tunnel setup script first:"
    echo "  ./scripts/setup_twilio_sms_tunnel.sh"
    echo ""
    echo "This will create a persistent named tunnel with a stable URL."
    echo ""
    read -p "Continue anyway? (y/N): " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo "Setup cancelled. Please run ./scripts/setup_twilio_sms_tunnel.sh first."
        exit 1
    fi
fi

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

# Setup polling service
echo ""
echo "Setting up polling service..."

# Unload existing service if running
if [ -f "$POLLING_INSTALLED" ]; then
    echo "Unloading existing polling service..."
    launchctl unload "$POLLING_INSTALLED" 2>/dev/null || true
fi

# Generate plist file with correct paths
echo "Installing polling LaunchAgent..."
sed "s|/Users/markmhendrickson/Projects/personal|$PROJECT_ROOT|g" "$POLLING_PLIST" > "$POLLING_INSTALLED"

# Load the LaunchAgent
echo "Loading polling LaunchAgent..."
launchctl load "$POLLING_INSTALLED"

# Wait a moment for services to start
sleep 2

echo ""
echo "=== Setup Complete ==="
echo ""
echo "Services installed and started:"
echo "  - Webhook server: com.finances.twilio-sms-webhook-server (port 8081)"
echo "  - Cloudflare Tunnel: com.cloudflare.twilio-sms-webhook-tunnel"
echo "  - Polling: com.finances.twilio-sms-polling (daily at 2 AM)"
echo ""
echo "Useful commands:"
echo ""
echo "Check service status:"
echo "  launchctl list | grep -E '(twilio-sms|cloudflare.*twilio)'"
echo ""
echo "View webhook server logs:"
echo "  tail -f $PROJECT_ROOT/data/logs/twilio_sms_webhook.log"
echo ""
echo "View tunnel logs:"
echo "  tail -f $PROJECT_ROOT/data/logs/cloudflare_twilio_sms_tunnel.log"
echo ""
echo "View polling logs:"
echo "  tail -f $PROJECT_ROOT/data/logs/twilio_sms_polling.log"
echo ""
echo "Get tunnel URL:"
echo "  grep -i 'https://' $PROJECT_ROOT/data/logs/cloudflare_twilio_sms_tunnel.log | tail -1"
echo ""
echo "Stop services:"
echo "  launchctl unload $WEBHOOK_INSTALLED"
echo "  launchctl unload $TUNNEL_INSTALLED"
echo "  launchctl unload $POLLING_INSTALLED"
echo ""
echo "Start services:"
echo "  launchctl load $WEBHOOK_INSTALLED"
echo "  launchctl load $TUNNEL_INSTALLED"
echo "  launchctl load $POLLING_INSTALLED"
echo ""
echo "Next steps:"
echo "  1. If you haven't set up the named tunnel yet, run:"
echo "     ./scripts/setup_twilio_sms_tunnel.sh"
echo ""
echo "  2. Wait for tunnel URL to appear in logs (may take 10-30 seconds)"
echo "     tail -f $PROJECT_ROOT/data/logs/cloudflare_twilio_sms_tunnel.log"
echo ""
echo "  3. Get your tunnel URL:"
echo "     - With custom domain: https://twilio-sms-webhook.yourdomain.com/webhook/twilio/sms"
echo "     - With free service: cloudflared tunnel info twilio-sms-webhook"
echo ""
echo "  4. Update Twilio phone number webhook URL in Twilio Console:"
echo "     Phone Numbers → +16503198857 → Messaging → A MESSAGE COMES IN"
echo ""
echo "Note: Named tunnels provide stable URLs that don't change on restart."
echo "      No need to update webhook URLs after restarts!"
echo ""

