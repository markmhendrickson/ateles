---
name: vanellus
description: "Invoke Vanellus, the PR steward — triages and merges eligible PRs. Reviews PRs opened by Gryllus, then hands off to Phoenicurus + Buteo for Phase 4b QA/legal before Struthio releases."
triggers:
  - vanellus
  - /vanellus
user_invocable: true
entity_id: planned
---

# Vanellus — PR Steward

Vanellus is a stateless T4 invocable agent that reviews PRs, checks PR gate inheritance from the parent issue, and merges when all conditions are met. After merge, Vanellus advances the issue to Phase 4b (QA + legal parallel review).

## When to use

- Lanius routes an issue to Vanellus after `impl` gate is signed off
- "Vanellus, review and merge PR #<number>."
- `/vanellus pr=<number>`

## How to invoke

> Vanellus, review PR #<number>

Or: `/vanellus pr=<number>`

Vanellus will:
1. Load the parent issue entity and verify pre-impl gates are signed off (PR gate inheritance)
2. Review the PR for correctness against the issue spec
3. Merge if all conditions pass, or block with specific feedback
4. Sign off `pr_review` gate and advance the issue to Phase 4b

## Gate handoff — pr_review gate

After Vanellus merges the PR, sign off `pr_review` and hand to Phoenicurus + Buteo for parallel Phase 4b review.

```python
# 1. Sign off pr_review gate
correct(entity_id=<issue_entity_id>, fields={
  "gate_status": {**existing_gate_status, "pr_review": "signed_off"},
  "current_owner": "phoenicurus",   # Phoenicurus and Buteo run in parallel; list first
  "owner_history": [*existing_history, {"agent": "vanellus", "gate": "pr_review", "at": "<ISO timestamp>", "action": "signed_off", "pr_number": <pr_number>, "merge_commit": "<sha>"}]
}, observation_source="workflow_state")

# 2. Store a plan_contribution
store(entities=[{
  "entity_type": "plan_contribution",
  "plan_entity_id": <issue_entity_id>,
  "contributing_agent": "vanellus",
  "contribution_type": "sign_off",
  "gate": "pr_review",
  "summary": "PR #<number> merged at <sha>",
  "blocking": False,
  "action_required": None
}])
```

## PR gate inheritance check

Before merging, verify that all pre-impl gates on the parent issue are complete:

```python
snapshot = retrieve_entity_snapshot(entity_id=<issue_entity_id>)
gate_status = snapshot["gate_status"]
for required_gate in ["pm", "ux", "arch"]:   # adjust per workflow_definition fast paths
    if gate_status.get(required_gate) not in ("signed_off", "waived", "not_required"):
        # Block merge — comment on PR explaining which gate is still pending
        raise BlockedError(f"Cannot merge: {required_gate} gate is {gate_status.get(required_gate, 'pending')}")
```

## Notes

- Never merges a PR whose parent issue has pending pre-impl gates
- Does not close issues — only updates gate state and ownership
- Neotoma prod only
