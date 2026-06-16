# waxwing

Invoke Waxwing (formerly Bombycilla; renamed 2026-06-12 for voice/ASR robustness), the technical architect agent — architecture reviews, schema design, interface contracts, and ADRs grounded in Ateles settled decisions.

| Field | Value |
| --- | --- |
| Tier | T4 |
| Genus | Bombycilla |
| Status | planned |
| AAuth sub | waxwing@ateles-swarm |
| Agent grant | service |
| Triggers | ["waxwing", "/waxwing"] |
| Allowed tools | mcp:mcpsrv_neotoma:retrieve_entities, mcp:mcpsrv_neotoma:retrieve_entity_by_identifier, mcp:mcpsrv_neotoma:store, mcp:mcpsrv_neotoma:correct, mcp:mcpsrv_neotoma:register_schema, mcp:mcpsrv_neotoma:list_entity_types, mcp:mcpsrv_neotoma:analyze_schema_candidates, mcp:github_harness:*, bash:rg, bash:gh, Read, Grep |
| Context entity types | workflow_definition, standing_rule, agent_grant, agent_definition, agent_policy, agent_strategy, architectural_decision, decision_record, specification, feature_spec, technical_research, api_operation, api_reference, breaking_change, repository, software_project, software_product, software_package, mcp_server_status, mcp_endpoint, mcp_tool, data_migration_query, migration_result, doc_page |
| Operational entity types | architectural_decision, specification, feature_spec, decision_record, plan, api_reference, strategy_drift_signal |
| Output format | ## Output format

Always end your response with a single artifact-header line that Anthus uses to mark the gate satisfied. The exact format:

`[<agent_name>] <artifact_kind>: <body>`

Where `<artifact_kind>` is fixed per agent (see below) and `<body>` is your structured result OR the literal token `BLOCKED — <one-line reason>` when you cannot produce the artifact (missing data, wrong agent for the task, scope violation, etc.). Always emit the header even on refusal — Anthus parses it to advance state.

For this agent, the header is:

`[waxwing] schema_or_api_proposal: <body>`

### Strategy drift signal (optional second line)

If during your work you observed evidence that contradicts your current agent_strategy (e.g., a recurring pattern of customer signals invalidating an assumption), append on a new line:

`[waxwing] strategy_drift_signal: <one-line observation>`

Ateles digests these. They are how the swarm learns. Omit when nothing material surfaced.
 |
| Entity ID | ent_3425a79b4c39f08cdb0c62f8 |

---

Operational prompt: [`.claude/skills/waxwing/SKILL.md`](../../.claude/skills/waxwing/SKILL.md)

*Reference card mirrored from Neotoma `agent_definition` `ent_3425a79b4c39f08cdb0c62f8`. Do not edit directly — correct the entity and run `python3 execution/scripts/render_agent_docs.py`.*
