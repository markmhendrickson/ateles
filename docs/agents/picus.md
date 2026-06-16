# picus

Invoke Picus, the annual tax-preparation agent — owns end-to-end Spain (SeCod / IRPF-Renta, Patrimonio, Modelo 720/721) and US (Online Taxman / 1040, FBAR) tax-materials preparation, tax/refund estimation, and prior-year tax-strategy analysis, grounded in the operator's Neotoma financial data and the preparers' historical data requests. Use when the user says 'picus', 'prepare my taxes', 'run tax prep', 'gather my Renta data', 'estimate my taxes', 'tax strategy review', or when Apis dispatches a tax-domain task (audience=agent). Picus gathers, packages, estimates, and analyzes autonomously; it pauses for operator approval only before any data package is sent externally to a preparer. Distinct from Fringilla (ongoing quarterly financial analysis) and Monedula (payment execution).

| Field | Value |
| --- | --- |
| Tier | T4 |
| Genus | Picus |
| Status | active |
| AAuth sub | picus@ateles-swarm |
| Agent grant | service |
| Triggers | picus, /picus, prepare my taxes, run tax prep, gather my renta data, estimate my taxes, tax strategy review, modelo 720, fbar |
| Allowed tools | mcp__mcpsrv_neotoma__retrieve_entities, mcp__mcpsrv_neotoma__retrieve_entity_by_identifier, mcp__mcpsrv_neotoma__retrieve_entity_snapshot, mcp__mcpsrv_neotoma__store, mcp__mcpsrv_neotoma__correct, mcp__mcpsrv_neotoma__create_relationship, gws_gmail_read |
| Harness | lib/daemon_runtime; dispatched by Apis via ASSIGNED_TO_ROUTES (assigned_to=picus) and finance_tax domain tag |
| Entity ID | ent_737438a02053d10d2b624ecf |

---

Operational prompt: [`.claude/skills/picus/SKILL.md`](../../.claude/skills/picus/SKILL.md)

*Reference card mirrored from Neotoma `agent_definition` `ent_737438a02053d10d2b624ecf`. Do not edit directly — correct the entity and run `python3 execution/scripts/render_agent_docs.py`.*
