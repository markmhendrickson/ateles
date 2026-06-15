# regulus

Developer relations agent. Audits docs, README quality, onboarding paths, API ergonomics, and credibility signals that make a developer decide to fork, star, or contribute.

| Field | Value |
| --- | --- |
| Tier | T4 |
| Genus | Regulus |
| Status | planned |
| Agent grant | service |
| Allowed tools | [], ["mcp:mcpsrv_neotoma:retrieve_entities","mcp:mcpsrv_neotoma:store","mcp:mcpsrv_neotoma:correct","bash:gh repo view","Read","WebFetch","Grep","mcp:github_harness:*"] |
| Context entity types | ["workflow_definition","standing_rule","agent_grant","agent_definition","agent_policy","agent_strategy","doc_page","api_reference","api_operation","specification","feature_spec","architectural_decision","documentation_feedback","documentation_decision","feedback_artifact","repository","software_project","software_package","code_change","git_commit","breaking_change","tester_feedback","product_feedback","mcp_tool","mcp_endpoint","query_example"] |
| Operational entity types | ["doc_page","api_reference","documentation_decision","documentation_feedback","gist","reference","query_example","strategy_drift_signal"] |
| Output format | ## Output format

Always end your response with a single artifact-header line that Anthus uses to mark the gate satisfied. The exact format:

`[<agent_name>] <artifact_kind>: <body>`

Where `<artifact_kind>` is fixed per agent (see below) and `<body>` is your structured result OR the literal token `BLOCKED — <one-line reason>` when you cannot produce the artifact (missing data, wrong agent for the task, scope violation, etc.). Always emit the header even on refusal — Anthus parses it to advance state.

For this agent, the header is:

`[regulus] docs_diff_or_no_change_note: <body>`

### Strategy drift signal (optional second line)

If during your work you observed evidence that contradicts your current agent_strategy (e.g., a recurring pattern of customer signals invalidating an assumption), append on a new line:

`[regulus] strategy_drift_signal: <one-line observation>`

Onychomys digests these. They are how the swarm learns. Omit when nothing material surfaced.
 |
| Entity ID | ent_46f3385204e51cd91efd1ab3 |

---

Operational prompt: [`.claude/skills/regulus/SKILL.md`](../../.claude/skills/regulus/SKILL.md)

*Reference card mirrored from Neotoma `agent_definition` `ent_46f3385204e51cd91efd1ab3`. Do not edit directly — correct the entity and run `python3 execution/scripts/render_agent_docs.py`.*
