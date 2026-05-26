---
name: email-triage
description: "Step-by-step email inbox triage workflow with draft generation, data persistence, and archiving. Use when processing emails, triaging inbox, or when user mentions email triage, inbox processing, or email workflow."
triggers:
  - triage inbox
  - process emails
  - email workflow
  - go through inbox
  - handle emails
  - email triage
  - inbox processing
user_invocable: true
entity_id: ent_e3f5f239427961d2f4608208
---

# Email Triage

Process inbox emails one at a time with structured display, draft responses, data persistence, and archiving.

## When to Use

Use this skill when:
- User mentions "triage inbox", "process emails", "email workflow"
- Context suggests email processing (multiple emails to review)
- User asks to "go through inbox" or "handle emails"
- Working with Gmail MCP tools to process inbox

## Workflow

Process emails **one at a time** following this sequence:

### 1. Search inbox
Retrieve emails from inbox (unread or all).

### 2. Display email (MANDATORY format)

**Email Header:**
- Email number (e.g., "Email 1 of 7")
- From: Sender name and email address
- Subject: Full subject line
- Date: Full date and time

**Email Content:**
- Summary: Brief summary of content and action needed
- Full Content: Complete email body (including thread history)
- Original Message: **MANDATORY** - Always show the original message being replied to or forwarded
- Attachments: **MANDATORY** - Download and analyze all attachments; extract and summarize key information

**Draft Response:**
- Always generate a draft (even if no response needed)
- For informational: "No response needed - informational notification"
- For action items: Draft following `/docs/communication_rules.mdc`

**Input Needed:**
- Explicitly list what information is required to finalize/refine

**Data Object Recommendations:**
- Always recommend data objects to save/update (transactions, contacts, tasks, events, etc.)
- Specify fields that can be extracted and actions available
- Check for related tasks that should be updated

### 3. Wait for user decision
User approves, modifies, or provides input.

### 4. Send response (if applicable)
Send approved response.

**Attachment handling:**
- **NEVER** send emails automatically when attachments included
- Always show updated draft with attachment details before sending
- Wait for explicit user confirmation

**Reply behavior:**
- **ALWAYS** reply all (include all original recipients) unless user explicitly specifies otherwise
- Only reply to sender if user requests "reply to sender only"

### 5. Post-response actions (MANDATORY)

After sending a response:

**a. Save contacts:**
- Create or update contact records for all recipients
- Check for existing by name, email, company
- Extract all details (name, email, phone, address, company, title)
- Follow `/docs/persistence_rules.mdc`

**b. Update related tasks:**
- Search for tasks related to email (subject, sender, keywords, project names)
- Update task notes with email outcomes, documents, status changes
- Update task status if email completes milestone
- Link email to task via notes
- **For emails with attachments:**
  - Move attachments to permanent location (`data/attachments/[category]/YYYY/`)
  - Create `task_attachments` record
  - Update task notes with permanent file path
- Per `/docs/neotoma_parquet_migration_rules.mdc`: query Neotoma first for tasks; if not found, use `mcp_parquet_read_parquet` or `mcp_parquet_search_parquet`

**c. Create follow-up task (for important emails):**
- Insurance claims, reimbursements, financial matters
- Legal/contractual communications
- Requests for information from third parties
- Applications, submissions, formal requests
- Any email where completion depends on external response

Follow-up timing:
- Response expected 2-3 days → due 5-7 days
- Response expected 1 week → due 10-14 days
- No stated time → due 7-10 days

Task requirements:
- Title: "Follow up on [subject/topic]"
- Domain: Match email domain
- Priority: Match email importance
- Status: `pending`
- Notes: Email ID, date, recipient, expected response time, action plan
- Link to parent task if applicable

**d. Archive thread:**
- Remove INBOX label: `mcp_gmail_modify_email` with `removeLabelIds: ['INBOX']`
- Applies to entire thread automatically

### 6. Move to next email
Display next email following same protocol.

## Draft Response Requirements

**MANDATORY:** Always generate draft response indicating:

1. **Response type:**
   - Action required (reply needed)
   - Informational (no reply needed)
   - Follow-up needed
   - Archive/delete

2. **Content requirements:**
   - Follow Spanish email formatting (see `/docs/communication_rules.mdc`)
   - Use time-appropriate greeting
   - Use "Saludos" for sign-off (never "Saludos cordiales")
   - Never include NIF/NIE in footers
   - Match language to recipient (Spanish, Catalan, English)

3. **Input needed:**
   - List specific questions or required information
   - Indicate if ready to send or needs refinement
   - Specify missing details (dates, amounts, confirmations)

## Constraints

- **MUST** process emails one at a time
- **MUST** display in required format (header, content, draft, input needed, data recommendations)
- **MUST** show original message
- **MUST** download and analyze attachments
- **MUST** generate draft for every email
- **MUST** save contacts after response
- **MUST** update related tasks
- **MUST** create follow-up task for important emails
- **MUST** archive after responding
- **MUST** reply all unless explicitly told otherwise
- **MUST NOT** send emails with attachments automatically

## Related Documentation

- `/docs/communication_rules.mdc` — Email formatting, language rules
- `/docs/persistence_rules.mdc` — Contact and data persistence
- `/docs/email_triage_protocol_rules.mdc` — Full protocol with examples
