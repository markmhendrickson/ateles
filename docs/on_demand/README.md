# On-demand rules and docs

Rules and docs in this directory (and those listed in the router but living under `docs/`) are **not** installed into `.cursor/rules/`. They are loaded on demand when the task matches a trigger in the always-on **rule router** (`.cursor/rules/rule_router.mdc`).

- **Router:** See `.cursor/rules/rule_router.mdc` for trigger → doc/skill mapping.
- **Skills:** Full content for workflows is in Neotoma; local stubs under `.cursor/skills/*/SKILL.md` reference `entity_id` for fetch. See `skills_neotoma_proactive_fetch.mdc` (always-on).
- **Source of truth:** `cursor_rules_manifest.json` defines which rules are always-on; everything else is on-demand.
