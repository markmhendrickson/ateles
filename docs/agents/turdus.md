# turdus

Email triage daemon. Polls Gmail every 5 minutes via gws CLI, classifies messages (actionable/informational/noise), creates email_message entities in Neotoma, creates agent-audience task entities for actionable messages that flow downstream to Apis.

| Field | Value |
| --- | --- |
| Tier | T3 |
| Genus | Turdus |
| Status | active |
| AAuth sub | turdus@ateles-swarm |
| Agent grant | service |
| Allowed tools | neotoma_read, neotoma_write, gws_gmail |
| Harness | polling loop via asyncio.sleep |
| Entity ID | ent_138a463654de2b1d46cec0db |

---

Operational prompt: [`.claude/skills/turdus/SKILL.md`](../../.claude/skills/turdus/SKILL.md)

*Reference card mirrored from Neotoma `agent_definition` `ent_138a463654de2b1d46cec0db`. Do not edit directly — correct the entity and run `python3 execution/scripts/render_agent_docs.py`.*
