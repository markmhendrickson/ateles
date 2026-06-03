# Agent Persistence Requirements

**Status:** Active  
**Last Updated:** 2025-12-23  
**Related:** `/shared/docs/agent-context.md`, `/shared/docs/agent-data-entry-requirements.md`

---

## Purpose

This document defines mandatory requirements for agents to persist user instructions, contact information, and other data that should be captured for future reference.

---

## Instruction Persistence

**MANDATORY:** When the user provides new standing rules or behavioral instructions (e.g., "always do X", "never do Y"), persist them by updating appropriate canonical documents.

**MANDATORY:** Always save operational guidance to repository documentation and/or appropriate rule files in `/shared/docs/agent-*.md`. This includes any guidance about how agents should operate, behave, or execute tasks.

### Persistence Targets

- Update appropriate topical rule files in `/shared/docs/agent-*.md` based on guidance type:
  - Data-related rules → `/shared/docs/agent-data-rules.md`
  - Persistence rules → `/shared/docs/agent-persistence-rules.md`
  - Development workflows → `/shared/docs/agent-development-workflows.md`
  - Security/automation → `/shared/docs/agent-security-automation.md`
  - Time/communication → `/shared/docs/agent-time-communication.md`
  - Workflow specifics → `/shared/docs/agent-workflow-specifics.md`
  - Repository structure → `/shared/docs/agent-repository-structure.md`
  - Reference materials → `/shared/docs/agent-reference-guide.md`
  - Prompt integration → `/shared/docs/agent-prompt-integration.md`
- Update domain-specific strategy/tactics/operations documents for domain-specific rules
- Update `/shared/docs/agent-*.md` policy documents for policy-level rules

### Terminal & Automation Behavior

Treat instructions like "always continue running terminal commands as needed to debug or execute task" as standing rules. Continuously use terminal commands and automation proactively (including restarting services, tailing logs, rerunning scripts) until the current task is either working or the precise failure point is fully isolated.

**Default:** Treat instruction persistence as a mandatory duty, not an optional enhancement.

---

## Contact Persistence

**MANDATORY:** Whenever contact-relevant data points appear or interactions occur, update `$DATA_DIR/contacts/contacts.parquet`.

### Contact-Relevant Data Points

Capture when any of these appear:
- Names, emails, phone numbers
- Crypto addresses, bank details
- Payment interactions, invoices
- BTC transactions, authorizations
- Any other contact-related information

### Update Process

1. **Check for existing contacts** - Prefer updating an existing contact (merge new info) rather than creating duplicates
2. **Store attributes** - Store new attributes directly on the contact (e.g., `btc_address`, `wallet_label`) and/or as linked interaction records per the contacts schema
3. **Merge information** - When updating existing contacts, merge new information with existing data

### Contact Schema

Follow the contacts schema defined in `$DATA_DIR/schemas/contacts_schema.json`:
- Use MCP tools to add/update contacts (see `/shared/docs/agent-mcp-access-policy.md`)
- Include all available contact details
- Link to related interactions (payments, transactions, etc.)

**Default:** Treat contact/interaction capture as a mandatory duty, not an optional enhancement.

---

## Email Persistence

**MANDATORY:** Always save emails (sent and received) to `$DATA_DIR/emails/emails.parquet` and link them to contacts.

### Email Capture Process

1. **Create snapshot** - Always create timestamped snapshot before modifying `$DATA_DIR/emails/emails.parquet`

2. **Identify or create contact:**
   - Extract email addresses from `from_email`, `to_emails`, `cc_emails`, `bcc_emails`
   - Check for existing contacts in `$DATA_DIR/contacts/contacts.parquet` by email address
   - If contact exists: Use `contact_id` and `contact_name` from existing contact
   - If contact doesn't exist: Create new contact entry first, then use its `contact_id` and `contact_name`

3. **Build email entry:**
   - Generate unique `email_id` (16-character UUID)
   - Set `direction` ('sent' or 'received')
   - Extract and set `from_email`, `from_name`, `to_emails`, `cc_emails`, `bcc_emails`
   - Set `subject`, `body`, `html_body` (if available)
   - Set `gmail_message_id` and `thread_id` if available
   - Set `status` ('sent', 'delivered', 'read', 'failed', 'draft')
   - Set `date_sent`, `date_received`, `date_created`, `date_updated`
   - Link to contact via `contact_id` and `contact_name`
   - Set `import_date` to current date
   - Set `import_source` ('gmail_mcp' for MCP server, 'manual_entry' for manually added)

