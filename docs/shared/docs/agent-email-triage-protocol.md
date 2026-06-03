# Email Triage Protocol

**Status:** Active  
**Last Updated:** 2025-12-24  
**Related:** `/shared/docs/agent-communication-rules.md`, `/shared/docs/agent-context.md`, `$DATA_DIR/schemas/payroll_documents_schema.json`, `$DATA_DIR/schemas/task_attachments_schema.json`

---

## Purpose

This document defines the mandatory protocol for triaging emails one-by-one, including display requirements, draft response generation, and post-response actions.

---

## Workflow

**MANDATORY:** Process emails one at a time, following this sequence:

1. **Search inbox** - Retrieve emails from inbox (unread or all)
2. **Display email** - Show full content + summary for current email
3. **Generate draft response** - Always create draft response (even if no response needed)
4. **Indicate input needed** - Specify what information is required to finalize/refine
5. **Wait for user decision** - User approves, modifies, or provides input
6. **Send response** (if applicable) - Send approved response
7. **Archive thread** - Remove INBOX label after responding
8. **Move to next email** - Repeat for next email in queue

---

## Display Requirements

**MANDATORY:** For each email, display:

### Email Header
- **Email number** (e.g., "Email 1 of 7")
- **From:** Sender name and email address
- **Subject:** Full subject line
- **Date:** Full date and time

### Email Content
- **Summary:** Brief summary of email content and action needed
- **Full Content:** Complete email body text (including thread history if applicable)
- **Original Message:** **MANDATORY:** Always show the original message that this email is replying to or forwarding (if applicable). Include the full quoted/reply content from the email thread.
- **Attachments:**
  - **MANDATORY:** Always download and analyze all attachments as part of email assessment
  - Extract and summarize key information from attachments
  - Include attachment analysis in the email summary and draft response recommendations

### Draft Response
- **Draft Response:** Always show a draft response, even if no response is needed
  - For informational emails: "No response needed - informational notification"
  - For action items: Draft appropriate response following `/shared/docs/agent-communication-rules.md`
- **Input Needed:** Explicitly list what information is required from user to finalize/refine the draft

### Data Object Recommendations
- **Data Objects:** Always recommend data objects that can be saved or updated from the email
  - Examples: transactions, contacts, fixed_costs, tasks, events, tax_events, etc.
  - Specify which fields can be extracted and what actions are available (create, update)
  - Reference relevant schemas in `$DATA_DIR/schemas/` if applicable
- **Related Tasks:** Always check for and identify related tasks that should be updated
  - Search tasks by keywords from email subject/content
  - Identify tasks that this email relates to or impacts
  - Note which task fields should be updated (notes, status, description)

---

## Draft Response Requirements

**MANDATORY:** Always generate a draft response for every email, indicating:

1. **Response type:**
   - Action required (reply needed)
   - Informational (no reply needed)
   - Follow-up needed
   - Archive/delete

2. **Content requirements:**
   - Follow Spanish email formatting rules (see `/shared/docs/agent-communication-rules.md`)
   - Use appropriate greeting based on time of day
   - Use "Saludos" for sign-off (never "Saludos cordiales" or other variations)
   - Never include NIF/NIE in email footers
   - Match language to recipient (Spanish, Catalan, English)

3. **Input needed section:**
   - List specific questions or information required
   - Indicate if draft is ready to send or needs refinement
   - Specify any missing details (dates, amounts, confirmations, etc.)

4. **Attachments:**
   - **MANDATORY:** Never send emails automatically when attachments are included
   - **MANDATORY:** Always show updated draft with attachment details before sending
   - Wait for explicit user confirmation before sending emails with attachments

5. **Reply behavior:**
   - **MANDATORY:** Always reply all (include all original recipients) unless user explicitly specifies otherwise
   - Include all recipients from original email (To, CC) in reply
   - Only reply to sender if user explicitly requests "reply to sender only"

---

## Post-Response Actions

**MANDATORY:** After sending a response:

