# Agent Instructions Index

**Purpose:** Central reference for all AI agent instruction files and their locations.

**Last Updated:** 2025-12-25

---

## Primary Agent Configuration

### `.cursorrules` (Root)
**Location:** `/Users/markmhendrickson/repos/ateles/.cursorrules`  
**Purpose:** Main Cursor AI agent behavior configuration  
**Scope:** Repository-wide agent rules, data access patterns, file organization  
**Status:** Active - primary agent instruction file

---

## Agent Context and Reference

### `agent-context.md`
**Location:** `shared/docs/agent-context.md`  
**Purpose:** Essential context and quick reference for agents  
**Contains:**
- Mandatory pre-task checklist
- Data query decision tree
- Path references for all layers
- Quick reference to policy documents
- Data entry requirements summary

**Status:** Active - required reading before any task

---

## Agent Policy Documents

All agent policy documents are located in `shared/docs/`:

### Core Policies

1. **`agent-mcp-access-policy.md`**
   - MCP server access rules
   - When to use MCP vs direct file access
   - Security and credential handling

2. **`agent-data-entry-requirements.md`**
   - Data entry standards
   - Schema compliance
   - Required fields and validation

3. **`agent-confirmation-requirements.md`**
   - When confirmation is required
   - User approval workflows
   - Risk assessment criteria

4. **`agent-communication-rules.md`**
   - Communication style and tone
   - Response formatting
   - User interaction patterns

5. **`agent-persistence-requirements.md`**
   - When to persist data
   - Data storage locations
   - Snapshot requirements

6. **`agent-workflow-requirements.md`**
   - Workflow execution patterns
   - Task organization
   - Process documentation

7. **`agent-decision-framework.md`**
   - Decision-making guidelines
   - When to ask vs act
   - Risk-based decision trees

### Specialized Protocols

8. **`agent-email-triage-protocol.md`**
   - Email processing rules
   - Triage criteria
   - Action workflows

---

## Related Documentation

### Data Policies
- **`data-imports-policy.md`** - Rules for `$DATA_DIR/imports/` (read-only archive)

### Repository Structure
- **`/docs/repository-structure.md`** - Repository structure documentation

---

## Usage

### For Agents
1. **Always start with:** `shared/docs/agent-context.md`
2. **Reference policies as needed:** Check relevant `shared/docs/agent-*.md` files
3. **Follow `.cursorrules`:** Repository-wide behavior rules

### For Users
- All agent instructions are version-controlled in git
- Changes to agent behavior should be documented in commit messages
- Policy documents can be updated independently of code

---

## File Locations Summary

```
personal/
├── .cursorrules                                    # Main agent config (root)
├── strategy/
│   └── reference/
        └── agent-context.md                        # Agent context reference
└── shared/
    └── docs/
        ├── agent-mcp-access-policy.md              # MCP access rules
        ├── agent-data-entry-requirements.md        # Data entry standards
        ├── agent-confirmation-requirements.md      # Confirmation rules
        ├── agent-communication-rules.md            # Communication style
        ├── agent-persistence-requirements.md        # Data persistence
        ├── agent-workflow-requirements.md          # Workflow patterns
        ├── agent-decision-framework.md              # Decision guidelines
        └── agent-email-triage-protocol.md           # Email processing
```

---

## Maintenance

- **Update frequency:** As agent behavior requirements evolve
- **Version control:** All files tracked in git
- **Backup:** Git repository serves as backup
- **Review:** Periodic review of agent behavior effectiveness

---

## Notes

- All agent instruction files are in `shared/docs/` for cross-layer access
- `.cursorrules` remains at root as required by Cursor IDE
- Policy documents can be referenced independently or as a set





**Purpose:** Central reference for all AI agent instruction files and their locations.

**Last Updated:** 2025-12-25

---

## Primary Agent Configuration

### `.cursorrules` (Root)
**Location:** `/Users/markmhendrickson/repos/ateles/.cursorrules`  
**Purpose:** Main Cursor AI agent behavior configuration  
**Scope:** Repository-wide agent rules, data access patterns, file organization  
**Status:** Active - primary agent instruction file

---

## Agent Context and Reference

### `agent-context.md`
**Location:** `shared/docs/agent-context.md`  
**Purpose:** Essential context and quick reference for agents  
**Contains:**
- Mandatory pre-task checklist
- Data query decision tree
- Path references for all layers
- Quick reference to policy documents
- Data entry requirements summary

**Status:** Active - required reading before any task

---

## Agent Policy Documents

All agent policy documents are located in `shared/docs/`:

### Core Policies

1. **`agent-mcp-access-policy.md`**
   - MCP server access rules
   - When to use MCP vs direct file access
   - Security and credential handling

2. **`agent-data-entry-requirements.md`**
   - Data entry standards
   - Schema compliance
   - Required fields and validation

3. **`agent-confirmation-requirements.md`**
   - When confirmation is required
   - User approval workflows
   - Risk assessment criteria

4. **`agent-communication-rules.md`**
   - Communication style and tone
   - Response formatting
   - User interaction patterns

5. **`agent-persistence-requirements.md`**
   - When to persist data
   - Data storage locations
   - Snapshot requirements

6. **`agent-workflow-requirements.md`**
   - Workflow execution patterns
   - Task organization
   - Process documentation

7. **`agent-decision-framework.md`**
   - Decision-making guidelines
   - When to ask vs act
   - Risk-based decision trees

### Specialized Protocols

8. **`agent-email-triage-protocol.md`**
   - Email processing rules
   - Triage criteria
   - Action workflows

---

## Related Documentation

### Data Policies
- **`data-imports-policy.md`** - Rules for `$DATA_DIR/imports/` (read-only archive)

### Repository Structure
- **`/docs/repository-structure.md`** - Repository structure documentation

---

## Usage

### For Agents
1. **Always start with:** `shared/docs/agent-context.md`
2. **Reference policies as needed:** Check relevant `shared/docs/agent-*.md` files
3. **Follow `.cursorrules`:** Repository-wide behavior rules

### For Users
- All agent instructions are version-controlled in git
- Changes to agent behavior should be documented in commit messages
- Policy documents can be updated independently of code

---

## File Locations Summary

```
personal/
├── .cursorrules                                    # Main agent config (root)
├── strategy/
│   └── reference/
        └── agent-context.md                        # Agent context reference
└── shared/
    └── docs/
        ├── agent-mcp-access-policy.md              # MCP access rules
        ├── agent-data-entry-requirements.md        # Data entry standards
        ├── agent-confirmation-requirements.md      # Confirmation rules
        ├── agent-communication-rules.md            # Communication style
        ├── agent-persistence-requirements.md        # Data persistence
        ├── agent-workflow-requirements.md          # Workflow patterns
        ├── agent-decision-framework.md              # Decision guidelines
        └── agent-email-triage-protocol.md           # Email processing
```

---

## Maintenance

- **Update frequency:** As agent behavior requirements evolve
- **Version control:** All files tracked in git
- **Backup:** Git repository serves as backup
- **Review:** Periodic review of agent behavior effectiveness

---

## Notes

- All agent instruction files are in `shared/docs/` for cross-layer access
- `.cursorrules` remains at root as required by Cursor IDE
- Policy documents can be referenced independently or as a set











