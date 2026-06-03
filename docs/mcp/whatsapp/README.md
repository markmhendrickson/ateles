# WhatsApp Business Platform MCP Server

MCP server for WhatsApp Business Platform API interactions, providing tools for reading messages, sending messages, and querying conversations, with automatic persistence to parquet files via the parquet MCP server.

## Features

- **Message Retrieval**: List and retrieve messages from WhatsApp Business Platform API
- **Message Sending**: Send text messages via WhatsApp Business Platform
- **Conversation Management**: List conversations and query messages
- **Message Context**: Get conversation context around specific messages
- **Automatic Persistence**: All messages automatically persisted to parquet files via MCP protocol
- **Parquet Integration**: Uses parquet MCP server for data operations (audit logs, snapshots, validation)

## Installation

**Requirements:**
- Python 3.10 or higher (MCP SDK requires Python 3.10+)

```bash
cd mcp/whatsapp
pip3 install -r requirements.txt
```

**Note:** If you have multiple Python versions, use `python3.10`, `python3.11`, or `python3.12` explicitly:
```bash
python3.11 -m pip install -r requirements.txt
```

## Configuration

### Authentication

The server supports multiple authentication methods (checked in priority order):

1. **Environment Variables** (recommended, highest priority):
   ```bash
   export WHATSAPP_ACCESS_TOKEN="your-access-token-here"
   export WHATSAPP_PHONE_NUMBER_ID="your-phone-number-id"
   export WHATSAPP_BUSINESS_ACCOUNT_ID="your-business-account-id"  # Optional
   ```

2. **Config Directory `.env` File** (portable, user-specific):
   - Location: `~/.config/whatsapp-mcp/.env`
   - Format:
     ```
     WHATSAPP_ACCESS_TOKEN=your-access-token-here
     WHATSAPP_PHONE_NUMBER_ID=your-phone-number-id
     WHATSAPP_BUSINESS_ACCOUNT_ID=your-business-account-id
     ```
   - The config directory is created automatically on first use

3. **1Password Integration** (optional, for backward compatibility):
   - Only available if parent repository structure exists
   - Configure 1Password item titled "WhatsApp Business Platform"
   - Add fields: "access token", "phone number id", "business account id"
   - Get your credentials from: https://developers.facebook.com/apps/

**Note:** The MCP server is self-contained and portable. It does not require any specific repository structure and can be used in any project.

### Getting WhatsApp Business Platform Credentials

1. **Create Meta Business Manager Account**: https://business.facebook.com/
2. **Register WhatsApp Business Account**: In Meta Business Manager, add WhatsApp Business account
3. **Get Phone Number ID**: From WhatsApp Business Platform settings
4. **Generate Access Token**: From Meta App Dashboard > WhatsApp > API Setup

### Cursor Configuration

Add to your Cursor MCP settings (typically `~/.cursor/mcp.json` or Cursor settings):

**Option 1: Using environment variables (recommended):**
```json
{
  "mcpServers": {
    "whatsapp": {
      "command": "python3.11",
      "args": [
        "/path/to/whatsapp_mcp_server.py"
      ],
      "env": {
        "WHATSAPP_ACCESS_TOKEN": "your-access-token-here",
        "WHATSAPP_PHONE_NUMBER_ID": "your-phone-number-id"
      }
    }
  }
}
```

**Option 2: Using config directory (credentials in ~/.config/whatsapp-mcp/.env):**
```json
{
  "mcpServers": {
    "whatsapp": {
      "command": "python3.11",
      "args": [
        "/path/to/whatsapp_mcp_server.py"
      ],
      "env": {}
    }
  }
}
```

**Note:** Use `python3.11` (or `python3.10`/`python3.12`) instead of `python3` to ensure Python 3.10+ is used.

**Note:** Replace `/path/to/whatsapp_mcp_server.py` with the actual path to the server file.

### Claude Desktop Configuration

Add to `claude_desktop_config.json` (typically `~/Library/Application Support/Claude/claude_desktop_config.json` on macOS):

```json
{
  "mcpServers": {
    "whatsapp": {
      "command": "python3.11",
      "args": [
        "/path/to/whatsapp_mcp_server.py"
      ],
      "env": {
        "WHATSAPP_ACCESS_TOKEN": "your-access-token-here",
        "WHATSAPP_PHONE_NUMBER_ID": "your-phone-number-id"
      }
    }
  }
}
```

**Note:** Use `python3.11` (or `python3.10`/`python3.12`) instead of `python3` to ensure Python 3.10+ is used.

**Note:** Replace `/path/to/whatsapp_mcp_server.py` with the actual path to the server file.

## Available Tools

### `list_messages`

Retrieve messages from WhatsApp Business Platform API and automatically persist to parquet.

**Parameters:**
- `phone_number_id` (optional, string): Phone number ID (uses configured if not provided)
- `limit` (optional, integer): Maximum number of messages to retrieve (default: 50)
- `after` (optional, string): Cursor for pagination

