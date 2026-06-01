"""
Helper class for interacting with the 1Password MCP server.

Enables MCP-to-MCP communication for secret resolution.
Eliminates fragile CLI session management by using persistent MCP connection.
"""

import json
import os
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class OnePasswordMCPClient:
    """Client for 1Password MCP server."""

    def __init__(self, server_path: Optional[str] = None):
        """
        Initialize 1Password MCP client.

        Args:
            server_path: Path to onepassword_mcp_server.py. If None, auto-detects.
        """
        self.server_path = server_path or self._detect_onepassword_server()

    def _detect_onepassword_server(self) -> str:
        """Auto-detect 1Password MCP server location."""
        # Try environment variable first
        env_path = os.getenv("ONEPASSWORD_MCP_SERVER_PATH")
        if env_path and Path(env_path).exists():
            return env_path

        # Try common locations relative to this file
        script_dir = Path(__file__).parent
        possible_paths = [
            # From execution/scripts/ to mcp/onepassword/
            script_dir.parent.parent
            / "mcp"
            / "onepassword"
            / "onepassword_mcp_server.py",
            # From scripts/ to mcp/onepassword/ (alternative structure)
            script_dir.parent / "mcp" / "onepassword" / "onepassword_mcp_server.py",
        ]

        for path in possible_paths:
            if path.exists():
                return str(path)

        raise RuntimeError(
            "Could not find 1Password MCP server. "
            "Set ONEPASSWORD_MCP_SERVER_PATH environment variable or ensure it exists at mcp/onepassword/"
        )

    def _get_python_command(self) -> str:
        """Get the Python command to use for running the 1Password server."""
        # Try to find venv Python relative to this file
        script_dir = Path(__file__).parent
        possible_venv_paths = [
            # From execution/scripts/ to execution/venv/
            script_dir.parent / "venv" / "bin" / "python3",
            # Alternative: from scripts/ to execution/venv/
            script_dir.parent / "execution" / "venv" / "bin" / "python3",
        ]

        for venv_python in possible_venv_paths:
            if venv_python.exists():
                return str(venv_python)

        # Fall back to system python3
        return "python3"

    async def _call_tool(
        self, tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        """Call a tool on the 1Password MCP server."""
        # Use venv Python if available, otherwise fall back to system python3
        python_cmd = self._get_python_command()

        # Load .env file from repo root to get environment variables
        env = os.environ.copy()
        script_dir = Path(__file__).parent
        possible_env_files = [
            script_dir.parent.parent / ".env",  # From execution/scripts/ -> repo root
            script_dir.parent / ".env",  # From scripts/ -> repo root
        ]
        for env_file in possible_env_files:
            if env_file.exists():
                load_dotenv(
                    env_file, override=False
                )  # Don't override existing env vars
                # Update env dict with loaded values
                for key, value in os.environ.items():
                    if key not in env:
                        env[key] = value
                break

        try:
            async with stdio_client(
                StdioServerParameters(
                    command=python_cmd, args=[self.server_path], env=env
                )
            ) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()

                    # Call the tool
                    result = await session.call_tool(tool_name, arguments)

                    # Parse TextContent result
                    if result.content and len(result.content) > 0:
                        content_text = result.content[0].text
                        return json.loads(content_text)

                    return {}
        except Exception as e:
            # Return error in consistent format
            return {
                "success": False,
                "error": f"MCP communication error: {str(e)}",
                "error_type": "mcp_error",
            }

    def call_tool_sync(
        self, tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        """Synchronous wrapper for async _call_tool."""
        import asyncio

        return asyncio.run(self._call_tool(tool_name, arguments))

    def read_secret(self, reference: str) -> str:
        """
        Read secret from 1Password via MCP.

        Args:
            reference: 1Password reference (e.g., op://Personal/item/field)

        Returns:
            Secret value as string

        Raises:
            RuntimeError: If read fails or returns empty value
        """
        result = self.call_tool_sync("read_secret", {"reference": reference})

        if not result.get("success", False):
            error_msg = result.get("error", "Unknown error")
            error_type = result.get("error_type", "unknown")
            raise RuntimeError(f"{error_msg} (type: {error_type})")

        value = result.get("value", "")
        if not value:
            raise RuntimeError(f"Empty value returned for reference: {reference}")

        return value

    def check_session(self) -> bool:
        """
        Check if 1Password CLI session is active.

        Returns:
            True if session is active, False otherwise
        """
        try:
            result = self.call_tool_sync("check_session", {})
            return result.get("active", False)
        except Exception:
            return False
