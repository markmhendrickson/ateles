#!/bin/bash
# Quick restart script for MCP proxy
# Note: setup_mcp_server_tunnel.sh now loads .env automatically

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

cd "$PROJECT_ROOT"

# Stop existing proxy
echo "Stopping existing proxy..."
pkill -f "mcp_authenticated_proxy.py" 2>/dev/null || true
sleep 1

# Start proxy (.env is loaded automatically by setup script)
echo "Starting MCP proxy..."
./execution/scripts/setup_mcp_server_tunnel.sh parquet mcp/parquet/parquet_mcp_server.py 8080
