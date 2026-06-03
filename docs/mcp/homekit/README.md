# HomeKit MCP Server

MCP server for HomeKit device control via HTTP API, providing tools for listing accessories, controlling lights/outlets/switches, and activating scenes. Works with Home Assistant, Homebridge, or native HomeKit HTTP API.

## Features

- **List Accessories**: Query all HomeKit devices with optional filtering by room or category
- **Get Status**: Retrieve current state of any accessory
- **Control Lights**: Turn lights on/off, adjust brightness, change colors
- **Control Outlets**: Turn outlets/plugs on/off
- **Control Switches**: Turn switches on/off
- **Activate Scenes**: Trigger HomeKit scenes
- **Multiple Bridge Support**: Works with Home Assistant, Homebridge, or native HomeKit HTTP API
- **Flexible Authentication**: Environment variables, config directory `.env` files, or 1Password integration

## Installation

```bash
cd mcp/homekit
pip install -r requirements.txt
```

## Configuration

### Authentication

The server supports multiple authentication methods (checked in priority order):

1. **Environment Variables** (recommended, highest priority):
   ```bash
   export HOMEKIT_API_URL="http://homeassistant.local:8123/api"
   export HOMEKIT_API_TOKEN="your-long-lived-access-token"
   export HOMEKIT_BRIDGE_TYPE="homeassistant"  # or "homebridge" or "native"
   ```

2. **Config Directory `.env` File** (portable, user-specific):
   - Location: `~/.config/homekit-mcp/.env`
   - Format:
     ```
     HOMEKIT_API_URL=http://homeassistant.local:8123/api
     HOMEKIT_API_TOKEN=your-long-lived-access-token
     HOMEKIT_BRIDGE_TYPE=homeassistant
     ```
   - The config directory is created automatically on first use

3. **1Password Integration** (optional, for backward compatibility):
   - Only available if parent repository structure exists
   - Configure 1Password item titled "HomeKit", "Home Assistant", or "Homebridge"
   - Add fields: `api_url`, `api_token`, `bridge_type`

### Getting Credentials

#### Home Assistant (Recommended)

