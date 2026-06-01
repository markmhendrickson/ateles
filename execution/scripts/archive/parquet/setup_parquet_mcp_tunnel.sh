#!/bin/bash
# Setup script to expose parquet MCP server via HTTP tunnel with MCP standard Bearer token authentication
# This is a convenience wrapper around the generic setup_mcp_server_tunnel.sh script

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HTTP_PORT=${1:-8080}
AUTH_TOKEN=${2:-""}

# Use the generic script
"$SCRIPT_DIR/setup_mcp_server_tunnel.sh" \
    "parquet" \
    "mcp/parquet/parquet_mcp_server.py" \
    "$HTTP_PORT" \
    "$AUTH_TOKEN"
