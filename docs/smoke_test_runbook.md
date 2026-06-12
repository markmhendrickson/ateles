# Swarm smoke-test runbook

End-to-end test of the agent swarm against `markmhendrickson/harness-sandbox`.

## Purpose

Operationalize the agent-swarm smoke test described in `swarm_orchestration.md`. Provides two execution procedures (operator-driven now, Anthus-driven once orchestrator is deployed) and success criteria for evaluating whether the swarm coordinates correctly.

## Scope

Covers running the smoke test against the harness-sandbox repo end to end, the expected artifacts per agent, and the success/failure interpretation rubric. Does not cover real production workflows; for those see `swarm_orchestration.md`.

## Procedures

Two procedures:

1. **Operator-driven** — you invoke each agent manually in the right order. Tests agent definitions in isolation; works today.
2. **Anthus-driven** — the orchestrator daemon sequences agents automatically. Tests the full system; works once Anthus is deployed against the sandbox.

## Test scenario

Issue: [markmhendrickson/harness-sandbox#1](https://github.com/markmhendrickson/harness-sandbox/issues/1) — "Add 'subscribe to updates' email form to README"

Workflow: `workflow_definition:harness-sandbox|smoke_test_full_lifecycle` (`ent_d0fe2d731da197cd722d3a67`)

Labels driving the run: `smoke-test`, `customer-facing`, `needs-product-review`, `triage-full-lifecycle`

Expected agent involvement (12):

| Phase | Gate | Agent | Expected artifact |
|---|---|---|---|
| 1 | pm_scope | Pavo | `[pavo] acceptance_criteria:` |
| 2 | ux_design | Manucode | `[manucode] copy_and_ux_flow:` |
| 2 | arch | Waxwing | `[waxwing] schema_or_api_proposal:` |
| 3 | impl | Cicada | `[cicada] pull_request_link: #N` |
| 4 | qa | Phoenicurus | `[phoenicurus] test_plan:` |
| 4 | legal | Buteo | `[buteo] compliance_review:` |
| 4 | compliance_supervisor | Robin | `[robin] compliance_verdict:` |
| 4 | pr_review | Vanellus | `[vanellus] merge_decision:` |
| 5 | release | Struthio | `[struthio] release_note:` |
| 6 | growth_announce | Accipiter | `[accipiter] launch_brief:` |
| 6 | social_draft | Corvus | `[corvus] social_post_draft:` |
| 6 | devrel_docs | Regulus | `[regulus] docs_diff_or_no_change_note:` |

The artifact-header convention (`[<agent>] <artifact_type>:`) is how Anthus recognizes a gate as satisfied. Each agent's SKILL.md should produce a comment with this header.

## Operator-driven procedure

Use this today to validate agent SKILL.md content and dispatch behavior before Anthus is wired to the sandbox.

```bash
# Setup: env for github_harness PAT (Phase 5 wiring)
export HARNESS_GRANTS_JSON="$(cd ~/repos/mcp-server-github-harness && node scripts/render-grants.mjs --inline)"
export GITHUB_PAT_MARKMHENDRICKSON_HARNESS_SANDBOX="op://Private/ateles-agent-github-pat/token"

# Phase 1
claude --print --skill pavo <<<'{"work_entity_id":"harness-sandbox#1","gate_name":"pm_scope","expected_artifact":"acceptance_criteria"}'

# Verify Pavo's comment landed on issue #1 with the right header. Then:

# Phase 2 (parallel)
claude --print --skill manucode <<<'{...gate_name: ux_design...}' &
claude --print --skill waxwing <<<'{...gate_name: arch...}' &
wait

# Phase 3
claude --print --skill cicada <<<'{...gate_name: impl...}'

# Phase 4 (qa + legal parallel; compliance_supervisor + pr_review sequential)
claude --print --skill phoenicurus <<<'{...}' &
claude --print --skill buteo <<<'{...}' &
wait
claude --print --skill robin <<<'{...}'
claude --print --skill vanellus <<<'{...}'

# Phase 5
claude --print --skill struthio <<<'{...}'

# Phase 6 (parallel)
claude --print --skill accipiter <<<'{...}' &
claude --print --skill corvus <<<'{...}' &
claude --print --skill regulus <<<'{...}' &
wait
```

After each phase, verify the GitHub issue/PR shows the expected comment(s). Record observations:

- Did the agent produce the right artifact header?
- Was the content actually useful, or generic boilerplate?
- Did the agent recognize when a phase didn't apply (e.g. Regulus on a README-only change)?

Bugs found go in a `swarm-quality` Neotoma collection for SKILL.md tuning.

## Anthus-driven procedure (once orchestrator is deployed)

Anthus subscribes to Neotoma SSE for `issue`, `pull_request`, `task`, `daemon_report`, `escalation`, `agent_grant`. When an event arrives for a work entity tagged with a workflow's labels, Anthus:

1. Selects the matching workflow_definition
2. Fetches all comments on the GitHub issue/PR via `github_harness`
3. Computes ready gates via `orchestrator.compute_ready_gates`
4. Dispatches `claude --print --skill <owner_agent>` for each ready gate
5. On next tick (next event), re-evaluates and either advances to the next phase or waits

Operator role:

- File the issue with the right labels
- Wait for Telegram notifications as agents progress
- Intervene only when an agent posts a `BLOCKER` or `OPERATOR_DECISION` via `lib/notify/`

To trigger Anthus dispatch manually for one entity (useful while debugging):

```bash
# From the ateles worktree
.venv/bin/python -c "
import asyncio
from execution.daemons.anthus.orchestrator import fetch_workflow_definitions, select_workflow, compute_ready_gates

async def main():
    workflows = await fetch_workflow_definitions('harness-sandbox')
    wf = select_workflow({'labels': ['smoke-test', 'customer-facing']}, workflows)
    # ...fetch issue + comments via gh CLI...
    state, ready = compute_ready_gates(wf, work_entity, comments)
    print('Ready gates:', [g.gate_name for g in ready])

asyncio.run(main())
"
```

## Success criteria

A passing smoke test:

1. All 12 agents produce a comment with the correct artifact header
2. Phase ordering is correct: no phase-N agent acts before phase-N-1 satisfaction
3. Parallel groups (phase 2, phase 4 qa/legal, phase 6) fire concurrently
4. Fast paths skip the right gates when their conditions hit (run with `label:internal-only` once to verify phase 6 skips)
5. The final PR opened by Cicada actually changes the README sensibly
6. No agent produces a generic "I cannot help with this" response

Failures of (1) or (2) usually mean SKILL.md fixes. Failures of (3)–(4) mean orchestrator bugs. (5)–(6) are agent-quality issues.
