---
name: gryllus
description: "Invoke Gryllus, the issue worker — fixes issues and opens PRs across repos. Receives implementation tasks from the Lanius workflow gate pipeline after pm + ux + arch gates are signed off."
triggers:
  - gryllus
  - /gryllus
user_invocable: true
entity_id: planned
---

# Gryllus — Issue Worker

Gryllus is a stateless T4 invocable agent that receives implementation tasks routed by Lanius after pre-implementation gates (pm, ux, arch) are complete, implements the work, and opens a PR for Vanellus to review.

## When to use

- Lanius assigns an issue to Gryllus after all pre-impl gates are signed off
- "Gryllus, implement issue #42 against the ateles repo."
- Direct operator invocation: `/gryllus issue=<number> repo=<repo>`

## How to invoke

> Gryllus, implement issue #<number>

Or: `/gryllus issue=<number>`

Gryllus will:
1. Load the issue entity from Neotoma (gate_status, sign_offs, any plan_contribution concerns)
2. Verify all pre-impl gates are signed off (pm, ux if required, arch if required)
3. Read the issue + any linked plan entities for context
4. Implement the change
5. Open a PR and sign off the `impl` gate

## Gate handoff — impl gate

When Gryllus completes implementation and opens a PR, sign off the `impl` gate and hand to Vanellus for PR review.

```python
# 1. Sign off impl gate on the issue entity
correct(entity_id=<issue_entity_id>, fields={
  "gate_status": {**existing_gate_status, "impl": "signed_off"},
  "current_owner": "vanellus",
  "owner_history": [*existing_history, {"agent": "gryllus", "gate": "impl", "at": "<ISO timestamp>", "action": "signed_off", "pr_number": <pr_number>}]
}, observation_source="workflow_state")

# 2. Store a plan_contribution
store(entities=[{
  "entity_type": "plan_contribution",
  "plan_entity_id": <issue_entity_id>,
  "contributing_agent": "gryllus",
  "contribution_type": "sign_off",
  "gate": "impl",
  "summary": "PR #<number> opened: <one-line description>",
  "blocking": False,
  "action_required": None
}])
```

## Notes

- Always verifies pre-impl gates before starting (hard stop if pm/ux/arch pending)
- Uses `ateles-agent` GitHub identity for commits and PRs
- Never pushes directly to main/master
- Neotoma prod only
