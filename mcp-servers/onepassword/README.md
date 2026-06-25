# 1Password MCP Server

MCP server for 1Password CLI integration, providing tools for reading secrets from 1Password vaults via persistent MCP connection.

## Features

- **Secret Reading**: Read secrets from 1Password by op:// reference
- **Session Checking**: Verify 1Password CLI session status
- **Persistent Connection**: Eliminates fragile session management via MCP
- **Secure**: Never logs or exposes secret values in errors or output

## Benefits Over CLI Direct Usage

**Traditional CLI approach:**
- Session tokens expire mid-script
- Each `op read` is a separate subprocess call
- Fragile session management
- No connection pooling

**MCP approach:**
- Persistent connection (auth once, reuse)
- No session expiration during execution
- Better error handling (structured MCP errors)
- Consistent with existing MCP architecture

## Installation

```bash
cd mcp/onepassword
pip install -r requirements.txt
```

## Prerequisites

**1Password CLI required:**
```bash
# Install 1Password CLI
brew install 1password-cli

# Sign in (creates session)
op signin
```

**Note:** The MCP server uses the 1Password CLI internally, so you must have `op` installed and signed in. However, once the MCP server is running, it maintains a persistent connection, eliminating the need for repeated authentication.

## Configuration

### Cursor Configuration

Add to your Cursor MCP settings (typically `~/.cursor/mcp.json` or Cursor settings):

**Option 1: Using wrapper script (recommended):**
```json
{
  "mcpServers": {
    "onepassword": {
      "command": "bash",
      "args": [
        "/Users/markmhendrickson/repos/ateles/mcp/onepassword/run-onepassword-mcp.sh"
      ],
      "env": {}
    }
  }
}
```

**Option 2: Direct Python invocation:**
```json
{
  "mcpServers": {
    "onepassword": {
      "command": "python3",
      "args": [
        "/Users/markmhendrickson/repos/ateles/mcp/onepassword/onepassword_mcp_server.py"
      ],
      "env": {}
    }
  }
}
```

**Note:** Replace the path with the actual path to your installation.

### Claude Desktop Configuration

Add to `claude_desktop_config.json` (typically `~/Library/Application Support/Claude/claude_desktop_config.json` on macOS):

```json
{
  "mcpServers": {
    "onepassword": {
      "command": "python3",
      "args": [
        "/path/to/onepassword_mcp_server.py"
      ],
      "env": {}
    }
  }
}
```

## Available Tools

### `read_secret`

Read a secret from 1Password by op:// reference.

**Parameters:**
- `reference` (required): 1Password reference in format `op://<vault>/<item>/<field>`

**Returns:**
- `success`: Boolean indicating if operation succeeded
- `value`: The secret value (only if success=true)
- `reference`: The reference that was read
- `error`: Error message (only if success=false)
- `error_type`: Error classification (only if success=false)

**Example Request:**
```json
{
  "reference": "op://Personal/API-Keys/openai_token"
}
```

**Example Success Response:**
```json
{
  "success": true,
  "value": "sk-proj-...",
  "reference": "op://Personal/API-Keys/openai_token"
}
```

**Example Error Response:**
```json
{
  "success": false,
  "error": "1Password CLI error for op://Personal/NonExistent/field. Ensure you're signed in (run: op signin)",
  "error_type": "cli_error",
  "exit_code": 1
}
```

**Error Types:**
- `timeout`: Command took longer than 10 seconds
- `cli_not_found`: 1Password CLI not installed
- `cli_error`: 1Password CLI returned an error (not signed in, invalid reference, etc.)
- `unknown`: Unexpected error

### `check_session`

Check if 1Password CLI session is active.

**Parameters:** None

**Returns:**
- `success`: Boolean indicating if check succeeded
- `active`: Boolean indicating if session is active
- `message`: Human-readable status message
- `error`: Error message (only if success=false)
- `error_type`: Error classification (only if success=false)

**Example Request:**
```json
{}
```

