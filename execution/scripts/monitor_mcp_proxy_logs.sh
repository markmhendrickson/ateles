#!/bin/bash
# Monitor MCP Proxy logs in real-time

LOG_FILE="${MCP_PROXY_LOG_FILE:-/private/tmp/mcp_proxy.log}"
CLOUDFLARE_LOG="${CLOUDFLARE_TUNNEL_LOG:-/Users/markmhendrickson/repos/ateles/data/logs/cloudflare_mcp_tunnel.log}"
CLOUDFLARE_ERROR_LOG="${CLOUDFLARE_TUNNEL_ERROR_LOG:-/Users/markmhendrickson/repos/ateles/data/logs/cloudflare_mcp_tunnel.error.log}"

echo "Monitoring MCP Proxy logs..."
echo "Proxy log: $LOG_FILE"
echo "Cloudflare tunnel log: $CLOUDFLARE_LOG"
echo "Cloudflare error log: $CLOUDFLARE_ERROR_LOG"
echo "Press Ctrl+C to stop"
echo ""

# Function to tail multiple files with labels
tail_multiple() {
    if [ -f "$LOG_FILE" ]; then
        tail -f "$LOG_FILE" | sed "s/^/[PROXY] /" &
    fi
    
    if [ -f "$CLOUDFLARE_LOG" ]; then
        tail -f "$CLOUDFLARE_LOG" | sed "s/^/[TUNNEL] /" &
    fi
    
    if [ -f "$CLOUDFLARE_ERROR_LOG" ]; then
        tail -f "$CLOUDFLARE_ERROR_LOG" | sed "s/^/[ERROR] /" &
    fi
    
    # Wait for all background processes
    wait
}

# Cleanup function
cleanup() {
    echo ""
    echo "Stopping log monitoring..."
    kill $(jobs -p) 2>/dev/null
    exit 0
}

trap cleanup SIGINT SIGTERM

tail_multiple