4. **Add email entry:**
   - Read existing `$DATA_DIR/emails/emails.parquet` (or create empty DataFrame if file doesn't exist)
   - Check for duplicates by `gmail_message_id` (if available) or `email_id`
   - Append new email entry
   - Write back to parquet file

5. **Update contact:**
   - Update contact's `last_contact_date` to email date
   - Update contact's `updated_date` to current date
   - Add note to contact's `notes` field about email interaction if relevant

### Email Schema

Follow the emails schema defined in `$DATA_DIR/schemas/emails_schema.json`:
- Use MCP tools to add emails (see `/shared/docs/agent-mcp-access-policy.md`)
- Link emails to contacts via `contact_id` and `contact_name`
- Include all available email metadata (subject, body, dates, status, etc.)

### When to Capture Emails

- **Sent emails:** Immediately after sending via Gmail MCP or other email service
- **Received emails:** When reading or processing emails via Gmail MCP
- **Draft emails:** When creating or updating drafts (status='draft')
- **Email replies:** Capture as new email entries linked to same contact

**Default:** Treat email persistence as a mandatory duty, not an optional enhancement.

---

## Task-Outcome-Project Association

**MANDATORY:** When a task is created, it must be associated with an appropriate outcome (what we're achieving) and optionally a project (how/time-bound workstream).

### Process

1. **Identify appropriate outcome:**
   - Check existing outcomes in `$DATA_DIR/outcomes/outcomes.parquet`
   - Determine which outcome(s) the task contributes to
   - If no suitable outcome exists, create a new outcome
   - Outcomes represent what we're achieving (the "why")

2. **Create outcome if needed:**
   - Add outcome entry to `$DATA_DIR/outcomes/outcomes.parquet` following `$DATA_DIR/schemas/outcomes_schema.json`
   - Set outcome type (strategic/tactical/operational)
   - Link to strategic goal if applicable
   - Set domain and status

3. **Identify appropriate project (optional):**
   - Check existing projects in `$DATA_DIR/projects/projects.parquet`
   - Determine if task is part of a time-bound workstream
   - If no suitable project exists, create a new project
   - Projects represent how we're achieving outcomes (the "how")

4. **Create project if needed:**
   - Add project entry to `$DATA_DIR/projects/projects.parquet` following `$DATA_DIR/schemas/projects_schema.json`
   - Link project to outcome via `outcome_id` and `outcome_name`
   - Create project execution plan via MCP: `mcp_parquet_add_record(data_type="execution_plans", ...)`
   - Set appropriate status, priority, and goals

5. **Link task to outcome and project:**
   - Set `outcome_ids` field in task to outcome ID(s) (pipe-delimited if multiple)
   - Set `outcome_names` field in task to outcome name(s)
   - Set `project_ids` field in task to project ID(s) if applicable
   - Set `project_names` field in task to project name(s) if applicable
   - Ensure task execution plan references the outcome and project

6. **Update project plan:**
   - Add task to project execution plan if it's a key component
   - Update project milestones/phases if task represents a new phase

### Outcome Identification Criteria

- **Strategic alignment:** Task contributes to strategic goal through outcome
- **Domain alignment:** Task domain matches outcome domain
- **Purpose fit:** Task objectives align with outcome description
- **Type match:** Task type matches outcome type (strategic/tactical/operational)

### Project Identification Criteria

- **Outcome linkage:** Project achieves the identified outcome
- **Time-bound:** Project represents a time-bound workstream
- **Scope fit:** Task is part of project's workstream
- **Timeline alignment:** Task timeline fits within project timeline

### Exception

If a task is truly standalone with no project context, it may be created without a project association. However, preference should always be given to creating or using a project to maintain organizational structure.

**Default:** Treat project association as a mandatory requirement for task creation, not an optional enhancement.

---

## Task and Data Object Maintenance

**MANDATORY:** When working on tasks together in chats, always create and update relevant tasks and related data objects as work progresses.

### Process

1. **Identify or create task:**
   - Check for existing tasks related to the work
   - Create new task if none exists
   - Update task status, notes, and metadata as work progresses

2. **Update task status:**
   - Set `status` to `in_progress` when work begins
   - Update `notes` field with current status, blockers, and progress
   - Update `updated_at` timestamp when making changes

3. **Create related data objects:**
   - Create transaction records when payments are made
   - Update contact records when interacting with vendors/parties
   - Create flow entries for financial transactions
   - Link related records via appropriate ID fields

4. **Maintain task notes:**
   - Include key details: amounts, dates, reference numbers, parties involved
   - Document decisions made and next steps
   - Reference related data objects (transaction IDs, contact IDs, etc.)

### Examples

- **Payment task:** Update task with payment status, create transaction record when payment is made, update contact record for vendor
- **Appeal task:** Update task with appeal status, link to payment task, update notes with deadlines and requirements
- **Email correspondence:** Update task notes with email sent/received, update contact record with interaction details

**Default:** Treat task and data object maintenance as a mandatory duty during collaborative work, not an optional enhancement.

---

## Related Documentation

- `/shared/docs/agent-context.md` - Agent context and quick reference (index to all rule documents)
- `/shared/docs/agent-mcp-access-policy.md` - MCP access requirements for updating contacts
- `/shared/docs/agent-data-entry-requirements.md` - Data entry requirements
- `$DATA_DIR/schemas/contacts_schema.json` - Contacts schema definition
- `$DATA_DIR/schemas/emails_schema.json` - Emails schema definition
- `/strategy/operations/tasks.md` - Task creation and project association process





