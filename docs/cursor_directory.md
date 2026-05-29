# .cursor Directory

Documentation of the `.cursor/` directory structure and how rules, skills, and configuration are used in this repository.

## Purpose

`.cursor/` holds Cursor IDE and agent configuration: rules (behavior and constraints), skills (workflow definitions), optional commands, and generated plans. Repo rules and foundation rules/skills are installed here by `foundation/scripts/setup_cursor_copies.sh`. Which rules are always loaded is controlled by `cursor_rules_manifest.json` in the repo root.

## Directory Layout

| Path | Purpose |
|------|---------|
| `.cursor/rules/` | Cursor rules (`.mdc`). Repo rules are source of truth here; foundation rules are copied in by setup. |
| `.cursor/skills/` | Skills (one dir per slug, each with `SKILL.md`). Foundation skills + repo-specific skills. |
| `.cursor/commands/` | Legacy commands (optional). Empty when using skills. |
| `.cursor/plans/` | Generated execution/plan files (transient). |
| `.cursor/mcp.json` | MCP server configuration (Cursor-managed). |
| `.cursor/environment.json` | Environment/config (Cursor-managed). |

## .cursor/rules/

Rules are Markdown Cursor rule files (`.mdc`). They define constraints, workflows, and when to load other docs or skills.

- **Source of truth:** Repo rules live in `.cursor/rules/` with names like `rule_router.mdc`, `mcp_server_testing.mdc`, `persistence.mdc` (no `_rules` suffix in the filename).
- **Always-on (kernel):** Controlled by `cursor_rules_manifest.json` → `always_on.repo_rules` and `always_on.foundation_rules`. Only those files are guaranteed to be loaded every session.
- **On-demand:** All other rules are loaded when the task matches a trigger in `.cursor/rules/rule_router.mdc`. The router maps triggers (e.g. "email triage", "data entry", "fix bug") to a rule path or skill.

### Repo rules (this repository)

| File | Purpose |
|------|--------|
| `rule_router.mdc` | Trigger → on-demand rule or skill mapping; always-on. |
| `mcp_server_testing.mdc` | Test and restart MCP servers after code changes; always-on. |
| `analyze.mdc` | Analyze command and repos. |
| `apply_new_rules_proactively.mdc` | Proactive rule/skill suggestions. |
| `ateles_website_static_data.mdc` | Website static data rules. |
| `blog_posts_draft_location.mdc` | Draft location for blog posts. |
| `communication.mdc` | Communication and email style. |
| `comprehensive_test_coverage.mdc` | Test coverage requirements. |
| `confirmation_requirements.mdc` | Preview/confirm patterns. |
| `content_style_enforcement.mdc` | Content and style enforcement. |
| `conversation_tracking.mdc` | Conversation storage and entity model. |
| `cursor_rules_sync_hook.mdc` | Pre-commit hook for cursor rules sync. |
| `data.mdc` | Data query and imports. |
| `data_entry_requirements.mdc` | Data entry and schema. |
| `decision_framework.mdc` | Decision-making framework. |
| `development_workflows.mdc` | Merge, website, MCP, deploy workflows. |
| `email_draft_display.mdc` | Email draft display. |
| `email_triage_protocol.mdc` | Email triage workflow. |
| `execution_plans.mdc` | Execution plans usage. |
| `mcp_access_policy.mdc` | MCP-only data access. |
| `mcp_retry_usage_after_fix.mdc` | MCP retry after fix. |
| `neotoma_parquet_migration.mdc` | Neotoma-first data; Parquet migration. |
| `persistence.mdc` | Persistence and contacts/tasks. |
| `post_updates_neotoma_cache.mdc` | Post updates and Neotoma cache. |
| `prompt_integration.mdc` | Instruction capture. |
| `release_status_readme_hook.mdc` | Release status README hook. |
| `security_audit_hook.mdc` | Security audit hook. |
| `security_automation.mdc` | Security automation. |
| `time_communication.mdc` | Time and communication. |
| `workflow_requirements.mdc` | Scorecard, reports, file location. |
| `workflow_specifics.mdc` | PDF, email, Amazon, blog workflows. |

