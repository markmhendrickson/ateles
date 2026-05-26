---
name: process-feedback
description: "Parse product feedback by bucket and assess it against release stage constraints, with Neotoma developer-release defaults."
triggers:
  - process feedback
  - feedback triage
  - assess feedback
  - release-stage feedback
  - /process-feedback
user_invocable: true
entity_id: ent_7bfe5981147fe99d40147790
---

# Process Feedback

Classify incoming product feedback before evaluating it, then score recommendations against the current release stage.

Default stage profile: `Neotoma developer release`.

## Inputs

- `feedback_text` (required): Raw feedback message(s), notes, screenshot transcript, or call summary.
- `product_name` (required): Product under review (for example, `Neotoma`).
- `release_stage` (required): Stage context (for example, `developer_release`, `alpha`, `beta`, `ga`).
- `stage_principles` (optional): Stage-specific goals, constraints, and positioning. If omitted for Neotoma developer release, use Neotoma repo [`docs/foundation/developer_release_principles.md`](https://github.com/markmhendrickson/neotoma/blob/main/docs/foundation/developer_release_principles.md).
- `feedback_source` (optional): Person/channel metadata.

## Core Classification Buckets

Classify each feedback claim into one primary bucket:

1. **Structural validity**
   - Question: Does this claim reveal that the problem is real, urgent, or inevitable?
   - Typical examples: "Users cannot complete setup", "State drift causes failures", "No audit trail for mutations".

2. **Addressability timing**
   - Question: Does this matter for users we are targeting at this stage, right now?
   - Typical examples: requests from non-ICP users, scale asks before model validation, broad onboarding during narrow release.

3. **Communication / legibility**
   - Question: Does this expose confusion in wording, naming, architecture explanation, or category framing?
   - Typical examples: "Looks like another AI memory app", "I don't understand deterministic memory vs RAG".

If a claim spans multiple buckets, pick a primary bucket and list secondary buckets.

## Stage-Aware Assessment Rules

Evaluate each claim with this sequence:

1. **Extract atomic claims**
   - Break compound feedback into distinct claims.
   - Preserve original wording as evidence.

2. **Bucket each claim**
   - Assign primary bucket and optional secondary bucket(s).

3. **Assess stage fit**
   - Compare claim against release goals and constraints.
   - Mark one: `aligned`, `misaligned`, or `mixed`.

4. **Determine action**
   - Mark one: `keep_now`, `defer`, `discard_for_stage`.
   - Provide a one-sentence rationale tied to stage constraints.

5. **Define evidence threshold**
   - Specify what evidence would upgrade or downgrade confidence.

## Neotoma Developer Release Defaults

Use these defaults when `product_name=Neotoma` and `release_stage=developer_release`:

- **Core framing**
  - Deterministic state infrastructure for long-running agents.
  - Not a generic AI memory retrieval tool.
- **Primary goals**
  - Validate memory invariant.
  - Attract builders with real state problems.
  - Generate architectural feedback.
  - Prove deterministic model in real workflows.
- **Primary constraints**
  - Avoid consumer framing.
  - Avoid over-promising.
  - Avoid feature creep away from determinism/replayability/inspectability.
- **Preferred language**
  - deterministic, versioned, replayable, auditable, schema-bound, state evolution.
- **Avoid language**
  - smart, powerful, intelligent, magical.
- **Success signals**
  - Architectural questions, serious technical feedback, private builder engagement.
- **Non-goal signals**
  - Viral/social engagement, broad top-of-funnel metrics.

## Output Format

Return results in this structure:

```markdown
## Feedback Assessment: <product_name> (<release_stage>)

### Stage Profile
- Goals: ...
- Constraints: ...
- Non-goals: ...

### Claim Table
| Claim | Bucket | Stage Fit | Action | Why |
|---|---|---|---|---|
| ... | Structural validity | aligned | keep_now | ... |

### High-Leverage Actions (Now)
1. ...
2. ...

### Deferred Items
- ...

### Discarded For This Stage
- ...

### Confidence + Evidence Needed
- ...
```

## Decision Heuristics

- Prioritize **bucket 1** claims when they indicate real structural failure.
- Treat many emotionally destabilizing comments as **bucket 2 or 3** until proven structural.
- For narrow early releases, reject broad-market optimization requests unless they improve stage goals directly.
- If feedback attacks framing precision, fix messaging quickly even if product mechanics are correct.

## Optional Persistence

When Neotoma memory is available, store:

- the raw feedback artifact,
- atomic claims,
- bucket labels,
- stage-fit decisions,
- chosen action (`keep_now` / `defer` / `discard_for_stage`),
- follow-up task links.
