# Asana MCP Server Implementation

**Date:** 2025-12-26  
**Status:** Complete

## Overview

Converted Asana integration scripts to a standardized MCP server following the [MCP Server Development Guide](../../../foundation/docs/mcp-server-development-guide.md). The server uses the parquet MCP server for all data operations, replacing direct parquet file access.

## Architecture

```
Asana MCP Server
├── asana_mcp_server.py      # Main MCP server with 9 tools
├── config.py                 # Configuration (env vars → config file → 1Password)
├── client.py                 # Asana API client wrapper
├── parquet_client.py         # Parquet MCP client for data operations
├── import_engine.py          # Task import logic
├── export_engine.py          # Task export logic
├── sync_engine.py            # Bidirectional sync with three-way merge
├── utils.py                  # Error handling and formatting
├── README.md                 # Comprehensive documentation
├── SETUP.md                  # Setup instructions
└── requirements.txt          # Dependencies
```

## Data Flow

```
Asana MCP Server → Parquet MCP Server → Parquet Files
                                      ↓
                            Automatic Snapshots & Audit Log
```

**No direct parquet file access** - All data operations go through parquet MCP server.

## Tools Implemented

1. **`import_asana_tasks`** - Import tasks from Asana workspace
2. **`export_asana_tasks`** - Export local tasks to Asana
3. **`sync_asana_tasks`** - Bidirectional sync with three-way merge
4. **`import_asana_task_comments`** - Import task comments
5. **`import_asana_task_metadata`** - Import custom fields, dependencies, stories
6. **`register_asana_webhooks`** - Register webhooks for projects
7. **`get_asana_task`** - Get single task from Asana
8. **`list_asana_projects`** - List projects in workspace
9. **`get_asana_workspace_info`** - Get workspace information

## Key Features

### Three-Way Merge Sync

The sync engine uses a three-way merge approach for field-level reconciliation:

- **Last Synced State**: Stores task state after last successful sync
- **Three-Way Comparison**: Compares last_synced vs current_local vs current_asana
- **Merge Logic**:
  - If only Asana changed: Use Asana value
  - If only Local changed: Use Local value
  - If both changed: Use newer timestamp (conflict resolution)

This preserves changes on both sides when different properties are modified.

### Parquet MCP Integration

All data operations use the parquet MCP server:

- **Read**: `read_parquet` tool
- **Add**: `add_record` tool
- **Update**: `update_records` tool
- **Upsert**: `upsert_record` tool

Benefits:
- Automatic snapshots before modifications
- Audit logging for all changes
- Rollback capabilities
- Consistent data access patterns

### Authentication

Standard priority order:
1. Environment variables (highest priority)
2. Config file (`~/.config/asana-mcp/.env`)
3. 1Password integration (optional, backward compatibility)

### Error Handling

Structured error responses following guide patterns:
- Error message, type, and context
- Logged to stderr (not stdout)
- Graceful handling of API errors, timeouts, and MCP errors

## Source Code Extraction

**From `execution/scripts/sync_asana_tasks.py`:**
- Three-way merge logic → `sync_engine.py`
- Task normalization → `import_engine.py`, `sync_engine.py`

**From `execution/scripts/export_asana_tasks.py`:**
- Task export logic → `export_engine.py`
- Duplicate detection → `export_engine.py`

**From `execution/scripts/import_asana_tasks.py`:**
- Task import logic → `import_engine.py`
- Classification functions → `import_engine.py`

**From `execution/scripts/import_asana_task_comments.py`:**
- Comment import → `import_engine.py`

**From `execution/scripts/import_asana_task_metadata.py`:**
- Metadata import → `import_engine.py`

**From `execution/scripts/register_asana_webhooks.py`:**
- Webhook registration → `asana_mcp_server.py`

**From `execution/scripts/config.py` and `execution/scripts/client.py`:**
- Configuration and client wrapper → `config.py`, `client.py`

## Testing

All tests passed:
- ✓ Server initializes successfully
- ✓ All 9 tools are present
- ✓ Tool schemas are valid
- ✓ No syntax or import errors

Test script: `test_server.py`

## What Remains Separate

**Webhook Server** (`execution/scripts/asana_webhook_server.py`):
- Remains as separate background service
- Long-running service that listens on a port
- Processes webhook events asynchronously
- Triggers sync operations when events arrive

**Monitoring Scripts**:
- `monitor_asana_import.py` - Monitors import script
- `monitor_asana_export.py` - Monitors export script
- These remain as separate utilities

## Git Repository

- **Repository:** https://github.com/markmhendrickson/mcp-server-asana.git
- **Submodule:** `execution/mcp-servers/asana/`
- **Initial Commit:** 2025-12-26

## Next Steps

1. Push repository to GitHub
2. Configure in Cursor/Claude Desktop
3. Test with actual Asana workspaces
4. Validate parquet MCP integration in production
5. Update parent repository submodule reference

## Notes

- Server is portable and works standalone
- No hard dependencies on parent repository structure
- All data operations go through parquet MCP server
- Follows standardized patterns from MCP Server Development Guide








