# Agent Workflow Specifics

**Purpose:** Workflow-specific rules for email processing and Amazon order extraction.

**Email Processing:** Always show original message content (full quoted/reply). See `/shared/docs/agent/rules/email-triage-protocol.md`.

**Purchase/Tracking Emails:** Record delivery status in `$DATA_DIR/purchases/purchases.parquet`. Pull details from Gmail. SKIP during inbox triage - process automatically, archive after updating.

**Domain Expiration Emails:** Check auto-renewal status before creating task. If enabled, create email record only. If disabled/unknown, create task 4+ days before expiration.

**Email Workflows:** Stored in `$DATA_DIR/email_workflows/email_workflows.parquet`. Query via MCP with `trigger_*_pattern`, `steps`, `data_types_created/updated`, `file_storage_path`, `draft_email_template` fields.

**Reference:** `/shared/docs/agent/rules/email-triage-protocol.md` for complete workflow execution.

---

## Amazon Order Extraction

**Process:** Use Gmail MCP tools directly (no script required).

1. **Search:** `mcp_gmail_search_emails` with Gmail syntax (`from:auto-confirm@amazon.es OR from:order-update@amazon.es subject:"pedido"`, `"order_number"`, `"product_name"`)

2. **Read:** `mcp_gmail_read_email` with message ID. Extract: order number, product, amount, order date, delivery date, address, URL.

3. **Save:** `mcp_parquet_add_record(data_type="purchases")`. Map to schema: `item_name`, `status` ("completed"/"in_progress"), `location`, `vendor` ("Amazon.es"/"Amazon.com"), `actual_cost_usd`, `currency`, `category`, `created_date`, `completed_date`, `notes` (order number, status), `import_date`, `import_source_file` ("gmail_email_parsing").

4. **Snapshot:** Create timestamped snapshot before modifying: `$DATA_DIR/snapshots/purchases-[YYYY-MM-DD-HHMMSS].parquet`

**Notes:** No Amazon consumer API. Email parsing avoids ToS violations. Multiple emails per order (confirmation, shipping, delivery).

