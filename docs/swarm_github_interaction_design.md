# Swarm GitHub Interaction & Artifact Design

**Status:** design (2026-06-18) · **Scope:** the 8 GitHub-facing swarm agents (lanius, pavo, vanellus, waxwing, accipiter, buteo, phoenicurus, corvus)

**Goal:** make swarm agents (1) interact on GitHub **consistently** (shared comment chrome + conventions), (2) produce **role-specific artifacts** that match their expertise (designer → design spec, arch → ADR, etc.), and (3) use GitHub's **richer review primitives** (formal Reviews, inline/suggested changes, templates, status labels) instead of only plain comments.

This is the answer to: *"enhance each GitHub agent with skills that strengthen their GitHub habits per expertise AND ensure consistent interaction, plus other GitHub artifacts for more robust review."*

---

## Current state (verified 2026-06-18)

- Agents only post **plain `gh issue/pr comment`s**. No formal PR Reviews, no inline/suggested changes, no templates.
- GitHub-interaction guidance is **uneven**: lanius (12 github-refs), vanellus (23) are fluent; accipiter (ux) = 1, pavo = 1, bombycilla/buteo/phoenicurus ≈ 2. Their *expertise* sections are rich but their *GitHub-output format* is unspecified.
- The only shared format is the `review_expectation` checklist, **hardcoded in `swarm_dispatch.py`** — not a reusable convention.
- `build_system_prompt` composes `agent_def.prompt_markdown` + per-skill `SKILL.md`. **No shared block is injected for all dispatched agents** — this is the natural insertion point for the shared convention.
- Agent SKILLs are **generated mirrors** of Neotoma `agent_definition.prompt_markdown` — edit via `correct()`, never the `.md`.

---

## Layer A — Shared GitHub interaction convention (one contract, all 8 inherit)

A single canonical block injected into every dispatched agent's system prompt **in code** (extend `build_system_prompt` to prepend/append a `SWARM_GITHUB_CONTRACT` constant), so it lives in ONE place, not duplicated across 8 entities.

**Every swarm GitHub comment follows this skeleton:**
```
🤖 <Agent> — <role> · <repo>#<n>            ← attribution (dropped when agent posts as its own account, per #109)
**<VERDICT or ACTION>**                       ← one-line machine-readable status

<role-specific body>                          ← the substance (Layer B defines per role)

---
*<footer: links to the artifacts this comment references — see Layer C>*
```

