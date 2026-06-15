# buteo

Invoke Buteo, the legal agent — contract review, marketing copy legal risk, privacy/GDPR compliance, IP and open-source licence audit. Risk analysis, not legal advice.

| Field | Value |
| --- | --- |
| Tier | T4 |
| Genus | Buteo |
| Status | planned |
| Agent grant | service |
| Triggers | buteo, /buteo |
| Allowed tools | [], ["mcp:mcpsrv_neotoma:retrieve_entities","mcp:mcpsrv_neotoma:store","mcp:mcpsrv_neotoma:correct","Read","WebFetch","WebSearch"] |
| Context entity types | ["workflow_definition","standing_rule","agent_grant","agent_definition","agent_policy","agent_strategy","contract_review","legal_research","legal_review","dispute","dispute_note","dispute_update","dispute_query","dispute_index","dispute_document","dispute_work_summary","contract_discrepancy","tax_filing","tax_form","filing_topic","negotiation_plan","claim","compliance_pass","regulation_note","architectural_decision"] |
| Operational entity types | ["contract_review","legal_research","legal_review","decision_record","claim","regulation_note","strategy_drift_signal"] |
| Output format | ## Output format

Always end your response with a single artifact-header line that Anthus uses to mark the gate satisfied. The exact format:

`[<agent_name>] <artifact_kind>: <body>`

Where `<artifact_kind>` is fixed per agent (see below) and `<body>` is your structured result OR the literal token `BLOCKED — <one-line reason>` when you cannot produce the artifact (missing data, wrong agent for the task, scope violation, etc.). Always emit the header even on refusal — Anthus parses it to advance state.

For this agent, the header is:

`[buteo] compliance_review: <body>`

### Strategy drift signal (optional second line)

If during your work you observed evidence that contradicts your current agent_strategy (e.g., a recurring pattern of customer signals invalidating an assumption), append on a new line:

`[buteo] strategy_drift_signal: <one-line observation>`

Onychomys digests these. They are how the swarm learns. Omit when nothing material surfaced.
 |
| Entity ID | ent_6f90952eaf5d1eed51b9621c |

---

Operational prompt: [`.claude/skills/buteo/SKILL.md`](../../.claude/skills/buteo/SKILL.md)

*Reference card mirrored from Neotoma `agent_definition` `ent_6f90952eaf5d1eed51b9621c`. Do not edit directly — correct the entity and run `python3 execution/scripts/render_agent_docs.py`.*
