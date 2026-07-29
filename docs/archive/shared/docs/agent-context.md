# Agent Context: Personal Workflow Repository

**Purpose:** This document provides an index to essential context and quick reference for AI agents working in this repository. For detailed policies, see the focused policy documents in `/shared/docs/agent-*.md`.

**Last Updated:** 2025-01-23

---

## Quick Start

**MANDATORY PRE-TASK CHECKLIST:**

1. ✅ Read this document for essential context
2. ✅ Read `/shared/docs/data-imports-policy.md` - Data imports directory rules
3. ✅ Review relevant policy documents in `/shared/docs/agent-*.md` based on your task

---

## Core Rule Documents

### Repository & Structure
- **`/shared/docs/agent-repository-structure.md`** - Repository organization, architecture context, document hierarchy, development workflows, domain organization

### Data Management
- **`/shared/docs/agent-data-rules.md`** - Data query decision tree, snapshot management, adding data entries
- **`/shared/docs/data-imports-policy.md`** - Data imports directory policy (read-only archive rules)

### Persistence Rules
- **`/shared/docs/agent-persistence-rules.md`** - Personal data, contact information, account information persistence
- **`/shared/docs/agent-persistence-requirements.md`** - Instruction persistence, contact persistence, email persistence, task-outcome-project association

### Development Workflows
- **`/shared/docs/agent-development-workflows.md`** - Website development, MCP server development

### Security & Automation
- **`/shared/docs/agent-security-automation.md`** - Security requirements (1Password CLI), browser automation requirements (Playwright)

### Time & Communication
- **`/shared/docs/agent-time-communication.md`** - Time-sensitive operations, communication follow-up requirements

### Workflows & References
- **`/shared/docs/agent-workflow-specifics.md`** - Email processing workflows, Amazon order extraction
- **`/shared/docs/agent-reference-guide.md`** - Key reference documents, data sources, update cadences, exception protocol

### Instruction Integration
- **`/shared/docs/agent-prompt-integration.md`** - Automatic integration of persistent instructions from user prompts

---

## Agent Policy Documents

All agent-specific policies are documented in focused policy documents in `/shared/docs/`:

### Core Policies

- **`/shared/docs/agent-mcp-access-policy.md`** - MANDATORY: MCP-only data access requirements
- **`/shared/docs/agent-data-entry-requirements.md`** - Data entry requirements, schema evolution, file-backed data types
- **`/shared/docs/agent-confirmation-requirements.md`** - Email/transaction confirmation requirements
- **`/shared/docs/agent-communication-rules.md`** - Spanish email formatting, WhatsApp style, transaction language rules
- **`/shared/docs/agent-email-triage-protocol.md`** - Email triage workflow, draft response requirements, archive after responding
- **`/shared/docs/agent-workflow-requirements.md`** - Scorecard saving, quarterly reports, file naming conventions
- **`/shared/docs/agent-decision-framework.md`** - Decision-making framework and behavioral compliance

---

## Document Hierarchy

- **Strategy** (highest level) - Long-term principles and goals by domain
- **Tactics** (mid level) - Methods and approaches to achieve strategy by domain
- **Operations** (execution level) - Specific procedures and workflows organized by domain

When conflicts arise, strategy takes precedence over tactics, and tactics over operations.

## Domain Organization

Operations are organized by domain:
- **Finance** - Financial operations (portfolio reviews, transaction processing, data imports)
- **Admin** - Administrative workflows (utilities, forms, filings, government interactions)
- **Work** - Work and professional workflows
- **Health** - Health and fitness workflows

---

## Agent Action Checklist

When working in this repository:

- [ ] **Read this document** for essential context
- [ ] **Read `/shared/docs/data-imports-policy.md`** - Data imports directory rules
- [ ] **Review relevant policy documents** in `/shared/docs/agent-*.md` based on your task
- [ ] **Use MCP parquet server** for all data queries (see `/shared/docs/agent-mcp-access-policy.md`)
- [ ] **Proactively store personal context** - Use MCP server tools to create/update records for any relevant structured data (contacts, tasks, transactions, preferences, etc.) without waiting for explicit requests
- [ ] **Never expose secrets via `op` CLI** - See `/shared/docs/agent-security-automation.md`
- [ ] **Use Playwright for browser automation** - See `/shared/docs/agent-security-automation.md` (never use Cursor's native browser tools)
- [ ] **Check relevant strategy documents** first (see `/shared/docs/agent-reference-guide.md`)
- [ ] **Verify alignment** with behavioral mandates (see `/strategy/operations/operating-manual.md`)
- [ ] **Follow established workflows** and templates (see `/strategy/operations/README.md`)
- [ ] **Save all scorecard results** to `/strategy/operations/finance/` (see `/shared/docs/agent-workflow-requirements.md`)
- [ ] **Use correct file naming conventions** (see `/shared/docs/agent-workflow-requirements.md`)
- [ ] **Update canonical documents** when making changes
- [ ] **Respect document hierarchy** (strategy > tactics > operations)
- [ ] **Verify no violations** of forbidden actions (see `/shared/docs/agent-decision-framework.md`)
- [ ] **Ensure quarterly-only cadence** for trading decisions
- [ ] **Persist new standing rules** by updating appropriate canonical documents (see `/shared/docs/agent-persistence-requirements.md`)
- [ ] **Associate tasks with outcomes** - Every new task must be linked to an appropriate outcome (what we're achieving) and optionally a project (how/time-bound workstream) (see `/shared/docs/agent-persistence-requirements.md`)
- [ ] **Link new tasks to appropriate projects** - Always relate new tasks to appropriate projects when creating them. Check available projects and link based on task domain, title, and description
- [ ] **Save operational guidance** to repository documentation and/or appropriate rule files whenever provided (see `/shared/docs/agent-persistence-requirements.md`)
- [ ] **Update fixed costs** when subscriptions, plans, or recurring expenses change (e.g., plan upgrades, cancellations, price changes)
- [ ] **Check current date/time** when executing time-sensitive tasks (task due dates, scheduling, time-based decisions) - Always verify current date before making time-sensitive recommendations (see `/shared/docs/agent-time-communication.md`)
- [ ] **Create follow-up subtask** when sending communications that require a response to complete a task - Set due date after reasonable wait time (response window + buffer), include context and contact info in notes (see `/shared/docs/agent-time-communication.md`)

---

**Note:** This document provides an index to essential context. For detailed rules, procedures, and workflows, see the focused rule and policy documents referenced above.






