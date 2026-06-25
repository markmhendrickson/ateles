# Documentation

This directory contains policy documents, guidelines, and technical documentation that define how the repository should be used and maintained.

## Documents

### Data Management

- **`data-imports-policy.md`** - Policy for handling `$DATA_DIR/imports/` directory (read-only archive). Defines when to use normalized data vs. imported source files, modification rules, and correct workflows for data queries and updates.

### Integration Documentation

- **`asana-api-coverage-analysis.md`** - Comprehensive analysis of Asana API coverage. Documents currently imported fields, missing data types (custom fields, dependencies, story types), and recommendations for future enhancements.

### Architecture Documentation

- **`neotoma-architecture-integration.md`** - Three-layer architecture (Strategy/Truth/Execution) and Neotoma migration plan. Documents how this repository implements Strategy and Execution layers, current use of Parquet MCP for data layer, and planned migration to Neotoma for enhanced Truth Layer capabilities.

### Agent Policy Documentation

- **`agent-instructions-index.md`** - **START HERE**: Central index of all agent instruction files, locations, and purposes. Use this to navigate all agent documentation.

- **`agent-mcp-access-policy.md`** - MANDATORY: MCP-only data access requirements. Defines how agents must access normalized parquet data through MCP server.
- **`agent-data-entry-requirements.md`** - Data entry requirements, schema evolution, file-backed data types, audio import workflow.
- **`agent-confirmation-requirements.md`** - Email/transaction confirmation requirements. Mandatory preview and confirmation before sending emails or executing transactions.
- **`agent-communication-rules.md`** - Spanish email formatting, WhatsApp style, transaction reference language rules.
- **`agent-persistence-requirements.md`** - Instruction persistence, contact persistence. Requirements for capturing and storing user instructions and contact information.
- **`agent-workflow-requirements.md`** - Scorecard saving, quarterly reports, file naming conventions, document update procedures.
- **`agent-decision-framework.md`** - Decision-making framework, behavioral compliance, constraint verification.

## Purpose

These documents establish rules and guidelines that:
- Ensure data integrity and consistency
- Prevent accidental data corruption
- Define correct workflows and procedures
- Guide agent behavior and decision-making
- Document integration capabilities and coverage
- Establish technical standards and best practices

## Usage

These documents are automatically referenced by Cursor agents via `.cursorrules`. They should be consulted before making changes that affect:
- Data structure, import processes, or repository organization
- Integration implementations (Asana, Gmail, etc.)
- Workflow modifications
- Policy decisions

## Related Documentation

- **`/shared/docs/agent/context.md`** - Essential context for AI agents (index to all rule documents, MANDATORY reading)
- **`/$DATA_DIR/README.md`** - Data storage structure, current status, and usage guidelines
- **`/strategy/operations/README.md`** - Operational procedures organized by domain
- **`/execution/scripts/README.md`** - Script documentation and usage examples
- **`/truth/mcp-servers/parquet/README.md`** - Parquet MCP server documentation for programmatic data access
- **`/execution/mcp-servers/gmail/README.md`** - Gmail MCP server documentation
- **`/execution/mcp-servers/minted/README.md`** - Minted MCP server documentation





