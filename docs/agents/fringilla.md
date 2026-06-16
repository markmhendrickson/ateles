# fringilla

Invoke Fringilla, the financial analysis agent — quarterly financial reviews, portfolio performance analysis, fixed-cost and subscription reconciliation, income-vs-expense tracking, and anomaly surfacing grounded in the operator's own Neotoma transaction and financial data. Use when the user says "fringilla", "run the quarterly financial review", "reconcile my subscriptions", "review fixed costs", "how are my finances trending", or when Apis dispatches a finance-domain analysis task (audience=agent). Distinct from Monedula, which only executes payments — Fringilla analyzes and reviews, it does not move money.

| Field | Value |
| --- | --- |
| Tier | T4 |
| Genus | Fringilla |
| Status | active |
| AAuth sub | fringilla@ateles-swarm |
| Agent grant | service |
| Triggers | ["fringilla", "/fringilla", "run the quarterly financial review", "reconcile my subscriptions", "review fixed costs", "how are my finances trending"] |
| Allowed tools | mcp__mcpsrv_neotoma__retrieve_entities, mcp__mcpsrv_neotoma__retrieve_entity_by_identifier, mcp__mcpsrv_neotoma__store, mcp__mcpsrv_neotoma__correct |
| Entity ID | ent_a6e9d4d4d684a7f3603b1fe3 |

---

Operational prompt: [`.claude/skills/fringilla/SKILL.md`](../../.claude/skills/fringilla/SKILL.md)

*Reference card mirrored from Neotoma `agent_definition` `ent_a6e9d4d4d684a7f3603b1fe3`. Do not edit directly — correct the entity and run `python3 execution/scripts/render_agent_docs.py`.*
