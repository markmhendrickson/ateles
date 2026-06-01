# Sync Direction Determination Logic

## Overview

The sync script uses **timestamp comparison** to determine sync direction for each task. The direction is determined independently for each task by comparing:
- **Local timestamp**: `updated_at` field in local parquet file
- **Asana timestamp**: `modified_at` field from Asana API

## Sync Process Flow

The sync script runs in **three phases**:

### Phase 1: Asana → Local (Remote → Local)

**Location**: `sync_workspace_to_local()` method (lines 865-1086)

**Logic**:
1. Fetch tasks from Asana workspace modified since last sync
2. For each task, compare timestamps:
   ```python
   local_updated = local_task.get('updated_at')
   asana_modified = asana_task.get('modified_at')
   
   # Conflict resolution: most recent wins
   if local_updated and asana_modified:
       if local_updated > asana_modified:
           # Local is newer - skip Asana update, will sync to Asana later
           continue  # Skip this task in Asana → Local phase
   ```
3. **If `asana_modified > local_updated`**: Update local parquet with Asana data
4. **If `local_updated > asana_modified`**: Skip (will be handled in Phase 3)

**Result**: Local parquet is updated with newer Asana data

### Phase 2: Cross-Workspace Sync

**Location**: `ensure_cross_workspace_sync()` method (lines 1088-1151)

**Logic**:
- Creates tasks in target workspace if they only exist in source
- Creates tasks in source workspace if they only exist in target
- No timestamp comparison (ensures tasks exist in both workspaces)

### Phase 3: Local → Asana (Local → Remote)

**Location**: `sync_local_to_workspace()` method (lines 1153-1275)

**Logic**:
1. For each task in local parquet with a workspace GID:
   ```python
   local_updated = row.get('updated_at')
   asana_modified = asana_task.get('modified_at')
   
   # Only sync if local is newer
   if local_updated <= asana_modified:
       continue  # Asana is newer, skip
   ```
2. **If `local_updated > asana_modified`**: Update Asana task with local data
3. **If `local_updated <= asana_modified`**: Skip (already synced in Phase 1)

**Result**: Asana tasks are updated with newer local data

## Key Decision Points

### For Each Task, the Script Determines:

1. **Does task exist in Asana?**
   - If NO → Create in Asana (Phase 3)
   - If YES → Continue to timestamp comparison

2. **Timestamp Comparison**:
   ```
   if local_updated > asana_modified:
       Direction: LOCAL → REMOTE (Phase 3)
   elif asana_modified > local_updated:
       Direction: REMOTE → LOCAL (Phase 1)
   else:
       No sync needed (timestamps match)
   ```

3. **Conflict Resolution**:
   - **Most recent timestamp wins**
   - If timestamps are equal, no sync occurs
   - If one timestamp is missing, the other source is used

## Example Scenarios

### Scenario 1: Local is Newer
- Local `updated_at`: `2025-12-26 10:00:00`
- Asana `modified_at`: `2025-12-26 09:00:00`
- **Result**: LOCAL → REMOTE sync (Phase 3)

### Scenario 2: Asana is Newer
- Local `updated_at`: `2025-12-26 09:00:00`
- Asana `modified_at`: `2025-12-26 10:00:00`
- **Result**: REMOTE → LOCAL sync (Phase 1)

### Scenario 3: Timestamps Match
- Local `updated_at`: `2025-12-26 10:00:00`
- Asana `modified_at`: `2025-12-26 10:00:00`
- **Result**: No sync (skip task)

### Scenario 4: Task Doesn't Exist in Asana
- Local task has no `asana_target_gid`
- **Result**: Create task in Asana (Phase 3)

## Code References

### Asana → Local Direction Check
```python
# Line 927-930
if local_updated and asana_modified:
    if local_updated > asana_modified:
        # Local is newer - skip Asana update, will sync to Asana later
        continue
# If we get here, asana_modified >= local_updated, so sync Asana → Local
```

### Local → Asana Direction Check
```python
# Line 1219-1221
if local_updated <= asana_modified:
    continue  # Asana is newer, skip
# If we get here, local_updated > asana_modified, so sync Local → Asana
```

## Important Notes

1. **Bidirectional Sync**: The script runs both directions in a single sync operation
2. **No Data Loss**: Conflict resolution ensures the most recent data wins
3. **Efficiency**: Tasks are only synced if timestamps differ
4. **Independence**: Each task's sync direction is determined independently
5. **Phase Order**: Asana → Local runs first, then Local → Asana, ensuring conflicts are resolved correctly















