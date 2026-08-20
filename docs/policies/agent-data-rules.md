# Agent Data Rules

**Purpose:** Rules for data queries, modifications, snapshots, and adding entries.

**Last Updated:** 2025-01-23

---

## Purpose

Rules for data queries, modifications, snapshots, and adding entries.

## Scope

Applies to all agents reading or writing operator data — covers the decision tree, query rules, and snapshot / modification policy below.

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

## When User Requests Data Query

**TRIGGER:** User asks to "show", "list", "query", "find", or "get" any data

**REQUIRED ACTIONS:**
1. Check if query is about fixed costs, expenses, transactions, income, or other financial data
2. If yes → Use normalized data files (`$DATA_DIR/flows/flows.parquet`, `$DATA_DIR/transactions/transactions.parquet`, etc.)
3. DO NOT read from `$DATA_DIR/imports/` unless explicitly troubleshooting an import failure
4. If you catch yourself reading from `$DATA_DIR/imports/` for a routine query, STOP and switch to normalized data

**EXAMPLES:**
- ❌ "Show fixed costs" → Reading `$DATA_DIR/imports/Finances/Fixed costs-Table 1.csv` is WRONG
- ✅ "Show fixed costs" → Reading `$DATA_DIR/flows/flows.parquet` is CORRECT
- ❌ "List transactions" → Reading `$DATA_DIR/imports/.../transactions.csv` is WRONG  
- ✅ "List transactions" → Reading `$DATA_DIR/transactions/transactions.parquet` is CORRECT

---

## When Processing Subscription or Price-Update Information

**TRIGGER:** User shares or you extract subscription, price-update, or recurring-cost information (e.g. from email, screenshot, or chat mentioning a vendor and new price).

**REQUIRED ACTIONS (before storing or responding with the update):**
1. **Look up existing fixed cost** — Query Parquet `fixed_costs` (via Parquet MCP `read_parquet` with filter on `merchant` or `expense_name` containing the vendor name). If no match, optionally query `flows` for the same vendor.
2. **Link or update** — If a matching fixed cost exists, reference it in your response and offer to update it with the new amount/effective date (create snapshot before modifying). If none exists, proceed to store the notification/entities as usual.
3. **Forbidden:** Storing or responding with a subscription/price update without first checking `fixed_costs` (and flows if relevant) for the merchant/vendor.

**EXAMPLES:**
- ❌ User shares 1Password price email → Storing notification only, without querying `fixed_costs` for merchant "1Password", is WRONG.
- ✅ User shares 1Password price email → Query `fixed_costs` with `merchant` containing "1Password" first; then store notification and report existing record (e.g. current $60/yr, renews Oct 1) and offer to update to new price.

**Reference:** Fixed costs schema and location: `$DATA_DIR/fixed_costs/fixed_costs.parquet`; query via Parquet MCP. Flows: `$DATA_DIR/flows/flows.parquet`.

---

## When User Requests Data Modification

**TRIGGER:** User asks to "update", "modify", "change", "edit", or "fix" any data

**REQUIRED ACTIONS:**
1. DO NOT modify files in `$DATA_DIR/imports/` - these are read-only archives
2. Modify normalized data files instead (`$DATA_DIR/flows/flows.parquet`, etc.)
3. If source needs updating, note that source files are external to this repo

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

**Generic Python pattern:**
```python
import pandas as pd
import uuid
from datetime import date, datetime
import os

# Determine data type and file path
from scripts.config import get_data_dir
data_type = 'fixed_costs'  # or 'transactions', 'income', 'flows', etc.
DATA_DIR = get_data_dir()
file_path = DATA_DIR / data_type / f"{data_type}.parquet"
schema_path = DATA_DIR / "schemas" / f"{data_type}_schema.json"

# Create snapshot
df = pd.read_parquet(file_path)
filename = file_path.stem
timestamp = datetime.now().strftime('%Y-%m-%d-%H%M%S')
snapshots_dir = DATA_DIR / "snapshots"
snapshots_dir.mkdir(parents=True, exist_ok=True)
df.to_parquet(snapshots_dir / f"{filename}-{timestamp}.parquet", index=False)

# Build new entry (reference schema for required fields)
new_row = {
    # Required fields per schema
    # ID fields: generate unique ID
    # Date fields: use date.today() or specific date
    # Amount fields: set numeric values
    # Status fields: use valid enum values from schema
    'import_date': date.today(),
    'import_source_file': 'manual_entry',
    # Optional fields: set to None if not applicable
}

# Add entry
df_new = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)
df_new.to_parquet(file_path, index=False)
```

**EXAMPLES:**
- ✅ "Add fixed cost for Cursor $20/mo" → Snapshot `fixed_costs.parquet`, add to `$DATA_DIR/fixed_costs/fixed_costs.parquet`
- ✅ "Add transaction" → Snapshot `transactions.parquet`, add to `$DATA_DIR/transactions/transactions.parquet`
- ✅ "Add income entry" → Snapshot `income.parquet`, add to `$DATA_DIR/income/income.parquet`
- ❌ Adding to wrong file type (e.g., fixed cost to `flows.parquet`) is WRONG
- ❌ Skipping snapshot creation is WRONG
- ❌ Not checking schema for required fields is WRONG

**Rationale:** Snapshots enable rollback/recovery. Schema ensures data consistency and completeness. Each data type has specific structure and requirements.

**Reference:** `/shared/docs/agent-data-entry-requirements.md` for detailed data entry requirements.






