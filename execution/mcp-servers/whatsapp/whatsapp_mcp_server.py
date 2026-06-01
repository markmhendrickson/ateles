#!/usr/bin/env python3
"""
MCP Server for WhatsApp Business Platform API

Provides tools for reading messages, sending messages, and querying conversations,
with automatic persistence to parquet files via the parquet MCP server.
"""

import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import requests
from parquet_client import ParquetMCPClient

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

# WhatsApp Business Platform API configuration
WHATSAPP_API_BASE = "https://graph.facebook.com/v21.0"

# Configuration directory (portable, uses user's home directory)
CONFIG_DIR = Path.home() / ".config" / "whatsapp-mcp"
CONFIG_DIR.mkdir(parents=True, exist_ok=True)
ENV_FILE = CONFIG_DIR / ".env"

# Local .env file (in repo directory, for development)
SERVER_DIR = Path(__file__).parent
LOCAL_ENV_FILE = SERVER_DIR / ".env"

# Optional: Try to import 1Password credential utility if available
HAS_CREDENTIALS_MODULE = False
try:
    server_dir = Path(__file__).parent
    possible_paths = [
        server_dir.parent.parent.parent,  # execution/mcp-servers/whatsapp -> execution -> personal
        server_dir.parent.parent,  # mcp-servers/whatsapp -> mcp-servers -> execution
    ]

    for parent_path in possible_paths:
        credentials_path = parent_path / "execution" / "scripts" / "credentials.py"
        if credentials_path.exists():
            sys.path.insert(0, str(parent_path))
            try:
                from execution.scripts.credentials import get_credential

                HAS_CREDENTIALS_MODULE = True
                break
            except ImportError:
                continue
except Exception:
    pass

# Initialize MCP server
app = Server("whatsapp")

# Initialize parquet client (will be created when needed)
parquet_client: ParquetMCPClient | None = None


def get_parquet_client() -> ParquetMCPClient:
    """Get or create parquet MCP client."""
    global parquet_client
    if parquet_client is None:
        parquet_client = ParquetMCPClient()
    return parquet_client


def load_credentials_from_env() -> dict[str, str | None]:
    """Load WhatsApp credentials from environment variables or .env file."""
    credentials = {
        "access_token": os.getenv("WHATSAPP_ACCESS_TOKEN"),
        "phone_number_id": os.getenv("WHATSAPP_PHONE_NUMBER_ID"),
        "business_account_id": os.getenv("WHATSAPP_BUSINESS_ACCOUNT_ID"),
    }

    # Helper function to load from .env file
    def load_from_file(env_file: Path) -> None:
        if not env_file.exists():
            return
        try:
            with open(env_file) as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if line.startswith("WHATSAPP_ACCESS_TOKEN="):
                        if not credentials["access_token"]:
                            credentials["access_token"] = (
                                line.split("=", 1)[1].strip().strip("\"'")
                            )
                    elif line.startswith("WHATSAPP_PHONE_NUMBER_ID="):
                        if not credentials["phone_number_id"]:
                            credentials["phone_number_id"] = (
                                line.split("=", 1)[1].strip().strip("\"'")
                            )
                    elif line.startswith("WHATSAPP_BUSINESS_ACCOUNT_ID="):
                        if not credentials["business_account_id"]:
                            credentials["business_account_id"] = (
                                line.split("=", 1)[1].strip().strip("\"'")
                            )
        except Exception:
            pass

    # Priority: environment variables > local .env > config directory .env
    if not all(credentials.values()):
        # Try local .env file first (for development)
        load_from_file(LOCAL_ENV_FILE)
        # Then try config directory .env (for portable deployment)
        if not all(credentials.values()):
            load_from_file(ENV_FILE)

    return credentials


def get_credentials_from_1password() -> dict[str, str | None]:
    """Get WhatsApp credentials from 1Password."""
    if not HAS_CREDENTIALS_MODULE:
        return {
            "access_token": None,
            "phone_number_id": None,
            "business_account_id": None,
        }

    credentials = {}
    try:
        credentials["access_token"] = get_credential(
            "WhatsApp Business Platform", field="access token"
        )
    except Exception:
        credentials["access_token"] = None

    try:
        credentials["phone_number_id"] = get_credential(
            "WhatsApp Business Platform", field="phone number id"
        )
    except Exception:
        credentials["phone_number_id"] = None

    try:
        credentials["business_account_id"] = get_credential(
            "WhatsApp Business Platform", field="business account id"
        )
    except Exception:
        credentials["business_account_id"] = None

    return credentials


def get_whatsapp_credentials() -> dict[str, str | None]:
    """Get WhatsApp credentials from environment, .env file, or 1Password."""
    # Priority: environment variable > .env file > 1Password
    credentials = load_credentials_from_env()

    if not credentials["access_token"]:
        credentials_1p = get_credentials_from_1password()
        for key, value in credentials_1p.items():
            if not credentials.get(key):
                credentials[key] = value

    return credentials


