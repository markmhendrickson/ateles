# Agent MCP Access Policy

**Status:** Active  
**Last Updated:** 2025-01-15  
**Related:** `/shared/docs/agent-context.md`, `/shared/docs/data-imports-policy.md`, `/truth/mcp-servers/parquet/README.md`

---

## Purpose

This policy defines mandatory requirements for AI agents accessing normalized parquet data. All data access must go through the MCP (Model Context Protocol) server to ensure consistency, auditability, and automatic snapshot creation.

## Scope

Applies to all agents reading normalized operator data — mandates MCP-server access (never direct parquet/file reads) and the server-configuration and policy rules below.

---

## MANDATORY: MCP-Only Data Access

**Agents must NEVER access parquet files directly with Python scripts.** Always use the parquet MCP server. If an operation is not supported by MCP, fix or enhance the MCP server to support it.

### Server Configuration

- **Server name:** `parquet`
- **Server code location:** `mcp-servers/parquet/parquet_mcp_server.py`
- **Scope:** All normalized parquet datasets under `/$DATA_DIR/[type]/[type].parquet` (e.g., `flows`, `transactions`, `tasks`, `contacts`, etc.)

### Primary Access Path

Use MCP tools exposed by the `parquet` server:
- Read/query records
- Add/update/delete records
- Statistics and schema discovery
- Semantic search
- Audit log access

### Data-Imports Policy

All existing rules from `/shared/docs/data-imports-policy.md` remain in force:
- Do **not** query or modify `$DATA_DIR/imports/` for routine work
- Use only normalized parquet files as the source of truth

### Automatic Snapshots

The MCP server automatically creates timestamped snapshots in `$DATA_DIR/snapshots/` before any write operation. Do not bypass this by writing directly to parquet files.

---

## Enhancement Requirements

If MCP doesn't support a needed operation:

1. **First:** Attempt to use existing MCP tools in creative ways
2. **If that fails:** Enhance the MCP server to add the missing functionality
3. **Never:** Write direct Python scripts to access parquet files

### Exception Window

Direct script-based parquet access is **ONLY** allowed when:
- Developing or repairing the MCP server itself (`mcp-servers/parquet/parquet_mcp_server.py`)
- Performing one-off maintenance under explicit user instruction (and even then, prefer enhancing MCP)
- Migrating or refactoring the MCP access layer itself

---

## Default Behavior

For **ALL** querying and record-level edits of normalized data, call MCP tools on `parquet`. If MCP lacks functionality, enhance MCP rather than bypassing it.

---

## MCP authentication failures

When any MCP returns an authentication or authorization error (e.g. `invalid_grant`, 401, token expired, or similar), you MUST prompt the user to re-authenticate as needed for that MCP. Do not only suggest manual workarounds (e.g. "send the email yourself"); always explicitly tell the user they may need to re-auth or reconnect the integration so future MCP calls can succeed.

---

## Rationale

- **Consistent snapshot creation** - All modifications automatically create rollback points
- **Audit trails** - All changes tracked through MCP audit logs
- **Centralized data access patterns** - Single point of control for data operations
- **Prevents bypassing** - Ensures established data access layer is always used
- **Maintainability** - Changes to data access patterns happen in one place

---

## Related Documentation

- `/shared/docs/agent-context.md` - Agent context and quick reference (index to all rule documents)
- `/shared/docs/data-imports-policy.md` - Data imports directory policy
- `/truth/mcp-servers/parquet/README.md` - Complete MCP server documentation







