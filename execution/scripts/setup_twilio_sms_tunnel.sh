#!/bin/bash
# Setup script for Cloudflare Named Tunnel for Twilio SMS webhook
# Creates a persistent tunnel with a stable URL

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TUNNEL_CONFIG_DIR="$HOME/.cloudflared"
TUNNEL_CONFIG_FILE="$TUNNEL_CONFIG_DIR/config.yml"
TUNNEL_NAME="twilio-sms-webhook"
WEBHOOK_PORT=8081

echo "Setting up Cloudflare Named Tunnel for Twilio SMS webhook..."
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

# Check if user is logged in
if ! cloudflared tunnel list &> /dev/null 2>&1; then
    echo "Not logged in to Cloudflare. Logging in..."
    echo "This will open a browser for authentication."
    cloudflared tunnel login
fi

# Create config directory
mkdir -p "$TUNNEL_CONFIG_DIR"

# Check if tunnel already exists
TUNNEL_EXISTS=false
if cloudflared tunnel list 2>/dev/null | grep -q "$TUNNEL_NAME"; then
    echo "Tunnel '$TUNNEL_NAME' already exists."
    read -p "Delete and recreate? (y/N): " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        echo "Deleting existing tunnel..."
        cloudflared tunnel delete "$TUNNEL_NAME" || true
    else
        echo "Using existing tunnel."
        TUNNEL_EXISTS=true
    fi
fi

if [ "$TUNNEL_EXISTS" != "true" ]; then
    # Create tunnel
    echo "Creating tunnel '$TUNNEL_NAME'..."
    cloudflared tunnel create "$TUNNEL_NAME"
fi

# Get tunnel UUID
TUNNEL_UUID=$(cloudflared tunnel list 2>/dev/null | grep "$TUNNEL_NAME" | awk '{print $1}')

if [ -z "$TUNNEL_UUID" ]; then
    echo "Error: Could not find tunnel UUID"
    exit 1
fi

echo "✓ Tunnel UUID: $TUNNEL_UUID"

# Check if config file exists and has other tunnels
if [ -f "$TUNNEL_CONFIG_FILE" ]; then
    echo ""
    echo "Config file already exists at: $TUNNEL_CONFIG_FILE"
    echo "Checking if tunnel is already configured..."
    
    if grep -q "tunnel: $TUNNEL_UUID" "$TUNNEL_CONFIG_FILE" || grep -q "tunnel: $TUNNEL_NAME" "$TUNNEL_CONFIG_FILE"; then
        echo "✓ Tunnel is already configured in config file"
    else
        echo ""
        echo "Adding tunnel configuration to existing config file..."
        echo ""
        echo "You'll need to manually add this ingress rule to your config:"
        echo ""
        echo "  - hostname: twilio-sms-webhook.YOUR_DOMAIN.com"
        echo "    service: http://localhost:$WEBHOOK_PORT"
        echo ""
        echo "Or if using Cloudflare's free tunnel service (trycloudflare.com),"
        echo "you can use a catch-all rule:"
        echo ""
        echo "  - service: http://localhost:$WEBHOOK_PORT"
        echo ""
        echo "Edit config: nano $TUNNEL_CONFIG_FILE"
    fi
else
    # Create new config file
    echo "Creating tunnel configuration..."
    
    # Check if user has a Cloudflare domain
    echo ""
    read -p "Do you have a Cloudflare domain configured? (y/N): " -n 1 -r
    echo
    
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        read -p "Enter your Cloudflare domain (e.g., example.com): " CLOUDFLARE_DOMAIN
        echo ""
        echo "Creating config with custom domain: $CLOUDFLARE_DOMAIN"
        
        cat > "$TUNNEL_CONFIG_FILE" <<EOF
tunnel: $TUNNEL_UUID
credentials-file: $TUNNEL_CONFIG_DIR/$TUNNEL_UUID.json

ingress:
  # Twilio SMS webhook
  - hostname: twilio-sms-webhook.$CLOUDFLARE_DOMAIN
    service: http://localhost:$WEBHOOK_PORT
  
  # Catch-all rule (must be last)
  - service: http_status:404
EOF
        
        echo "✓ Config created with custom domain"
        echo ""
        echo "Next steps:"
        echo "1. Create DNS record in Cloudflare:"
        echo "   Type: CNAME"
        echo "   Name: twilio-sms-webhook"
        echo "   Target: $TUNNEL_UUID.cfargotunnel.com"
        echo ""
        echo "2. Run the tunnel:"
        echo "   cloudflared tunnel run $TUNNEL_NAME"
        echo ""
        echo "3. Your webhook URL will be:"
        echo "   https://twilio-sms-webhook.$CLOUDFLARE_DOMAIN/webhook/twilio/sms"
    else
        echo "Creating config for Cloudflare's free tunnel service..."
        echo "Note: This will use a stable tunnel but URL format may vary."
        echo ""
        
        cat > "$TUNNEL_CONFIG_FILE" <<EOF
tunnel: $TUNNEL_UUID
credentials-file: $TUNNEL_CONFIG_DIR/$TUNNEL_UUID.json

ingress:
  # Twilio SMS webhook (catch-all for free tunnel service)
  - service: http://localhost:$WEBHOOK_PORT
EOF
        
        echo "✓ Config created for free tunnel service"
        echo ""
        echo "Note: With free tunnel service, you'll get a URL like:"
        echo "  https://$TUNNEL_UUID.cfargotunnel.com"
        echo ""
        echo "This URL is stable and won't change on restart."
        echo ""
        echo "To get your tunnel URL, run:"
        echo "  cloudflared tunnel info $TUNNEL_NAME"
        echo ""
        echo "Or check the tunnel logs when running."
    fi
fi

echo ""
echo "Configuration file: $TUNNEL_CONFIG_FILE"
echo ""
echo "To run the tunnel manually:"
echo "  cloudflared tunnel run $TUNNEL_NAME"
echo ""
echo "To run as a service (LaunchAgent), use:"
echo "  ./scripts/setup-twilio-sms-services.sh"
echo ""