1. **Save contacts** - Create or update contact records for all recipients of email replies
   - Check for existing contacts by name, email, or company
   - Create new contact or update existing contact with latest information
   - Follow contact persistence requirements (see `/shared/docs/agent-persistence-requirements.md`)
   - Extract all available contact details from email (name, email, phone, address, company, title)

2. **Update related tasks** - **MANDATORY:** Always check for and update related tasks when processing emails
   - Search for tasks related to email content (subject, sender, keywords, project names)
   - Update task notes/description with email outcomes, received documents, or status changes
   - Update task status if email completes a task milestone or delivers required items
   - Link email to task via notes (include email date, sender, key information)
   - Use `mcp_parquet_read_parquet` or `mcp_parquet_search_parquet` to find related tasks
   - **MANDATORY:** For emails with attachments related to tasks:
     - Move attachments to permanent location (e.g., `data/attachments/[category]/YYYY/`)
     - Create `task_attachments` record linking file to task (`task_id`, `local_path`, `name`, `content_type`, `size_bytes`)
     - Update task notes with permanent file path (not temporary `tmp/` location)
     - Link email record to task (via email record and task notes)
   - Examples:
     - Email contains requested document → Move to permanent location, create `task_attachments` record, update task notes with file path
     - Email responds to task question → Update task with answer/response
     - Email confirms meeting → Update related meeting/event task
     - Email from contractor with proposal → Move attachment, link to task, update task notes

3. **Archive thread** - Remove INBOX label from the email thread
   - Use `mcp_gmail_modify_email` with `removeLabelIds: ['INBOX']`
   - This applies to the entire thread automatically

4. **Move to next email** - Display next email in queue following same protocol

**Rationale:** Keeps inbox clean, ensures all responded emails are archived automatically, maintains contact database for future reference, and keeps tasks synchronized with email communications.

---

## Process Flow

```
1. Search inbox → Get list of emails
2. For each email:
   a. Read full email content
   b. Display: Header + Summary + Full Content
   c. Generate draft response
   d. Display: Draft Response + Input Needed
   e. Wait for user decision:
      - "send" → Send response → Archive thread → Next email
      - "modify" → User provides changes → Update draft → Wait for approval
      - "skip" → Archive/delete → Next email
      - User provides input → Refine draft → Wait for approval
3. Continue until inbox is empty or user stops
```

---

## Examples

### Example 1: Action Required Email

**Email 1 of 7**

**From:** Advisor Name | Example Advisory (advisor@example.com)  
**Subject:** RE: [redacted subject]  
**Date:** Mon, 22 Dec 2025 11:45:22 +0000

**Summary:** [redacted summary of a personal administrative matter]

**Full Content:**
```
[Complete email body]
```

**Draft Response:**
```
[redacted]

[redacted]

[redacted]

Saludos,
Mark Hendrickson
```

**Input needed:**
1. Confirm the dates (01.01.2026 to 14.01.2026) or specify different dates?
2. Any other details to include?

### Example 2: Informational Email

**Email 3 of 7**

**From:** noreply@expirationwarning.net  
**Subject:** ICANN ERRP for the domain humans.name  
**Date:** Sun, 21 Dec 2025 01:28:26 +0000 (UTC)

**Summary:** Domain expiration reminder for humans.name. Expires 2025-12-28. Renew through your provider.

**Full Content:**
```
[Complete email body]
```

**Draft Response:**
No response needed - informational notification.

**Input needed:**
1. Do you want to renew humans.name before Dec 28, 2025?
2. Archive this notification, or keep it for reference?

---

## Specialized Email Processing Workflows

**MANDATORY:** All email processing workflows are stored in structured format (`$DATA_DIR/email_workflows/email_workflows.parquet`) for queryable, scalable processing. This allows:

- **Dynamic workflow discovery** - Query workflows by trigger patterns (sender, subject, attachments)
- **Version control** - Workflow changes tracked via audit log
- **Linking** - Workflows can reference data types, schemas, and file paths
- **Validation** - Workflow definitions validated against schema
- **Scalability** - Add new workflows without modifying markdown files

**Agents should:**

