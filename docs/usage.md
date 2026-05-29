# Usage Guide

## Data Import

```bash
# Install dependencies
cd execution/scripts && pip install -r requirements.txt

# Import transactions
python execution/scripts/import_data.py transactions file.csv --source bank_name --currency EUR

# Import holdings
python execution/scripts/import_data.py holdings portfolio.csv --source broker_name

# Import tasks from Asana (direct API → parquet with intelligent merge)
python execution/scripts/import_asana_tasks.py

# Sync tasks bidirectionally (webhooks + polling)
python execution/scripts/sync_asana_tasks.py

# Start webhook server for real-time Asana sync
python execution/scripts/asana_webhook_server.py --port 8080

# Import contacts from Gmail (via MCP integration)
python execution/scripts/import_gmail_contacts.py

# Import data from Notion export
python execution/scripts/import_notion.py
```

## Data Query

```bash
# Query transactions
python execution/scripts/query_transactions.py --summary

# Query tasks
python execution/scripts/query_tasks.py today
python execution/scripts/query_tasks.py this_week --domain finance

# Query purchases
python execution/scripts/query_purchases.py --status pending
```

## MCP Server

The MCP server provides programmatic access to all 60+ data types with read, query, add, update, delete, semantic search, and audit log capabilities. Configure in Cursor or Claude Desktop:

```json
{
  "mcpServers": {
    "parquet": {
      "command": "python",
      "args": ["/path/to/mcp/parquet/parquet_mcp_server.py"]
    }
  }
}
```

### Features

- **Query with filters**: Enhanced operators ($contains, $starts_with, $fuzzy, $gt, $lt, $in, etc.)
- **Semantic search**: Embedding-based similarity search across text fields
- **Audit logging**: Complete change history with rollback support
- **Schema discovery**: Automatic introspection for all data types
- **Statistics**: Basic stats and record counts

See `/mcp/README.md` for complete MCP servers documentation.

## PDF Form Automation

```bash
# Fill PDF form
python execution/scripts/fill-pdf-form.py --template form.pdf --data data.json --output filled.pdf

# Detect form fields automatically
python execution/scripts/pdf-field-detector.py --template form.pdf --output positions.json

# Interactive field calibration
python execution/scripts/pdf-interactive-calibrator.py --template form.pdf --data data.json --output positions.json
```

## Additional Resources

- **Script Documentation**: `/execution/scripts/README.md` - Complete script documentation and examples
- **MCP Servers**: `/mcp/README.md` - MCP server configuration and usage
- **Asana Integration**: `/execution/scripts/ASANA_SYNC_SERVICE.md` - Asana sync service documentation
- **Webhooks**: `/execution/scripts/ASANA_WEBHOOKS.md` - Webhook setup and configuration

