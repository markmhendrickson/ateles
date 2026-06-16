# sitta

Neotoma librarian daemon (PROPOSED). Background curation agent that monitors entity storage activity and proactively maintains the knowledge graph: detects potential duplicates, surfaces missing relationships between related entities, and proposes merges and schema promotions. Proposal-first by default — auto-executes only a narrow high-confidence allow-list; escalates everything else to Columba/operator. Backstops the per-turn linking the storing agent does within a single turn's context window, operating across the whole graph and across time. Off-hot-path: event-triggered via Neotoma subscribe (entity_created/entity_updated) but debounced and batched on an interval; never reacts to its own writes. Governed by a paired execution_policy.

| Field | Value |
| --- | --- |
| Tier | T3 |
| Genus | Sitta |
| Status | proposed |
| AAuth sub | sitta@ateles-swarm |
| Agent grant | service |
| Allowed tools | neotoma |
| Entity ID | ent_0a092db508311dc817c37df3 |

---

Operational prompt: [`.claude/skills/sitta/SKILL.md`](../../.claude/skills/sitta/SKILL.md)

*Reference card mirrored from Neotoma `agent_definition` `ent_0a092db508311dc817c37df3`. Do not edit directly — correct the entity and run `python3 execution/scripts/render_agent_docs.py`.*
