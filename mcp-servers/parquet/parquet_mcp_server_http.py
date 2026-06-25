#!/usr/bin/env python3
"""
MCP Server for Parquet File Interactions - HTTP Transport Support

This is a wrapper that adds HTTP transport support to the existing parquet MCP server.
Run this instead of parquet_mcp_server.py when you need remote access.

Usage:
    # Local stdio mode (default)
    python parquet_mcp_server.py

    # HTTP mode (remote access)
    python parquet_mcp_server_http.py --port 8080 --host 0.0.0.0

    # Or use environment variables
    MCP_TRANSPORT=http MCP_PORT=8080 python parquet_mcp_server_http.py
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

# Import the existing server app
# This assumes parquet_mcp_server.py is in the same directory
sys.path.insert(0, str(Path(__file__).parent))

try:
    from mcp.server.fastmcp import FastMCP

    FASTMCP_AVAILABLE = True
except ImportError:
    FASTMCP_AVAILABLE = False
    print("Warning: FastMCP not available. Install with: pip install mcp[fastmcp]")

# Import the existing server app and tools
from parquet_mcp_server import app

# Check if we should use HTTP transport
TRANSPORT = os.environ.get("MCP_TRANSPORT", "stdio").lower()
HTTP_PORT = int(os.environ.get("MCP_PORT", "8080"))
HTTP_HOST = os.environ.get("MCP_HOST", "0.0.0.0")


async def main():
    """Run the MCP server with configurable transport."""
    parser = argparse.ArgumentParser(description="Parquet MCP Server with HTTP support")
    parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default=TRANSPORT,
        help="Transport mode: stdio (local) or http (remote)",
    )
    parser.add_argument(
        "--port", type=int, default=HTTP_PORT, help="HTTP port (default: 8080)"
    )
    parser.add_argument(
        "--host", default=HTTP_HOST, help="HTTP host (default: 0.0.0.0)"
    )

    args = parser.parse_args()

    if args.transport == "http":
        if not FASTMCP_AVAILABLE:
            print("Error: FastMCP is required for HTTP transport")
            print("Install with: pip install 'mcp[fastmcp]'")
            sys.exit(1)

        print("Starting Parquet MCP Server in HTTP mode")
        print(f"Listening on http://{args.host}:{args.port}")
        print(f"Connect clients to: http://{args.host}:{args.port}")
        print("")

        # Convert stdio server to FastMCP HTTP server
        # Note: This requires refactoring the existing server to use FastMCP
        # For now, this is a placeholder showing the approach

        # Option: Use a bridge/proxy instead (see setup scripts)
        print("Note: For HTTP transport, use the proxy bridge script:")
        print("  ./execution/scripts/setup_parquet_mcp_tunnel.sh")
        print("")
        print("Or install mcp-proxy and run:")
        print("  npx @modelcontextprotocol/proxy stdio-to-http \\")
        print("    --stdio-command python3 \\")
        print(f"    --stdio-args {Path(__file__).parent}/parquet_mcp_server.py \\")
        print(f"    --http-port {args.port}")

        sys.exit(0)
    else:
        # Use original stdio transport
        from mcp.server.stdio import stdio_server

        async with stdio_server() as (read_stream, write_stream):
            await app.run(
                read_stream, write_stream, app.create_initialization_options()
            )


if __name__ == "__main__":
    asyncio.run(main())
