---
name: prioritize-plans
description: "Portfolio-mode prioritisation of all outstanding Neotoma/Ateles plans against tracked ICP feedback and needs. Enumerates live plan entities, grounds them in the latest aggregate ICP-feedback synthesis, scores each against the plan_prioritization_rubric, and emits a single ranked sequence with rationale and confidence. This is the batch harness around Pavo's per-plan prioritisation reasoning — Pavo evaluates one plan at a time; this skill evaluates the whole portfolio in one pass and persists a plan_prioritization entity. Use when the operator asks to prioritise plans, rank the backlog, decide what to build next, or evaluate outstanding plans against ICP needs."
triggers:
  - prioritize plans
  - prioritise plans
  - rank plans
  - rank the backlog
  - what should we build next
  - evaluate plans against icp
  - /prioritize-plans
user_invocable: true
---

# Prioritize Plans — Portfolio-mode ICP-grounded plan ranking

Rank **all outstanding plans** against tracked ICP feedback/needs in one pass, and emit a single defensible sequence. This is the batch counterpart to Pavo's per-plan review: Pavo (the product-manager agent) supplies the prioritisation reasoning per item; this skill is the harness that enumerates the portfolio, grounds it in ICP evidence, applies the rubric uniformly, and persists the ranked output.

## When to use

- "Prioritise the outstanding plans / the backlog."
- "What should we build next for Neotoma's Tier 1 ICP?"
- "Evaluate all outstanding plans against ICP feedback."
- Periodic portfolio review (pairs well with `quarterly-portfolio-review` at a coarser grain).

For prioritising a **single** plan or a small named set, invoke Pavo directly (`/pavo`) — you do not need the batch harness.

## Inputs this skill requires (verify before scoring)

1. **The prioritisation rubric** — `plan_prioritization_rubric` entity (canonical: `plan_prioritization_rubric:default`, `ent_…`). This is distinct from the `priority_rubric` entity, which is a *notification-routing* rubric and MUST NOT be used here. Resolve via `retrieve_entities(entity_type='plan_prioritization_rubric')`. If none exists, STOP and tell the operator to create one (or offer to scaffold it) — do not invent ad-hoc scoring criteria, because that makes runs non-comparable.
2. **A current ICP-feedback aggregate** — the most recent `feedback_aggregate_analysis` entity. Check its `analysis_date`. If older than **14 days**, or if substantially more `product_feedback` / `feedback_analysis` items exist than it covered (`items_analyzed_count`), first run `/analyze-neotoma-feedback --aggregate` to refresh it, then proceed. Record which aggregate entity_id you grounded on.
3. **The outstanding plan set** — see Step 2 for the status filter.

## Step 0: Resolve rubric + ICP evidence

1. `retrieve_entities(entity_type='plan_prioritization_rubric')` → load the active rubric. Capture its axes and weights.
2. `retrieve_entities(entity_type='feedback_aggregate_analysis', sort_by='last_observation_at', sort_order='desc', limit=1)` → load the latest aggregate. Apply the freshness rule above. Capture `axis_themes` (especially `need_validation`, `activation_conditions`, `solution_efficacy`), `confidence_delta`, and `proposed_doc_edits` — these are the ICP "needs" each plan is scored against.
3. Optionally also pull the canonical ICP docs (`docs/icp/primary_icp.md`, `developer_release_targeting.md`) from the `../neotoma` repo when available, to ground archetype/tier references. Skip with a `[gap: <path>]` note if the repo is not checked out.

## Step 1: Frame the decision

State, in one or two lines, what the operator is deciding with this ranking (e.g. "the next 2–3 weeks of build sequencing for the Neotoma developer release"). Name the time horizon and any hard constraints (a release date, a blocking dependency) up front. This anchors the rubric weighting.

## Step 2: Enumerate outstanding plans

1. `retrieve_entities(entity_type='plan', include_snapshots=false, limit=100)` and paginate via `offset` until exhausted. There are hundreds of plan entities — page through all of them.
2. **Filter to outstanding.** Exclude:
   - plans whose `status` is `done` / `shipped` / `cancelled` / `superseded`,
   - plans superseded by a newer version (e.g. "… v9" when "… v11" exists — keep only the latest; record the supersession),
   - merged entities (`merged_to_entity_id` set).
   Use `include_snapshots=true` in a second targeted pass (batched) only for the candidates that survive the lightweight filter, to read `status`, `tags`, `next_steps`, and `body` summary.
3. Produce the candidate list: `entity_id`, name, one-line scope, current `status`, `tags`, `last_observation_at`.

## Step 3: Score each plan against the rubric (Pavo's reasoning, applied uniformly)

