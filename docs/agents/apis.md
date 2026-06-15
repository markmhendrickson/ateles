# apis

Universal task dispatcher daemon. Subscribes to task.created/updated/due_today SSE events, infers domain tags, routes tasks to appropriate T4 agents (Gryllus, Monedula, etc.) via domain routing table. Phase 4 skeleton; Phase 5 adds subprocess dispatch.

| Field | Value |
| --- | --- |
| Tier | T3 |
| Genus | Apis |
| Status | active |
| AAuth sub | apis@ateles-swarm |
| Agent grant | service |
| Allowed tools | neotoma_read, neotoma_write, neotoma_correct |
| Harness | lib/daemon_runtime SSEClient |
| Entity ID | ent_acdb65a8c5dccc1c5f6c7171 |

---

Operational prompt: [`.claude/skills/apis/SKILL.md`](../../.claude/skills/apis/SKILL.md)

*Reference card mirrored from Neotoma `agent_definition` `ent_acdb65a8c5dccc1c5f6c7171`. Do not edit directly — correct the entity and run `python3 execution/scripts/render_agent_docs.py`.*
