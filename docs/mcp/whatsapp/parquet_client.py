"""
Helper class for interacting with the parquet MCP server from the WhatsApp MCP server.

This enables MCP-to-MCP communication for all data operations.
"""

import json
import os
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class ParquetMCPClient:
    """Helper class for interacting with parquet MCP server for WhatsApp messages."""

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
            server_dir.parent.parent
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

    async def _call_tool(
        self, tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        """Call a tool on the parquet MCP server."""
        # Use python3.11 which has mcp package installed
        python_cmd = os.getenv("PARQUET_MCP_PYTHON", "python3.11")
        async with stdio_client(
            StdioServerParameters(
                command=python_cmd,
                args=[self.parquet_server_path],
                env=os.environ.copy(),
            )
        ) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(tool_name, arguments)
                # Parse the text content from the result
                if result.content and len(result.content) > 0:
                    return json.loads(result.content[0].text)
                return {}

    async def read_messages(
        self,
        filters: dict | None = None,
        columns: list[str] | None = None,
        limit: int | None = None,
        sort_by: list[dict] | None = None,
    ) -> list[dict]:
        """
        Read messages from parquet via MCP.

        Args:
            filters: Optional filters to apply (e.g., {"from_number": "+1234567890"})
            columns: Optional list of columns to return
            limit: Optional limit on number of records
            sort_by: Optional sort specifications

        Returns:
            List of message records
        """
        args = {"data_type": "messages"}
        if filters:
            args["filters"] = filters
        if columns:
            args["columns"] = columns
        if limit:
            args["limit"] = limit
        if sort_by:
            args["sort_by"] = sort_by

        result = await self._call_tool("read_parquet", args)
        return result.get("records", [])

    async def add_message(self, record: dict) -> dict:
        """
        Add a message to parquet via MCP.

        Args:
            record: Message record to add

        Returns:
            Result from add_record operation
        """
        result = await self._call_tool(
            "add_record", {"data_type": "messages", "record": record}
        )
        return result

    async def upsert_message(self, filters: dict, record: dict) -> dict:
        """
        Upsert a message in parquet via MCP.

        Args:
            filters: Filters to identify existing message (e.g., {"whatsapp_message_id": "..."})
            record: Message record to insert or update

        Returns:
            Result from upsert_record operation
        """
        result = await self._call_tool(
            "upsert_record",
            {"data_type": "messages", "filters": filters, "record": record},
        )
        return result

    async def query_messages(
        self,
        filters: dict | None = None,
        limit: int | None = None,
        sort_by: list[dict] | None = None,
    ) -> list[dict]:
        """
        Query messages with filters.

        Args:
            filters: Filters to apply (supports enhanced operators like $contains, $fuzzy, etc.)
            limit: Maximum number of messages to return
            sort_by: Sort specifications

        Returns:
            List of matching message records
        """
        return await self.read_messages(filters=filters, limit=limit, sort_by=sort_by)

    async def get_message_by_id(self, whatsapp_message_id: str) -> dict | None:
        """
        Get a specific message by WhatsApp message ID.

        Args:
            whatsapp_message_id: WhatsApp message ID

        Returns:
            Message record or None if not found
        """
        messages = await self.read_messages(
            filters={"whatsapp_message_id": whatsapp_message_id}, limit=1
        )
        return messages[0] if messages else None

    async def get_conversation_messages(
        self, phone_number: str, limit: int | None = None
    ) -> list[dict]:
        """
        Get messages for a specific phone number/conversation.

        Args:
            phone_number: Phone number to filter by
            limit: Optional limit on number of messages

        Returns:
            List of messages for the conversation
        """
        # Query messages where phone_number is either from_number or to_number
        # Note: This may require multiple queries or enhanced filter operators
        messages = await self.read_messages(
            filters={
                "$or": [{"from_number": phone_number}, {"to_number": phone_number}]
            },
            limit=limit,
            sort_by=[{"column": "timestamp", "ascending": False}],
        )
        return messages
