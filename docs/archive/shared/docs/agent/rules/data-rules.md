# Agent Data Rules

**Purpose:** Rules for data queries, modifications, snapshots, and adding entries.

**Last Updated:** 2025-01-23

---

## Data Query Decision Tree

**BEFORE querying any data, follow this decision tree:**

```
User requests data query
    ↓
Is this an explicit import task or troubleshooting import failures?
    ├─ NO → Use normalized data files:
    │        • $DATA_DIR/flows/flows.parquet (for fixed costs, recurring expenses)
    │        • $DATA_DIR/transactions/transactions.parquet (for transactions)
    │        • $DATA_DIR/income/income.parquet (for income)
    │        • $DATA_DIR/[type]/[type].parquet (for other data types)
    │        DO NOT read from $DATA_DIR/imports/
    │
    └─ YES → Can read from $DATA_DIR/imports/ for that specific purpose only
```

**RULE:** If you read from `$DATA_DIR/imports/` for a routine query, you have violated policy. Stop and use normalized data instead.

**Reference:** `/shared/docs/data-imports-policy.md` for complete data imports directory policy.

---

**Query Rules:** Use normalized data files (`$DATA_DIR/flows/flows.parquet`, `$DATA_DIR/transactions/transactions.parquet`, etc.). DO NOT read from `$DATA_DIR/imports/` unless troubleshooting imports. Examples: ❌ `$DATA_DIR/imports/.../Fixed costs-Table 1.csv` → ✅ `$DATA_DIR/flows/flows.parquet`

---

**Modification Rules:** DO NOT modify `$DATA_DIR/imports/` (read-only archive). Modify normalized data files instead. Source files are external to repo.

---

## Parquet File Snapshot Management

**MANDATORY:** Always create timestamped snapshots before modifying any parquet file.

**Process:**
1. **Before modification** - Create snapshot with format: `[filename]-[YYYY-MM-DD-HHMMSS].parquet`
   - Example: `$DATA_DIR/flows/flows.parquet` → `$DATA_DIR/snapshots/flows-2025-01-15-143022.parquet`
   - Store in `$DATA_DIR/snapshots/` directory
2. **During queries** - NEVER query historical snapshots unless user explicitly requests historical records
3. **Default behavior** - Always use current parquet files (without timestamp suffix) for all data queries

**Snapshot naming:**
- Format: `[original-name]-[YYYY-MM-DD-HHMMSS].parquet`
- Location: `$DATA_DIR/snapshots/` directory
- Purpose: Historical record for rollback/recovery

**Query rules:**
- ✅ **Default:** Query current files (`flows.parquet`, `transactions.parquet`, etc.)
- ❌ **Never query:** Timestamped snapshot files unless explicitly requested
- ✅ **When requested:** User asks for "historical", "previous", "snapshot", or "version" data

**Examples:**
- ❌ "Show transactions" → Query `$DATA_DIR/snapshots/transactions-2025-01-10-120000.parquet` is WRONG
- ✅ "Show transactions" → Query `$DATA_DIR/transactions/transactions.parquet` is CORRECT
- ✅ "Show transactions from snapshot 2025-01-10" → Query `$DATA_DIR/snapshots/transactions-2025-01-10-*.parquet` is CORRECT

**Rationale:** Enables rollback/recovery while ensuring queries always use current data unless historical analysis is explicitly requested.

---

## When Adding Data Entries

**TRIGGER:** User asks to "add", "create", "insert", or "record" any data entry (fixed costs, transactions, income, flows, holdings, etc.)

**REQUIRED ACTIONS:**
1. **Identify data type** - Determine which normalized data file to modify:
   - Fixed costs/recurring expenses → `$DATA_DIR/fixed_costs/fixed_costs.parquet`
   - Cash flow entries → `$DATA_DIR/flows/flows.parquet`
   - Transactions → `$DATA_DIR/transactions/transactions.parquet`
   - Income → `$DATA_DIR/income/income.parquet`
   - Holdings → `$DATA_DIR/holdings/holdings.parquet`
   - Other types → `$DATA_DIR/[type]/[type].parquet`

2. **Create snapshot first** - Always create timestamped snapshot before modification:
   - Format: `$DATA_DIR/snapshots/[filename]-[YYYY-MM-DD-HHMMSS].parquet`
   - Extract filename from parquet path (e.g., `fixed_costs.parquet` → `fixed_costs`)
   - Use current timestamp: `datetime.now().strftime('%Y-%m-%d-%H%M%S')`
   - Ensure `$DATA_DIR/snapshots/` directory exists

3. **Reference schema** - Check `/$DATA_DIR/schemas/[type]_schema.json` for:
   - Required fields and their types
   - Field descriptions and constraints
   - Valid values for enum/status fields
   - Date formats and ID generation patterns

4. **Build new entry dictionary:**
   - Include all required fields from schema
   - Generate unique IDs if needed (e.g., `str(uuid.uuid4())[:16]` for ID fields)
   - Set `import_date` to current date
   - Set `import_source_file` to "manual_entry" for manually added entries
   - Calculate derived fields (e.g., yearly amounts from monthly, percentages)
   - Set optional fields to None if not applicable
   - Include comprehensive notes per data entry requirements

5. **Add entry** - Use pandas to:
   - Read existing parquet file
   - Create new row dictionary matching schema
   - Concatenate with existing dataframe: `pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)`
   - Write back to original parquet file

**Python Pattern:** Create snapshot (`$DATA_DIR/snapshots/[filename]-[YYYY-MM-DD-HHMMSS].parquet`), read parquet, build new row dict per schema (required fields, generate IDs, set dates/amounts/status), concat with `pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)`, write back.

**Examples:** ✅ "Add fixed cost" → Snapshot + add to `$DATA_DIR/fixed_costs/fixed_costs.parquet`. ❌ Wrong file type, skipping snapshot, or not checking schema is WRONG.

**Reference:** `/shared/docs/agent/rules/data-entry-requirements.md` for detailed data entry requirements.