**Shared conventions (all agents):**
- **Verdict vocabulary** (machine-parseable, consistent): `APPROVE` / `REQUEST_CHANGES` / `COMMENT` / `BLOCKED` / `SIGNED_OFF` — same tokens every agent uses, so the dispatcher (and humans) can parse outcomes uniformly.
- **Checkbox definition-of-done**: `- [ ]` GitHub task-list syntax (already enforced for expectations; generalize to all role checklists).
- **Blocking markers**: `[BLOCKING] <category>: <summary>` / `[NON-BLOCKING] <category>: <summary>` (already in some prompts; make universal).
- **Cite standing rules**: when a finding rests on a guardrail/decision, link the Neotoma decision or doc — marks it systemic, not opinion.
- **One comment, edited not duplicated**: update your prior comment in place (the #105 check-off pattern) rather than stacking new ones.
- **Brevity contract**: checklist/structured, not essay.

---

## Layer B — Per-role GitHub artifact contracts (expertise → required deliverable)

Each agent's `agent_definition.prompt_markdown` gains a **"GitHub deliverable"** section: when invoked on an issue/PR, it MUST produce its role's artifact in the shared skeleton. The artifact is the expertise made concrete.

| Agent | Lens | Required GitHub artifact | Key sections |
|---|---|---|---|
| **lanius** | triage/gate | **Triage + gate-status board** | gate_status table, owner, next phase, labels applied |
| **pavo** | pm | **Scope & acceptance spec** | problem, in/out of scope, acceptance criteria (checkboxes), priority, sign-off verdict |
| **waxwing** | arch | **ADR (Architecture Decision Record)** | decision, options + tradeoffs, chosen approach, schema/contract impact, reversibility, sign-off |
| **accipiter** | ux | **Design spec** | user-facing surface, interaction/flow, discoverability, naming, error/empty states, a11y, accept-checklist |
| **buteo** | legal | **Compliance checklist** | deps/licensing, secrets/PII surface, data-handling, ToS, each as a checkbox verdict |
| **phoenicurus** | qa | **Test plan** | what to test, unit/integration coverage, edge cases, regression risks, repro steps, pass/fail gates |
| **corvus** | content | **Content/comms note** | doc impact, changelog entry, external-comms hook, naming/voice consistency |
| **vanellus** | PR steward | **Aggregated review verdict** | per-lens roll-up, blocking vs non-blocking, merge recommendation (operator-gated) |

Each contract reuses the shared skeleton (Layer A) so the chrome is consistent; the **body** is role-specific.

---

## Layer C — GitHub primitives beyond plain comments (all four adopted)

1. **Formal PR Reviews** (`gh pr review --approve` / `--request-changes` / `--comment`). Vanellus + lenses emit real GitHub Review verdicts so outcomes show in GitHub's review state + branch protection, not just prose. Map the shared verdict vocabulary → the review event. (Best-effort + operator-gated for merge, per current policy.)
2. **Inline + suggested changes.** Findings anchor to specific lines (`gh api .../comments` with `path`+`line`), and code fixes use ```suggestion blocks the author one-clicks to apply. Turns "line 218 should…" into an actionable diff. (Primarily waxwing/phoenicurus/buteo.)
3. **Issue/PR templates** (`.github/ISSUE_TEMPLATE/*.yml` + `PULL_REQUEST_TEMPLATE.md`) in ateles + neotoma. Scaffolds the fields agents depend on at the SOURCE: acceptance criteria, parent issue (so PR gate-inheritance works), workflow type, gate info. Reduces "PR has no parent issue" failure modes.
4. **Labels / Projects / Milestones as status.** Extend the `lanius-triage` label into a consistent **gate/phase taxonomy** (`gate:pm-signed`, `phase:arch`, `blocked:gates`, …) so the swarm's gate state projects natively onto GitHub (filters, boards). Optionally a Projects board per repo. (Lanius owns label application.)

---

## Build phases (proposed)

- **Phase 1 — Layer A (shared convention).** Add `SWARM_GITHUB_CONTRACT` to `build_system_prompt`; generalize verdict vocabulary + blocking markers. One code PR. Immediately makes all 8 consistent.
- **Phase 2 — Layer B (per-role contracts).** `correct()` each of the 8 `agent_definition.prompt_markdown` to add its GitHub-deliverable section. 8 Neotoma corrections + SKILL regen. (Entity IDs: phoenicurus ent_42843b65…, buteo ent_6f90952e…, accipiter ent_7079893d…, corvus ent_b95bf915…, pavo ent_bf712273…, lanius ent_f9c2c573…, vanellus ent_fedc0fba…, waxwing ent_3425a79b4c39f08cdb0c62f8.)
- **Phase 3 — Layer C primitives.** (3a) formal PR Reviews in the dispatcher/Vanellus; (3b) inline/suggested changes; (3c) issue/PR templates (cheap, do early); (3d) status-label taxonomy.

Each phase is independently shippable + verifiable on a live PR (the swarm reviews its own PRs).

## Risks / notes
- Per-agent accounts (#109) are a prerequisite for *attributed* formal Reviews — until then, Reviews post under the shared account (the Layer A attribution line still names the agent).
- Formal `--request-changes` can BLOCK merge under branch protection — keep operator-gated; default lenses to `--comment` unless a `[BLOCKING]` finding, then `--request-changes`.
- Don't over-template: leave role peculiarities to Layer B; Layer A is chrome only.
