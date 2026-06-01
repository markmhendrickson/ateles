# Documentation

## Documents

### Data Management

- **`data_rules.md`** - Data query and imports directory rules. Defines when to use normalized data vs. imported source files, modification rules, and correct workflows for data queries and updates. Includes `$DATA_DIR/imports/` read-only archive rules.

### Integration Documentation

- **`/reports/asana-api-coverage-analysis.md`** - Comprehensive analysis of Asana API coverage. Documents currently imported fields, missing data types (custom fields, dependencies, story types), and recommendations for future enhancements.

### Architecture Documentation

- **`/plans/neotoma-architecture-integration.md`** - Three-layer architecture (Strategy/Truth/Execution) and Neotoma migration plan. Documents how this repository implements Strategy and Execution layers, current use of Parquet MCP for data layer, and planned migration to Neotoma for enhanced Truth Layer capabilities.

### Agent Policy Documentation


- **`mcp_access_policy_rules.md`** - MANDATORY: MCP-only data access requirements. Defines how agents must access normalized parquet data through MCP server.
- **`data_entry_requirements_rules.md`** - Data entry requirements, schema evolution, file-backed data types, audio import workflow.
- **`confirmation_requirements_rules.md`** - Email/transaction confirmation requirements. Mandatory preview and confirmation before sending emails or executing transactions.
- **`communication_rules.md`** - Spanish email formatting, WhatsApp style, transaction reference language rules.
- **`persistence_rules.md`** - Instruction persistence, contact persistence, email persistence, task-outcome-project association. Requirements for capturing and storing user instructions and contact information.
- **`workflow_requirements_rules.md`** - Scorecard saving, quarterly reports, file naming conventions, document update procedures.
- **`decision_framework_rules.md`** - Decision-making framework, behavioral compliance, constraint verification.

## Usage

These documents are automatically referenced by Cursor agents via `.cursorrules`. Consult before making changes that affect:
- Data structure, import processes, or repository organization
- Integration implementations (Asana, Gmail, etc.)
- Workflow modifications
- Policy decisions





