# Swarm orchestration

How the Ateles agent swarm decides which agents participate in which work.

## Purpose

Define the data model and runtime behavior by which Anthus (the swarm coordinator) decides which agents engage with which issues, PRs, plans, and tasks. Covers both the explicit gate-based model in production today and the contract-based emergent model planned for Phase 6.

## Scope

Covers the orchestrator's input data (workflow_definition, agent_definition), evaluation logic (phase ordering, parallel groups, fast paths), dispatch surface (`claude --print --skill`), and persistence model. Does not cover individual agent prompts or the github_harness MCP server — see those projects for their respective concerns.

## Two models

### Today: gate-based (workflow_definition)

The current model is **explicit gates with phase ordering**. Each `workflow_definition` entity declares:

- `gates[]`: each with `phase`, `gate_name`, `owner_agent`, `parallel_group`, `join_gate`, `required`
- `fast_paths[]`: label-conditional gate skips (e.g. `label:no-ui-changes` skips `ux_design`)
- `legal_required`: signals whether a legal gate must always run

Anthus (T3 daemon) loads workflow_definitions at startup and on every Neotoma event:

1. Selects the applicable workflow via `select_workflow` (label override → label match → default `feature`)
2. Walks phases in order, dispatching all ready gates in a given phase before moving on
3. Tracks per-gate state in memory (will persist to Neotoma in Phase 6+)
4. A gate is **satisfied** when its `owner_agent` posts a comment beginning `[<agent>] <artifact_type>:`

Gate satisfaction rules live in `execution/daemons/anthus/orchestrator.py:GATE_SATISFACTION_RULES`. The artifact-header convention (`[<agent>] <type>:`) is enforced in each agent's SKILL.md.

Strengths:

- Explicit. Easy to reason about.
- Maps directly to the existing `workflow_definition` schema.
- Fast paths handle obvious skip cases (no-UI, no-data, internal-only).

Limits:

- Static. Adding a new agent means editing every relevant workflow_definition.
- Phase ordering is brittle for emergent quality concerns (e.g. accessibility, privacy) that may apply to any work.
- Doesn't express *why* an agent participates — only *when*.

### Tomorrow: contract-based emergent participation

The Phase 6 evolution replaces phases with **declarations**:

Each agent's `agent_definition` carries a new `participant_contract` field:

```jsonc
{
  "agent": "buteo",
  "produces": ["compliance_review"],
  "consumes": [],
  "approves": ["legal_clearance"],
  "observes": [],
  "triggers": {
    // Any of these match → Buteo participates.
    "any_of": [
      { "labels_contains_any": ["legal", "gdpr", "privacy"] },
      { "body_matches": "/gdpr|privacy|tos|license|trademark/i" },
      { "audience": "customer", "touches_paths": ["*"] }
    ]
  },
  "required_when": {
    "labels_contains_any": ["customer-facing", "needs-legal"]
  }
}
```

The orchestrator's loop becomes:

1. On any event affecting a work entity, evaluate every agent's `triggers`.
2. For each trigger match without a `participation_record` (or with a stale `dispatched` record), dispatch the agent.
3. When an agent produces an artifact, create a `participation_record` and emit an event so step 1 re-runs.
4. When all agents with `required_when` matched have status ∈ {`produced`, `approved`, `skipped`}, the work item is **release-ready**.

Why this is better:

- **Density emerges from work signals.** A trivial typo: trigger predicates of expensive agents don't match → only Cicada runs. A customer-facing privacy-touching feature: 12+ triggers match → 12 agents pile on.
- **Adding an agent is local.** Define its `participant_contract` and triggers; it joins all matching work automatically.
- **`required_when` distinguishes "should participate" from "must produce".** A blocking review (Buteo on a legal-tagged PR) is `required_when`. An advisory observation (Buteo notices a small concern on a non-tagged PR) is `triggers` only — informational, not blocking.

### Schemas needed for the evolution

| Schema | Purpose |
|---|---|
| `agent_definition.participant_contract` | Extend existing schema with this nested object |
| `participation_record` | New entity type: `{work_entity_id, agent, status, produced[], started_at, finished_at}` |
| `quality_signal` | New entity type: optional but recommended. `{work_entity_id, signal: "impact_score" | "risk_score" | "audience", value, source_agent}`. Lets triggers reference signals other agents have written. |

Issue: [ateles#TBD — emergent participation Phase 6 evolution](https://github.com/markmhendrickson/ateles/issues).

## How dispatch happens

Anthus uses the same `_spawn_claude_skill` pattern as Formica and neotoma-agent:

```python
cmd = [CLAUDE_BIN, "--print", "--skill", owner_agent_skill]
await asyncio.create_subprocess_exec(*cmd, stdin=PIPE, stdout=PIPE, stderr=PIPE)
await proc.communicate(input=json.dumps({
    "work_entity_id": entity_id,
    "snapshot": snapshot,
    "comments": comments,
    "workflow_definition_id": workflow.entity_id,
    "gate_name": gate.gate_name,
    "expected_artifact": GATE_SATISFACTION_RULES[gate.gate_name],
}).encode())
```

The agent receives full context — the work entity, prior agent artifacts, and which artifact it's expected to produce. It uses `github_harness` MCP for any GitHub-side interaction (commenting, opening PRs).

## Smoke test

See [smoke_test_runbook.md](smoke_test_runbook.md) for the operator-driven and Anthus-driven test procedures against `markmhendrickson/harness-sandbox`.
