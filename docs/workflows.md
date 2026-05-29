# Key Workflows

## Financial Operations

### Quarterly Portfolio Reviews
- Liquidity regime scorecards
- Portfolio analysis and assessment
- Rebalancing decisions
- See `/strategy/operations/finance/quarterly-portfolio-review-process.md` for complete workflow

### Transaction Import
- Multi-bank CSV import with normalization and categorization
- Currency conversion to USD
- Automatic categorization and tagging
- See `/strategy/operations/finance/transaction-import-process.md` for procedures

### Data Import
- Unified import pipeline for all financial data types
- Schema validation and normalization
- Provenance tracking
- See `/strategy/operations/finance/data-import-process.md` for general procedures

### Rebalancing
- Three-axis liquidity-centered rebalancing framework
- Strategic allocation decisions
- Execution planning

## Task Management

### Asana Integration
- Import tasks with automatic domain classification
- Domain classification (finance, admin, work, health, social, other)
- Full metadata support (comments, attachments, stories/history, custom fields)
- See `/execution/scripts/ASANA_SYNC_SERVICE.md` for sync service documentation

### Bidirectional Sync
- Webhook-based real-time sync
- Polling-based backup sync between workspaces
- Intelligent field-level merging preserves local modifications
- See `/execution/scripts/ASANA_WEBHOOKS.md` for webhook setup

### Task Organization
- Domain classification for cross-domain reasoning
- Query views: Today, this week, high-benefit backlog, domain-specific
- Daily/weekly review workflows
- See `/strategy/operations/tasks-daily-review-process.md` for review procedures

### Background Services
- macOS LaunchAgent services for continuous sync
- Webhook processing
- Automated task processing

## PDF Automation

### Form Filling
- Programmatic PDF form completion
- Data-driven field population
- See `/strategy/reference/pdf-form-filler-usage.md` for usage guide

### Field Detection
- OCR-based automatic field position detection
- Interactive calibration GUI for manual tuning
- Visual field position verification
- See `/strategy/reference/pdf-form-filler-setup.md` for setup

## Conversation Tracking

### Automatic Per-Turn Tracking
- Automatic conversation storage after every agent response
- Hierarchical model: conversation (metadata) + agent_message (individual turns)
- Stored via Neotoma MCP with full provenance and entity resolution
- See `.cursor/rules/conversation_tracking.mdc` for complete specification

### Query Conversations
```typescript
// Get today's conversations
mcp_neotoma_retrieve_entities({
  entity_type: "conversation",
  filters: { start_timestamp: "2026-01-29" }
})

// Get conversation with messages
conv = mcp_neotoma_retrieve_entity_snapshot({entity_id: conversation_id})
messages = mcp_neotoma_retrieve_graph_neighborhood({
  entity_id: conversation_id,
  relationship_types: ["PART_OF"]
})

// Get timeline view
mcp_neotoma_list_timeline_events({
  entity_type: "agent_message",
  filters: { conversation_id: conversation_id }
})
```

### Graph Exploration
- Explore conversation relationships to tasks, contacts, projects
- Use `retrieve_graph_neighborhood` to discover related entities
- Timeline queries for chronological conversation history

## Data Access

### MCP Server
- Programmatic read/write access via Model Context Protocol
- Semantic search with embeddings
- Query with enhanced filter operators
- See `/mcp/README.md` for complete documentation

### Query Scripts
- Command-line tools for data exploration and analysis
- Domain-specific queries
- Summary and aggregation views

### Schema Discovery
- Automatic schema introspection for all 60+ data types
- Type information and validation rules
- Field descriptions and constraints

### Audit Trail
- Complete change history with rollback capabilities
- Provenance tracking from source to derived data
- Timestamped snapshots for recovery

## Additional Workflows

- **Audio Transcription**: Automated transcription of voice memos with file watching
  - See `/strategy/operations/audio-transcription-process.md` for workflow
- **Contact Management**: Gmail and Minted.com integration for contact extraction
- **Strategic Planning**: Quarterly reviews, decision frameworks, execution planning
- **External System Sync**: Real-time sync with Asana, Gmail, Google Calendar, and other services

