# cotinga

Daily event-prep briefing daemon. Runs at 05:30 Madrid time: Phase 1 (fast ~30s) fetches today's calendar via gws, cross-references attendees against Neotoma, sends shallow Telegram briefing. Phase 2 spawns async Claude agents per meeting to do deep participant research, agenda/goals/talking-points generation, and pre-event task creation.

| Field | Value |
| --- | --- |
| Tier | T3 |
| Genus | Cotinga cotinga |
| Status | active |
| AAuth sub | cotinga@ateles-swarm |
| Agent grant | service |
| Allowed tools | ["mcp__mcpsrv_neotoma__retrieve_entities", "mcp__mcpsrv_neotoma__retrieve_entity_by_identifier", "mcp__mcpsrv_neotoma__retrieve_entity_snapshot", "mcp__mcpsrv_neotoma__retrieve_related_entities", "mcp__mcpsrv_neotoma__store", "mcp__mcpsrv_neotoma__correct", "WebSearch", "WebFetch", "Bash"] |
| Entity ID | ent_6c85e2a550580c88024da8f4 |

---

Operational prompt: [`.claude/skills/cotinga/SKILL.md`](../../.claude/skills/cotinga/SKILL.md)

*Reference card mirrored from Neotoma `agent_definition` `ent_6c85e2a550580c88024da8f4`. Do not edit directly — correct the entity and run `python3 execution/scripts/render_agent_docs.py`.*
