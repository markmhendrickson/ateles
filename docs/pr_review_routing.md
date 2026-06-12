# Spec — Per-domain PR review routing

**Status:** proposed (spec only, no implementation)
**Owner:** Ateles (orchestration) · implementation TBD
**Related:** `.github/workflows/loxia-pr-review.yml`, `execution/scripts/loxia_review.py`, `execution/daemons/apis/routing.py`

## Problem

Today every PR to `main` is reviewed by a single generic agent, **Loxia**
(`loxia-pr-review.yml` → `loxia_review.py`). Loxia applies one fixed checklist
(scope, secrets, gitleaks, linting, T3 pattern consistency, docs) regardless of
what the PR touches. That is the right *baseline* — every PR should get the
secrets/scope/style pass — but it means a finance change and a health change get
the same reviewer with no domain expertise, unlike the neotoma repo where review
is routed to the relevant owning agent.

## Goal

Keep Loxia as the universal baseline reviewer, and **additionally** route each PR
to the **domain-owning agent(s)** for a domain-specific review, reusing the
routing table that already exists for task dispatch.

Non-goals: replacing Loxia; blocking merges on the domain review (advisory
first); changing how non-PR work is routed.

## Existing material to reuse

`execution/daemons/apis/routing.py` already maps domains → agents for task
dispatch. The same table is the natural source of truth for review routing:

| Domain | Owning agent | Example PR signals |
|---|---|---|
| finance | monedula | `execution/**/finance`, payment/invoice/wage files |
| health | gorilla | workout/fitness files |
| ops / engineering / agents / neotoma / product / comms | cicada | daemons, scripts, schemas, docs |
| ... | ... | (full map lives in `routing.py` `DOMAIN_ROUTES`) |

Each domain agent already has a SKILL.md (`.claude/skills/<agent>/SKILL.md`)
that can be appended as the review system prompt — the same way Loxia uses its
inline checklist.

## Proposed design

1. **Path → domain inference.** Add a path-based classifier (a thin wrapper over,
   or a shared module with, `routing.py`) that maps a PR's changed-file paths to
   zero or more domains. Reuse `routing.py` patterns; do not fork the regexes.
   - 0 domains matched → Loxia baseline only (current behavior).
   - 1+ domains matched → Loxia baseline **plus** one domain review per matched
     owning agent (deduplicated by agent).

2. **Reviewer invocation.** Generalize `loxia_review.py` into a parameterized
   reviewer that takes `(agent_name, skill_md_path, review_focus)`:
   - Loxia runs with its current generic checklist.
   - A domain agent runs with its SKILL.md appended + a domain-focused checklist
     (e.g. monedula: "no hardcoded IBANs/memos; payment-profile reads from
     parquet/env; Yoga/therapy never marked completed").
   - Same Claude API call path; same comment-posting path; one comment per
     reviewing agent, clearly attributed in the heading.

3. **Workflow shape.** Two viable options — recommend **B** to start:
   - **A — matrix job.** One workflow, a `strategy.matrix` over the set
     `{loxia} ∪ matched-domain-agents`, computed by a pre-step. Most parallel,
     but matrix from dynamic JSON adds YAML complexity.
   - **B — Loxia dispatches.** Keep the single `loxia-pr-review.yml`; after Loxia
     posts its baseline review, it computes matched domains and invokes each
     domain reviewer in-process (sequential API calls). Simplest, one secret
     scope, easy to ship incrementally. Promote to A if review latency matters.

4. **Attribution.** Each comment heading names the reviewing agent
   (`## Monedula Review 🐦` vs `## Loxia Review 🪶`) and the commit SHA. When
   `NEOTOMA_BEARER_TOKEN` is present, `REQUEST_CHANGES` files a Neotoma issue
   attributed to the reviewing agent (extend the existing `file_neotoma_issue`
   with an `agent` field), so domain findings are traceable per agent.

5. **Advisory vs blocking.** Phase 1: all reviews advisory (comment only; the
   check passes regardless). Phase 2 (optional): a domain agent may emit
   `REQUEST_CHANGES` that sets a failing status for *its* domain only, gated
   behind branch protection if desired. Decide per-domain, not globally.

## Rollout

1. Activate baseline Loxia first (this PR's permissions fix + `ANTHROPIC_API_KEY`
   secret). Prove single-agent review posts correctly.
2. Land the path→domain classifier as a shared module with `routing.py` (unit
   tested against representative paths).
3. Ship option **B** with **one** domain agent (suggest monedula — highest-risk
   domain, clear checklist) as a vertical slice.
4. Expand to the remaining agents; revisit matrix (option A) only if latency or
   isolation demands it.

## Open questions

- Should domain review run on **every** PR touching the domain, or only above a
  size threshold (avoid noise on one-line doc tweaks)?
- One consolidated comment with per-agent sections, or one comment per agent?
  (Per-agent is simpler to attribute; consolidated is quieter.)
- Do we want domain `REQUEST_CHANGES` to ever block merge, or stay advisory and
  rely on the operator? (Default: advisory.)
