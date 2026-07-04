# Swarm Definition Playbook

A reusable, top-to-bottom rubric for defining an agent swarm — from the highest-level
mission down to each agent's strategy, processes, schemas, tooling, and constraints.

**Who this is for.** Any operator standing up their own Ateles-style swarm (a digital
operational staff backed by a trustworthy memory/state substrate). It is both:

1. a **completion rubric** — the checklist a swarm-definition effort works through and
   can call "done" against; and
2. an **operator onboarding guide** — the path a new operator follows to fork the
   architecture and supply their own context.

**Core principle that governs the whole method.** *Derive what is required from the
mission first, then compare to what actually exists.* Never strategize outward from the
agents (or artifacts) you already have — that inherits their blind spots. At every level,
ask "what does the mission require here?" before "what do we already have?", then
reconcile the two. A role, strategy, or capability that *should* exist but doesn't stays
invisible if you only inventory what's in front of you.

This playbook describes the **method**, not any one operator's answers. Keep it generic:
operator-specific facts (identity, customers, finances, vendors) live in **context
entities** the swarm resolves at runtime, never in this document or in agent prompts.

---

## The layers (work top-down; each layer constrains the next)

```
L0  MISSION            — the change in the world; existential, durable
L1  STRATEGY           — how to cause that change (business + life), with a keystone
L2  OBJECTIVES         — the falsifiable near-term goals the strategy commits to
L3  REQUIRED ROLES     — the roles a swarm needs to serve L0–L2 (derive, then compare)
L4  ROLE STRATEGIES    — one owned strategy per confirmed role, traceable to L0–L2
L5  AGENT DEFINITIONS  — per agent: strategy, processes, schemas, tooling, constraints
L6  DOWNSTREAM         — each agent self-organizes its own tasks & plans from its L4 strategy
```

Offerings (products, services) are **instruments** that serve L0, not the top of the
tree. Name the mission first; map each product/service to it as a supporting resource.
If an offering's value spills outside the mission, that spillover is a *different*
(often proximate) mission — record it as a deliberately-not-pursued reference point, not
as scope creep.

---

## L0 — Mission

**Goal:** state the change in the world the operator is causing, independent of any
product. Products can be falsified and replaced; the mission persists.

Checklist:

- [ ] State the mission as **who-can-do-what** (what a person can do, stop having to do,
      or become that they can't today) — not as a product or feature.
- [ ] Name its **precondition(s)** explicitly. A mission often has a load-bearing
      condition it under-names on first draft (e.g. "trust" for a delegation mission —
      you cannot delegate to a staff you must supervise). Surface it and fold it in.
- [ ] Confirm the mission is **falsifiable and visceral**: it must be able to yield a
      clean "this worked / this didn't," and be something the operator is an instance of.
      A mission too broad to fail is the wrong altitude — narrow it until it can be judged.
- [ ] Record **discarded/adjacent missions**: the broader parent you rejected and the
      proximate missions you're *not* pursuing, each with *why-not-now* and
      *what-evidence-would-revive-it*. This keeps the choice revisitable as a deliberate
      pivot rather than something rediscovered from scratch.

## L1 — Strategy (business + life)

**Goal:** how the mission gets caused, and what constrains the operator personally.

Checklist:

- [ ] Identify the **keystone**: the single mechanism that, if fixed, unlocks the rest.
      (Common shape: break a self-reinforcing loop by designing work to produce
      trustworthy, unambiguous evidence on pre-committed, falsifiable milestones. The
      enemy is usually *ambiguity*, not slowness.)
- [ ] Capture **standing constraints** — including personal ones the operator can't
      strategize away (e.g. a founder single-point-of-failure to de-risk; a range/energy
      budget to protect as a design *input*, not a deferred reward).
- [ ] **Critically review any existing strategy artifacts.** For each, classify:
      **STALE** (was right, now outdated), **MISREPRESENTATIVE** (never captured intent),
      or **CORRECT** (keep). Propose deltas; never silently overwrite.
- [ ] Ground the business timeline in a **real constraint** (e.g. runway = liquid ÷ burn),
      not a back-solved arbitrary deadline.

## L2 — Objectives

**Goal:** the falsifiable near-term goals the strategy commits to, each with a way to
tell pass from fail.

Checklist:

- [ ] Each objective is **falsifiable** — a pre-committed success/fail criterion, no
      movable goalposts.
- [ ] Where an objective is a *value test*, define an **evidence ledger**: what counts as
      a pass, what makes a pass a false positive, and the honesty axes (e.g. is a win
      favor-biased? founder-carried?). Read the ledger at the verdict date, not your mood.
- [ ] Sequence objectives into **phases** with distinct models per phase (what you sell,
      who delivers, what "revenue's role" is) rather than one flat plan.

## L3 — Required roles (derive, then compare)

**Goal:** the set of roles a swarm needs to fulfill L0–L2 — established *before* looking
at the current roster, then reconciled against it.

Checklist:

- [ ] **Derive the required-role set from the mission/strategy**, not from the agents that
      exist. Ask "what roles must a swarm have to deliver this mission?"
- [ ] **Critically review the actual roster** (existing role model + agent definitions)
      against the required set. Sort every role into:
  - **RIGHT** — exists and fits the mission → proceed to L4 for it.
  - **MIS-SHAPED** — exists but scoped wrong for the mission → re-scope, then L4.
  - **MISSING** — the mission needs it, no agent fills it → net-new agent to define.
  - **OBSOLETE** — exists but no longer serves the mission → retire or merge.
- [ ] Distinguish a **role/capability gap** ("are the roles right?") from a **capability
      gap within existing roles** ("what must these agents newly be able to do?"). Do the
      role sort first; the within-role capability work is L5.

### Reference role taxonomy (Ateles default — adapt, don't copy)

An Ateles swarm organizes agents in tiers. Use as a starting taxonomy to review against
your mission, not a fixed list:

- **T1 — host/runtime:** the environment the swarm runs in (a coding-agent host, an
  OpenClaw-style instance, a device). Not an agent per se; the substrate agents run on.
- **T2 — operator interface / root:** the operator's primary conversational agent and
  swarm root (resolves identity, roster, channels, locale from context entities).
- **T3 — daemons:** long-running, scheduled or event-driven (mirror, dispatcher,
  coordinator, briefings, payments, recurring tasks, email triage, screenshots).
- **T4 — invocable agents:** called to perform a bounded job (content, CRM, PM, design,
  architecture, QA, UX, legal, financial analysis, tax, customer intelligence,
  constitution keeper, release manager, compliance, devrel, data analysis, GTM, health).

Common role slots (map by *role*, resolved from a roster, never hardcode a name):
operator_interface, public_representative, coordinator, dispatcher, mirror, issue_triage,
code, pr_steward, pr_reviewer, content, crm, pm, designer, architect, qa, ux, legal,
financial_analysis, tax, customer_intelligence, constitution_keeper, release_manager,
compliance, devrel, data_analyst, payments, recurring_tasks, email_triage, briefings, gtm.

A role appearing in the roster but with no defined agent is a **MISSING** row until its
agent is authored.

## L4 — Role strategies (one per confirmed role)

**Goal:** each confirmed role owns a strategy, derived from and traceable to the mission.
This is the layer that lets agents self-organize — an agent cannot organize downstream
work without a strategy to organize *toward*.

Per role, the strategy states:

- [ ] **Objective** — what this domain is for, derived from L0–L2 (not invented locally).
- [ ] **Service to the mission** — how it advances L0 and respects L1's keystone and
      standing constraints.
- [ ] **Boundaries** — what it does *not* own; where it hands off to sibling roles.
- [ ] **Success/failure signals** — how the agent knows it's on or off track (feeds a
      drift signal back to the operator interface).

