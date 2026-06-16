# sylvia

Recurring task lifecycle daemon. Daily poll loop: scans Neotoma tasks with `recurrence` set, rolls due_date forward after completion, creates/updates Google Calendar events as the scheduling surface. Also scans Calendar for events with no matching Neotoma task and imports them, running the agent-routing lookup to set `assigned_to` on each import. On due date: audience=agent tasks are dispatched to the task's `assigned_to` agent (falling back to Apis only when `assigned_to` is unset or `apis`); audience=human tasks trigger a Telegram reminder to the operator. Neotoma is authoritative for recurrence rules — Calendar is output/import surface only.

| Field | Value |
| --- | --- |
| Tier | T3 |
| Genus | Sylvia |
| Status | active |
| AAuth sub | sylvia@ateles-swarm |
| Agent grant | service |
| Allowed tools | mcp__mcpsrv_neotoma__retrieve_entities, mcp__mcpsrv_neotoma__store, mcp__mcpsrv_neotoma__correct, gws_calendar, telegram_notify |
| Harness | daily poll loop via asyncio.sleep |
| Entity ID | ent_1faed5788fcc0e5200bb0120 |

---

Operational prompt: [`.claude/skills/sylvia/SKILL.md`](../../.claude/skills/sylvia/SKILL.md)

*Reference card mirrored from Neotoma `agent_definition` `ent_1faed5788fcc0e5200bb0120`. Do not edit directly — correct the entity and run `python3 execution/scripts/render_agent_docs.py`.*
