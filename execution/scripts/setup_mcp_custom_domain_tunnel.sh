#!/bin/bash
# Setup script for Cloudflare Tunnel with custom domain for MCP servers
# Creates a persistent tunnel with custom domain like dev.neotoma.io/mcp

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TUNNEL_CONFIG_DIR="$HOME/.cloudflared"
TUNNEL_CONFIG_FILE="$TUNNEL_CONFIG_DIR/config.yml"
TUNNEL_NAME="mcp-servers"
LOCAL_PORT=${1:-8080}
CUSTOM_DOMAIN=${2:-"dev.neotoma.io"}
SUBPATH=${3:-"mcp"}

echo "Setting up Cloudflare Tunnel for MCP servers with custom domain..."
echo ""

# Check if cloudflared is installed
if ! command -v cloudflared &> /dev/null; then
    echo "cloudflared is not installed."
    echo ""
    echo "Install it with:"
    echo "  brew install cloudflared"
    echo ""
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
TUNNEL_EXISTS=false
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

echo ""
echo "Tunnel UUID: $TUNNEL_UUID"
echo ""

# Check if config file exists and has other tunnels
if [ -f "$TUNNEL_CONFIG_FILE" ]; then
    echo "Config file already exists at: $TUNNEL_CONFIG_FILE"
    echo ""
    
    if grep -q "tunnel: $TUNNEL_UUID" "$TUNNEL_CONFIG_FILE" || grep -q "tunnel: $TUNNEL_NAME" "$TUNNEL_CONFIG_FILE"; then
        echo "✓ Tunnel is already configured in config file"
    else
        echo "Adding MCP tunnel configuration to existing config file..."
        echo ""
        echo "You'll need to manually add this ingress rule to your config:"
        echo ""
        echo "  - hostname: $CUSTOM_DOMAIN"
        echo "    path: /$SUBPATH/*"
        echo "    service: http://localhost:$LOCAL_PORT"
        echo ""
        echo "Edit config: nano $TUNNEL_CONFIG_FILE"
        echo ""
        read -p "Press Enter after adding the ingress rule..."
    fi
else
    # Create new config file
    echo "Creating tunnel configuration..."
    
    cat > "$TUNNEL_CONFIG_FILE" <<EOF
tunnel: $TUNNEL_UUID
credentials-file: $TUNNEL_CONFIG_DIR/$TUNNEL_UUID.json

ingress:
  # MCP servers (with path prefix)
  - hostname: $CUSTOM_DOMAIN
    path: /$SUBPATH/*
    service: http://localhost:$LOCAL_PORT
  
  # Catch-all rule (must be last)
  - service: http_status:404
EOF
    
    echo "✓ Configuration file created at: $TUNNEL_CONFIG_FILE"
fi

# Setup DNS
echo ""
echo "Setting up DNS..."
echo ""
echo "You need to create a DNS record in Cloudflare:"
echo ""
echo "  Type: CNAME"
echo "  Name: dev (for dev.neotoma.io)"
echo "  Target: $TUNNEL_UUID.cfargotunnel.com"
echo "  Proxy: Enabled (orange cloud)"
echo ""
read -p "Have you created the DNS record? (y/N): " -n 1 -r
echo

if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo ""
    echo "Please create the DNS record in Cloudflare Dashboard:"
    echo "  https://dash.cloudflare.com -> Your domain -> DNS -> Records"
    echo ""
    echo "Then run this script again or start the tunnel manually:"
    echo "  cloudflared tunnel run $TUNNEL_NAME"
    exit 0
fi

# Test tunnel connection
echo ""
echo "Testing tunnel configuration..."
cloudflared tunnel info "$TUNNEL_NAME" || echo "Warning: Could not get tunnel info"

echo ""
echo "✓ Setup complete!"
echo ""
echo "Your MCP server will be accessible at:"
echo "  https://$CUSTOM_DOMAIN/$SUBPATH"
echo ""
echo "To start the tunnel:"
echo "  cloudflared tunnel run $TUNNEL_NAME"
echo ""
echo "Or run as a service (see setup_mcp_tunnel_service.sh)"
echo ""
