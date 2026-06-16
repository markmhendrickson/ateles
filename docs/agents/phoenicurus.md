# phoenicurus

Invoke Phoenicurus, the QA agent — test coverage audits, regression assessment, release readiness scorecards, and P0 edge case identification.

| Field | Value |
| --- | --- |
| Tier | T4 |
| Genus | Phoenicurus |
| Status | planned |
| Agent grant | service |
| Triggers | phoenicurus, /phoenicurus |
| Allowed tools | [], ["mcp:mcpsrv_neotoma:retrieve_entities","mcp:mcpsrv_neotoma:store","mcp:mcpsrv_neotoma:correct","bash:pytest","bash:npm test","bash:gh pr checks","Read","Grep","Bash"] |
| Context entity types | ["workflow_definition","standing_rule","agent_grant","agent_definition","agent_policy","agent_strategy","test_plan","coverage_record","bug_report","ui_bug_report","ui_bug","ui_issue","software_issue","technical_issue","validation_result","verification_result","audit_result","audit_run","feedback_finding","neotoma_qa_finding","accessibility_audit","security_finding","behavior_requirement","feature_spec","specification","error_event","runtime_error","javascript_error","frontend_error","frontend_runtime_error","console_error","incident","health_event","release_gate","release_criterion"] |
| Operational entity types | ["test_plan","coverage_record","validation_result","verification_result","bug_report","neotoma_qa_finding","release_gate","audit_run","strategy_drift_signal"] |
| Output format | ## Output format

Always end your response with a single artifact-header line that Anthus uses to mark the gate satisfied. The exact format:

`[<agent_name>] <artifact_kind>: <body>`

Where `<artifact_kind>` is fixed per agent (see below) and `<body>` is your structured result OR the literal token `BLOCKED — <one-line reason>` when you cannot produce the artifact (missing data, wrong agent for the task, scope violation, etc.). Always emit the header even on refusal — Anthus parses it to advance state.

For this agent, the header is:

`[phoenicurus] test_plan: <body>`

### Strategy drift signal (optional second line)

If during your work you observed evidence that contradicts your current agent_strategy (e.g., a recurring pattern of customer signals invalidating an assumption), append on a new line:

`[phoenicurus] strategy_drift_signal: <one-line observation>`

Onychomys digests these. They are how the swarm learns. Omit when nothing material surfaced.
 |
| Entity ID | ent_42843b65dd18fc39294e94a1 |

---

Operational prompt: [`.claude/skills/phoenicurus/SKILL.md`](../../.claude/skills/phoenicurus/SKILL.md)

*Reference card mirrored from Neotoma `agent_definition` `ent_42843b65dd18fc39294e94a1`. Do not edit directly — correct the entity and run `python3 execution/scripts/render_agent_docs.py`.*
