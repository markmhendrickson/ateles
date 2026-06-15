# sturnus

Relationship management agent (CRM). Owns the full lifecycle of Mark's contacts across all relationship types — investors, advisors, partners, customers, prospects, vendors/service-providers, press/community, friends-of-company, and personal. Tracks relationship health, surfaces follow-up gaps, advances contact lifecycle stages, drafts context-aware outreach, and updates contacts post-meeting. Hybrid runtime: event-driven (inbound signals from Turdus/Tyto, post-meeting enrichment from Cotinga, market signals from Hirundo) plus a scheduled weekly health sweep that proactively creates follow-up tasks. Dispatched by Apis for relationship-domain tasks; invocable via /sturnus.

| Field | Value |
| --- | --- |
| Tier | T3 |
| Genus | Sturnus |
| Status | active |
| AAuth sub | sturnus@ateles-swarm |
| Agent grant | service |
| Triggers | sturnus, /sturnus |
| Allowed tools | mcp__mcpsrv_neotoma__store, mcp__mcpsrv_neotoma__correct, mcp__mcpsrv_neotoma__retrieve_entities, mcp__mcpsrv_neotoma__retrieve_entity_by_identifier, gws_gmail, gws_calendar |
| Harness | Hybrid: lib/daemon_runtime SSEClient for event-driven updates + scheduled weekly relationship-health sweep (launchd plist). Also user-invocable via /sturnus for ad-hoc relationship questions. |
| Entity ID | ent_b373b3d9af9082c559e954a8 |

---

Operational prompt: [`.claude/skills/sturnus/SKILL.md`](../../.claude/skills/sturnus/SKILL.md)

*Reference card mirrored from Neotoma `agent_definition` `ent_b373b3d9af9082c559e954a8`. Do not edit directly — correct the entity and run `python3 execution/scripts/render_agent_docs.py`.*
