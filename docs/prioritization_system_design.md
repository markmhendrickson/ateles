# Prioritization System Design — funnel-first, instrumentation-gated plan prioritization

**Status:** design (no schemas registered yet) · **Owner:** Pavo (PM), with Columba (strategy)
**Canonical entity:** Neotoma `plan` (this file is the repo mirror)
**Created:** 2026-05-29

## Purpose

Re-base how Pavo and related agents prioritize work. Instead of asking an agent to read raw
evaluator feedback and rank plans directly, prioritization runs through a cascading model:
intended experience flow → aha/pain opportunities per stage per ICP → instrumentation coverage →
constraint identification → ranked plans, with people mapped to the plans that unblock them.

## Decisions locked (2026-05-29)

- **Source of truth:** Full Neotoma-canonical. The strategy layer (funnel, opportunities, ICP
  definitions, instrumentation specs, person lifecycle state, prioritization output) lives as
  Neotoma entities and is mirrored to the repo via Apus (same generated-file pattern as agent
  skills). Long-form narrative is also Neotoma-canonical and generated into the repo — accepting
  higher edit-friction (edits go through corrections) in exchange for one queryable graph and the
  elimination of the "repo not checked out → can't read ICP" blind spot.
- **Framework depth:** Full stack (see below).
- **Mode:** Keep designing — this plan is the design; schema registration and the skill rewrite
  come after the funnel stages and per-ICP aha/blocker definitions are confirmed.

## Framework stack (grounded in PM/marketing best practice)

| Layer | Framework | Role |
|---|---|---|
| Funnel spine | AARRR (Pirate Metrics) | ordered stages |
| Activation decomposition | Reforge Setup → Aha → Habit | activation is 3 sub-moments, not one |
| Aha / pain | Jobs-to-be-Done Four Forces (push, pull, anxiety, habit-inertia) | *why* people stick or bounce |
| Hierarchy | Opportunity-Solution Tree (Teresa Torres) | outcome → opportunity → solution → evidence |
| Instrumentation | Goals → Signals → Metrics (HEART method) | turn "instrument it" into named metrics |
| Scoring | Theory of Constraints + RICE | attack the one constraint; Reach = users at that stage |

## The experience funnel

Install ≠ activation. A distinct **Evaluation / Trial** stage sits between Comprehension and
Activation: experiencing value *without* production commitment.

| # | Stage | Definition | Existing gate mapping |
|---|---|---|---|
| 1 | Acquisition | qualified visitor arrives | — (uninstrumented) |
| 2 | Comprehension | can state what Neotoma is in their own words (cold-read) | gate4 cold-start (partial) |
| 3 | **Evaluation / Trial** | experiences value with no production commitment | — (new; the funnel plans build this) |
| 4 | Activation | adopts in production — real-data ingest + production aha + habit trigger | gate3 activation, gate1/4 |
| 5 | Retention | sustained weekly store ≥4 weeks, broadening entity types | gate1 + gate2 |
| 6 | Referral / Advocacy | recommends, intros, partnership pull | — (uninstrumented) |
| 7 | Revenue / Expansion | WTP, seats, fan-out | parked (pre-PMF) |

**Trial modalities** (each a distinct path):
- Hosted sandbox — no install, sample data (`ent_adb92fee…` hosted-sandbox funnel UX).
- Agent-mediated trial — the visitor's own agent runs a seed prompt against their own data in an
  ephemeral namespace (`ent_6dcb1dc7…` Agent-Mediated Onboarding Funnel).
- Throwaway local install — installs, but exploratory; not wired to real workflow.
- Shadow test — runs alongside the existing setup to compare without switching; de-risks the
  Capable-DIY and Forget-by-Default boundaries (Emil, Jeff).

**Two aha moments:** a *Trial aha* ("proof it remembers / catches a wrong fact") in stage 3, and a
*Production aha* ("it works on my data, in my workflow, and I trust it") in stage 4. The strategic
bet of the funnel plans is to deliver the aha in Trial to de-risk the Activation commitment.
Splitting Trial from Activation also disambiguates install (gate-3 "unassisted activation" currently
conflates throwaway-install with production-commit) and gives Trial→Activation its own conversion
metric and blocker (sandbox-to-local migration + the trust leap of handing over real data).

## ICP model

Canonical primary ICP (`neotoma/docs/icp/primary_icp.md`): **Personal Agentic OS Builders/Operators**
— ONE archetype with three *modes* (Infrastructure Engineering, Building Agent Systems, Operating
Across AI Tools), explicitly "not separate personas, but workflow facets." Corrects the earlier
treatment of infra/builder/operator as three ICPs.

- **Person↔ICP representation:** a *mode blend* (weights across the three modes) + a boundary or
  secondary label + a confidence — not a single bucket. Most people exhibit a blend.
- **Boundaries (non-ICP):** Platform Builders, Capable-DIY, Thought-Partner, Forget-by-Default,
  Autonomous-Loop Builders on raw SDKs.
- **Secondaries:** Toolchain Integrators, Internal-Tools Engineer at a model lab, Identity-Vendor
  partnership, AI-for-Management (not pursued). No compliance/healthcare/security ICP exists yet —
  Isaac Silverman (compliance pivot) and Bottega8 (healthcare/trust-layer) map to none and are a
  candidate new secondary ICP, low confidence pending the round-3 aggregate.

### Bottom-up ICP validation (derive, don't assume)

ICPs must be evidence-backed, not asserted. The method:
1. Treat the 11 qualification criteria (Q1–Q11) as a feature vector.
2. Score every person (evaluators and real users) against each criterion from their
   `feedback_analysis`: present / absent / unknown.
