# Asana MCP Server Setup

## Prerequisites

- Python 3.10 or higher
- Asana account with API access
- Parquet MCP server configured and accessible

## Installation Steps

### 1. Install Dependencies

```bash
cd execution/mcp-servers/asana
pip install -r requirements.txt
```

### 2. Configure Authentication

**Option A: Environment Variables (Recommended)**

```bash
export ASANA_SOURCE_PAT="your-source-pat"
export ASANA_TARGET_PAT="your-target-pat"  # Optional, defaults to source
export SOURCE_WORKSPACE_GID="source-workspace-gid"
export TARGET_WORKSPACE_GID="target-workspace-gid"
export FALLBACK_ASSIGNEE_EMAIL="your@email.com"  # Optional
```

**Option B: Config File**

Create `~/.config/asana-mcp/.env`:

```
ASANA_SOURCE_PAT=your-source-pat
ASANA_TARGET_PAT=your-target-pat
SOURCE_WORKSPACE_GID=source-workspace-gid
TARGET_WORKSPACE_GID=target-workspace-gid
FALLBACK_ASSIGNEE_EMAIL=your@email.com
```

**Option C: 1Password Integration**

If using the parent repository structure, configure 1Password item:
- Title: "Asana"
- Fields: `source_pat`, `target_pat`, `source_workspace_gid`, `target_workspace_gid`

### 3. Get Asana Credentials

1. **Personal Access Token (PAT):**
   - Go to https://app.asana.com/0/my-apps
   - Create a new personal access token
   - Copy the token (you'll need one for source and optionally one for target)

2. **Workspace GIDs:**
   - Open Asana in browser
   - Navigate to your workspace
   - Check the URL: `https://app.asana.com/0/{WORKSPACE_GID}/...`
   - Copy the workspace GID from the URL

### 4. Configure Parquet MCP Server

Ensure the parquet MCP server is configured in your MCP settings. The Asana server will auto-detect its location or you can set:

```bash
export PARQUET_MCP_SERVER_PATH="/path/to/parquet_mcp_server.py"
```

### 5. Add to Cursor/Claude Desktop

**Cursor** (`~/.cursor/mcp.json`):
```json
{
  "mcpServers": {
    "asana": {
      "command": "python3",
      "args": [
        "/path/to/asana_mcp_server.py"
      ],
      "env": {
        "ASANA_SOURCE_PAT": "your-source-pat",
        "SOURCE_WORKSPACE_GID": "source-workspace-gid",
        "TARGET_WORKSPACE_GID": "target-workspace-gid"
      }
    }
  }
}
```

**Claude Desktop** (`~/Library/Application Support/Claude/claude_desktop_config.json`):
```json
{
  "mcpServers": {
    "asana": {
      "command": "python3",
      "args": [
        "/path/to/asana_mcp_server.py"
      ],
      "env": {
        "ASANA_SOURCE_PAT": "your-source-pat",
        "SOURCE_WORKSPACE_GID": "source-workspace-gid",
        "TARGET_WORKSPACE_GID": "target-workspace-gid"
      }
    }
  }
}
```

### 6. Test the Server

```bash
cd execution/mcp-servers/asana
python3 test_server.py
```

Expected output:
```
============================================================
Asana MCP Server Tests
============================================================
Testing tool listing...

Found 9 tools:
  - import_asana_tasks: ...
  - export_asana_tasks: ...
  ...

✓ All 9 expected tools are present
✓ All tool schemas are valid

============================================================
All tests passed!
============================================================
```

## Verification

1. **Check Configuration:**
   ```bash
   python3 -c "from config import AsanaConfig; config = AsanaConfig.from_env(); print(f'Source: {config.source_workspace_gid}'); print(f'Target: {config.target_workspace_gid}')"
   ```

2. **Test Server Initialization:**
   ```bash
   python3 -c "from asana_mcp_server import app; print(f'Server: {app.name}')"
   ```

3. **List Tools:**
   ```bash
   python3 test_server.py
   ```

## Next Steps

- Configure webhook server (separate service) for real-time sync
- Set up monitoring scripts for background sync operations
- Review sync logs in parquet MCP server audit log

## Troubleshooting

See README.md for detailed troubleshooting guidance.













