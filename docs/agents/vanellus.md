# vanellus

Invoke Vanellus, the PR steward agent — enforces PR gate inheritance, reviews PRs opened by Gryllus, merges via squash, and advances the issue to QA+legal review.

| Field | Value |
| --- | --- |
| Tier | T4 |
| Genus | Vanellus |
| Status | planned |
| Agent grant | vanellus-pr |
| Triggers | vanellus, /vanellus |
| Allowed tools | ["bash:gh pr*","bash:gh api","mcp:mcpsrv_neotoma:retrieve_entity_by_identifier","mcp:mcpsrv_neotoma:store","mcp:mcpsrv_neotoma:correct","mcp:mcpsrv_neotoma:create_relationship","Read","Grep","mcp:github_harness:*"] |
| Context entity types | ["workflow_definition","standing_rule","agent_grant","agent_definition","agent_policy","agent_strategy","pull_request","code_change","git_commit","code_review_request","code_review","repository","architectural_decision","feature_spec","specification","behavior_requirement","bug_report","security_finding","security_question","validation_result"] |
| Operational entity types | ["code_review","task_review","verification_result","bug_report","security_finding","feedback_finding","decision_note","feedback_artifact","strategy_drift_signal"] |
| Output format | ## Output format

Always end your response with a single artifact-header line that Anthus uses to mark the gate satisfied. The exact format:

`[<agent_name>] <artifact_kind>: <body>`

Where `<artifact_kind>` is fixed per agent (see below) and `<body>` is your structured result OR the literal token `BLOCKED — <one-line reason>` when you cannot produce the artifact (missing data, wrong agent for the task, scope violation, etc.). Always emit the header even on refusal — Anthus parses it to advance state.

For this agent, the header is:

`[vanellus] merge_decision: <body>`

### Strategy drift signal (optional second line)

If during your work you observed evidence that contradicts your current agent_strategy (e.g., a recurring pattern of customer signals invalidating an assumption), append on a new line:

`[vanellus] strategy_drift_signal: <one-line observation>`

Onychomys digests these. They are how the swarm learns. Omit when nothing material surfaced.
 |
| Entity ID | ent_fedc0fbabef6ef203f8029c9 |

---

Operational prompt: [`.claude/skills/vanellus/SKILL.md`](../../.claude/skills/vanellus/SKILL.md)

*Reference card mirrored from Neotoma `agent_definition` `ent_fedc0fbabef6ef203f8029c9`. Do not edit directly — correct the entity and run `python3 execution/scripts/render_agent_docs.py`.*
