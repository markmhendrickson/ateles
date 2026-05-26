---
name: extract-amazon-order
description: "Extract an Amazon order from Gmail and store as a purchase record. Use when user says \\"extract Amazon order\\", \\"get order from email\\", or \\"record Amazon purchase from Gmail\\". Can be invoked via /extract-amazon-order."
triggers:
  - extract Amazon order
  - get order from email
  - record Amazon purchase from Gmail
  - extract-amazon-order
user_invocable: true
entity_id: ent_fb08a1386e8bb950b505c442
---

# Extract Amazon Order

Search Gmail for Amazon order emails, extract order details, and store as a purchase in Neotoma first (then Parquet if purchases still use Parquet). Create a snapshot before any Parquet write.

## When to Use

Use this skill when:
- User says "extract Amazon order", "get order from email", "record Amazon purchase from Gmail"
- User wants to capture an Amazon order from email (standalone or during email triage)

## Required Documents (load first)

1. **Amazon order extraction:** [docs/workflow_specifics_rules.mdc](docs/workflow_specifics_rules.mdc) (Amazon Order Extraction)
2. **Storage and snapshot:** [docs/neotoma_parquet_migration_rules.mdc](docs/neotoma_parquet_migration_rules.mdc) (Neotoma first; snapshot before Parquet modification when required)
3. **Purchases schema:** `$DATA_DIR/schemas/` for purchases if needed (item_name, status, vendor, etc.)

## Workflow

1. **Search Gmail:** Use Gmail MCP search (e.g. `search_emails`) with query such as `from:auto-confirm@amazon.es OR from:order-update@amazon.es subject:"pedido"`, or by order number/product name as appropriate.
2. **Read message:** Use Gmail MCP read (e.g. `read_email`) with message ID. Extract: order number, product, amount, order date, delivery date, address, URL.
3. **Store in Neotoma first:** Use Neotoma MCP to store purchase entity (entity_type appropriate to schema). Include all extracted fields.
4. **If purchases still in Parquet:** Use `mcp_parquet_add_record(data_type="purchases", record={...})`. Map to schema: item_name, status ("completed"/"in_progress"), location, vendor ("Amazon.es"/"Amazon.com"), actual_cost_usd, currency, category, created_date, completed_date, notes (order number, status), import_date, import_source_file ("gmail_email_parsing").
5. **Snapshot before Parquet write:** Create timestamped snapshot `$DATA_DIR/snapshots/purchases-YYYY-MM-DD-HHMMSS.parquet` before modifying purchases parquet if the workflow requires it per repo rules.

## Constraints

- Store in Neotoma first; do not write only to Parquet for user/agent-captured data.
- Create snapshot before modifying purchases parquet when Parquet is used.
- No Amazon consumer API; use email parsing only. Multiple emails per order (confirmation, shipping, delivery) are possible.

## Related Rules

- [docs/workflow_specifics_rules.mdc](docs/workflow_specifics_rules.mdc) — Amazon Order Extraction
- [docs/neotoma_parquet_migration_rules.mdc](docs/neotoma_parquet_migration_rules.mdc) — Write path, snapshot requirements
- [docs/data_rules.mdc](docs/data_rules.mdc) — Parquet snapshot management when applicable
