#!/usr/bin/env python3
"""
Authenticated HTTP Proxy for Parquet MCP Server

This script wraps the MCP proxy with MCP standard Bearer token authentication.
It forwards authenticated requests to the underlying stdio MCP server.

Uses MCP standard: Authorization: Bearer <token> (OAuth 2.1 style)

Usage:
    python parquet_mcp_authenticated_proxy.py --port 8080 --auth-token "your-secret-token"

    Or use environment variables (MCP standard):
    MCP_PROXY_PORT=8080 MCP_AUTH_TOKEN="your-secret-token" python parquet_mcp_authenticated_proxy.py
"""

import argparse
import asyncio
import os
import secrets
import subprocess
import sys
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")

try:
    from aiohttp import ClientError, ClientSession, web
    from aiohttp.web_request import Request

    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False


# Default configuration
DEFAULT_PORT = 8080
DEFAULT_HOST = "0.0.0.0"
# MCP standard uses Authorization: Bearer <token> (OAuth 2.1 style)
AUTH_HEADER = "Authorization"


def generate_api_key() -> str:
    """Generate a secure random API key."""
    return secrets.token_urlsafe(32)


def get_auth_token_from_env() -> Optional[str]:
    """Get MCP auth token from environment variable or 1Password.

    Uses MCP_AUTH_TOKEN (standard) or MCP_PROXY_API_KEY (backward compatibility).
    """
    # Try MCP_AUTH_TOKEN first (MCP standard)
    token = os.environ.get("MCP_AUTH_TOKEN")
    if token:
        return token

    # Backward compatibility: try MCP_PROXY_API_KEY
    token = os.environ.get("MCP_PROXY_API_KEY")
    if token:
        return token

    # Try 1Password (if op CLI is available)
    try:
        if os.path.exists("/usr/local/bin/op") or os.path.exists(
            "/opt/homebrew/bin/op"
        ):
            # Try MCP_AUTH_TOKEN first
            result = subprocess.run(
                ["op", "read", "op://Private/Parquet MCP Proxy/MCP_AUTH_TOKEN"],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()

            # Fallback to old field name
            result = subprocess.run(
                ["op", "read", "op://Private/Parquet MCP Proxy/API Key"],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
    except Exception:
        pass

    return None


async def handle_request(
    request: Request, auth_token: str, proxy_url: str
) -> web.Response:
    """Handle HTTP request with MCP standard Bearer token authentication."""
    # Check Authorization header (MCP standard: Authorization: Bearer <token>)
    auth_header = request.headers.get(AUTH_HEADER, "")

    # Extract Bearer token
    if auth_header.startswith("Bearer "):
        provided_token = auth_header[7:]  # Remove "Bearer " prefix
    else:
        provided_token = None

    # Validate token
    if not provided_token or provided_token != auth_token:
        return web.Response(
            text='{"error": "Unauthorized. Missing or invalid authorization token."}',
            status=401,
            content_type="application/json",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Forward request to underlying MCP proxy
    try:
        async with ClientSession() as session:
            # Forward the request
            method = request.method
            url = f"{proxy_url}{request.path_qs}"
            headers = dict(request.headers)
            # Keep Authorization header for MCP protocol compliance

            # Get request body if present
            body = await request.read() if request.can_read_body else None

            async with session.request(
                method=method,
                url=url,
                headers=headers,
                data=body,
            ) as response:
                response_body = await response.read()
                return web.Response(
                    body=response_body,
                    status=response.status,
                    headers=dict(response.headers),
                )
    except ClientError as e:
        return web.Response(
            text=f'{{"error": "Proxy error: {str(e)}"}}',
            status=502,
            content_type="application/json",
        )


async def create_app(auth_token: str, proxy_url: str):
    """Create aiohttp application with MCP standard Bearer token authentication."""
    app = web.Application()

    # Handle all routes
    async def handler(request: Request):
        return await handle_request(request, auth_token, proxy_url)

    app.router.add_route("*", "/{path:.*}", handler)

    return app


def start_stdio_proxy(server_script: str, stdio_port: int) -> subprocess.Popen:
    """Start the stdio MCP proxy in the background."""
    # Start mcp-proxy stdio-to-http on a different port
    cmd = [
        "npx",
        "-y",
        "@modelcontextprotocol/proxy",
        "stdio-to-http",
        "--stdio-command",
        "python3",
        "--stdio-args",
        server_script,
        "--http-port",
        str(stdio_port),
        "--http-host",
        "127.0.0.1",  # Localhost only
    ]

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    # Wait a moment to check if it started successfully
    import time

    time.sleep(2)

    if process.poll() is not None:
        # Process exited, read error
        _, stderr = process.communicate()
        raise RuntimeError(f"MCP proxy failed to start: {stderr.decode()}")

    return process


async def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Authenticated HTTP proxy for Parquet MCP Server"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("MCP_PROXY_PORT", DEFAULT_PORT)),
        help=f"HTTP port (default: {DEFAULT_PORT})",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("MCP_PROXY_HOST", DEFAULT_HOST),
        help=f"HTTP host (default: {DEFAULT_HOST})",
    )
    parser.add_argument(
        "--auth-token",
        default=get_auth_token_from_env(),
        help="MCP auth token for authentication (or set MCP_AUTH_TOKEN env var)",
    )
    parser.add_argument(
        "--api-key",  # Backward compatibility
        help="[DEPRECATED] Use --auth-token or MCP_AUTH_TOKEN instead",
    )
    parser.add_argument(
        "--generate-key", action="store_true", help="Generate a new API key and exit"
    )
    parser.add_argument(
        "--stdio-proxy-port",
        type=int,
        default=8081,
        help="Port for internal stdio proxy (default: 8081)",
    )

    args = parser.parse_args()

    if not AIOHTTP_AVAILABLE:
        print("Error: aiohttp is required")
        print("Install with: pip install aiohttp")
        sys.exit(1)

    # Handle deprecated --api-key argument
    if args.api_key:
        print(
            "Warning: --api-key is deprecated. Use --auth-token or MCP_AUTH_TOKEN instead."
        )
        auth_token = args.api_key
    else:
        auth_token = args.auth_token

    # Generate token if requested
    if args.generate_key:
        token = generate_api_key()
        print(f"Generated MCP auth token: {token}")
        print("\nSave this token securely and use it in client configuration:")
        print(f'  "headers": {{"Authorization": "Bearer {token}"}}')
        print("\nOr set environment variable (MCP standard):")
        print(f'  export MCP_AUTH_TOKEN="{token}"')
        sys.exit(0)

    # Get auth token
    if not auth_token:
        print("Error: MCP auth token is required")
        print("\nOptions:")
        print("  1. Set MCP_AUTH_TOKEN environment variable (MCP standard)")
        print("  2. Use --auth-token argument")
        print(
            "  3. Store in 1Password: 'Parquet MCP Proxy' item, 'MCP_AUTH_TOKEN' field"
        )
        print("  4. Generate new token: --generate-key")
        sys.exit(1)

    # Find parquet server script
    script_dir = Path(__file__).parent
    project_root = script_dir.parent.parent
    server_script = project_root / "mcp" / "parquet" / "parquet_mcp_server.py"

    if not server_script.exists():
        print(f"Error: Parquet MCP server not found at {server_script}")
        sys.exit(1)

    # Start stdio proxy in background
    print(f"Starting stdio MCP proxy on port {args.stdio_proxy_port}...")
    try:
        stdio_proxy_process = start_stdio_proxy(
            str(server_script), args.stdio_proxy_port
        )
        print(f"✓ Stdio proxy started (PID: {stdio_proxy_process.pid})")
    except Exception as e:
        print(f"Error starting stdio proxy: {e}")
        sys.exit(1)

    # Proxy URL for forwarding
    proxy_url = f"http://127.0.0.1:{args.stdio_proxy_port}"

    # Create and start HTTP server
    app = await create_app(auth_token, proxy_url)

    print(f"\n{'='*60}")
    print("Authenticated Parquet MCP Proxy (MCP Standard)")
    print(f"{'='*60}")
    print(f"Listening on: http://{args.host}:{args.port}")
    print(f"Auth Header: {AUTH_HEADER} (Bearer token)")
    print(f"Token: {auth_token[:8]}...{auth_token[-4:]} (hidden)")
    print("\nClient Configuration (MCP Standard):")
    print('  "type": "streamable-http",')
    print(f'  "url": "http://{args.host}:{args.port}",')
    print(f'  "headers": {{"Authorization": "Bearer {auth_token}"}}')
    print("\nOr set environment variable:")
    print(f'  export MCP_AUTH_TOKEN="{auth_token}"')
    print("\nPress Ctrl+C to stop")
    print(f"{'='*60}\n")

    # Cleanup on exit
    def cleanup():
        if stdio_proxy_process.poll() is None:
            stdio_proxy_process.terminate()
            stdio_proxy_process.wait()

    import atexit

    atexit.register(cleanup)

    try:
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, args.host, args.port)
        await site.start()

        # Keep running
        try:
            await asyncio.Event().wait()
        except KeyboardInterrupt:
            print("\nShutting down...")
    finally:
        cleanup()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
