# Execution Layer

## Purpose

The Execution Layer sits below the Truth Layer and contains systems that carry out actions based on strategies and plans from the Strategy Layer. This layer focuses on automation, process execution, and external system integration.

## Structure

- **`scripts/`** - Automation scripts for data processing and workflows (160+ Python/shell scripts)
- **`automation/`** - Specialized automation tools
  - **`pdf/`** - PDF form automation tools
  - **`asana/`** - Asana integration tools
  - **`transactions/`** - Transaction execution tools
  - **`audio/`** - Audio transcription tools
- **`workflows/`** - Workflow definitions and execution plans
  - **`execution-plans/`** - Detailed execution plans for tasks

## Key Capabilities

### Data Import & Processing
- Bank CSV imports
- Broker data imports
- Crypto transaction imports
- Task imports from Asana
- Contact imports from Gmail
- Notion exports

### Automation Tools
- PDF form detection and filling
- Audio transcription
- Transaction execution
- Asana bidirectional sync
- Webhook servers

### Background Services
- Asana sync service
- Audio transcription watcher
- Twilio SMS services
- Webhook servers

## Usage Examples

### Data Import
```bash
python execution/scripts/import_data.py transactions file.csv --source bank_name
python execution/scripts/import_asana_tasks.py
```

### Data Query
```bash
python execution/scripts/query_transactions.py --summary
python execution/scripts/query_tasks.py today
```

### PDF Automation
```bash
python execution/scripts/fill-pdf-form.py --template form.pdf --data data.json
```

## Relationship to Other Layers

**Reads from:** Truth Layer via MCP (data access)  
**Guided by:** Strategy Layer (implements strategies and plans)  
**Executes:** Actions, automation, external system sync

## Data Access

This layer accesses data through the MCP servers in the Truth Layer. Scripts use MCP tools for all data operations:
- Read: Query parquet files via MCP
- Write: Add/update records via MCP (automatic snapshots)
- Search: Semantic search via MCP embeddings

## Execution Plans & Processes

Execution plans and recurring processes are stored as structured parquet data objects, enabling queryable, version-controlled definitions with execution tracking.

### Execution Plans

**Storage:** `$DATA_DIR/execution_plans/execution_plans.parquet`  
**Schema:** `$DATA_DIR/schemas/execution_plans_schema.json`  
**Access:** Parquet MCP server only

Execution plans define project/task-specific plans with:
- Objectives and scope
- Milestones and phases
- Dependencies and constraints
- Success criteria
- Progress tracking

**Usage:**
- Created automatically when tasks/projects are created
- Linked to tasks via `execution_plan_id` field
- Updated via MCP as work progresses
- Queryable by project, status, domain, priority

**Query Examples:**
```python
# By project
mcp_parquet_read_parquet(
    data_type="execution_plans",
    filters={"project_id": "abc123"}
)

# By status
mcp_parquet_read_parquet(
    data_type="execution_plans",
    filters={"status": "in_progress"}
)
```

**Documentation:** See `docs/execution_plans_rules.mdc` for complete usage guide.

### Processes

**Storage:** `$DATA_DIR/processes/processes.parquet`

**Schema:** `$DATA_DIR/schemas/processes_schema.json`

**Access:** Parquet MCP server only

Processes define recurring operational workflows with:

- Execution frequency (daily, weekly, quarterly, annual)
- Workflow steps and procedures
- Domain and status tracking
- Execution history (last executed, next scheduled, execution count)
- Related documentation and processes

**Usage:**

- Migrated from markdown files in `strategy/operations/` (markdown files removed after migration)
- Queryable by frequency, domain, status
- Execution tracking updates on process completion
- Full workflow content stored in `workflow_content` field (markdown format)

**Query Examples:**

```python
# Daily processes
mcp_parquet_read_parquet(
    data_type="processes",
    filters={"frequency": "daily"}
)

# Finance domain processes
mcp_parquet_read_parquet(
    data_type="processes",
    filters={"domain": "Finance"}
)

# Processes due for execution
mcp_parquet_read_parquet(
    data_type="processes",
    filters={"next_scheduled_date": {"$lte": "2025-01-15"}}
)
```

**Documentation:** See `strategy/operations/README.md` for process definitions and `docs/execution_plans_rules.mdc` for usage patterns.

## Architecture

Part of the three-layer architecture:
- **Strategy Layer** - Planning and decision-making
- **Execution Layer** (this layer) - Automation and execution
- **Truth Layer** - Data and memory substrate

See `/plans/neotoma-architecture-integration.md` for complete architecture documentation.

## Documentation

- **`scripts/README.md`** - Script documentation and usage
- **`scripts/ASANA_SYNC_SERVICE.md`** - Asana sync documentation
- **`scripts/ASANA_WEBHOOKS.md`** - Webhook setup guide


