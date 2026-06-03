# Shared Resources

## Purpose

The Shared directory contains cross-cutting resources used by all three layers (Strategy, Execution, and Truth). This includes documentation, policies, agent rules, and historical archives.

## Structure

- **`docs/`** - Documentation, policies, guides, and agent rules
  - **`agent-*.md`** - Agent policy documents (9 policy files)
  - **`data-imports-policy.md`** - Data imports directory policy
  - **`neotoma-architecture-integration.md`** - Three-layer architecture documentation
  - **`credential-management.md`** - Credential management guide
  - **`README.md`** - Documentation index

- **`archive/`** - Historical documents and completed analyses

## Key Documents

### Agent Policies (9 Documents)
1. **`agent-mcp-access-policy.md`** - MCP-only data access requirements (MANDATORY)
2. **`agent-data-entry-requirements.md`** - Data entry, schema evolution, file-backed types
3. **`agent-confirmation-requirements.md`** - Email/transaction confirmation rules
4. **`agent-communication-rules.md`** - Spanish email, WhatsApp style, transaction language
5. **`agent-email-triage-protocol.md`** - Email triage workflow, draft responses
6. **`agent-persistence-requirements.md`** - Instruction, contact, email persistence
7. **`agent-workflow-requirements.md`** - Scorecard saving, reports, file naming
8. **`agent-decision-framework.md`** - Decision-making framework, behavioral compliance
9. **`agent-communication-rules.md`** - Communication standards and formats

### Architecture & Integration
- **`neotoma-architecture-integration.md`** - Three-layer architecture (Strategy/Truth/Execution), Neotoma migration plan
- **`data-imports-policy.md`** - Policy for `$DATA_DIR/imports/` (read-only archive rules)

### Operational Guides
- **`credential-management.md`** - 1Password CLI setup, credential management
- **`generic-pdf-form-filler-guide.md`** - PDF automation guide
- **`outcome-based-organization.md`** - Task organization by outcomes
- **`task-organization-analysis.md`** - Task management analysis

### Integration Analyses
- **`asana-api-coverage-analysis.md`** - Asana API integration details
- **`asana-project-fields-analysis.md`** - Asana project field analysis
- **`asana-projects-import-plan.md`** - Asana project import strategy

## Agent Pre-Task Checklist

**YOU MUST COMPLETE THESE STEPS BEFORE STARTING ANY TASK:**

1. ✅ Read `/docs/agent/context.md` - Essential context (index to all rule documents)
2. ✅ Read `/docs/data-imports-policy.md` - Data imports rules
3. ✅ Review relevant `/docs/agent/*.md` policy documents

## Relationship to Layers

**Used by:** All three layers (Strategy, Execution, Truth)  
**Contains:** Cross-cutting documentation, policies, guides  
**Purpose:** Shared knowledge and rules applicable across all layers

## Archive

The `archive/` directory contains:
- Historical documents
- Deprecated strategies
- Completed analyses
- Migration notes
- Outdated reference materials

## Architecture

Part of the three-layer architecture supporting all layers:
- **Strategy Layer** - References docs for decision-making
- **Execution Layer** - References docs for automation rules
- **Truth Layer** - References docs for data policies
- **Shared** (this directory) - Cross-cutting resources

See `docs/neotoma-architecture-integration.md` for complete architecture documentation.





## Purpose

The Shared directory contains cross-cutting resources used by all three layers (Strategy, Execution, and Truth). This includes documentation, policies, agent rules, and historical archives.

## Structure

- **`docs/`** - Documentation, policies, guides, and agent rules
  - **`agent-*.md`** - Agent policy documents (9 policy files)
  - **`data-imports-policy.md`** - Data imports directory policy
  - **`neotoma-architecture-integration.md`** - Three-layer architecture documentation
  - **`credential-management.md`** - Credential management guide
  - **`README.md`** - Documentation index

- **`archive/`** - Historical documents and completed analyses

## Key Documents

### Agent Policies (9 Documents)
1. **`agent-mcp-access-policy.md`** - MCP-only data access requirements (MANDATORY)
2. **`agent-data-entry-requirements.md`** - Data entry, schema evolution, file-backed types
3. **`agent-confirmation-requirements.md`** - Email/transaction confirmation rules
4. **`agent-communication-rules.md`** - Spanish email, WhatsApp style, transaction language
5. **`agent-email-triage-protocol.md`** - Email triage workflow, draft responses
6. **`agent-persistence-requirements.md`** - Instruction, contact, email persistence
7. **`agent-workflow-requirements.md`** - Scorecard saving, reports, file naming
8. **`agent-decision-framework.md`** - Decision-making framework, behavioral compliance
9. **`agent-communication-rules.md`** - Communication standards and formats

### Architecture & Integration
- **`neotoma-architecture-integration.md`** - Three-layer architecture (Strategy/Truth/Execution), Neotoma migration plan
- **`data-imports-policy.md`** - Policy for `$DATA_DIR/imports/` (read-only archive rules)

### Operational Guides
- **`credential-management.md`** - 1Password CLI setup, credential management
- **`generic-pdf-form-filler-guide.md`** - PDF automation guide
- **`outcome-based-organization.md`** - Task organization by outcomes
- **`task-organization-analysis.md`** - Task management analysis

### Integration Analyses
- **`asana-api-coverage-analysis.md`** - Asana API integration details
- **`asana-project-fields-analysis.md`** - Asana project field analysis
- **`asana-projects-import-plan.md`** - Asana project import strategy

## Agent Pre-Task Checklist

**YOU MUST COMPLETE THESE STEPS BEFORE STARTING ANY TASK:**

1. ✅ Read `/docs/agent/context.md` - Essential context (index to all rule documents)
2. ✅ Read `/docs/data-imports-policy.md` - Data imports rules
3. ✅ Review relevant `/docs/agent/*.md` policy documents

## Relationship to Layers

**Used by:** All three layers (Strategy, Execution, Truth)  
**Contains:** Cross-cutting documentation, policies, guides  
**Purpose:** Shared knowledge and rules applicable across all layers

## Archive

The `archive/` directory contains:
- Historical documents
- Deprecated strategies
- Completed analyses
- Migration notes
- Outdated reference materials

## Architecture

Part of the three-layer architecture supporting all layers:
- **Strategy Layer** - References docs for decision-making
- **Execution Layer** - References docs for automation rules
- **Truth Layer** - References docs for data policies
- **Shared** (this directory) - Cross-cutting resources

See `docs/neotoma-architecture-integration.md` for complete architecture documentation.




