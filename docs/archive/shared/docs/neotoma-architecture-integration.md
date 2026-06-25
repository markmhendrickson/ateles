# Neotoma Architecture Integration Plan

**Status:** Active  
**Last Updated:** 2025-01-15  
**Related:** `/strategy/strategy/neotoma-strategy.md`, `/README.md`

---

## Overview

This repository serves as the implementation ground for initial versions of the **Strategy Layer** and **Execution Layer** as defined in the [Neotoma architecture](https://github.com/markmhendrickson/neotoma). The data layer will initially use Parquet MCP, with a planned migration to Neotoma for enhanced capabilities.

## Three-Layer Architecture

Neotoma defines a three-layer architecture for AI memory and automation:

### 1. Strategy Layer (This Repository - Initial Implementation)

**Position:** Above the Truth Layer  
**Purpose:** Systems that read typed memory and produce plans, strategies, and decisions

**Current Implementation:**
- **Strategy documents** (`/strategy/strategy/`) - Long-term principles and goals by domain
- **Tactics documents** (`/strategy/tactics/`) - Methods and approaches to achieve strategy
- **Operations documents** (`/strategy/operations/`) - Execution procedures organized by domain
- **Decision-making frameworks** - Deterministic protocols for decisions across domains
- **Portfolio reasoning** - Liquidity regime analysis, rebalancing frameworks
- **Planning agents** - Task prioritization, workflow orchestration

**Key Capabilities:**
- Read structured data via MCP (currently Parquet MCP, future Neotoma)
- Generate strategic plans and tactical approaches
- Execute quarterly reviews, scorecards, and analysis
- Produce execution plans and workflows
- Maintain canonical strategy/tactics/operations hierarchy

### 2. Truth Layer (Neotoma - Future Migration Target)

**Position:** Middle layer (data/memory substrate)  
**Purpose:** Typed, event-sourced memory with full provenance and entity resolution

**Current State:** Using Parquet MCP as interim solution  
**Target State:** Migrate to Neotoma for enhanced capabilities

**Neotoma Benefits:**
- **Entity resolution** - Canonical IDs across all personal data (hash-based)
- **Event sourcing** - Complete audit trail with immutable history
- **Provenance** - Full traceability from source documents to derived insights
- **Graph relationships** - Entity graph with neighborhoods and relationship edges
- **Timeline awareness** - Automatic chronological ordering from date fields
- **Schema evolution** - Versioned schemas with backward compatibility
- **Cross-domain joins** - Unified entity model across finance, admin, work, health
- **Deterministic workflows** - Repeatable outputs with evidence
- **Cryptographic integrity** - Hash-based entity IDs and event chaining

**Reference:** [Neotoma Repository](https://github.com/markmhendrickson/neotoma) - See architecture docs for complete Truth Layer specification

### 3. Execution Layer (This Repository - Initial Implementation)

**Position:** Below the Truth Layer  
**Purpose:** Systems that carry out actions based on strategies and plans

**Current Implementation:**
- **Automation scripts** (`/execution/scripts/`) - Data processing, imports, exports
- **PDF form automation** - Programmatic form filling with OCR
- **Task management** - Asana integration, bidirectional sync
- **Transaction execution** - Wise transfers, crypto payments
- **Data import pipelines** - Multi-source data normalization
- **Background services** - Webhooks, sync, file watching

**Key Capabilities:**
- Execute financial transactions (Wise, crypto)
- Fill and submit forms (PDF automation)
- Sync external systems (Asana, Gmail, Minted)
- Process and import data from multiple sources
- Run scheduled workflows and background services

## Data Layer Evolution

### Phase 1: Parquet MCP (Current)

**Status:** Active  
**Implementation:** `/truth/mcp-servers/parquet/parquet_mcp_server.py`

**Capabilities:**
- Read/write access to 60+ normalized data types
- Query with enhanced filters ($contains, $fuzzy, $gt, $lt, etc.)
- Semantic search via embeddings
- Audit logging with rollback support
- Schema discovery and validation
- Automatic snapshots before modifications

**Limitations:**
- No entity resolution across data types
- No graph relationships or cross-domain joins
- Limited provenance tracking (audit logs only)
- No timeline reconstruction
- Manual schema evolution

**Rationale:** Provides immediate structured data access while Neotoma development continues. Enables Strategy and Execution layer development without blocking on Truth Layer completion.

### Phase 2: Neotoma Migration (Planned)

**Status:** Planned  
**Target:** Migrate data layer to Neotoma when v1.0.0 is ready

**Migration Benefits:**
- **Unified entity model** - Single canonical ID system across all domains
- **Relationship graph** - Automatic relationship detection and traversal
- **Event sourcing** - Complete immutable history with time-travel queries
- **Provenance chains** - Full traceability from source to derived insights
- **Timeline reconstruction** - Automatic chronological ordering across all data
- **Schema evolution** - Versioned schemas with migration protocols
- **Cross-domain intelligence** - Automatic joins across finance, admin, work, health
- **Deterministic workflows** - Repeatable outputs with evidence trails

**Migration Strategy:**
1. **Parallel operation** - Run both Parquet MCP and Neotoma MCP during transition
2. **Data migration** - Import existing parquet data into Neotoma's Record → Entity → Observation → Snapshot model
3. **Entity resolution** - Generate canonical entity IDs for existing records
4. **Relationship mapping** - Build entity graph from existing relationships
5. **Gradual cutover** - Migrate Strategy and Execution layer access to Neotoma MCP
6. **Validation** - Verify data integrity and query correctness
7. **Deprecation** - Retire Parquet MCP once migration is complete

**Reference:** See `/strategy/strategy/neotoma-strategy.md` for Neotoma implementation status and roadmap

## Implementation Status

### Strategy Layer

**Status:** Active  
**Coverage:**
- ✅ Financial strategy, tactics, and operations
- ✅ Administrative workflows
- ✅ Work and professional workflows
- ✅ Health and fitness workflows
- ✅ Decision-making frameworks
- ✅ Quarterly portfolio review process
- ✅ Liquidity regime scorecards

**Data Access:** Currently via Parquet MCP, planned migration to Neotoma

### Execution Layer

**Status:** Active  
**Coverage:**
- ✅ Data import pipelines (transactions, holdings, income, tasks, contacts)
- ✅ PDF form automation
- ✅ Asana bidirectional sync
- ✅ Gmail contact extraction
- ✅ Transaction execution (Wise, crypto)
- ✅ Background services (webhooks, file watching)

**Data Access:** Currently via Parquet MCP, planned migration to Neotoma

### Truth Layer

**Status:** Development (Neotoma repository)  
**Current:** Parquet MCP as interim solution  
**Target:** Neotoma v1.0.0 (target ~2026-02-24 per Neotoma roadmap)

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────┐
│              Strategy Layer (This Repo)                  │
│  • Strategy/Tactics/Operations documents                 │
│  • Decision-making frameworks                           │
│  • Portfolio reasoning & planning                       │
│  • Quarterly reviews & scorecards                        │
└──────────────────────┬──────────────────────────────────┘
                      │ Reads typed memory
                      │ Produces plans
                      ▼
┌─────────────────────────────────────────────────────────┐
│              Truth Layer (Neotoma)                      │
│  • Record → Entity → Observation → Snapshot            │
│  • Entity resolution & graph relationships             │
│  • Event sourcing & provenance                         │
│  • Timeline reconstruction                             │
│  • Cross-domain joins                                  │
└──────────────────────┬──────────────────────────────────┘
                      │ Provides data
                      │ Receives actions
                      ▼
┌─────────────────────────────────────────────────────────┐
│              Execution Layer (This Repo)                │
│  • Automation scripts                                   │
│  • PDF form filling                                    │
│  • Transaction execution                               │
│  • External system sync                                 │
│  • Background services                                 │
└─────────────────────────────────────────────────────────┘
```

## Data Flow

### Current (Parquet MCP)

```
Strategy Layer → Parquet MCP → data/[type]/[type].parquet
Execution Layer → Parquet MCP → data/[type]/[type].parquet
```

### Future (Neotoma)

```
Strategy Layer → Neotoma MCP → Neotoma (Postgres/JSONB)
Execution Layer → Neotoma MCP → Neotoma (Postgres/JSONB)
```

## Migration Timeline

**Phase 1 (Current):** Strategy and Execution layers using Parquet MCP
- ✅ Strategy layer operational
- ✅ Execution layer operational
- ✅ Parquet MCP providing data access

**Phase 2 (Q1 2026):** Neotoma v1.0.0 readiness
- Neotoma MVP completion (target ~2026-02-24)
- Structured personal data memory
- Dual-path ingestion
- Entity resolution
- Timelines
- Cross-platform MCP access

**Phase 3 (Q2 2026):** Migration planning
- Data migration scripts
- Entity resolution mapping
- Relationship graph construction
- Parallel operation setup
- Validation framework

**Phase 4 (Q2-Q3 2026):** Gradual migration
- Import existing parquet data to Neotoma
- Generate canonical entity IDs
- Build entity graph
- Migrate Strategy layer to Neotoma MCP
- Migrate Execution layer to Neotoma MCP
- Validate data integrity

**Phase 5 (Q3 2026):** Cutover and deprecation
- Full cutover to Neotoma MCP
- Deprecate Parquet MCP
- Archive parquet files as historical reference

## Key Documents

- **Strategy:** `/strategy/strategy/neotoma-strategy.md` - Neotoma product strategy
- **Architecture:** This document - Three-layer architecture and migration plan
- **Data Access:** `/truth/mcp-servers/parquet/README.md` - Current Parquet MCP documentation
- **Agent Context:** `/shared/docs/agent/context.md` - Process requirements and workflows

## References

- **Neotoma Repository:** https://github.com/markmhendrickson/neotoma
- **Neotoma Architecture:** https://github.com/markmhendrickson/neotoma (see docs/architecture/)
- **Neotoma Truth Model:** Record → Entity → Observation → Snapshot (see neotoma docs)
- **MCP Protocol:** Model Context Protocol for AI tool integration

---

**Note:** This plan aligns with Neotoma's positioning as the Truth Layer for AI Memory, with this repository implementing the Strategy and Execution layers that sit above and below it, respectively.







