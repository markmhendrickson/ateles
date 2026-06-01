#!/bin/bash
# Setup script to install LaunchAgent for MCP Cloudflare tunnel
# This makes the tunnel start automatically on system startup

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
PLIST_NAME="com.cloudflare.mcp-servers-tunnel.plist"
PLIST_SOURCE="$SCRIPT_DIR/$PLIST_NAME"
PLIST_TARGET="$HOME/Library/LaunchAgents/$PLIST_NAME"
LOG_DIR="$PROJECT_ROOT/data/logs"

echo "Setting up MCP Cloudflare Tunnel Auto-Start"
echo "============================================"
echo ""

# Check if cloudflared is installed
if ! command -v cloudflared &> /dev/null; then
    echo "Error: cloudflared is not installed"
    echo ""
    echo "Install with: brew install cloudflared"
    exit 1
fi

# Check if tunnel exists
TUNNEL_NAME="mcp-servers"
if ! cloudflared tunnel list 2>/dev/null | grep -q "$TUNNEL_NAME"; then
    echo "Error: Tunnel '$TUNNEL_NAME' does not exist"
    echo ""
    echo "Create it first with:"
    echo "  ./execution/scripts/setup_mcp_custom_domain_tunnel.sh"
    echo ""
    exit 1
fi

# Create log directory
mkdir -p "$LOG_DIR"

# Check if plist source exists
if [ ! -f "$PLIST_SOURCE" ]; then
    echo "Error: LaunchAgent plist not found at $PLIST_SOURCE"
    exit 1
fi

# Check if already installed
if [ -f "$PLIST_TARGET" ]; then
    echo "LaunchAgent already installed at: $PLIST_TARGET"
    read -p "Unload and reinstall? (y/N): " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        echo "Unloading existing LaunchAgent..."
        launchctl unload "$PLIST_TARGET" 2>/dev/null || true
    else
        echo "Keeping existing installation."
        exit 0
    fi
fi

# Copy plist to LaunchAgents
echo "Installing LaunchAgent..."
cp "$PLIST_SOURCE" "$PLIST_TARGET"

# Load the LaunchAgent
echo "Loading LaunchAgent..."
launchctl load "$PLIST_TARGET"

# Verify it's running
sleep 2
if launchctl list | grep -q "com.cloudflare.mcp-servers-tunnel"; then
    echo ""
    echo "✓ LaunchAgent installed and running"
    echo ""
    echo "Tunnel will now start automatically on system startup."
    echo ""
    echo "To check status:"
    echo "  launchctl list | grep mcp-servers-tunnel"
    echo ""
    echo "To view logs:"
    echo "  tail -f $LOG_DIR/cloudflare_mcp_tunnel.log"
    echo ""
    echo "To stop:"
    echo "  launchctl unload $PLIST_TARGET"
    echo ""
    echo "To start manually:"
    echo "  launchctl load $PLIST_TARGET"
    echo ""
    echo "Note: The tunnel connects to localhost:8080"
    echo "Make sure the MCP proxy is running on port 8080"
    echo "  ./execution/scripts/setup_mcp_server_tunnel.sh parquet mcp/parquet/parquet_mcp_server.py 8080 ..."
else
    echo ""
    echo "⚠ LaunchAgent installed but may not be running yet"
    echo "Check logs: tail -f $LOG_DIR/cloudflare_mcp_tunnel.error.log"
fi
