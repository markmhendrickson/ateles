---
name: publish
description: Publish workflow per foundation publish command.
triggers:
  - publish
  - /publish
user_invocable: true
entity_id: ent_7cebd7eff619334df4bc1716
---

## Summary

**Publish Modes:**
1. `/publish` - Publish main repository (detect planned vs incremental)
2. `/publish <submodule>` - Publish specific submodule only

**Release Types:**
- **Planned** (vX.Y.0): Detected from commits, uses existing release document
- **Incremental** (vX.Y.Z): Auto-generated, creates new release document

**Workflow:**
Validate → Merge → Detect Release Type → Version → Document → Commit → Tag → Deploy
