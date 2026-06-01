#!/bin/bash
# Setup script for Cloudflare Tunnel to expose webhook server locally
# This creates a persistent tunnel with a stable URL

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TUNNEL_CONFIG_DIR="$HOME/.cloudflared"
TUNNEL_CONFIG_FILE="$TUNNEL_CONFIG_DIR/config.yml"
TUNNEL_NAME="asana-webhook"

echo "Setting up Cloudflare Tunnel for Asana webhook server..."
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
if ! cloudflared tunnel list &> /dev/null; then
    echo "Not logged in to Cloudflare. Logging in..."
    cloudflared tunnel login
fi

# Create config directory
mkdir -p "$TUNNEL_CONFIG_DIR"

# Check if tunnel already exists
if cloudflared tunnel list | grep -q "$TUNNEL_NAME"; then
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
TUNNEL_UUID=$(cloudflared tunnel list | grep "$TUNNEL_NAME" | awk '{print $1}')

if [ -z "$TUNNEL_UUID" ]; then
    echo "Error: Could not find tunnel UUID"
    exit 1
fi

# Create tunnel config
echo "Creating tunnel configuration..."
cat > "$TUNNEL_CONFIG_FILE" <<EOF
tunnel: $TUNNEL_UUID
credentials-file: $TUNNEL_CONFIG_DIR/$TUNNEL_UUID.json

ingress:
  # Webhook server
  - hostname: asana-webhook.YOUR_DOMAIN.com
    service: http://localhost:8080
  
  # Catch-all rule (must be last)
  - service: http_status:404
EOF

echo ""
echo "Configuration created at: $TUNNEL_CONFIG_FILE"
echo ""
echo "IMPORTANT: Edit the config file and replace YOUR_DOMAIN.com with your Cloudflare domain:"
echo "  nano $TUNNEL_CONFIG_FILE"
echo ""
echo "Then run the tunnel:"
echo "  cloudflared tunnel run $TUNNEL_NAME"
echo ""
echo "Or run in background:"
# Require DATA_DIR environment variable
if [ -z "$DATA_DIR" ]; then
    echo "Error: DATA_DIR environment variable is not set" >&2
    echo "Please set DATA_DIR to your data directory path, e.g.:" >&2
    echo "  export DATA_DIR=\"/absolute/path/to/data\"" >&2
    exit 1
fi
LOG_PATH="$DATA_DIR/logs/cloudflare_tunnel.log"
echo "  cloudflared tunnel run $TUNNEL_NAME > $LOG_PATH 2>&1 &"
echo ""
echo "To get the public URL:"
echo "  cloudflared tunnel route dns show $TUNNEL_NAME"
echo ""