For every candidate plan, apply each rubric axis and produce a per-axis score plus a one-line justification grounded in a **specific ICP-feedback signal** (cite the aggregate theme or a `feedback_analysis` entity_id). Default rubric axes (the rubric entity is authoritative — use its axes/weights if they differ):

| Axis | Question | Evidence source |
|---|---|---|
| `icp_need_alignment` | Does this plan directly address a validated ICP pain or activation precondition? | aggregate `need_validation` / `activation_conditions` themes |
| `evidence_strength` | How strong/recent is the feedback signal behind it? | item counts, confidence_delta, recency |
| `gate_unblock_leverage` | Does it unblock a developer-release gate or downstream plans? | release_gate / developer_release_targeting, plan DEPENDS_ON graph |
| `effort_cost` | Rough effort to ship (inverse-weighted). | plan `todos` size, scope |
| `strategic_fit` | Alignment with current positioning / first principles. | strategic_analysis, foundation docs |
| `reversibility_risk` | Cost of getting it wrong / hard to reverse. | plan body, architecture tags |

Compute a weighted composite per the rubric. Keep each plan's evidence citation explicit — a score without a cited ICP signal is not admissible (mark it `unsupported` and rank it below supported items).

Do not score features into existence: this skill ranks **existing** plans only. If a high-signal ICP need has **no** plan addressing it, record it under "Gaps — needs with no plan" rather than inventing a plan.

## Step 4: Emit the ranked sequence

Produce a single ordered list, highest priority first. For each: rank, plan name + entity_id, composite score, the one-line rationale, confidence (`high`/`medium`/`low`), and the key assumption the ranking depends on. Group into tiers (e.g. Now / Next / Later) if that aids the operator. Then:

- **Gaps — ICP needs with no plan**: validated needs from the aggregate that no outstanding plan addresses.
- **Supersession / hygiene notes**: duplicate or stale plans found during enumeration (candidates for `SUPERSEDES` / status correction).
- **Open questions**: only those that actually block sequencing (keep short).

## Step 5: Persist to Neotoma (same turn)

Follow the Neotoma turn lifecycle. Store one `plan_prioritization` entity (reuse the type if it already exists — `list_entity_types` keyword `priorit`; only create if absent). Fields:
- `title`, `decision_framed`, `time_horizon`
- `rubric_entity_id`, `aggregate_feedback_entity_id` (the evidence you grounded on)
- `ranked_plans` (array of `{ rank, plan_entity_id, plan_name, composite_score, axis_scores, rationale, confidence, key_assumption, evidence_entity_ids }`)
- `needs_without_plan` (array of `{ need, backing_entity_ids }`)
- `supersession_notes` (array of `{ stale_plan_id, superseded_by_id }`)
- `open_questions`
- `plans_evaluated_count`, `plans_excluded_count`
- `data_source`

Relationships: `REFERS_TO` from the `plan_prioritization` entity to the rubric, the aggregate, and the top-N ranked plans; `REFERS_TO` from the current `agent_message` to the `plan_prioritization` entity.

If hygiene issues are clear-cut (a `v9` plan plainly superseded by `v11`), apply a `status` correction and a `SUPERSEDES` relationship as a Tier-1 auto-fix; surface anything ambiguous as an open question instead.

## Step 6: Surface to the operator

Reply with: the framed decision, the Now/Next/Later ranking (top items with one-line rationale + confidence each), the ICP-needs-without-a-plan list, hygiene notes, and the entity_id of the stored `plan_prioritization`. Render Created/Retrieved entities per the display rule.

## Relationship to other skills/agents

- **Pavo (`/pavo`)** — owns the per-plan prioritisation reasoning and the plan-review gate protocol. This skill applies that reasoning across the whole portfolio; hand individual deep-dives back to Pavo.
- **`analyze-neotoma-feedback --aggregate`** — produces the `feedback_aggregate_analysis` this skill consumes. Run it first when the aggregate is stale.
- **`process-feedback`** — release-stage triage of individual feedback items (upstream of the aggregate).
- **`quarterly-portfolio-review`** — coarser, periodic strategic review; this skill is the plan-grained operational ranking.
- **`update-plan` / `update-tasks`** — apply the resulting sequencing decisions back onto plan/task entities.

## Constraints

- **Rubric is mandatory.** Never fall back to ad-hoc scoring. No rubric → stop.
- **Never use `priority_rubric` for scoring** — it is the notification-routing rubric, not the plan-prioritisation rubric.
- **Every score cites ICP evidence.** Uncited scores rank below cited ones and are flagged `unsupported`.
- **Rank existing plans only** — do not mint new plans; record uncovered needs as gaps.
- **Stale evidence is disclosed.** Always state which aggregate (date, N) the ranking is grounded on; refresh if stale before ranking.
