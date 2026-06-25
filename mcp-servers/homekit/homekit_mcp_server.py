#!/usr/bin/env python3
"""
MCP Server for HomeKit Device Control

Provides tools for controlling HomeKit devices via HTTP API (Home Assistant, Homebridge, or native HomeKit).
Supports listing accessories, controlling lights/outlets/switches, and activating scenes.
"""

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import requests
import websockets

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

# Configuration directory (portable, uses user's home directory)
CONFIG_DIR = Path.home() / ".config" / "homekit-mcp"
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
        server_dir.parent.parent,  # mcp/homekit -> mcp -> personal
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
app = Server("homekit")


def load_credentials_from_env() -> dict[str, str | None]:
    """Load HomeKit credentials from environment variables or .env file."""
    credentials = {
        "api_url": os.getenv("HOMEKIT_API_URL"),
        "api_token": os.getenv("HOMEKIT_API_TOKEN"),
        "bridge_type": os.getenv("HOMEKIT_BRIDGE_TYPE", "homeassistant"),
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
                    if line.startswith("HOMEKIT_API_URL="):
                        if not credentials["api_url"]:
                            credentials["api_url"] = (
                                line.split("=", 1)[1].strip().strip("\"'")
                            )
                    elif line.startswith("HOMEKIT_API_TOKEN="):
                        if not credentials["api_token"]:
                            credentials["api_token"] = (
                                line.split("=", 1)[1].strip().strip("\"'")
                            )
                    elif line.startswith("HOMEKIT_BRIDGE_TYPE="):
                        if not credentials["bridge_type"]:
                            credentials["bridge_type"] = (
                                line.split("=", 1)[1].strip().strip("\"'")
                            )
        except Exception:
            pass

    # Priority: environment variables > local .env > config directory .env
    if not credentials["api_url"]:
        # Try local .env file first (for development)
        load_from_file(LOCAL_ENV_FILE)
        # Then try config directory .env (for portable deployment)
        if not credentials["api_url"]:
            load_from_file(ENV_FILE)

    return credentials


def get_credentials_from_1password() -> dict[str, str | None]:
    """Get HomeKit credentials from 1Password."""
    if not HAS_CREDENTIALS_MODULE:
        return {
            "api_url": None,
            "api_token": None,
            "bridge_type": "homeassistant",
        }

    credentials = {}

    # Try multiple possible item titles
    item_titles = ["HomeKit", "Home Assistant", "Homebridge"]

    for title in item_titles:
        try:
            credentials["api_url"] = get_credential(title, field="api_url")
            if credentials["api_url"]:
                # Found the item, get other fields
                try:
                    credentials["api_token"] = get_credential(title, field="api_token")
                except Exception:
                    credentials["api_token"] = None
                try:
                    credentials["bridge_type"] = get_credential(
                        title, field="bridge_type"
                    )
                except Exception:
                    credentials["bridge_type"] = "homeassistant"
                break
        except Exception:
            continue

    if not credentials.get("api_url"):
        credentials = {
            "api_url": None,
            "api_token": None,
            "bridge_type": "homeassistant",
        }

    return credentials


def get_homekit_credentials() -> dict[str, str | None]:
    """Get HomeKit credentials from environment, .env file, or 1Password."""
    # Priority: environment variable > .env file > 1Password
    credentials = load_credentials_from_env()

    if not credentials["api_url"]:
        credentials_1p = get_credentials_from_1password()
        for key, value in credentials_1p.items():
            if not credentials.get(key):
                credentials[key] = value

    return credentials


def make_homekit_request(
    method: str,
    endpoint: str,
    api_url: str,
    api_token: str | None = None,
    data: dict | None = None,
    params: dict | None = None,
) -> dict[str, Any]:
    """
    Make a request to HomeKit HTTP API.

    Args:
        method: HTTP method (GET, POST, PUT, etc.)
        endpoint: API endpoint (e.g., "/accessories")
        api_url: Base API URL
        api_token: Optional API token for authentication
        data: Optional request body data
        params: Optional query parameters

    Returns:
        API response as dictionary

    Raises:
        requests.exceptions.RequestException: On API errors
    """
    url = f"{api_url.rstrip('/')}{endpoint}"
    headers = {"Content-Type": "application/json"}

    if api_token:
        headers["Authorization"] = f"Bearer {api_token}"

    try:
        if method.upper() == "GET":
            response = requests.get(url, headers=headers, params=params, timeout=30)
        elif method.upper() == "POST":
            response = requests.post(
                url, headers=headers, json=data, params=params, timeout=30
            )
        elif method.upper() == "PUT":
            response = requests.put(
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
            f"HomeKit API error: {error_msg}"
        ) from e


async def make_homeassistant_websocket_request(
    api_url: str, api_token: str, message_type: str, data: dict[str, Any]
) -> dict[str, Any]:
    """
    Make a WebSocket request to Home Assistant API.

    Args:
        api_url: Base API URL (e.g., "http://localhost:8123/api")
        api_token: API token for authentication
        message_type: WebSocket message type (e.g., "config/entity_registry/update")
        data: Message data payload

    Returns:
        API response as dictionary

    Raises:
        Exception: On WebSocket or API errors
    """
    # Convert HTTP URL to WebSocket URL
    ws_url = api_url.replace("http://", "ws://").replace("https://", "wss://")
    ws_url = ws_url.replace("/api", "/api/websocket")

    try:
        async with websockets.connect(ws_url) as websocket:
            # Step 1: Receive auth_required message
            auth_required = await websocket.recv()
            auth_required_data = json.loads(auth_required)
            if auth_required_data.get("type") != "auth_required":
                raise ValueError(
                    f"Expected auth_required, got: {auth_required_data.get('type')}"
                )

            # Step 2: Send auth message
            auth_message = {"type": "auth", "access_token": api_token}
            await websocket.send(json.dumps(auth_message))

            # Step 3: Receive auth_ok or auth_invalid
            auth_response = await websocket.recv()
            auth_response_data = json.loads(auth_response)
            if auth_response_data.get("type") == "auth_invalid":
                raise ValueError(
                    f"Authentication failed: {auth_response_data.get('message')}"
                )
            if auth_response_data.get("type") != "auth_ok":
                raise ValueError(
                    f"Expected auth_ok, got: {auth_response_data.get('type')}"
                )

            # Step 4: Send the actual request
            message_id = 1
            request_message = {
                "id": message_id,
                "type": message_type,
                **data,
            }
            await websocket.send(json.dumps(request_message))

            # Step 5: Receive response
            response = await websocket.recv()
            response_data = json.loads(response)

            if response_data.get("id") != message_id:
                raise ValueError(
                    f"Response ID mismatch: expected {message_id}, got {response_data.get('id')}"
                )

            if response_data.get("success") is False:
                error = response_data.get("error", {})
                raise ValueError(
                    f"WebSocket API error: {error.get('code', 'unknown')} - {error.get('message', 'Unknown error')}"
                )

            return response_data.get("result", response_data)

    except websockets.exceptions.WebSocketException as e:
        raise Exception(f"WebSocket connection error: {str(e)}") from e
    except Exception as e:
        raise Exception(f"WebSocket API error: {str(e)}") from e


@app.list_tools()
async def list_tools() -> list[Tool]:
    """List available HomeKit MCP tools."""
    return [
        Tool(
            name="list_accessories",
            description="List all HomeKit accessories/devices",
            inputSchema={
                "type": "object",
                "properties": {
                    "room": {
                        "type": "string",
                        "description": "Filter by room name (optional)",
                    },
                    "category": {
                        "type": "string",
                        "description": "Filter by category (light, outlet, switch, etc.) (optional)",
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="get_accessory_status",
            description="Get current status of a specific HomeKit accessory",
            inputSchema={
                "type": "object",
                "properties": {
                    "accessory_id": {
                        "type": "string",
                        "description": "HomeKit accessory ID or name",
                    },
                },
                "required": ["accessory_id"],
            },
        ),
        Tool(
            name="control_light",
            description="Control a HomeKit light accessory (on/off, brightness, color)",
            inputSchema={
                "type": "object",
                "properties": {
                    "accessory_id": {
                        "type": "string",
                        "description": "HomeKit accessory ID or name",
                    },
                    "on": {
                        "type": "boolean",
                        "description": "Turn light on (true) or off (false) (optional)",
                    },
                    "brightness": {
                        "type": "integer",
                        "description": "Brightness level (0-100) (optional)",
                        "minimum": 0,
                        "maximum": 100,
                    },
                    "color": {
                        "type": "string",
                        "description": "Color in hex format (e.g., '#FF0000' for red) (optional)",
                    },
                },
                "required": ["accessory_id"],
            },
        ),
        Tool(
            name="control_outlet",
            description="Control a HomeKit outlet/plug accessory (on/off)",
            inputSchema={
                "type": "object",
                "properties": {
                    "accessory_id": {
                        "type": "string",
                        "description": "HomeKit accessory ID or name",
                    },
                    "on": {
                        "type": "boolean",
                        "description": "Turn outlet on (true) or off (false)",
                    },
                },
                "required": ["accessory_id", "on"],
            },
        ),
        Tool(
            name="control_switch",
            description="Control a HomeKit switch accessory (on/off)",
            inputSchema={
                "type": "object",
                "properties": {
                    "accessory_id": {
                        "type": "string",
                        "description": "HomeKit accessory ID or name",
                    },
                    "on": {
                        "type": "boolean",
                        "description": "Turn switch on (true) or off (false)",
                    },
                },
                "required": ["accessory_id", "on"],
            },
        ),
        Tool(
            name="activate_scene",
            description="Activate a HomeKit scene",
            inputSchema={
                "type": "object",
                "properties": {
                    "scene_name": {
                        "type": "string",
                        "description": "Name of the scene to activate",
                    },
                },
                "required": ["scene_name"],
            },
        ),
        Tool(
            name="rename_accessory",
            description="Rename a HomeKit accessory/entity in Home Assistant",
            inputSchema={
                "type": "object",
                "properties": {
                    "accessory_id": {
                        "type": "string",
                        "description": "HomeKit accessory ID or entity ID (e.g., 'light.techo_despacho')",
                    },
                    "new_name": {
                        "type": "string",
                        "description": "New friendly name for the accessory",
                    },
                },
                "required": ["accessory_id", "new_name"],
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    """Handle tool calls for HomeKit operations."""
    credentials = get_homekit_credentials()

    if not credentials["api_url"]:
        return [
            TextContent(
                type="text",
                text="Error: HomeKit API URL not configured. Set HOMEKIT_API_URL environment variable or add to ~/.config/homekit-mcp/.env",
            )
        ]

    api_url = credentials["api_url"]
    api_token = credentials["api_token"]
    bridge_type = credentials["bridge_type"]

    try:
        if name == "list_accessories":
            # List all HomeKit accessories
            room = arguments.get("room")
            category = arguments.get("category")

            # Endpoint depends on bridge type
            if bridge_type == "homeassistant":
                endpoint = "/states"  # API URL already includes /api
            elif bridge_type == "homebridge":
                endpoint = "/accessories"
            else:
                endpoint = "/accessories"

            result = make_homekit_request("GET", endpoint, api_url, api_token)

            # Filter results if room or category specified
            if isinstance(result, list):
                accessories = result
                if room:
                    accessories = [
                        acc
                        for acc in accessories
                        if acc.get("attributes", {})
                        .get("friendly_name", "")
                        .lower()
                        .find(room.lower())
                        != -1
                        or acc.get("room", "").lower() == room.lower()
                    ]
                if category:
                    accessories = [
                        acc
                        for acc in accessories
                        if acc.get("entity_id", "").startswith(category.lower())
                        or acc.get("category", "").lower() == category.lower()
                    ]
                result = accessories

            return [
                TextContent(
                    type="text",
                    text=f"Found {len(result) if isinstance(result, list) else 'unknown number of'} accessories:\n\n{json.dumps(result, indent=2)}",
                )
            ]

        elif name == "get_accessory_status":
            accessory_id = arguments["accessory_id"]

            # Endpoint depends on bridge type
            if bridge_type == "homeassistant":
                endpoint = f"/states/{accessory_id}"  # API URL already includes /api
            elif bridge_type == "homebridge":
                endpoint = f"/accessories/{accessory_id}"
            else:
                endpoint = f"/accessories/{accessory_id}"

            result = make_homekit_request("GET", endpoint, api_url, api_token)

            return [
                TextContent(
                    type="text",
                    text=f"Accessory status:\n\n{json.dumps(result, indent=2)}",
                )
            ]

        elif name == "control_light":
            accessory_id = arguments["accessory_id"]
            on = arguments.get("on")
            brightness = arguments.get("brightness")
            color = arguments.get("color")

            # Build service call data
            service_data = {"entity_id": accessory_id}

            if on is not None:
                service = "turn_on" if on else "turn_off"
            else:
                service = "turn_on"

            if brightness is not None:
                service_data["brightness_pct"] = brightness

            if color is not None:
                # Convert hex color to RGB
                color = color.lstrip("#")
                if len(color) == 6:
                    r, g, b = (
                        int(color[0:2], 16),
                        int(color[2:4], 16),
                        int(color[4:6], 16),
                    )
                    service_data["rgb_color"] = [r, g, b]

            # Endpoint depends on bridge type
            if bridge_type == "homeassistant":
                endpoint = f"/services/light/{service}"  # API URL already includes /api
            elif bridge_type == "homebridge":
                endpoint = f"/accessories/{accessory_id}"
            else:
                endpoint = f"/accessories/{accessory_id}"

            result = make_homekit_request(
                "POST", endpoint, api_url, api_token, data=service_data
            )

            return [
                TextContent(
                    type="text",
                    text=f"Light control successful:\n\n{json.dumps(result, indent=2)}",
                )
            ]

        elif name == "control_outlet":
            accessory_id = arguments["accessory_id"]
            on = arguments["on"]

            service = "turn_on" if on else "turn_off"
            service_data = {"entity_id": accessory_id}

            # Endpoint depends on bridge type
            if bridge_type == "homeassistant":
                endpoint = (
                    f"/services/switch/{service}"  # API URL already includes /api
                )
            elif bridge_type == "homebridge":
                endpoint = f"/accessories/{accessory_id}"
            else:
                endpoint = f"/accessories/{accessory_id}"

            result = make_homekit_request(
                "POST", endpoint, api_url, api_token, data=service_data
            )

            return [
                TextContent(
                    type="text",
                    text=f"Outlet control successful:\n\n{json.dumps(result, indent=2)}",
                )
            ]

        elif name == "control_switch":
            accessory_id = arguments["accessory_id"]
            on = arguments["on"]

            service = "turn_on" if on else "turn_off"
            service_data = {"entity_id": accessory_id}

            # Endpoint depends on bridge type
            if bridge_type == "homeassistant":
                endpoint = (
                    f"/services/switch/{service}"  # API URL already includes /api
                )
            elif bridge_type == "homebridge":
                endpoint = f"/accessories/{accessory_id}"
            else:
                endpoint = f"/accessories/{accessory_id}"

            result = make_homekit_request(
                "POST", endpoint, api_url, api_token, data=service_data
            )

            return [
                TextContent(
                    type="text",
                    text=f"Switch control successful:\n\n{json.dumps(result, indent=2)}",
                )
            ]

        elif name == "activate_scene":
            scene_name = arguments["scene_name"]

            # Endpoint depends on bridge type
            if bridge_type == "homeassistant":
                endpoint = "/api/services/scene/turn_on"
                service_data = {
                    "entity_id": f"scene.{scene_name.lower().replace(' ', '_')}"
                }
            elif bridge_type == "homebridge":
                endpoint = f"/scenes/{scene_name}"
            else:
                endpoint = f"/scenes/{scene_name}"

            result = make_homekit_request(
                "POST",
                endpoint,
                api_url,
                api_token,
                data=service_data if bridge_type == "homeassistant" else None,
            )

            return [
                TextContent(
                    type="text",
                    text=f"Scene '{scene_name}' activated successfully:\n\n{json.dumps(result, indent=2)}",
                )
            ]

        elif name == "rename_accessory":
            accessory_id = arguments["accessory_id"]
            new_name = arguments["new_name"]

            # Endpoint depends on bridge type
            if bridge_type == "homeassistant":
                # Home Assistant Entity Registry API is WebSocket-only
                try:
                    # Use WebSocket API to update entity registry
                    result = await make_homeassistant_websocket_request(
                        api_url,
                        api_token,
                        "config/entity_registry/update",
                        {"entity_id": accessory_id, "name": new_name},
                    )

                    return [
                        TextContent(
                            type="text",
                            text=f"Accessory '{accessory_id}' renamed to '{new_name}' successfully:\n\n{json.dumps(result, indent=2)}",
                        )
                    ]
                except Exception as e:
                    # If WebSocket fails, return helpful error message
                    return [
                        TextContent(
                            type="text",
                            text=f"Error: Could not rename accessory via WebSocket API. Error: {str(e)}\n\nTo rename entities manually, use Home Assistant UI: Settings > Devices & Services > Entities > {accessory_id} > Edit\n\nSee ENTITY_REGISTRY_INVESTIGATION.md for details.",
                        )
                    ]
            else:
                return [
                    TextContent(
                        type="text",
                        text="Error: Renaming is only supported for Home Assistant bridge type",
                    )
                ]

        else:
            return [
                TextContent(
                    type="text",
                    text=f"Unknown tool: {name}",
                )
            ]

    except Exception as e:
        return [
            TextContent(
                type="text",
                text=f"Error: {str(e)}",
            )
        ]


async def main():
    """Run the MCP server."""
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
