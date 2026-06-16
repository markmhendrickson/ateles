# gorilla

Invoke Gorilla, the health & fitness agent — log gym workouts, analyze training progression, and consult on health & fitness grounded in your own Neotoma data. Use when the user says "gorilla", "log my workout", "how's my <lift> trending", "track my fitness", or asks a health/fitness question.

| Field | Value |
| --- | --- |
| Tier | T4 |
| Genus | Gorilla |
| Status | active |
| AAuth sub | gorilla@ateles-swarm |
| Agent grant | service |
| Triggers | gorilla, /gorilla, log my workout, track my fitness |
| Allowed tools | mcp__mcpsrv_neotoma__retrieve_entities, mcp__mcpsrv_neotoma__retrieve_entity_snapshot, mcp__mcpsrv_neotoma__retrieve_entity_by_identifier, mcp__mcpsrv_neotoma__retrieve_related_entities, mcp__mcpsrv_neotoma__store, mcp__mcpsrv_neotoma__correct, Read, WebSearch, WebFetch |
| Entity ID | ent_a4697e7c2ba6deeb22be6e41 |

---

Operational prompt: [`.claude/skills/gorilla/SKILL.md`](../../.claude/skills/gorilla/SKILL.md)

*Reference card mirrored from Neotoma `agent_definition` `ent_a4697e7c2ba6deeb22be6e41`. Do not edit directly — correct the entity and run `python3 execution/scripts/render_agent_docs.py`.*
