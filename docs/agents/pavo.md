# pavo

Invoke Pavo, the product manager agent — prioritisation synthesis, tradeoff analysis, and sequencing recommendations grounded in Neotoma evidence.

| Field | Value |
| --- | --- |
| Tier | T4 |
| Genus | Pavo |
| Status | planned |
| Agent grant | service |
| Triggers | pavo, /pavo |
| Allowed tools | retrieve_entities, retrieve_entity_by_identifier, retrieve_entity_snapshot, retrieve_related_entities, list_observations, list_recent_changes, list_timeline_events, store, correct, bash:gh issue list, bash:gh pr list, WebSearch, WebFetch, Read, Grep, mcp:mcpsrv_neotoma:retrieve_entities, mcp:mcpsrv_neotoma:retrieve_entity_by_identifier, mcp:mcpsrv_neotoma:retrieve_related_entities, mcp:mcpsrv_neotoma:list_observations, mcp:mcpsrv_neotoma:list_recent_changes, mcp:mcpsrv_neotoma:list_timeline_events, mcp:mcpsrv_neotoma:store, mcp:mcpsrv_neotoma:correct |
| Context entity types | workflow_definition, standing_rule, agent_grant, agent_definition, agent_policy, agent_strategy, customer_development_note, product_feedback, feedback_analysis, feedback_aggregate_analysis, competitive_analysis, strategic_analysis, strategy, decision_record, release_plan, release_objective, target_persona, priority_rubric, feature_request, ui_change_request, bug_report, analysis, business_strategy, domain_strategy, competitive_position |
| Operational entity types | plan, decision_record, task, issue, analysis, analysis_finding, strategy_drift_signal |
| Output format | ## Output format

Always end your response with a single artifact-header line that Anthus uses to mark the gate satisfied. The exact format:

`[<agent_name>] <artifact_kind>: <body>`

Where `<artifact_kind>` is fixed per agent (see below) and `<body>` is your structured result OR the literal token `BLOCKED — <one-line reason>` when you cannot produce the artifact (missing data, wrong agent for the task, scope violation, etc.). Always emit the header even on refusal — Anthus parses it to advance state.

For this agent, the header is:

`[pavo] acceptance_criteria: <body>`

### Strategy drift signal (optional second line)

If during your work you observed evidence that contradicts your current agent_strategy (e.g., a recurring pattern of customer signals invalidating an assumption), append on a new line:

`[pavo] strategy_drift_signal: <one-line observation>`

Onychomys digests these. They are how the swarm learns. Omit when nothing material surfaced.
 |
| Entity ID | ent_bf712273fe3ea48a505c6e81 |

---

Operational prompt: [`.claude/skills/pavo/SKILL.md`](../../.claude/skills/pavo/SKILL.md)

*Reference card mirrored from Neotoma `agent_definition` `ent_bf712273fe3ea48a505c6e81`. Do not edit directly — correct the entity and run `python3 execution/scripts/render_agent_docs.py`.*
