# Agent Workflow Specifics

**Purpose:** Workflow-specific rules for email processing and Amazon order extraction.

**Last Updated:** 2025-01-23

**Related:** `/shared/docs/agent-email-triage-protocol.md` for complete email triage workflow.

---

## Email Processing Workflows

**MANDATORY:** Always show the original message content when displaying emails (full quoted/reply content from email threads). See `/shared/docs/agent-email-triage-protocol.md` for complete display requirements.

### Purchase/Tracking Email Handling

- **MANDATORY:** Always record delivery status for purchases in `$DATA_DIR/purchases/purchases.parquet`
- Pull relevant purchase details from Gmail if needed to populate purchase records (order number, vendor, items, cost)
- Inform user of latest status update
- **SKIP email during inbox triage** - Process tracking/delivery notification emails automatically without displaying them in inbox triage flow
- Archive tracking emails after updating purchase records

### Domain Expiration Email Handling

- **MANDATORY:** Always check if domain is set to auto-renew before creating renewal task
- If auto-renewal is enabled, create email record but skip task creation
- If auto-renewal is disabled or unknown, create task with due date 4+ days before expiration
- Check domain provider settings (DNSimple, Squarespace, etc.) for auto-renewal status

### Email Workflow Structure

Email processing workflows are stored in structured format (`$DATA_DIR/email_workflows/email_workflows.parquet`) and can be queried dynamically:

```python
# Find matching workflows for an email
workflows = mcp_parquet_read_parquet(
    data_type="email_workflows",
    filters={
        "trigger_from_pattern": "@example.com",  # or use pattern matching
        "active": True
    }
)
```

**Workflow Structure:**
- `trigger_*_pattern` fields define when workflow applies
- `steps` JSON array defines processing steps
- `data_types_created/updated` lists which data types are affected
- `file_storage_path` defines where attachments are stored
- `draft_email_template` provides email template

**Reference:** `/shared/docs/agent-email-triage-protocol.md` for complete workflow execution guidance.

---

## Amazon Order Extraction via Gmail MCP

**Purpose:** Extract Amazon order details from Gmail emails and save to purchases data.

**No Script Required:** Use Gmail MCP tools directly via agent instructions.

### Process

**1. Search for Amazon Order Emails:**
- Use `mcp_gmail_search_emails` with Gmail search syntax:
  - Query examples:
    - `from:auto-confirm@amazon.es OR from:order-update@amazon.es subject:"pedido" OR subject:"order"`
    - `"order_number"` (e.g., `"XXX-XXXXXXX-XXXXXXX"`)
    - `"product_name"` (e.g., `"Help Flash"`)
- Gmail search operators supported:
  - `from:` - Sender email address
  - `subject:` - Subject line text
  - `OR` - Logical OR operator
  - Quoted strings for exact matches

**2. Read Email Content:**
- Use `mcp_gmail_read_email` with message ID from search results
- Extract order details from email body:
  - Order number (e.g., "Pedido n.º XXX-XXXXXXX-XXXXXXX")
  - Product name/description
  - Amount/total cost
  - Order date
  - Delivery date (from delivery confirmation emails)
  - Delivery address
  - Order URL

**3. Save to Purchases Data:**
- Use `mcp_parquet_add_record` with `data_type: "purchases"`
- Map email data to purchases schema:
  - `item_name`: Product name from email
  - `status`: "completed" (if delivered) or "in_progress" (if pending)
  - `location`: Delivery address/city
  - `vendor`: "Amazon.es" or "Amazon.com" (based on sender domain)
  - `actual_cost_usd`: Convert amount to USD if needed
  - `currency`: "EUR" or "USD" (from email)
  - `category`: Product category (infer from product name)
  - `created_date`: Order date from email
  - `completed_date`: Delivery date (if available)
  - `notes`: Include order number, delivery status, any relevant context
  - `import_date`: Current date
  - `import_source_file`: "gmail_email_parsing"

**4. Create Snapshot:**
- Always create timestamped snapshot before modifying purchases parquet file
- Format: `$DATA_DIR/snapshots/purchases-[YYYY-MM-DD-HHMMSS].parquet`

### Example Workflow

1. Search: `mcp_gmail_search_emails(query="from:auto-confirm@amazon.es subject:pedido", maxResults=10)`
2. Read: `mcp_gmail_read_email(messageId="<id_from_search>")`
3. Parse: Extract order number, product, amount, dates from email content
4. Save: `mcp_parquet_add_record(data_type="purchases", record={...})`

### Notes

- Amazon does not provide a public consumer API for order history
- Email parsing avoids ToS violations and uses existing Gmail MCP infrastructure
- Delivery confirmation emails provide delivery dates
- Order confirmation emails provide order placement dates and totals
- Multiple emails may exist for same order (confirmation, shipping, delivery)