**Example Success Response (Active Session):**
```json
{
  "success": true,
  "active": true,
  "message": "Session is active"
}
```

**Example Success Response (No Session):**
```json
{
  "success": true,
  "active": false,
  "message": "No active session"
}
```

**Example Error Response:**
```json
{
  "success": false,
  "active": false,
  "error": "1Password CLI (op) not found",
  "error_type": "cli_not_found"
}
```

## Security

**Security guarantees:**
- Never logs or prints secret values
- Error messages never include CLI output (could contain secrets)
- Only returns success/failure status and error types
- Timeout protection prevents hanging on authentication prompts

**Best practices:**
- Keep session active by signing in periodically: `op signin`
- Use service account tokens for automation (not yet supported, CLI only)
- Never commit configuration files with references to production secrets

## Usage Examples

### Python Client (MCP-to-MCP)

```python
from execution.scripts.onepassword_client import OnePasswordMCPClient

# Create client (auto-detects server location)
client = OnePasswordMCPClient()

# Read secret
value = client.read_secret("op://Personal/API-Keys/openai_token")
print(f"Successfully read secret (length: {len(value)})")

# Check session
if client.check_session():
    print("1Password session is active")
else:
    print("No active session - sign in with: op signin")
```

### Integration with Environment Sync

The 1Password MCP server is designed to work seamlessly with the environment variable sync script:

```python
# In execution/scripts/op_sync_env_from_1password.py

from execution.scripts.onepassword_client import OnePasswordMCPClient

def op_read(ref: str) -> str:
    """Read secret via MCP (preferred) or CLI (fallback)."""
    try:
        client = OnePasswordMCPClient()
        value = client.read_secret(ref)
        return value
    except Exception:
        # Fallback to CLI
        result = subprocess.run(["op", "read", ref], ...)
        return result.stdout.strip()
```

## Troubleshooting

### "1Password CLI (op) not found"

**Problem:** 1Password CLI is not installed or not in PATH.

**Solution:**
```bash
brew install 1password-cli
```

### "No active session"

**Problem:** Not signed in to 1Password CLI.

**Solution:**
```bash
op signin
```

### "Timeout reading secret"

**Problem:** Command took longer than 10 seconds (may be waiting for authentication).

**Solution:**
- Check if you're signed in: `op whoami`
- Sign in again: `op signin`
- Verify the reference is correct

### "Empty value returned"

**Problem:** The field exists but contains an empty value.

**Solution:**
- Verify the field has a value in 1Password app
- Check the field name spelling in the reference

### Server fails to start

**Problem:** Missing dependencies or Python version mismatch.

**Solution:**
```bash
# Ensure dependencies are installed
cd mcp/onepassword
pip install -r requirements.txt

# Verify Python version (3.9+ recommended)
python3 --version
```

## Development

### Testing the Server

```bash
# Test server starts
cd mcp/onepassword
./run-onepassword-mcp.sh

# Test with echo (server expects JSON-RPC via stdin)
echo '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | ./run-onepassword-mcp.sh
```

### Adding New Tools

To add new 1Password operations:

1. Add tool definition to `list_tools()` function
2. Add handler function (e.g., `async def _new_tool(args: dict)`)
3. Add routing in `call_tool()` function
4. Update this README with tool documentation

## Related Documentation

- `/sync_env_from_1password` command - Uses this MCP server for environment variable sync
- `execution/scripts/op_sync_env_from_1password.py` - Environment sync implementation
- `execution/scripts/onepassword_client.py` - MCP client for Python scripts
- 1Password CLI documentation: https://developer.1password.com/docs/cli

## Architecture

```
Environment Sync Script
    ↓
OnePasswordMCPClient (Python)
    ↓
1Password MCP Server (this)
    ↓
1Password CLI (op)
    ↓
1Password API
```

**Benefits:**
- Single point of authentication
- Persistent MCP connection
- Better error handling
- Consistent with other MCP integrations (Asana, Parquet, etc.)
