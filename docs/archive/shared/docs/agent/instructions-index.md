# Agent Instructions Index

**Purpose:** Central reference for all AI agent instruction files and their locations.

**Last Updated:** 2025-01-23

---

## Primary Entry Point

**`.cursorrules`** (Root) - Main Cursor AI agent configuration. Points to `/shared/docs/agent/context.md` for comprehensive index.

**`/shared/docs/agent/context.md`** - Essential context and index to all rule documents. **START HERE** for agent instructions.

---

## Rule Documents (`/shared/docs/agent/rules/`)

**Data:** `data-rules.md` (query decision tree, snapshots, entries), `data-entry-requirements.md` (entry standards, schema evolution), `mcp-access-policy.md` (MANDATORY: MCP-only access)

**Persistence:** `persistence-requirements.md` (instructions, contacts, emails, tasks), `persistence-rules.md` (personal data, contact extraction, accounts)

**Communication:** `communication-rules.md` (Spanish formatting, WhatsApp, transaction language), `confirmation-requirements.md` (email/transaction confirmations), `email-triage-protocol.md` (triage workflow, drafts, archive)

**Workflows:** `workflow-requirements.md` (scorecards, reports, naming), `development-workflows.md` (websites, MCP servers), `time-communication.md` (time-sensitive ops, follow-ups), `security-automation.md` (1Password CLI, Playwright)

---

## Reference Documents (`/shared/docs/agent/`)

**Index/Context:** `context.md` (main index), `reference-guide.md` (strategy docs, data sources, cadences), `repository-structure.md` (repo org, architecture)

**Guidance:** `decision-framework.md` (decision framework, behavioral compliance), `prompt-integration.md` (auto-integrate persistent instructions), `workflow-specifics.md` (email processing, Amazon orders)

---

## File Structure

```
personal/
├── .cursorrules                                    # Main agent config (root)
└── shared/docs/
    ├── agent/
    │   ├── context.md                              # Main index (START HERE)
    │   ├── reference-guide.md                      # Strategy docs, data sources
    │   ├── repository-structure.md                # Repo org, architecture
    │   ├── decision-framework.md                   # Decision framework
    │   ├── prompt-integration.md                   # Instruction integration
    │   ├── workflow-specifics.md                  # Workflow details
    │   └── rules/                                  # All mandatory rules
    │       ├── data-rules.md
    │       ├── data-entry-requirements.md
    │       ├── mcp-access-policy.md
    │       ├── persistence-requirements.md
    │       ├── persistence-rules.md
    │       ├── communication-rules.md
    │       ├── confirmation-requirements.md
    │       ├── email-triage-protocol.md
    │       ├── workflow-requirements.md
    │       ├── development-workflows.md
    │       ├── time-communication.md
    │       └── security-automation.md
    └── data-imports-policy.md                      # Data imports archive rules
```

---

**For complete documentation, see `/shared/docs/agent/context.md` which indexes all rule and policy documents.**
