# Agent Repository Structure

**Purpose:** Repository organization, architecture context, and development workflow guidance.

**Last Updated:** 2025-01-23

---

## Repository Structure

**Reference:** `/README.md` for complete repository structure and capabilities.

**Quick Reference:**
- `/strategy/strategy/` - High-level principles, long-term goals by domain
- `/strategy/tactics/` - Implementation methods, tactical approaches, frameworks by domain
- `/strategy/operations/` - Execution procedures organized by domain (finance, admin, work, health)
- `/strategy/reference/` - Templates, scorecards, research, models, agent context
- `/execution/scripts/` - Automation scripts for workflows
- `/$DATA_DIR/` - Normalized data across all domains (60+ types, 35,000+ records)
- `/truth/mcp-servers/` - MCP servers for data access (parquet)
- `/execution/mcp-servers/` - MCP servers for external API integrations (gmail, dnsimple, google-calendar, instagram, minted)
- `/shared/docs/` - Documentation, policies, agent rules
- `/foundation/` - Shared development processes and conventions (git submodule)
- `/execution/website/` - Website repositories as git submodules

---

## Architecture Context

**Reference:** `/shared/docs/neotoma-architecture-integration.md` for complete architecture documentation.

This repository implements a **three-layer architecture**: Strategy Layer, Execution Layer, and Truth Layer as defined in the [Neotoma architecture](https://github.com/markmhendrickson/neotoma). Currently using Parquet MCP for Truth Layer, with planned migration to Neotoma for enhanced capabilities.

---

## Document Hierarchy (Precedence Order)

1. **Strategy** (highest level) - Long-term principles and goals
2. **Tactics** (mid level) - Methods and approaches to achieve strategy
3. **Operations** (execution level) - Specific procedures and workflows

**Rule:** When conflicts arise, strategy takes precedence over tactics, and tactics over operations.

**Reference:** `/README.md` for domain organization details.

---

## Development Workflows

**Reference:** `/foundation/README.md` for standardized development processes.

This repository uses [Foundation](/foundation/README.md) for development workflows, code conventions, and security practices. Foundation provides:

- **Development Workflow** - Git branch strategy, PR process, worktree setup (see `/foundation/development/workflow.md`)
- **Code Conventions** - Python, shell, YAML, and markdown style guides (see `/foundation/conventions/code-conventions.md`)
- **Documentation Standards** - Structure, formatting, and writing style (see `/foundation/conventions/documentation-standards.md`)
- **Security Practices** - Pre-commit audits, credential management (see `/foundation/security/security-rules.md`)

**Note:** Foundation complements (does not replace) existing agent context and workflows. Agent-specific policies in `/shared/docs/agent-*.md` take precedence for agent behavior.

---

## Domain Organization

Operations are organized by domain:
- **Finance** - Financial operations (portfolio reviews, transaction processing, data imports)
- **Admin** - Administrative workflows (utilities, forms, filings, government interactions)
- **Work** - Work and professional workflows
- **Health** - Health and fitness workflows






