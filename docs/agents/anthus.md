# anthus

Swarm coordinator daemon (Phase 2 skeleton). Subscribes to escalation, daemon_report, agent_grant, and task events. Full swarm coordination logic deferred to Phase 6 — currently provides global visibility of work-in-flight and surfaces conflicts to Onychomys.

| Field | Value |
| --- | --- |
| Tier | T3 |
| Genus | Anthus |
| Status | active |
| AAuth sub | anthus@ateles-swarm |
| Agent grant | service |
| Allowed tools | neotoma_read, neotoma_write, telegram |
| Harness | lib/daemon_runtime SSEClient |
| Entity ID | ent_887e8fd74d79eb63344df63e |

---

Operational prompt: [`.claude/skills/anthus/SKILL.md`](../../.claude/skills/anthus/SKILL.md)

*Reference card mirrored from Neotoma `agent_definition` `ent_887e8fd74d79eb63344df63e`. Do not edit directly — correct the entity and run `python3 execution/scripts/render_agent_docs.py`.*
