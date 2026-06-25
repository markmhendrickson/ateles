# Cursor Rules Inventory and Classification

## Purpose

Catalog all rule sources for selective rule injection and classification (always_on, on_demand, hook).

## Scope

Repo rules under docs/ and foundation rules; used by router and agents to load the right rule. Does not define rule content.

---

Inventory of all rule sources for selective rule injection. Classifications: **always_on** (minimal kernel), **on_demand** (router → doc or skill), **hook** (policy pointer only; enforcement via script).

## Repo rules (docs/)

| Source | Classification | Notes |
|--------|----------------|-------|
| development_workflows_rules.mdc | on_demand | Merge conflict, website, MCP, deploy workflows |
| post_updates_neotoma_cache_rules.mdc | on_demand | Blog/post cache; → skill |
| persistence_rules.mdc | on_demand | Large; → skill / router |
| content_style_enforcement_rules.mdc | on_demand | → skill |
| workflow_specifics_rules.mdc | on_demand | PDF, email, Amazon, task review, blog |
| data_entry_requirements_rules.mdc | on_demand | → skill |
| conversation_tracking_rules.mdc | on_demand | → skill |
| release_status_readme_hook_rules.mdc | hook | Pointer; script: update_readme_release_status.py |
| cursor_rules_sync_hook_rules.mdc | hook | Pointer; script: cursor_rules_sync_pre_commit.sh |
| ateles_website_static_data_rules.mdc | on_demand | → skill |
| neotoma_parquet_migration_rules.mdc | on_demand | Data layer; keep as doc or skill |
| communication_rules.mdc | on_demand | Email/style; → skill |
| email_triage_protocol_rules.mdc | on_demand | → email-triage skill |
| data_rules.mdc | on_demand | Query/imports/parquet; → skill |
| blog_posts_draft_location_rules.mdc | on_demand | → skill |
| mcp_retry_usage_after_fix_rules.mdc | on_demand | MCP retry; → skill or doc |
| apply_new_rules_proactively_rules.mdc | on_demand | Behavioral; → skill |
| analyze_rules.mdc | on_demand | Analysis; → doc |
| security_audit_hook_rules.mdc | hook | Pointer; script: security_audit.py |
| prompt_integration_rules.mdc | on_demand | Instruction capture; → doc/skill |
| mcp_access_policy_rules.mdc | on_demand | Neotoma/Parquet; → doc |
| execution_plans_rules.mdc | on_demand | → create-execution-plan skill |
| comprehensive_test_coverage_rules.mdc | on_demand | → skill (when writing tests) |
| workflow_requirements_rules.mdc | on_demand | Scorecard, reports, file location |
| security_automation_rules.mdc | on_demand | 1Password, browser automation |
| time_communication_rules.mdc | on_demand | → skill |
| decision_framework_rules.mdc | on_demand | Strategy/tactics/operations |
| email_draft_display_rules.mdc | on_demand | → email skill |
| confirmation_requirements_rules.mdc | on_demand | Email/transaction confirm; → skill |
| personal_events_calendar.mdc | on_demand | Personal events → add to calendar; → rule_router |
| ../developer/chatgpt_share_url_rules.md | on_demand | ChatGPT share URLs → web-scraper `scrape_content`; no plain fetch |

**Always-on repo (kernel):**
- .cursor/rules/rule_router.mdc — router mapping triggers → on-demand
- .cursor/rules/mcp_server_testing.mdc (always-on)

## Foundation rules (foundation/agent_instructions/cursor_rules/)

| Slug (filename) | Classification | Notes |
|------------------|----------------|-------|
| agent_constraints.mdc | always_on | Safety, forbidden patterns |
| security.mdc | always_on | Pre-commit audit, protected paths |
| risk_management.mdc | always_on | Hold points |
| cursor_rules_editing.mdc | always_on | Do not edit .cursor/ copies |
| skills_neotoma_proactive_fetch.mdc | always_on | On-demand skill fetch |
| instruction_documentation.mdc | always_on | Where/how to document rules |
| dependency_installation.mdc | always_on | Small, universal |
| file_naming.mdc | always_on | Small, universal |
| configuration_management.mdc | always_on | Small, repo vs foundation config |
| behavioral_self_adaptation.mdc | on_demand | → doc |
| content_style_enforcement.mdc | on_demand | Duplicate of repo; → skill |
| cursor_rules_sync_requirement.mdc | always_on | Run setup after rule edits |
| neotoma_parquet_migration_rules.mdc | on_demand | Duplicate of repo; → doc/skill |
| bug_fix_detection.mdc | on_demand | → fix-feature-bug skill |
| checkpoint_management.mdc | on_demand | Release checkpoints |
| document_loading_order.mdc | on_demand | Doc load order |
| downstream_doc_updates.mdc | on_demand | Doc dependencies |
| documentation_rules.mdc | on_demand | Doc standards |
| release_status_readme_update.mdc | on_demand | Hook pointer |
| release_detection.mdc | on_demand | → create-release skill |
| readme_maintenance.mdc | on_demand | README sync |
| post_build_testing.mdc | on_demand | Release testing |
| plan_execution_testing.mdc | on_demand | Test implementation |
| feature_unit_detection.mdc | on_demand | → create-feature-unit skill |
| worktree_env.mdc | on_demand | Env in worktrees |
| environment_variables_*.mdc | on_demand | Env/1Password |
| prefer_cli_tools.mdc | on_demand | CLI preference |
| test_first_workflow.mdc | on_demand | Testing |
| native_browser_debugging.mdc | on_demand | Browser |
| git_remote_sync.mdc | on_demand | Git |
| autonomous_execution.mdc | on_demand | Execution |
| agent_test_execution.mdc | on_demand | Testing |

## Commands

Foundation workflows are **exposed as on-demand skills** (they replaced legacy commands). They are not installed into `.cursor/commands/`. **Foundation skills** live in `foundation/agent_instructions/cursor_skills/{slug}/SKILL.md` with full workflow content. Setup copies them into `.cursor/skills/`; load the skill when the trigger matches. No Neotoma. **Ateles-only skills** (e.g. email-triage, write-blog-post) use Neotoma and have `entity_id` in the stub. Trigger → skill mapping: see `.cursor/rules/rule_router.mdc`.

| Foundation skill slug |
|------------------------|
| commit, setup-cursor-copies, sync-env-from-1password, analyze, push, pull, publish |
| run-feature-workflow, create-rule, create-prototype, report, report-error |
| debug, manage-error-debugging, final-review, setup-commands |
| create-release, fix-feature-bug, create-feature-unit, store-neotoma |

## Summary

- **Always-on foundation:** agent_constraints, security, risk_management, cursor_rules_editing, skills_neotoma_proactive_fetch, instruction_documentation, dependency_installation, file_naming, configuration_management, cursor_rules_sync_requirement.
- **Always-on repo:** .cursor/rules/rule_router.mdc, .cursor/rules/mcp_server_testing.mdc.
- **On-demand:** All other repo and foundation rules; load via router → doc path or Neotoma skill.
- **Hooks:** security_audit_hook, cursor_rules_sync_hook, release_status_readme_hook — short pointer rules only; enforcement by scripts.