def make_whatsapp_request(
    method: str,
    endpoint: str,
    access_token: str,
    data: dict | None = None,
    params: dict | None = None,
) -> dict[str, Any]:
    """
    Make a request to WhatsApp Business Platform API.

    Args:
        method: HTTP method (GET, POST, etc.)
        endpoint: API endpoint (e.g., "/messages")
        access_token: WhatsApp access token
        data: Optional request body data
        params: Optional query parameters

    Returns:
        API response as dictionary

    Raises:
        requests.exceptions.RequestException: On API errors
    """
    url = f"{WHATSAPP_API_BASE}{endpoint}"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    try:
        if method.upper() == "GET":
            response = requests.get(url, headers=headers, params=params, timeout=30)
        elif method.upper() == "POST":
            response = requests.post(
                url, headers=headers, json=data, params=params, timeout=30
            )
        else:
            raise ValueError(f"Unsupported HTTP method: {method}")

        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        error_msg = str(e)
        if hasattr(e, "response") and e.response is not None:
            try:
                error_data = e.response.json()
                error_msg = json.dumps(error_data, indent=2)
            except Exception:
                error_msg = e.response.text
        raise requests.exceptions.RequestException(
            f"WhatsApp API error: {error_msg}"
        ) from e


@app.list_tools()
async def list_tools() -> list[Tool]:
    """List available WhatsApp MCP tools."""
    return [
        Tool(
            name="list_messages",
            description="Retrieve messages from WhatsApp Business Platform API and persist to parquet",
            inputSchema={
                "type": "object",
                "properties": {
                    "phone_number_id": {
                        "type": "string",
                        "description": "Phone number ID (optional, uses configured if not provided)",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of messages to retrieve (default: 50)",
                        "default": 50,
                    },
                    "after": {
                        "type": "string",
                        "description": "Cursor for pagination (optional)",
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="send_message",
            description="Send a text message via WhatsApp Business Platform API",
            inputSchema={
                "type": "object",
                "properties": {
                    "to": {
                        "type": "string",
                        "description": "Recipient phone number (with country code, e.g., +1234567890)",
                    },
                    "message": {
                        "type": "string",
                        "description": "Message text to send",
                    },
                    "phone_number_id": {
                        "type": "string",
                        "description": "Phone number ID to send from (optional, uses configured if not provided)",
                    },
                },
                "required": ["to", "message"],
            },
        ),
        Tool(
            name="get_conversations",
            description="List conversations/contacts from parquet messages",
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of conversations to return (default: 50)",
                        "default": 50,
                    }
                },
                "required": [],
            },
        ),
        Tool(
            name="query_messages",
            description="Query messages from parquet with filters",
            inputSchema={
                "type": "object",
                "properties": {
                    "filters": {
                        "type": "object",
                        "description": 'Filters to apply (e.g., {"from_number": "+1234567890"})',
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of messages to return (default: 100)",
                        "default": 100,
                    },
                    "sort_by": {
                        "type": "array",
                        "description": 'Sort specifications (e.g., [{"column": "timestamp", "ascending": false}])',
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="get_message_context",
            description="Get conversation context around a specific message",
            inputSchema={
                "type": "object",
                "properties": {
                    "message_id": {
                        "type": "string",
                        "description": "WhatsApp message ID",
                    },
                    "context_lines": {
                        "type": "integer",
                        "description": "Number of messages before and after to include (default: 10)",
                        "default": 10,
                    },
                },
                "required": ["message_id"],
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: Any) -> list[TextContent]:
    """Handle tool calls."""
    try:
        if name == "list_messages":
            result = await handle_list_messages(arguments)
        elif name == "send_message":
            result = await handle_send_message(arguments)
        elif name == "get_conversations":
            result = await handle_get_conversations(arguments)
        elif name == "query_messages":
            result = await handle_query_messages(arguments)
        elif name == "get_message_context":
            result = await handle_get_message_context(arguments)
        else:
            raise ValueError(f"Unknown tool: {name}")

        return [TextContent(type="text", text=json.dumps(result, indent=2))]
    except Exception as e:
        error_response = {
            "error": str(e),
            "code": "TOOL_ERROR",
            "details": {"tool": name, "arguments": arguments},
        }
        return [TextContent(type="text", text=json.dumps(error_response, indent=2))]


async def handle_list_messages(arguments: dict[str, Any]) -> dict[str, Any]:
    """Handle list_messages tool call - queries messages from parquet storage.

    Note: WhatsApp Business Platform API doesn't support listing messages directly.
    Messages are received via webhooks and stored in parquet. This function queries
    the stored messages from parquet.
    """
    limit = arguments.get("limit", 50)
    sort_by = [{"column": "timestamp", "ascending": False}]

    # Query messages from parquet storage
    client = get_parquet_client()
    messages = await client.read_messages(limit=limit, sort_by=sort_by)

    return {
        "success": True,
        "messages": messages,
        "count": len(messages),
        "note": "Messages are stored from webhooks. To receive new messages, configure webhooks in Meta App Dashboard.",
    }


async def handle_send_message(arguments: dict[str, Any]) -> dict[str, Any]:
    """Handle send_message tool call."""
    credentials = get_whatsapp_credentials()
    access_token = credentials.get("access_token")
    phone_number_id = arguments.get("phone_number_id") or credentials.get(
        "phone_number_id"
    )

    if not access_token:
        raise ValueError(
            "WhatsApp access token not configured. Set WHATSAPP_ACCESS_TOKEN environment variable."
        )

    if not phone_number_id:
        raise ValueError(
            "Phone number ID not provided and not configured. Set WHATSAPP_PHONE_NUMBER_ID environment variable."
        )

    to = arguments.get("to")
    message = arguments.get("message")

    if not to or not message:
        raise ValueError("Both 'to' and 'message' are required")

    # Build API request
    endpoint = f"/{phone_number_id}/messages"
    data = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": message},
    }

    # Send message via WhatsApp API
    response = make_whatsapp_request("POST", endpoint, access_token, data=data)

    # Persist sent message to parquet
    client = get_parquet_client()
    message_record = {
        "whatsapp_message_id": response.get("messages", [{}])[0].get("id"),
        "whatsapp_phone_number_id": phone_number_id,
        "from_number": phone_number_id,  # Sent from our number
        "to_number": to,
        "message_text": message,
        "timestamp": datetime.utcnow().isoformat(),
        "message_type": "text",
        "status": "sent",
        "raw_data": json.dumps(response),
    }

    try:
        await client.add_message(message_record)
    except Exception as e:
        print(f"Error persisting sent message: {e}", file=sys.stderr)

    return {
        "success": True,
        "message_id": message_record["whatsapp_message_id"],
        "status": "sent",
        "response": response,
    }


async def handle_get_conversations(arguments: dict[str, Any]) -> dict[str, Any]:
    """Handle get_conversations tool call."""
    limit = arguments.get("limit", 50)

    client = get_parquet_client()

    # Get all messages and group by phone number
    messages = await client.read_messages(
        sort_by=[{"column": "timestamp", "ascending": False}],
        limit=1000,  # Get more messages to find unique conversations
    )

    # Group by phone number (either from or to)
    conversations = {}
    for msg in messages:
        from_num = msg.get("from_number")
        to_num = msg.get("to_number")

        # Determine the "other" phone number (not our business number)
        phone_number_id = msg.get("whatsapp_phone_number_id")
        other_number = to_num if from_num == phone_number_id else from_num

        if other_number and other_number not in conversations:
            conversations[other_number] = {
                "phone_number": other_number,
                "last_message": msg.get("message_text"),
                "last_message_timestamp": msg.get("timestamp"),
                "message_count": 1,
            }
        elif other_number:
            conversations[other_number]["message_count"] += 1

    # Convert to list and limit
    conversation_list = list(conversations.values())[:limit]

    return {
        "success": True,
        "conversations": conversation_list,
        "count": len(conversation_list),
    }


async def handle_query_messages(arguments: dict[str, Any]) -> dict[str, Any]:
    """Handle query_messages tool call."""
    filters = arguments.get("filters", {})
    limit = arguments.get("limit", 100)
    sort_by = arguments.get("sort_by")

    client = get_parquet_client()
    messages = await client.query_messages(
        filters=filters, limit=limit, sort_by=sort_by
    )

    return {"success": True, "messages": messages, "count": len(messages)}


async def handle_get_message_context(arguments: dict[str, Any]) -> dict[str, Any]:
    """Handle get_message_context tool call."""
    message_id = arguments.get("message_id")
    context_lines = arguments.get("context_lines", 10)

    if not message_id:
        raise ValueError("message_id is required")

    client = get_parquet_client()

    # Get the target message
    target_message = await client.get_message_by_id(message_id)
    if not target_message:
        raise ValueError(f"Message not found: {message_id}")

    # Get conversation phone number
    from_num = target_message.get("from_number")
    to_num = target_message.get("to_number")
    phone_number_id = target_message.get("whatsapp_phone_number_id")
    other_number = to_num if from_num == phone_number_id else from_num

    # Get all messages in the conversation
    conversation_messages = await client.get_conversation_messages(
        phone_number=other_number, limit=context_lines * 2 + 1
    )

    # Find the target message index
    target_index = None
    for i, msg in enumerate(conversation_messages):
        if msg.get("whatsapp_message_id") == message_id:
            target_index = i
            break

    if target_index is None:
        # Target message not in results, return what we have
        context_messages = conversation_messages[: context_lines * 2 + 1]
    else:
        # Get context around target message
        start = max(0, target_index - context_lines)
        end = min(len(conversation_messages), target_index + context_lines + 1)
        context_messages = conversation_messages[start:end]

    return {
        "success": True,
        "target_message": target_message,
        "context_messages": context_messages,
        "context_count": len(context_messages),
    }


async def main():
    """Run the MCP server."""
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