**Returns:**
- `success`: Boolean indicating success
- `messages`: Array of message objects from WhatsApp API
- `count`: Number of messages retrieved
- `persisted_count`: Number of messages persisted to parquet
- `paging`: Pagination information

**Example Request:**
```json
{
  "limit": 25
}
```

**Example Response:**
```json
{
  "success": true,
  "messages": [
    {
      "id": "wamid.HBgLM...",
      "from": "+1234567890",
      "to": "+0987654321",
      "timestamp": "2025-12-26T10:30:00Z",
      "type": "text",
      "text": {
        "body": "Hello, this is a test message"
      }
    }
  ],
  "count": 25,
  "persisted_count": 25,
  "paging": {
    "cursors": {
      "after": "MAZDZD"
    }
  }
}
```

**Notes:**
- Messages are automatically persisted to parquet via parquet MCP server
- Uses upsert to avoid duplicates (based on whatsapp_message_id)
- Supports pagination via `after` cursor

### `send_message`

Send a text message via WhatsApp Business Platform API.

**Parameters:**
- `to` (required, string): Recipient phone number (with country code, e.g., "+1234567890")
- `message` (required, string): Message text to send
- `phone_number_id` (optional, string): Phone number ID to send from (uses configured if not provided)

**Returns:**
- `success`: Boolean indicating success
- `message_id`: WhatsApp message ID
- `status`: Message status (e.g., "sent")
- `response`: Full API response

**Example Request:**
```json
{
  "to": "+1234567890",
  "message": "Hello from WhatsApp MCP Server!"
}
```

**Example Response:**
```json
{
  "success": true,
  "message_id": "wamid.HBgLM...",
  "status": "sent",
  "response": {
    "messaging_product": "whatsapp",
    "contacts": [
      {
        "input": "+1234567890",
        "wa_id": "1234567890"
      }
    ],
    "messages": [
      {
        "id": "wamid.HBgLM..."
      }
    ]
  }
}
```

**Notes:**
- Sent messages are automatically persisted to parquet
- Requires valid WhatsApp Business Platform credentials
- Recipient must have opted in to receive messages

### `get_conversations`

List conversations/contacts from parquet messages.

**Parameters:**
- `limit` (optional, integer): Maximum number of conversations to return (default: 50)

**Returns:**
- `success`: Boolean indicating success
- `conversations`: Array of conversation objects
- `count`: Number of conversations

**Example Request:**
```json
{
  "limit": 10
}
```

**Example Response:**
```json
{
  "success": true,
  "conversations": [
    {
      "phone_number": "+1234567890",
      "last_message": "Thank you!",
      "last_message_timestamp": "2025-12-26T10:30:00Z",
      "message_count": 15
    }
  ],
  "count": 10
}
```

**Notes:**
- Queries parquet MCP server for message data
- Groups messages by phone number
- Sorted by most recent message

### `query_messages`

Query messages from parquet with filters.

**Parameters:**
- `filters` (optional, object): Filters to apply (e.g., `{"from_number": "+1234567890"}`)
- `limit` (optional, integer): Maximum number of messages to return (default: 100)
- `sort_by` (optional, array): Sort specifications (e.g., `[{"column": "timestamp", "ascending": false}]`)

**Returns:**
- `success`: Boolean indicating success
- `messages`: Array of message records
- `count`: Number of messages

**Example Request:**
```json
{
  "filters": {
    "from_number": "+1234567890"
  },
  "limit": 50,
  "sort_by": [
    {
      "column": "timestamp",
      "ascending": false
    }
  ]
}
```

**Example Response:**
```json
{
  "success": true,
  "messages": [
    {
      "whatsapp_message_id": "wamid.HBgLM...",
      "from_number": "+1234567890",
      "to_number": "+0987654321",
      "message_text": "Hello!",
      "timestamp": "2025-12-26T10:30:00Z",
      "message_type": "text",
      "status": "delivered"
    }
  ],
  "count": 50
}
```

**Notes:**
- Queries parquet MCP server via MCP protocol
- Supports enhanced filter operators ($contains, $fuzzy, etc.)
- Does not call WhatsApp API (queries local parquet data)

### `get_message_context`

Get conversation context around a specific message.

**Parameters:**
- `message_id` (required, string): WhatsApp message ID
- `context_lines` (optional, integer): Number of messages before and after to include (default: 10)

**Returns:**
- `success`: Boolean indicating success
- `target_message`: The requested message
- `context_messages`: Array of surrounding messages
- `context_count`: Number of context messages

**Example Request:**
```json
{
  "message_id": "wamid.HBgLM...",
  "context_lines": 5
}
```