1. **Access Home Assistant**: Navigate to your Home Assistant instance (e.g., `http://homeassistant.local:8123`)
2. **Create Long-Lived Access Token**:
   - Go to your profile (click your name in the sidebar)
   - Scroll down to "Long-Lived Access Tokens"
   - Click "Create Token"
   - Give it a name (e.g., "HomeKit MCP Server")
   - Copy the token (you won't be able to see it again)
3. **Set Environment Variables**:
   ```bash
   export HOMEKIT_API_URL="http://homeassistant.local:8123/api"
   export HOMEKIT_API_TOKEN="your-token-here"
   export HOMEKIT_BRIDGE_TYPE="homeassistant"
   ```

#### Homebridge

1. **Enable Homebridge API**: Configure API access in Homebridge settings
2. **Get API Credentials**: Obtain API URL and token from Homebridge
3. **Set Environment Variables**:
   ```bash
   export HOMEKIT_API_URL="http://localhost:51826"
   export HOMEKIT_API_TOKEN="your-token-here"
   export HOMEKIT_BRIDGE_TYPE="homebridge"
   ```

#### Native HomeKit HTTP

For direct HomeKit HTTP API access (advanced users):
```bash
export HOMEKIT_API_URL="http://localhost:51827"
export HOMEKIT_BRIDGE_TYPE="native"
```

### Cursor Configuration

Add to your Cursor MCP settings (typically `~/.cursor/mcp.json` or Cursor settings):

```json
{
  "mcpServers": {
    "homekit": {
      "command": "python3",
      "args": [
        "/Users/markmhendrickson/repos/ateles/mcp/homekit/homekit_mcp_server.py"
      ],
      "env": {
        "HOMEKIT_API_URL": "http://homeassistant.local:8123/api",
        "HOMEKIT_API_TOKEN": "your-token-here",
        "HOMEKIT_BRIDGE_TYPE": "homeassistant"
      }
    }
  }
}
```

Or use config directory (credentials in `~/.config/homekit-mcp/.env`):

```json
{
  "mcpServers": {
    "homekit": {
      "command": "python3",
      "args": [
        "/Users/markmhendrickson/repos/ateles/mcp/homekit/homekit_mcp_server.py"
      ],
      "env": {}
    }
  }
}
```

### Claude Desktop Configuration

Add to `claude_desktop_config.json` (typically `~/Library/Application Support/Claude/claude_desktop_config.json` on macOS):

```json
{
  "mcpServers": {
    "homekit": {
      "command": "python3",
      "args": [
        "/Users/markmhendrickson/repos/ateles/mcp/homekit/homekit_mcp_server.py"
      ],
      "env": {
        "HOMEKIT_API_URL": "http://homeassistant.local:8123/api",
        "HOMEKIT_API_TOKEN": "your-token-here",
        "HOMEKIT_BRIDGE_TYPE": "homeassistant"
      }
    }
  }
}
```

## Tool Documentation

### `list_accessories`

List all HomeKit accessories/devices.

**Parameters:**
- `room` (string, optional): Filter by room name
- `category` (string, optional): Filter by category (light, outlet, switch, etc.)

**Example Request:**
```json
{
  "name": "list_accessories",
  "arguments": {
    "room": "Living Room",
    "category": "light"
  }
}
```

**Example Response:**
```json
{
  "accessories": [
    {
      "entity_id": "light.living_room_ceiling",
      "state": "on",
      "attributes": {
        "friendly_name": "Living Room Ceiling Light",
        "brightness": 255,
        "supported_features": 43
      }
    }
  ]
}
```

### `get_accessory_status`

Get current status of a specific HomeKit accessory.

**Parameters:**
- `accessory_id` (string, required): HomeKit accessory ID or entity ID

**Example Request:**
```json
{
  "name": "get_accessory_status",
  "arguments": {
    "accessory_id": "light.living_room_ceiling"
  }
}
```

**Example Response:**
```json
{
  "entity_id": "light.living_room_ceiling",
  "state": "on",
  "attributes": {
    "brightness": 255,
    "color_temp": 370,
    "friendly_name": "Living Room Ceiling Light"
  }
}
```

### `control_light`

Control a HomeKit light accessory.

**Parameters:**
- `accessory_id` (string, required): HomeKit accessory ID or entity ID
- `on` (boolean, optional): Turn light on (true) or off (false)
- `brightness` (integer, optional): Brightness level (0-100)
- `color` (string, optional): Color in hex format (e.g., "#FF0000" for red)

**Example Request:**
```json
{
  "name": "control_light",
  "arguments": {
    "accessory_id": "light.living_room_ceiling",
    "on": true,
    "brightness": 75,
    "color": "#FF5733"
  }
}
```

**Example Response:**
```json
{
  "success": true,
  "state": "on",
  "brightness": 75
}
```

### `control_outlet`

Control a HomeKit outlet/plug accessory.

**Parameters:**
- `accessory_id` (string, required): HomeKit accessory ID or entity ID
- `on` (boolean, required): Turn outlet on (true) or off (false)

**Example Request:**
```json
{
  "name": "control_outlet",
  "arguments": {
    "accessory_id": "switch.bedroom_lamp",
    "on": true
  }
}
```

**Example Response:**
```json
{
  "success": true,
  "state": "on"
}
```

### `control_switch`

Control a HomeKit switch accessory.

**Parameters:**
- `accessory_id` (string, required): HomeKit accessory ID or entity ID
- `on` (boolean, required): Turn switch on (true) or off (false)

**Example Request:**
```json
{
  "name": "control_switch",
  "arguments": {
    "accessory_id": "switch.kitchen_lights",
    "on": false
  }
}
```

**Example Response:**
```json
{
  "success": true,
  "state": "off"
}
```

### `activate_scene`

Activate a HomeKit scene.

**Parameters:**
- `scene_name` (string, required): Name of the scene to activate

**Example Request:**
```json
{
  "name": "activate_scene",
  "arguments": {
    "scene_name": "Good Night"
  }
}
```

**Example Response:**
```json
{
  "success": true,
  "scene": "Good Night"
}
```

## Error Handling

### Common Errors

1. **API URL Not Configured**:
   ```
   Error: HomeKit API URL not configured
   ```
   Solution: Set `HOMEKIT_API_URL` environment variable or add to `~/.config/homekit-mcp/.env`

2. **Authentication Failed**:
   ```
   Error: 401 Unauthorized
   ```
   Solution: Check that your API token is valid and has proper permissions

3. **Accessory Not Found**:
   ```
   Error: 404 Not Found
   ```
   Solution: Verify the accessory ID/entity ID is correct using `list_accessories`

4. **Connection Refused**:
   ```
   Error: Connection refused
   ```
   Solution: Verify Home Assistant/Homebridge is running and accessible at the configured URL

### Troubleshooting

1. **Test API Connection**:
   ```bash
   curl -H "Authorization: Bearer YOUR_TOKEN" http://homeassistant.local:8123/api/states
   ```

2. **Check Logs**: Look for error messages in the MCP server output

3. **Verify Credentials**: Ensure environment variables are set correctly:
   ```bash
   echo $HOMEKIT_API_URL
   echo $HOMEKIT_API_TOKEN
   echo $HOMEKIT_BRIDGE_TYPE
   ```

4. **Test with Simple Command**: Try listing accessories first to verify connectivity

## Security Notes

- **API Token Security**: Store API tokens securely using environment variables or 1Password
- **Network Security**: Use HTTPS for remote access to Home Assistant/Homebridge
- **Access Control**: Create separate API tokens with minimal required permissions
- **Token Rotation**: Regularly rotate long-lived access tokens
- **Local Network**: For best security, run on the same network as your Home Assistant/Homebridge instance

## Bridge-Specific Notes

### Home Assistant

- **Entity IDs**: Use format `domain.object_id` (e.g., `light.living_room_ceiling`)
- **Services**: Calls Home Assistant services (e.g., `light.turn_on`, `switch.turn_off`)
- **API Documentation**: https://developers.home-assistant.io/docs/api/rest/

### Homebridge

- **API Plugin Required**: Install Homebridge Config UI X for API access
- **Authentication**: Use Homebridge API token
- **Accessory IDs**: May differ from Home Assistant entity IDs

### Native HomeKit HTTP

- **Advanced Setup**: Requires HomeKit HTTP API server
- **Direct Protocol**: Communicates directly with HomeKit Accessory Protocol
- **Limited Support**: May require additional configuration

## Requirements

- Python 3.10 or higher
- `mcp>=1.0.0`
- `requests>=2.31.0`
- `python-dotenv>=1.0.0`
- Home Assistant, Homebridge, or native HomeKit HTTP API setup

## Related Documentation

- Home Assistant REST API: https://developers.home-assistant.io/docs/api/rest/
- Homebridge API: https://github.com/homebridge/homebridge-config-ui-x
- HomeKit Accessory Protocol: https://developer.apple.com/documentation/homekit

