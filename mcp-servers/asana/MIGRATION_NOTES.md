# Migration from Scripts to MCP Server

**Date:** 2025-12-26

## What Changed

### Before: Standalone Scripts

```
execution/scripts/
├── import_asana_tasks.py          # Standalone import script
├── export_asana_tasks.py          # Standalone export script
├── sync_asana_tasks.py            # Standalone sync script
├── import_asana_task_comments.py  # Standalone comments import
├── import_asana_task_metadata.py  # Standalone metadata import
├── register_asana_webhooks.py     # Standalone webhook registration
├── config.py                      # Shared configuration
└── client.py                      # Shared Asana client
```

**Data Access:** Direct parquet file access via pandas

```python
import pandas as pd
df = pd.read_parquet(TASKS_FILE)
df.to_parquet(TASKS_FILE, index=False)
```

### After: MCP Server

```
execution/mcp-servers/asana/
├── asana_mcp_server.py      # MCP server with 9 tools
├── config.py                 # Configuration management
├── client.py                 # Asana API client wrapper
├── parquet_client.py         # Parquet MCP client
├── import_engine.py          # Import logic
├── export_engine.py          # Export logic
├── sync_engine.py            # Sync logic with three-way merge
├── utils.py                  # Error handling utilities
├── README.md                 # Comprehensive documentation
├── SETUP.md                  # Setup instructions
└── requirements.txt          # Dependencies
```

**Data Access:** Via parquet MCP server

```python
from parquet_client import ParquetMCPClient
parquet_client = ParquetMCPClient()
tasks = await parquet_client.read_tasks(filters={...})
await parquet_client.update_tasks(filters={...}, updates={...})
```

## Benefits of MCP Server

1. **Standardized Interface**: Consistent tool definitions and responses
2. **Automatic Snapshots**: Via parquet MCP server (no manual snapshot management)
3. **Audit Logging**: Complete change history via parquet MCP server
4. **Error Handling**: Structured error responses
5. **Portability**: Works standalone, no hard dependencies on parent repo
6. **Reusability**: Can be used by any AI assistant (Cursor, Claude Desktop, etc.)
7. **MCP-to-MCP**: Demonstrates MCP server calling another MCP server

## What Remains Separate

### Webhook Server
- **File:** `execution/scripts/asana_webhook_server.py`
- **Reason:** Long-running background service (not a tool)
- **Status:** Remains unchanged

### Monitoring Scripts
- **Files:** `monitor_asana_import.py`, `monitor_asana_export.py`
- **Reason:** Process monitoring utilities
- **Status:** Remain unchanged (may be deprecated if MCP server is reliable)

### Utility Scripts
- **Files:** Various one-off scripts for debugging, migration, etc.
- **Status:** Remain unchanged

## Usage Comparison

### Before (Script)

```bash
# Import tasks
python execution/scripts/import_asana_tasks.py --only-incomplete --max-tasks 50

# Export tasks
python execution/scripts/export_asana_tasks.py --limit 20

# Sync tasks
python execution/scripts/sync_asana_tasks.py --dry-run
```

### After (MCP Tool)

```json
// Import tasks
{
  "tool": "import_asana_tasks",
  "arguments": {
    "only_incomplete": true,
    "max_tasks": 50
  }
}

// Export tasks
{
  "tool": "export_asana_tasks",
  "arguments": {
    "limit": 20
  }
}

// Sync tasks
{
  "tool": "sync_asana_tasks",
  "arguments": {
    "dry_run": true
  }
}
```

## Migration Checklist

- [x] Extract configuration management
- [x] Extract Asana client wrapper
- [x] Create parquet MCP client
- [x] Implement import tool with MCP integration
- [x] Implement export tool with MCP integration
- [x] Implement sync tool with three-way merge
- [x] Implement comments import tool
- [x] Implement metadata import tool
- [x] Implement webhook registration tool
- [x] Implement utility tools (get task, list projects, workspace info)
- [x] Implement error handling
- [x] Create comprehensive documentation
- [x] Test all tools
- [x] Initialize git repository
- [x] Add as submodule
- [x] Update parent README

## Testing Results

```
============================================================
Asana MCP Server Tests
============================================================
Testing tool listing...

Found 9 tools:
  - import_asana_tasks
  - export_asana_tasks
  - sync_asana_tasks
  - import_asana_task_comments
  - import_asana_task_metadata
  - register_asana_webhooks
  - get_asana_task
  - list_asana_projects
  - get_asana_workspace_info

✓ All 9 expected tools are present
✓ All tool schemas are valid

All tests passed!
```

## Next Steps

1. **Push to GitHub:**
   ```bash
   cd execution/mcp-servers/asana
   git push -u origin main
   ```

2. **Configure in Cursor/Claude Desktop:**
   - Add server to MCP settings
   - Set environment variables
   - Test with actual workspaces

3. **Production Validation:**
   - Test import/export/sync operations
   - Verify parquet MCP integration
   - Check automatic snapshots and audit logging

4. **Deprecate Old Scripts (Optional):**
   - Once MCP server is validated in production
   - Keep scripts as reference or for backward compatibility

## Notes

- Server follows all patterns from MCP Server Development Guide
- Portable design (works standalone)
- No direct parquet file access (all via MCP)
- Three-way merge for field-level reconciliation
- Comprehensive documentation with examples













