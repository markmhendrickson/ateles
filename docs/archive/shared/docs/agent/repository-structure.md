# Agent Repository Structure

**Purpose:** Repository organization, architecture context, development workflows.

**Reference:** `/README.md` for complete structure. `/shared/docs/neotoma-architecture-integration.md` for architecture.

**Structure:** `/strategy/strategy/` (principles, goals), `/strategy/tactics/` (methods, frameworks), `/strategy/operations/` (procedures by domain), `/strategy/reference/` (templates, scorecards), `/execution/scripts/` (automation), `/$DATA_DIR/` (normalized data: 60+ types, 35,000+ records), `/truth/mcp-servers/` (data access), `/execution/mcp-servers/` (external APIs), `/shared/docs/` (documentation, policies), `/foundation/` (dev processes, git submodule), `/execution/website/` (website submodules)

**Architecture:** Three-layer (Strategy, Execution, Truth) per [Neotoma](https://github.com/markmhendrickson/neotoma). Currently Parquet MCP for Truth Layer, planned migration to Neotoma.

---

## Development Workflows

**Reference:** `/foundation/README.md` for standardized development processes.

This repository uses [Foundation](/foundation/README.md) for development workflows, code conventions, and security practices. Foundation provides development workflow, code conventions, documentation standards, and security practices.

**Note:** Foundation complements (does not replace) existing agent context and workflows. Agent-specific policies in `/shared/docs/agent/rules/*.md` take precedence for agent behavior.

