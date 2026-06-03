# MCP Server Setup Guide

## Installation

### 1. Install Dependencies

```bash
cd mcp/parquet
pip install -r requirements.txt
```

If the `mcp` package is not available via pip, install from source:

```bash
pip install git+https://github.com/modelcontextprotocol/python-sdk.git
```

### 2. Verify Installation

```bash
python parquet_mcp_server.py --help
```

Or test the server directly:

```bash
python parquet_mcp_server.py
```

The server should start and wait for stdio input (this is normal for MCP servers).

## Configuration

### Cursor Configuration

1. Open Cursor settings
2. Navigate to MCP settings (or edit `~/.cursor/mcp.json` directly)
3. Add the following configuration:

```json
{
  "mcpServers": {
    "parquet": {
      "command": "python3",
      "args": [
        "$REPO_ROOT/mcp/parquet/parquet_mcp_server.py"
      ],
      "env": {}
    }
  }
}
```

**Note:** Replace `$REPO_ROOT` with the absolute path to your repository root, or use the full path to `parquet_mcp_server.py`.

### Claude Desktop Configuration

1. Locate Claude Desktop config file:
   - **macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`
   - **Windows**: `%APPDATA%\Claude\claude_desktop_config.json`
   - **Linux**: `~/.config/Claude/claude_desktop_config.json`

2. Add the server configuration:

```json
{
  "mcpServers": {
    "parquet": {
      "command": "python3",
      "args": [
        "$REPO_ROOT/mcp/parquet/parquet_mcp_server.py"
      ]
    }
  }
}
```

3. Restart Claude Desktop

## Testing

After configuration, test the server by asking your AI assistant:

- "List all available data types"
- "Get the schema for flows"
- "Read 10 records from flows"
- "Get statistics for transactions"

## Troubleshooting

### Server Not Starting

1. Verify Python path:
   ```bash
   which python3
   ```

2. Test the server script directly:
   ```bash
   python3 /path/to/parquet_mcp_server.py
   ```

3. Check dependencies:
   ```bash
   pip list | grep -E "mcp|pandas|pyarrow"
   ```

### Import Errors

If you see import errors for `mcp`, try:

```bash
pip install --upgrade mcp
```

Or install from GitHub:

```bash
pip install git+https://github.com/modelcontextprotocol/python-sdk.git
```

### Permission Errors

Ensure the script is executable:

```bash
chmod +x parquet_mcp_server.py
```

### Path Issues

Use absolute paths in configuration files, not relative paths.

## Usage Examples

Once configured, you can use the MCP server tools through your AI assistant:

1. **Query data:**
   - "Show me all flows from 2025"
   - "List transactions with category 'Restaurante'"
   - "Get tasks with status 'pending'"

2. **Add records:**
   - "Add a new flow entry for rent payment"
   - "Create a new task"

3. **Update records:**
   - "Update task abc123 to completed status"
   - "Change the amount for flow xyz789"

4. **Get information:**
   - "What data types are available?"
   - "Show me the schema for transactions"
   - "Get statistics for flows"

## Security Notes

- The server has read/write access to all parquet files in `data/`
- All write operations create automatic snapshots
- Be cautious when deleting records - snapshots enable recovery
- The server runs with the permissions of the user running it

