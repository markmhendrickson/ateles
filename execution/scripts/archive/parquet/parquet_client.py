"""
Helper class for interacting with the parquet MCP server from Python scripts.

This enables scripts to use MCP for all data operations instead of direct parquet access.
"""

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class ParquetMCPClient:
    """Helper class for interacting with parquet MCP server."""

    def __init__(self, parquet_server_path: str | None = None):
        """
        Initialize Parquet MCP client.

        Args:
            parquet_server_path: Path to parquet_mcp_server.py. If None, auto-detects.
        """
        self.parquet_server_path = parquet_server_path or self._detect_parquet_server()

    def _detect_parquet_server(self) -> str:
        """Auto-detect parquet MCP server location."""
        # Try environment variable first
        env_path = os.getenv("PARQUET_MCP_SERVER_PATH")
        if env_path and Path(env_path).exists():
            return env_path

        # Try common locations relative to this file
        script_dir = Path(__file__).parent
        possible_paths = [
            script_dir.parent.parent
            / "truth"
            / "mcp-servers"
            / "parquet"
            / "parquet_mcp_server.py",
            script_dir.parent.parent.parent
            / "truth"
            / "mcp-servers"
            / "parquet"
            / "parquet_mcp_server.py",
        ]

        for path in possible_paths:
            if path.exists():
                return str(path)

        raise RuntimeError(
            "Could not find parquet MCP server. "
            "Set PARQUET_MCP_SERVER_PATH environment variable or ensure it's at the expected location."
        )

    def _get_python_command(self) -> str:
        """Get the Python command to use for running the parquet server."""
        # Try to find venv Python relative to this file
        script_dir = Path(__file__).parent
        possible_venv_paths = [
            script_dir.parent / "venv" / "bin" / "python3",
            script_dir.parent.parent / "execution" / "venv" / "bin" / "python3",
        ]

        for venv_python in possible_venv_paths:
            if venv_python.exists():
                return str(venv_python)

        # Fall back to system python3
        return os.getenv("PARQUET_MCP_PYTHON", "python3")

    async def _call_tool(
        self, tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        """Call a tool on the parquet MCP server."""
        python_cmd = self._get_python_command()

        # Load .env file from repo root to get DATA_DIR and other env vars
        env = os.environ.copy()
        script_dir = Path(__file__).parent
        possible_env_files = [
            script_dir.parent.parent
            / ".env",  # execution/scripts -> execution -> personal
            script_dir.parent / ".env",
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
                    command=python_cmd, args=[self.parquet_server_path], env=env
                )
            ) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool(tool_name, arguments)
                    # Parse the text content from the result
                    if result.content and len(result.content) > 0:
                        return json.loads(result.content[0].text)
                    return {}
        except Exception as e:
            # Re-raise with more context
            raise RuntimeError(
                f"Failed to call parquet MCP tool '{tool_name}': {e}. "
                f"Python: {python_cmd}, Server: {self.parquet_server_path}"
            ) from e

    def call_tool_sync(
        self, tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        """Synchronous wrapper for calling MCP tools."""
        return asyncio.run(self._call_tool(tool_name, arguments))

    def read_transcriptions(
        self,
        filters: dict | None = None,
        columns: list[str] | None = None,
        limit: int | None = None,
        sort_by: list[dict] | None = None,
    ) -> list[dict]:
        """
        Read transcriptions from parquet via MCP.

        Args:
            filters: Optional filters to apply (e.g., {"audio_file_name": "recording.m4a"})
            columns: Optional list of columns to return
            limit: Optional limit on number of records
            sort_by: Optional sort specifications

        Returns:
            List of transcription records
        """
        args = {"data_type": "transcriptions"}
        if filters:
            args["filters"] = filters
        if columns:
            args["columns"] = columns
        if limit:
            args["limit"] = limit
        if sort_by:
            args["sort_by"] = sort_by

        result = self.call_tool_sync("read_parquet", args)
        return result.get("data", [])

    def add_transcription(self, record: dict) -> dict:
        """
        Add a transcription record to parquet via MCP.

        Args:
            record: Transcription record matching the schema

        Returns:
            Result from MCP server
        """
        result = self.call_tool_sync(
            "add_record", {"data_type": "transcriptions", "record": record}
        )
        return result

    def upsert_transcription(self, filters: dict, record: dict) -> dict:
        """
        Upsert a transcription record (insert or update) via MCP.

        Args:
            filters: Filters to find existing record (e.g., {"audio_file_path": "path/to/file.m4a"})
            record: Transcription record to insert or update

        Returns:
            Result from MCP server indicating whether it was created or updated
        """
        result = self.call_tool_sync(
            "upsert_record",
            {"data_type": "transcriptions", "filters": filters, "record": record},
        )
        return result

    def update_transcriptions(self, filters: dict, updates: dict) -> dict:
        """
        Update transcription records matching filters.

        Args:
            filters: Filters to identify records to update
            updates: Fields to update

        Returns:
            Result from MCP server
        """
        result = self.call_tool_sync(
            "update_records",
            {"data_type": "transcriptions", "filters": filters, "updates": updates},
        )
        return result
