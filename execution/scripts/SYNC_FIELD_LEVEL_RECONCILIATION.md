# Field-Level Reconciliation Analysis

## Current Behavior

The sync script uses **all-or-nothing** conflict resolution based on timestamps:

- **If Asana is newer**: Replace ALL local properties with ALL Asana properties
- **If Local is newer**: Replace ALL Asana properties with ALL local properties
- **No field-level merging**: Individual property changes are not preserved

## Problem Scenario

**Example:**
- Local task: `due_date` changed to 2026-01-01, `description` unchanged
- Asana task: `description` changed to "new text", `due_date` unchanged  
- Local `updated_at`: 2025-12-26 10:00:00
- Asana `modified_at`: 2025-12-26 11:00:00

**Current Result:**
- Asana is newer → Local gets Asana's `description` AND Asana's `due_date`
- **Problem**: Local `due_date` change is lost

**Desired Result:**
- Local gets Asana's `description` (newer)
- Local keeps local `due_date` (local change preserved)
- Both changes reconciled

## Implementation Options

### Option 1: Field-Level Timestamp Tracking

Track `updated_at` per property:
- Store `property_updated_at` for each field (e.g., `due_date_updated_at`, `description_updated_at`)
- Compare per-field timestamps to determine which source to use
- Merge properties from both sources based on individual timestamps

**Pros:**
- Preserves all changes
- Most accurate reconciliation

**Cons:**
- Requires schema changes (add per-field timestamp columns)
- More complex logic
- Asana API doesn't provide per-field timestamps

### Option 2: Three-Way Merge with Last Known Good State

Store a "last synced" state for each task:
- Compare: `last_synced_state` vs `current_local` vs `current_asana`
- For each property:
  - If `local == last_synced` and `asana != last_synced`: Use Asana (Asana changed)
  - If `asana == last_synced` and `local != last_synced`: Use Local (Local changed)
  - If both changed: Use newer timestamp (current behavior)
  - If neither changed: Use either (they match)

**Pros:**
- No schema changes needed
- Preserves most changes
- Works with existing timestamp infrastructure

**Cons:**
- Requires storing last synced state
- More complex merge logic
- Edge cases when both changed

### Option 3: Bidirectional Sync with Property-Level Comparison

For each property, compare values and timestamps:
- Fetch both local and Asana current state
- For each property:
  - If values match: No change needed
  - If local value differs from Asana:
    - Check if local was modified after last sync
    - Check if Asana was modified after last sync
    - Use the one that was modified more recently
  - If both modified: Use newer timestamp

**Pros:**
- No schema changes
- Handles most cases correctly

**Cons:**
- Requires fetching full task details from both sources
- More API calls
- Still loses changes if both modified and timestamps are close

### Option 4: Manual Conflict Resolution

Detect conflicts and flag for manual resolution:
- When both local and Asana have changes:
  - Mark task as "conflict"
  - Store both versions
  - Require user to resolve

**Pros:**
- No data loss
- User has full control

**Cons:**
- Requires manual intervention
- Not automated

## Recommendation

**Option 2 (Three-Way Merge)** is the most practical:
- **Does NOT require per-property timestamps** - uses existing task-level `updated_at`/`modified_at`
- Works with existing infrastructure
- Preserves most changes automatically
- Falls back to timestamp-based resolution for true conflicts
- Can be implemented incrementally

## Implementation Plan

1. Store last synced state in a separate file or as task metadata
   - Store snapshot of task properties after each successful sync
   - Use existing `updated_at`/`modified_at` timestamps (no per-property timestamps needed)

2. During sync, compare three states:
   - `last_synced_state`: What the task looked like after last sync
   - `current_local`: Current state in local parquet
   - `current_asana`: Current state in Asana

3. For each property, use merge logic:
   - If `current_local == last_synced` and `current_asana != last_synced`: Use Asana (only Asana changed)
   - If `current_asana == last_synced` and `current_local != last_synced`: Use Local (only Local changed)
   - If both changed: Use newer timestamp (current behavior)
   - If neither changed: Use either (they match)

4. Update last synced state after successful sync

## Timestamp Requirements

**No per-property timestamps needed:**
- Uses existing task-level `updated_at` (local) and `modified_at` (Asana)
- Compares property values against last synced state to detect changes
- Only uses timestamps when both sides changed (conflict resolution)

**Alternative (if per-property timestamps were available):**
- Would enable more precise conflict resolution
- But not necessary for basic field-level reconciliation
- Asana API doesn't provide per-property timestamps anyway