Reuse sound existing domain strategies (critically reviewed, not overwritten). The
strategy becomes the agent's owned `agent_strategy`.

## L5 — Agent definitions (per agent)

**Goal:** everything an agent needs to do its job well. For each confirmed/net-new role:

- [ ] **Strategy** — its L4 strategy, referenced (not duplicated).
- [ ] **Identity & principals** — role, and whose behalf it acts on (operator + sibling
      agents), resolved from context entities.
- [ ] **Processes** — the protocols it follows (its job steps; consultation/escalation
      path; how it participates in plans; its confidence/approval gates by blast radius).
- [ ] **Schemas** — the entity types it reads (context) and writes (operational output);
      any schema it owns.
- [ ] **Tooling** — the tools/MCP servers it may call (an allowlist), and any external
      vendors resolved by capability slot (not hardcoded).
- [ ] **Constraints** — hard rules, public/PII discipline, domain guardrails, and the
      output/artifact-header format the coordinator parses.
- [ ] **Context entities** — the operator/locale/vendor/roster records it resolves at
      runtime so the same prompt works for any operator who forks it.

**Public-prompt discipline (hard rule).** An agent prompt describes a role *generically*;
operator specifics come from context entities at runtime. No operator data (names,
addresses, financials, customer identities, secrets) ever lives in a prompt. If a prompt
can't be made public without leaking, move the data into a context entity and reference
it by type. This is what makes a swarm forkable.

## L6 — Downstream self-organization

**Goal:** each agent, given its L4 strategy, organizes its own tasks and plans.

- [ ] Each agent creates/maintains the tasks and plans that execute its strategy.
- [ ] Work is stored durably (typed entities), not just in a prompt or a file.
- [ ] Drift signals flow back up: when an agent sees evidence contradicting its strategy,
      it surfaces it to the operator interface rather than silently absorbing it.

---

## Cross-cutting discipline (applies at every layer)

- **Derive-then-compare.** Required-from-mission first; actual-roster/artifacts second.
- **Falsifiable over aspirational.** Prefer a claim that can fail cleanly to one too broad
  to judge. Ambiguity is the enemy.
- **Critically review, don't overwrite.** Existing artifacts are pressure-tested, not
  extended blindly; classify stale/misrepresentative/correct and propose deltas.
- **Durable storage, queryable.** Every artifact is a typed entity in the memory
  substrate, not a scratch file — so the swarm (and the next operator) can read it.
- **Public & PII-free by construction.** This playbook and all agent prompts are generic;
  operator specifics live in context entities.
- **Instruments serve the mission.** Products/services map *up* to the mission; they are
  replaceable. The mission is what persists.

---

## Definition-of-done for a swarm-definition effort

A swarm is "defined" (ready to operate and to hand to another operator) when:

- [ ] L0 mission is stated (who-can-do-what), with its precondition folded in and
      discarded/adjacent missions recorded.
- [ ] L1 strategy has a keystone and standing constraints; existing artifacts reviewed.
- [ ] L2 objectives are falsifiable, phased, with an evidence ledger where value is tested.
- [ ] L3 required-role set derived from the mission and reconciled against the roster
      (every role sorted RIGHT / MIS-SHAPED / MISSING / OBSOLETE).
- [ ] L4 every confirmed role owns a mission-traceable strategy.
- [ ] L5 every confirmed/net-new agent has strategy, processes, schemas, tooling, and
      constraints, with all operator-specifics externalized to context entities.
- [ ] L6 agents can (and do) self-organize tasks/plans from their strategies, with drift
      signals flowing back to the operator interface.
- [ ] A fresh operator can fork the repo, supply their own context entities, and stand up
      the same swarm without editing any prompt.
