# Selective Rule Injection — Validation

## Installed footprint

- **Rules in `.cursor/rules/`:** 12 (allowlisted kernel only)
- **Size:** ~100K total
- **Source:** `cursor_rules_manifest.json` → `always_on.foundation_rules` (10) + `always_on.repo_rules` (2)

## On-demand loading

- **Router:** `rule_router_rules.mdc` (always-on) maps triggers to doc paths or skills.
- **Skills:** `.cursor/skills/*/SKILL.md` hold workflow content; agent loads by trigger or `/skill-name`. When Neotoma is used, fetch full content via `entity_id` (see `skills_neotoma_proactive_fetch.mdc`).

## Key flows (dry run)

| Flow | Expected behavior |
|------|-------------------|
| Triage inbox | Router → load email-triage skill (or doc). |
| Fix bug | Router → load fix-feature-bug skill. |
| Modify MCP server | mcp_server_testing_rules.mdc already installed. |
| Write blog post | Router → load write-blog-post skill. |
| Write tests | Router → load comprehensive testing doc/skill. |

## How to re-validate

1. Run `./foundation/scripts/setup_cursor_copies.sh` and confirm "Manifest install complete: 12 rules, 0 commands."
2. `ls .cursor/rules | wc -l` → 12.
3. For each flow above, confirm the router or skill is loaded when the task is started (no need to run full workflow).
