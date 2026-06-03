"""
Helper class for interacting with the parquet MCP server from the Asana MCP server.

This enables MCP-to-MCP communication for all data operations.
"""

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
        server_dir = Path(__file__).parent
        possible_paths = [
            server_dir.parent.parent.parent
            / "truth"
            / "mcp-servers"
            / "parquet"
            / "parquet_mcp_server.py",
            server_dir.parent.parent
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
        server_dir = Path(__file__).parent
        possible_venv_paths = [
            server_dir.parent.parent.parent / "execution" / "venv" / "bin" / "python3",
            server_dir.parent.parent / "execution" / "venv" / "bin" / "python3",
        ]

        for venv_python in possible_venv_paths:
            if venv_python.exists():
                return str(venv_python)

        # Fall back to system python3
        return "python3"

    async def _call_tool(
        self, tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        """Call a tool on the parquet MCP server."""
        # Use venv Python if available, otherwise fall back to system python3
        python_cmd = self._get_python_command()

        # Load .env file from repo root to get DATA_DIR and other env vars
        env = os.environ.copy()
        server_dir = Path(__file__).parent
        possible_env_files = [
            server_dir.parent.parent.parent
            / ".env",  # execution/mcp-servers/asana -> execution -> personal
            server_dir.parent.parent / ".env",
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

        # Support test data directories: if PARQUET_DATA_DIR is set, use it as DATA_DIR
        # This allows tests to use a separate test data directory without affecting production data
        if "PARQUET_DATA_DIR" in env and "DATA_DIR" not in env:
            env["DATA_DIR"] = env["PARQUET_DATA_DIR"]

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

    async def read_tasks(
        self,
        filters: dict | None = None,
        columns: list[str] | None = None,
        limit: int | None = None,
    ) -> list[dict]:
        """Read tasks from parquet via MCP."""
        args = {"data_type": "tasks"}
        if filters:
            args["filters"] = filters
        if columns:
            args["columns"] = columns
        if limit:
            args["limit"] = limit

        result = await self._call_tool("read_parquet", args)
        # Parquet server returns "data" field, not "records"
        return result.get("data", [])

    async def add_task(self, record: dict) -> dict:
        """Add a task to parquet via MCP."""
        result = await self._call_tool(
            "add_record", {"data_type": "tasks", "record": record}
        )
        return result

    async def update_tasks(self, filters: dict, updates: dict) -> dict:
        """Update tasks in parquet via MCP."""
        result = await self._call_tool(
            "update_records",
            {"data_type": "tasks", "filters": filters, "updates": updates},
        )
        return result

    async def upsert_task(self, filters: dict, record: dict) -> dict:
        """Upsert a task in parquet via MCP."""
        result = await self._call_tool(
            "upsert_record",
            {"data_type": "tasks", "filters": filters, "record": record},
        )
        return result

    async def read_comments(self, filters: dict | None = None) -> list[dict]:
        """Read task comments from parquet via MCP."""
        args = {"data_type": "task_comments"}
        if filters:
            args["filters"] = filters

        result = await self._call_tool("read_parquet", args)
        return result.get("records", [])

    async def upsert_comment(self, filters: dict, record: dict) -> dict:
        """Upsert a comment in parquet via MCP."""
        result = await self._call_tool(
            "upsert_record",
            {"data_type": "task_comments", "filters": filters, "record": record},
        )
        return result

    async def read_custom_fields(self, filters: dict | None = None) -> list[dict]:
        """Read task custom fields from parquet via MCP."""
        args = {"data_type": "task_custom_fields"}
        if filters:
            args["filters"] = filters

        result = await self._call_tool("read_parquet", args)
        return result.get("records", [])

    async def upsert_custom_field(self, filters: dict, record: dict) -> dict:
        """Upsert a custom field in parquet via MCP."""
        result = await self._call_tool(
            "upsert_record",
            {"data_type": "task_custom_fields", "filters": filters, "record": record},
        )
        return result

    async def read_dependencies(self, filters: dict | None = None) -> list[dict]:
        """Read task dependencies from parquet via MCP."""
        args = {"data_type": "task_dependencies"}
        if filters:
            args["filters"] = filters

        result = await self._call_tool("read_parquet", args)
        return result.get("records", [])

    async def upsert_dependency(self, filters: dict, record: dict) -> dict:
        """Upsert a dependency in parquet via MCP."""
        result = await self._call_tool(
            "upsert_record",
            {"data_type": "task_dependencies", "filters": filters, "record": record},
        )
        return result

    async def read_stories(self, filters: dict | None = None) -> list[dict]:
        """Read task stories from parquet via MCP."""
        args = {"data_type": "task_stories"}
        if filters:
            args["filters"] = filters

        result = await self._call_tool("read_parquet", args)
        return result.get("records", [])

    async def upsert_story(self, filters: dict, record: dict) -> dict:
        """Upsert a story in parquet via MCP."""
        result = await self._call_tool(
            "upsert_record",
            {"data_type": "task_stories", "filters": filters, "record": record},
        )
        return result

    async def read_attachments(self, filters: dict | None = None) -> list[dict]:
        """Read task attachments from parquet via MCP."""
        args = {"data_type": "task_attachments"}
        if filters:
            args["filters"] = filters

        result = await self._call_tool("read_parquet", args)
        return result.get("records", [])

    async def upsert_attachment(self, filters: dict, record: dict) -> dict:
        """Upsert an attachment in parquet via MCP."""
        result = await self._call_tool(
            "upsert_record",
            {"data_type": "task_attachments", "filters": filters, "record": record},
        )
        return result