Foundation rules (e.g. `agent_constraints.mdc`, `security.mdc`, `cursor_rules_editing.mdc`) are copied into `.cursor/rules/` by setup when listed in `cursor_rules_manifest.json` → `always_on.foundation_rules`.

## .cursor/skills/

Each skill is a directory named by slug with a `SKILL.md` file. Skills are invoked by trigger (see `rule_router.mdc`) or by name (e.g. `/create-release`).

- **Foundation skills:** Full workflow in `SKILL.md` (e.g. `commit`, `setup-cursor-copies`, `fix-feature-bug`, `create-release`). Load from `.cursor/skills/{slug}/SKILL.md`; do not fetch from Neotoma.
- **Ateles-only skills:** Stub in `.cursor/skills/{slug}/SKILL.md` with `entity_id`; full content is in Neotoma. Fetch via `retrieve_entity_snapshot` when using the skill (per `skills_neotoma_proactive_fetch.mdc`).

### Skill slugs (current)

| Slug | Type | Purpose |
|------|------|--------|
| `analyze` | Foundation | Comparative analysis across repos. |
| `commit` | Foundation | Commit workflow and security audit. |
| `create-execution-plan` | Ateles | Execution plans. |
| `create-feature-unit` | Foundation | Feature unit creation. |
| `create-prototype` | Foundation | Prototype creation. |
| `create-release` | Foundation | Release creation. |
| `create-rule` | Foundation | Rule creation. |
| `create-website` | Ateles | New website setup. |
| `debug` | Foundation | Debug workflow. |
| `deploy-website` | Ateles | Deploy website. |
| `disk-cleanup` | Ateles | Disk cleanup. |
| `email-triage` | Ateles | Email triage workflow. |
| `extract-amazon-order` | Ateles | Extract Amazon order from Gmail. |
| `final-review` | Foundation | Final review. |
| `fix-feature-bug` | Foundation | Bug fix workflow. |
| `import-audio` | Ateles | Import and transcribe audio. |
| `manage-error-debugging` | Foundation | Error debugging. |
| `pull` | Foundation | Pull workflow. |
| `publish` | Foundation | Publish workflow. |
| `push` | Foundation | Push workflow. |
| `quarterly-portfolio-review` | Ateles | Quarterly portfolio review. |
| `report` | Foundation | Report workflow. |
| `report-error` | Foundation | Report error. |
| `run-feature-workflow` | Foundation | Run feature workflow. |
| `run-scorecard` | Ateles | Run scorecard. |
| `setup-commands` | Foundation | Setup commands. |
| `setup-cursor-copies` | Foundation | Sync rules/skills to .cursor. |
| `sync-env-from-1password` | Foundation | Sync env from 1Password. |
| `verify-deployment` | Ateles | Verify deployment. |
| `write-blog-post` | Ateles | Write blog post. |

## .cursor/commands/

Reserved for legacy Cursor commands. Currently empty; workflows use `.cursor/skills/` instead.

## .cursor/plans/

Generated plan or execution artifacts (e.g. from planning tools). Treat as transient; not part of versioned config.

## Configuration (repo root)

- **`cursor_rules_manifest.json`** — Selective install. `always_on.foundation_rules` and `always_on.repo_rules` list which rules are installed and kept; `always_on.repo_rules` can be basenames (e.g. `rule_router.mdc`) when repo rules live in `.cursor/rules/`. Setup script reads this and copies foundation rules/skills and preserves repo rules.

## Setup and sync

- **Install/update:** Run `./foundation/scripts/setup_cursor_copies.sh` from the repo root (or use the `/setup_cursor_copies` skill). Copies foundation rules and skills into `.cursor/`; does not delete repo rules in `.cursor/rules/`.
- **After editing rules:** Run setup so `.cursor/` stays in sync. Repo rules are edited in place in `.cursor/rules/`; foundation rules are edited in `foundation/agent_instructions/cursor_rules/` (or in foundation’s `.cursor/rules/` per foundation layout) then re-copied by setup.

## Related

- **Trigger → load mapping:** `.cursor/rules/rule_router.mdc`
- **Manifest:** `cursor_rules_manifest.json` (repo root)
- **Inventory/classification:** `docs/cursor_rules/cursor_rules_inventory.md`
- **On-demand rules:** `docs/on_demand/README.md`
