# Agent Persistence Requirements

**Status:** Active  
**Last Updated:** 2026-01-14  
**Related:** `/shared/docs/agent/context.md`, `/shared/docs/agent/rules/data-entry-requirements.md`

---

## Purpose

This document defines mandatory requirements for agents to persist user instructions, contact information, and other data that should be captured for future reference.

---

## Instruction Persistence

**MANDATORY:** When the user provides new standing rules or behavioral instructions (e.g., "always do X", "never do Y"), persist them by updating appropriate canonical documents.

**MANDATORY:** Always save operational guidance to repository documentation and/or appropriate rule files in `/shared/docs/agent/rules/*.md`. This includes any guidance about how agents should operate, behave, or execute tasks.

### Persistence Targets

- Update appropriate topical rule files in `/shared/docs/agent/rules/*.md` based on guidance type:
  - Data-related rules → `/shared/docs/agent/rules/data-rules.md`
  - Persistence rules → `/shared/docs/agent/rules/persistence-rules.md`
  - Development workflows → `/shared/docs/agent/rules/development-workflows.md`
  - Security/automation → `/shared/docs/agent/rules/security-automation.md`
  - Time/communication → `/shared/docs/agent/rules/time-communication.md`
  - Workflow specifics → `/shared/docs/agent/workflow-specifics.md`
  - Repository structure → `/shared/docs/agent/repository-structure.md`
  - Reference materials → `/shared/docs/agent/reference-guide.md`
  - Prompt integration → `/shared/docs/agent/prompt-integration.md`
- Update domain-specific strategy/tactics/operations documents for domain-specific rules
- Update `/shared/docs/agent/rules/*.md` policy documents for policy-level rules

### Terminal & Automation Behavior

Treat instructions like "always continue running terminal commands as needed to debug or execute task" as standing rules. Continuously use terminal commands and automation proactively (including restarting services, tailing logs, rerunning scripts) until the current task is either working or the precise failure point is fully isolated.

**Default:** Treat instruction persistence as a mandatory duty, not an optional enhancement.

---

## Contact Persistence

**MANDATORY:** Whenever contact-relevant data appears (names, emails, phones, crypto addresses, bank details, payment interactions, BTC transactions), update `$DATA_DIR/contacts/contacts.parquet`.

**Process:** Check for existing contacts (prefer updating/merging over creating duplicates). Store attributes on contact or as linked interaction records. Follow `$DATA_DIR/schemas/contacts_schema.json`. Use MCP tools (see `/shared/docs/agent/rules/mcp-access-policy.md`). Link to related interactions.

**Reference:** `/shared/docs/agent/rules/persistence-rules.md` for detailed contact extraction and duplicate detection logic.

---

## Email Persistence

**MANDATORY:** Always save emails (sent and received) to `$DATA_DIR/emails/emails.parquet` and link them to contacts.

**Process:** Create snapshot. Identify/create contact (extract emails, check existing, create if needed). Build email entry (generate `email_id`, set `direction`, extract addresses/subject/body, set `gmail_message_id`/`thread_id` if available, set `status`/dates, link to contact, set `import_date`/`import_source`). Add entry (read parquet, check duplicates by `gmail_message_id`/`email_id`, append, write back). Update contact (`last_contact_date`, `updated_date`, notes).

**Schema:** Follow `$DATA_DIR/schemas/emails_schema.json`. Use MCP tools (`/shared/docs/agent/rules/mcp-access-policy.md`). Link via `contact_id`/`contact_name`.

**When:** Sent (immediately after sending), received (when reading/processing), drafts (when creating/updating), replies (as new entries).

**Default:** Treat email persistence as a mandatory duty, not an optional enhancement.

---

## Research Analysis Persistence

**MANDATORY:** When creating analysis of research (e.g., validating claims, comparing arguments to evidence, evaluating frameworks), always store the analysis alongside the original research in `$DATA_DIR/research/research.parquet`.

**Process:**
1. Identify the original research record (by author, title, or source URL)
2. Create analysis record with:
   - `source_type`: "analysis"
   - `author`: "Analysis based on [original research] and [evidence sources]"
   - `title`: Descriptive title indicating it's an analysis (e.g., "Analysis: [Original Title] - [Time Period/Context]")
   - `summary`: Full analysis content (markdown format)
   - `topics`: Include original topics plus "analysis", "validation", or relevant analysis keywords
   - Link to original research via `source_url` or reference in `summary` if original research doesn't have URL
3. Store in same `research` data type to maintain relationship

**When:** After analyzing research against:
- Recent events or evidence
- Competing arguments or frameworks
- Historical data or trends
- Validation of claims or predictions

**Purpose:** Maintain analytical context alongside source material for future reference and verification.

**Default:** Treat research analysis persistence as a mandatory duty, not an optional enhancement.

---

## Task-Outcome-Project Association

**MANDATORY:** When a task is created, it must be associated with an appropriate outcome (what we're achieving) and optionally a project (how/time-bound workstream).

**Process:** Identify outcome (check `$DATA_DIR/outcomes/outcomes.parquet`, create if needed per schema, set type/domain/status). Identify project (check `$DATA_DIR/projects/projects.parquet`, create if needed, link to outcome, create execution plan via `mcp_parquet_add_record(data_type="execution_plans", ...)`). Link task (`outcome_ids`/`outcome_names`, `project_ids`/`project_names`, reference via `execution_plan_id`). Update execution plan via `mcp_parquet_update_records` (add task, update milestones/phases).

**Criteria:** Outcome: strategic alignment, domain match, purpose fit, type match. Project: outcome linkage, time-bound, scope fit, timeline alignment. **Exception:** Standalone tasks may skip project, but prefer project association.

---

## Task and Data Object Maintenance

**MANDATORY:** Always update relevant tasks and related data objects as work advances. Do not wait until work is complete—update tasks continuously throughout the conversation as progress is made.

**Process:** 
1. **Identify/create task** (check existing via MCP, create if needed)
2. **Update continuously as work advances:**
   - Update status (`in_progress` when work begins, `completed` when finished, `blocked` if stuck)
   - Update `notes` field with progress, decisions, findings, and next steps
   - Update `updated_at` timestamp whenever task is modified
   - Update execution plan `notes_updates` field with chronological progress
3. **Create related objects** (transactions for payments, update contacts for vendors, create flow entries, link via IDs)
4. **Maintain comprehensive notes:** amounts, dates, reference numbers, parties, decisions, next steps, related object IDs, any relevant context

**Update Frequency:**
- Update task status when work begins
- Update notes after each significant step or finding
- Update when decisions are made or blockers encountered
- Update when related objects are created or modified
- Update when work completes or is paused

**Examples:** 
- Payment task → update status to `in_progress`, create transaction, update vendor contact, update notes with payment details
- Appeal task → update status, link payment task, update notes with appeal submission details
- Email sent → update task notes with email content and outcome, update contact record
- Analysis complete → update task notes with findings, update status to reflect progress

---

## Related Documentation

- `/shared/docs/agent/context.md` - Agent context and quick reference (index to all rule documents)
- `/shared/docs/agent/rules/mcp-access-policy.md` - MCP access requirements for updating contacts
- `/shared/docs/agent/rules/data-entry-requirements.md` - Data entry requirements
- `$DATA_DIR/schemas/contacts_schema.json` - Contacts schema definition
- `$DATA_DIR/schemas/emails_schema.json` - Emails schema definition
- `$DATA_DIR/schemas/research_schema.json` - Research schema definition
- `/strategy/operations/tasks.md` - Task creation and project association process





