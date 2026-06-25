# Audit Log Guide

## Overview

The MCP Parquet Server now uses an efficient **audit log** approach for tracking data changes, replacing the previous full-snapshot-on-every-write strategy.

## Key Changes

### Before (Full Snapshots)
- Every add/update/delete operation created a complete copy of the parquet file
- 10 MB file × 100 operations = 1 GB storage
- No detailed change history
- Difficult to track what changed

### After (Audit Log + Optional Snapshots)
- Every operation creates a lightweight audit log entry (~1 KB)
- 1 KB × 100 operations = ~100 KB storage (99%+ reduction)
- Complete audit trail with old/new values
- Easy rollback with inverse operations
- Optional periodic full snapshots for safety

## Storage Efficiency

| Dataset Size | Operations | Full Snapshots | Audit Log | Savings |
|--------------|------------|----------------|-----------|---------|
| 10 MB | 100 | 1 GB | ~100 KB | 99.99% |
| 10 MB | 1,000 | 10 GB | ~1 MB | 99.99% |
| 100 MB | 100 | 10 GB | ~100 KB | 99.999% |

## Audit Log Schema

Location: `data/logs/audit_log.parquet`

Fields:
- `audit_id`: Unique identifier for each operation
- `timestamp`: When the operation occurred
- `operation`: Type (add, update, delete)
- `data_type`: Which parquet file was modified
- `record_id`: Identifier of affected record
- `affected_fields`: JSON array of modified field names
- `old_values`: JSON object with field:value pairs before change
- `new_values`: JSON object with field:value pairs after change
- `user`: Who performed the operation
- `snapshot_reference`: Link to periodic snapshot (if created)
- `notes`: Additional context

## Configuration

### Automatic Snapshots

**All write operations automatically create timestamped snapshots before modification.** No configuration is required - snapshots are always created for:
- `add_record` operations
- `update_records` operations
- `upsert_record` operations
- `delete_records` operations

**Snapshot Location:**
```
data/snapshots/[data_type]-[YYYY-MM-DD-HHMMSS].parquet
```

**Note:** The `MCP_FULL_SNAPSHOTS` and `MCP_SNAPSHOT_FREQUENCY` environment variables are no longer used - snapshots are always created automatically.

## New Tools

### 1. `read_audit_log`

View audit log entries with optional filters.

**Parameters:**
- `data_type` (optional): Filter by data type
- `operation` (optional): Filter by operation (add/update/delete)
- `record_id` (optional): Filter by specific record
- `limit` (default: 100): Maximum entries to return

**Example:**
```json
{
  "data_type": "beliefs",
  "operation": "update",
  "limit": 50
}
```

### 2. `rollback_operation`

Rollback a specific operation using its audit ID.

**Parameters:**
- `audit_id` (required): The audit ID to rollback

**Rollback Logic:**
- `add` → Delete the record
- `update` → Restore old values
- `delete` → Restore the record

**Example:**
```json
{
  "audit_id": "abc123def456"
}
```

## Usage Examples

### Track Changes to a Record

```python
# 1. Read audit log for specific record
mcp_parquet_read_audit_log(
    data_type="transactions",
    record_id="txn_123"
)

# Shows complete history:
# - When added
# - All updates with old/new values
# - If/when deleted
```

### Rollback Recent Change

```python
# 1. Find recent operations
mcp_parquet_read_audit_log(
    data_type="fixed_costs",
    limit=10
)

# 2. Rollback specific operation
mcp_parquet_rollback_operation(
    audit_id="abc123def456"
)
```

### Audit Trail Analysis

```python
# View all delete operations across all data types
mcp_parquet_read_audit_log(
    operation="delete",
    limit=100
)

# Find all changes in last period (filter client-side by timestamp)
```

## Benefits

1. **Storage Efficiency**: 99%+ reduction in backup storage
2. **Audit Trail**: Complete history of who changed what when
3. **Granular Rollback**: Undo specific operations without full restore
4. **Compliance**: Financial data audit requirements
5. **Analysis**: Track patterns in data modifications
6. **Performance**: Faster operations (no full file copy)

## Backup Strategy

### Audit Log Only (Default)
- Best for: Development, frequent changes, storage-constrained environments
- Recovery: Rollback operations via audit log
- Risk: Cannot recover from audit log corruption

### Hybrid (Recommended)
- Best for: Production, critical data
- Full snapshots: Weekly (configurable)
- Audit log: Every operation
- Recovery: Restore from snapshot + replay audit log since snapshot
- Risk: Minimal (multiple recovery paths)

## Migration from Old Approach

Existing snapshots in `data/snapshots/` are preserved and remain accessible. The new audit log system works alongside them.

**No action required** - the system automatically starts using audit log for new operations.

## Monitoring

Check audit log size:
```bash
ls -lh data/logs/audit_log.parquet
```

View recent operations:
```python
mcp_parquet_read_audit_log(limit=20)
```

Count operations by type:
```python
df = pd.read_parquet("data/logs/audit_log.parquet")
print(df["operation"].value_counts())
```

## Troubleshooting

### Audit Log Not Created

Check:
1. `data/logs/` directory exists and is writable
2. Python has permissions to write files
3. No schema conflicts with `audit_log_schema.json`

### Rollback Fails

Common causes:
1. Record ID changed after original operation
2. `old_values` missing from audit entry (shouldn't happen with new system)
3. Data type schema changed since operation

### Large Audit Log File

The audit log grows over time. To archive:
```bash
# Move to archive
mv data/logs/audit_log.parquet data/logs/audit_log_2025_archive.parquet

# System will create fresh audit log on next operation
```

## Future Enhancements

Potential improvements:
- Automatic audit log rotation/archiving
- Batch rollback (multiple operations)
- Audit log compression
- Change replay for testing
- Diff visualization tools