1. **Query workflows** - Use `mcp_parquet_read_parquet` with filters matching email attributes (from, subject, attachments)
2. **Execute workflow steps** - Follow the `steps` JSON array definition from matching workflow
3. **Reference documentation** - Check `documentation_path` for detailed instructions

**Example Query:**
```python
# Find workflows matching email from @example.com with subject containing "NOMINA"
workflows = mcp_parquet_read_parquet(
    data_type="email_workflows",
    filters={
        "trigger_from_pattern": "@example.com",
        "trigger_subject_pattern": "NOMINA",
        "active": True
    }
)
```

### Example Advisory Payroll Emails (Nóminas)

**Workflow ID:** `payroll-workflow`  
**Reference:** Query `$DATA_DIR/email_workflows/email_workflows.parquet` for workflow_id = 'payroll-workflow'

**MANDATORY:** When processing emails from Example Advisory (any @example.com email) containing payroll documents (nóminas):

**Trigger Conditions:**
- From: Any email address ending in `@example.com`
- Subject: Contains "NOMINA" or "NÓMINA" or "nómina"
- Attachments: PDF files with payroll/payroll-related names

**Automatic Processing Steps (from workflow definition):**

1. **Download and analyze attachments** (per standard protocol)
   - Download all PDF attachments
   - Extract text from PDFs using `scripts/extract_pdf_text.py`

2. **Create/update contact record**
   - Use `mcp_parquet_upsert_record` or `read_parquet` + `update_records`/`add_record`
   - Filter by email address (e.g., `{'email': 'assistant@example.com'}`)
   - Update/create with:
     - Name, email, phone, address from email signature
     - Category: "Legal/Tax Services"
     - Platform: "Example Advisory"
     - Notes: Include role (e.g., "Handles payroll documentation, nóminas")
     - Update `last_contact_date` to current date

3. **Create email record**
   - Use `mcp_parquet_add_record` for `emails` data type
   - Link to contact via `contact_id`
   - Include all standard email fields
   - Mark `has_attachments: true` and `num_attachments: <count>`

4. **Extract payroll data from PDFs**
   - Parse text from extracted PDF content
   - Extract:
     - Employee name and NIF
     - Employer name
     - Pay period (start/end dates, days)
     - Gross amount (EUR)
     - Net amount (EUR)
     - SS base amount
     - IRPF amount
     - SS employee contribution
     - SS employer contribution

5. **Create/update company record (employer)**
   - Use `mcp_parquet_add_record` for `companies` data type
   - Type: "employer"
   - Extract company name and address from payroll document
   - Generate `company_id` if missing

6. **Create payroll_documents record**
   - Use `mcp_parquet_add_record` for `payroll_documents` data type
   - Include all extracted payroll data
   - Set file paths (temporary location initially)
   - Link to: `email_id`, `contact_id`, `company_id`

7. **Move files to permanent location**
   - Create directory: `data/attachments/payroll/YYYY/` (where YYYY is the year)
   - Move PDF files: `nomina_*.pdf` and `*_resumen.pdf`
   - Move text extraction files: `*.txt`
   - Update `payroll_documents` record with new file paths

8. **Generate draft acknowledgment email**
   - Standard acknowledgment: "Recibido. Gracias por enviar las nóminas de [month]."
   - Save draft to `/strategy/operations/admin/email-<sender-name>-nomina-<month>-draft.md`
   - Wait for user approval before sending

9. **Archive email after response**
   - Remove INBOX label after sending response (per standard protocol)

**Rationale:** Payroll documents are important tax records that need to be permanently stored, tracked, and linked to employers, contacts, and emails for easy retrieval during tax filing.

**Note:** This workflow is stored as a structured record in `$DATA_DIR/email_workflows/email_workflows.parquet`. For programmatic access, query by `workflow_id = 'payroll-workflow'`. The workflow definition includes all steps, data types, file paths, and templates as structured fields, making it queryable and maintainable without editing markdown files.

---

## Related Documentation

- `/shared/docs/agent-communication-rules.md` - Spanish email formatting, WhatsApp style, transaction language rules
- `/shared/docs/agent-context.md` - Agent context and quick reference (index to all rule documents)

---

