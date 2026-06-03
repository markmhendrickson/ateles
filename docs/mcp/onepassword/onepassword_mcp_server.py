#!/usr/bin/env python3
"""
MCP Server for 1Password Integration

Provides tools for reading secrets from 1Password vaults via CLI.
Eliminates fragile session management by maintaining persistent MCP connection.
"""

import json
import subprocess
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

# Initialize MCP server
app = Server("onepassword")


@app.list_tools()
async def list_tools() -> list[Tool]:
    """List available 1Password tools."""
    return [
        Tool(
            name="read_secret",
            description=(
                "Read a secret from 1Password by op:// reference. "
                "Reference format: op://<vault>/<item>/<field>"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "reference": {
                        "type": "string",
                        "description": "1Password reference (e.g., op://Personal/API-Keys/token)",
                    }
                },
                "required": ["reference"],
            },
        ),
        Tool(
            name="check_session",
            description=(
                "Check if 1Password CLI session is active. "
                "Returns session status without exposing sensitive information."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: Any) -> list[TextContent]:
    """Route tool calls to appropriate handlers."""
    if name == "read_secret":
        return await _read_secret(arguments)
    elif name == "check_session":
        return await _check_session()

    raise ValueError(f"Unknown tool: {name}")


async def _read_secret(args: dict) -> list[TextContent]:
    """
    Read secret from 1Password via op CLI.

    Security: Never logs or exposes secret values, only references and error types.
    """
    reference = args.get("reference", "")

    if not reference:
        error_result = {
            "success": False,
            "error": "Missing required parameter: reference",
        }
        return [TextContent(type="text", text=json.dumps(error_result))]

    try:
        # Use op read command
        result = subprocess.run(
            ["op", "read", reference],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )

        value = result.stdout.rstrip("\n")
        if not value:
            error_result = {
                "success": False,
                "error": f"Empty value returned for reference: {reference}",
            }
            return [TextContent(type="text", text=json.dumps(error_result))]

        # Return success with value
        success_result = {"success": True, "value": value, "reference": reference}
        return [TextContent(type="text", text=json.dumps(success_result))]

    except subprocess.TimeoutExpired:
        error_result = {
            "success": False,
            "error": f"Timeout reading {reference}. Command took longer than 10 seconds.",
            "error_type": "timeout",
        }
        return [TextContent(type="text", text=json.dumps(error_result))]

    except FileNotFoundError:
        error_result = {
            "success": False,
            "error": "1Password CLI (op) not found. Install with: brew install 1password-cli",
            "error_type": "cli_not_found",
        }
        return [TextContent(type="text", text=json.dumps(error_result))]

    except subprocess.CalledProcessError as e:
        # SECURITY: Never include stderr/stdout in error message as they might contain sensitive info
        error_result = {
            "success": False,
            "error": f"1Password CLI error for {reference}. Ensure you're signed in (run: op signin)",
            "error_type": "cli_error",
            "exit_code": e.returncode,
        }
        return [TextContent(type="text", text=json.dumps(error_result))]

    except Exception as e:
        error_result = {
            "success": False,
            "error": f"Unexpected error reading {reference}: {str(e)}",
            "error_type": "unknown",
        }
        return [TextContent(type="text", text=json.dumps(error_result))]


async def _check_session() -> list[TextContent]:
    """
    Check if 1Password CLI session is active.

    Security: Never prints any output from 'op whoami' to avoid exposing tokens.
    """
    try:
        result = subprocess.run(
            ["op", "whoami"], capture_output=True, text=True, timeout=5
        )

        active = result.returncode == 0
        session_result = {
            "success": True,
            "active": active,
            "message": "Session is active" if active else "No active session",
        }
        return [TextContent(type="text", text=json.dumps(session_result))]

    except subprocess.TimeoutExpired:
        error_result = {
            "success": False,
            "active": False,
            "error": "Timeout checking session status",
            "error_type": "timeout",
        }
        return [TextContent(type="text", text=json.dumps(error_result))]

    except FileNotFoundError:
        error_result = {
            "success": False,
            "active": False,
            "error": "1Password CLI (op) not found",
            "error_type": "cli_not_found",
        }
        return [TextContent(type="text", text=json.dumps(error_result))]

    except Exception as e:
        error_result = {
            "success": False,
            "active": False,
            "error": f"Error checking session: {str(e)}",
            "error_type": "unknown",
        }
        return [TextContent(type="text", text=json.dumps(error_result))]


# Main entry point
async def main():
    """Run the MCP server."""
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