3. Cluster the vectors; compare emergent clusters to the hypothesized archetype + three modes.
4. Flag: (a) people matching no cluster (candidate new ICP), (b) modes with thin evidence,
   (c) people mapped by intuition who fail the criteria.
5. Emit per person: `archetype_fit` (in / boundary / secondary), `mode_blend`,
   `qualification_criteria_met[]`, `disqualifiers_hit[]`, confidence. ICP definitions carry a
   confidence and a backing person-set.

## Opportunity layer (two taxonomies)

- **Product-capability pains** (`neotoma/docs/icp/prioritized_pain_points_and_failure_modes.md`):
  P0 stale replay / duplicate entity resolution / missing task state; P1 stale edges / weak
  correction loop / cross-tool context loss; P2 schema drift / status drift.
- **Funnel-experience pains** (not yet in the docs): install friction, "what is this?" cold-read,
  retrieval/read-side opacity, "what should I store?" cold-start, local-LLM/egress, trust leap.

Opportunities (aha or pain) are typed, linked to a funnel stage + ICP mode + backing evidence, and
are the middle layer of the Opportunity-Solution Tree.

## Instrumentation (GSM) and the instrument-first rule

For each stage: Goal → Signal → Metric, plus `instrumented?` and `has-data?`. Current state: **most
of the funnel is dark.** Qualitative call evidence exists at Comprehension/Trial/Setup; Aha, Habit,
Acquisition, and Referral are essentially uninstrumented.

**Rule:** instrument front-to-back before investing downstream — you may only commit serious product
work to a stage you can see. The Trial stage (hosted sandbox / agent-mediated) is server-side and is
therefore the first place aha becomes measurable, so Trial telemetry is a strong top-of-funnel data
candidate. Distinguish *top-of-funnel-first* (for instrumentation) from *constraint-first* (for
product work) — they are different orderings and will conflict if merged.

## Prioritization cascade (the rewritten process)

0. Load the funnel + opportunity tree + the active `plan_prioritization_rubric`
   (`ent_43689408f2873928165804b5`).
1. Map evidence (people + aggregate) to funnel stages and opportunities.
2. Per stage × ICP: compute conversion, confidence (by N), and instrumentation coverage.
3. Identify the constraint stage (Theory of Constraints).
4. Emit **instrumentation plans for any blind stage first**.
5. Rank product plans within the constraint stage (constraint-first RICE).
6. Map people → the plans that unblock them.

### Evidence weighting (resolves the active-vs-tepid tension)

Weight evidence by `engagement_depth × relevance_to_current_constraint_stage`, recomputed as the
constraint moves. When the constraint is Activation (now), people who bounced at install are the
*most* informative, not the least; active-user evidence dominates only once the constraint is
Retention. `engagement_weight` ≈ furthest_stage_reached × recency × frequency, normalized 0–1.

Also: per-ICP funnels (aha/blockers differ by mode); guardrail/counter-metrics per optimization
(the explicit-control invariant is one); leading vs lagging indicators (gates are lagging).

## Schema set (design — not yet registered)

- `experience_flow` + `flow_stage`
- `opportunity` (kind: aha | pain; linked to flow_stage + ICP mode + evidence)
- `instrumentation_spec` (GSM + instrumented? + has-data?)
- `funnel_metric` (measured value per stage × ICP)
- `icp_definition` (archetype/mode/boundary/secondary + Q-criteria + confidence + backing set)
- `person` extension (lifecycle_stage, primary_icp / mode_blend, engagement_weight, current_blocker,
  aha_reached[])
- Edge chain: `plan → opportunity → flow_stage`; `person → flow_stage / opportunity`

## Neotoma-canonical / mirroring model

Graph-structured strategy entities live in Neotoma and mirror to `neotoma/docs/**` via Apus; the
repo files become generated mirrors ("do not edit directly; corrections via Neotoma"). Absorbing the
ICP docs into Neotoma fixes the repo-checkout blind spot.

**Absorb-and-correct targets** (apply when absorbing the docs): add the Evaluation/Trial stage to
the activation model; add funnel-experience pains alongside P0–P2; open a low-confidence candidate
secondary ICP for compliance/regulated + healthcare trust-layer.

## Agent division of labor

- **Columba (strategy):** funnel North Star + ICP definitions.
- **Pavo (PM):** opportunity tree, instrumentation-coverage check, constraint-first ranking.
- **`analyze-neotoma-feedback`:** evidence feeder — extend to tag each person with funnel-stage,
  aha-reached, blocker, and mode-blend.
- **`prioritize-plans`:** rewritten as the cascade above.

## Open design questions

1. Person↔ICP as mode-blend + boundary/secondary + confidence (vs single dominant bucket)?
2. Make "score corpus against Q1–Q11 and cluster" a first-class skill run before any prioritization?
3. Open compliance/healthcare as a candidate secondary ICP now, or hold for the round-3 aggregate?
4. Make Trial telemetry the top-of-funnel data plan, ahead of local-install telemetry?

## Phased next steps

1. Confirm funnel stages + per-ICP-mode aha/blocker definitions (this plan).
2. Absorb `neotoma/docs/icp/**` + foundation positioning into Neotoma as canonical entities
   (apply absorb-and-correct targets); stand up the Apus mirror back to the repo.
3. Register the schema set; seed `experience_flow` from the funnel above and the gate framework.
4. Run bottom-up ICP validation (Q1–Q11 scoring + clustering) over the person corpus.
5. Rewrite `prioritize-plans` as the cascade; re-run the prioritization against the validated model.