**Example Response:**
```json
{
  "success": true,
  "target_message": {
    "whatsapp_message_id": "wamid.HBgLM...",
    "from_number": "+1234567890",
    "to_number": "+0987654321",
    "message_text": "Can you help me with this?",
    "timestamp": "2025-12-26T10:30:00Z"
  },
  "context_messages": [
    {
      "whatsapp_message_id": "wamid.HBgLM...",
      "message_text": "Hello!",
      "timestamp": "2025-12-26T10:25:00Z"
    },
    {
      "whatsapp_message_id": "wamid.HBgLM...",
      "message_text": "Hi there!",
      "timestamp": "2025-12-26T10:26:00Z"
    }
  ],
  "context_count": 11
}
```

**Notes:**
- Queries parquet MCP server for conversation history
- Returns messages before and after the target message
- Useful for understanding conversation flow

## Error Handling

All tools return errors in a consistent format:

```json
{
  "error": "Error message describing what went wrong",
  "code": "ERROR_CODE",
  "details": {
    "tool": "tool_name",
    "arguments": {...}
  }
}
```

**Common Error Codes:**
- `TOOL_ERROR`: General tool execution error
- `AUTHENTICATION_ERROR`: Missing or invalid credentials
- `API_ERROR`: WhatsApp API error (includes API response)
- `VALIDATION_ERROR`: Invalid parameters
- `NOT_FOUND`: Resource not found

**Example Error Response:**
```json
{
  "error": "WhatsApp access token not configured. Set WHATSAPP_ACCESS_TOKEN environment variable.",
  "code": "TOOL_ERROR",
  "details": {
    "tool": "list_messages",
    "arguments": {"limit": 50}
  }
}
```

## Security Notes

1. **Credential Storage**:
   - Store credentials in environment variables or config directory `.env` file
   - Never commit credentials to version control
   - Use `.gitignore` to exclude `.env` files

2. **API Token Security**:
   - WhatsApp access tokens should be kept secure
   - Rotate tokens regularly
   - Use environment-specific tokens (development, production)

3. **Data Privacy**:
   - Messages are persisted to parquet files locally
   - Ensure parquet files are properly secured
   - Consider encryption for sensitive message data

4. **Parquet MCP Integration**:
   - All data operations go through parquet MCP server
   - Preserves audit logs and snapshots
   - Never access parquet files directly (bypasses validation)

## Troubleshooting

### Server won't start

**Issue**: Server fails to initialize

**Solutions**:
1. Check Python version (requires Python 3.10+): `python3 --version`
2. Install dependencies: `pip3 install -r requirements.txt` or `python3.11 -m pip install -r requirements.txt`
3. Verify MCP SDK is installed: `pip3 show mcp`
4. If using Python 3.9 or earlier, upgrade to Python 3.10+ or use `python3.11` explicitly

### Authentication errors

**Issue**: "WhatsApp access token not configured"

**Solutions**:
1. Set environment variable: `export WHATSAPP_ACCESS_TOKEN="your-token"`
2. Create config file: `~/.config/whatsapp-mcp/.env`
3. Verify credentials in Meta Business Manager

### Parquet MCP server not found

**Issue**: "Could not find parquet MCP server"

**Solutions**:
1. Set `PARQUET_MCP_SERVER_PATH` environment variable
2. Ensure parquet MCP server is at expected location
3. Check parquet server path in error message

### Messages not persisting

**Issue**: Messages retrieved but not saved to parquet

**Solutions**:
1. Check parquet MCP server is running
2. Verify parquet server has write permissions
3. Check error logs for persistence failures
4. Ensure messages schema exists in parquet data directory

### WhatsApp API errors

**Issue**: API returns error responses

**Solutions**:
1. Verify access token is valid and not expired
2. Check phone number ID is correct
3. Ensure WhatsApp Business account is active
4. Review WhatsApp API documentation for specific error codes
5. Check API rate limits

## Architecture

```
WhatsApp MCP Server
    |
    |-- WhatsApp Business Platform API (read/send messages)
    |
    |-- Parquet MCP Client (via MCP protocol)
         |
         |-- Parquet MCP Server
              |
              |-- messages.parquet (persistent storage)
```

**Data Flow:**
1. WhatsApp MCP Server calls WhatsApp Business Platform API
2. Messages retrieved from API
3. ParquetMCPClient calls parquet MCP server via MCP protocol
4. Parquet MCP server persists messages with audit logs and snapshots
5. Query operations read from parquet via MCP protocol

**Benefits:**
- Proper separation of concerns
- Audit logs for all data operations
- Automatic snapshots before modifications
- Validation and error handling
- Follows repository architecture patterns

## License

This MCP server is part of a personal workflow repository and is provided as-is for reference and use.

## Related Documentation

- WhatsApp Business Platform API: https://developers.facebook.com/docs/whatsapp/cloud-api
- Meta Business Manager: https://business.facebook.com/
- MCP Protocol: https://modelcontextprotocol.io/
- Parquet MCP Server: See `mcp/parquet/README.md`

