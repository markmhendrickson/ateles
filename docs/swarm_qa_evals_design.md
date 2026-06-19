# QA Evals — Design (agents-as-primary-users)

**Status:** design (2026-06-19) · **Scope:** the swarm QA role (Phoenicurus) + the neotoma eval substrate.

**Goal:** make the QA agent (Phoenicurus) **author a reproducible eval** for **any functional change** — not just a manual test plan. The eval encodes the exact tests QA would run by hand, runs automatically in CI, and produces a reproducible QA report. **Agents are presumed the primary users of the product**, so an "agent-facing" eval is the default lens regardless of whether the change is operator- or agent-initiated.

This is the answer to: *"QA should not only manually test new functionality on the PR, but also write comprehensive evals so rerunning those exact tests is fully automatic and yields a reproducible QA report — for any functional change, because agents are the primary users."*

---

## What already exists (build on, don't replace)

Neotoma has a real eval substrate:
- **`packages/eval-harness`** — scenario runner: `*.scenario.yaml` scenarios, cassettes (recorded interactions), assertions, reporters, a CLI.
- **agentic_eval fixtures** — `tests/fixtures/agentic_eval/*.json`, shape `{meta, events, assertions, expected_outputs}`; the matrix expands `harness × model` per fixture (`tests/integration/agentic_eval_matrix.test.ts`).
- **`npm run eval:tier1`** — runs the agentic-eval matrix; **`eval:tier1:update`** regenerates snapshots.
- **CI lane `agentic_evals`** (`ci_test_lanes.yml:146`) — runs Tier-1 evals on every PR touching code/agent surfaces.

The gap: Phoenicurus's QA contract is "Test Plan" (prose) — it **describes** tests but doesn't **author the reproducible eval**. The substrate exists but isn't the QA deliverable.

---

## The reframe: QA's deliverable IS a reproducible eval

Phoenicurus's `agent_definition` GitHub-deliverable (Layer B) changes from "Test Plan" to **"Eval + QA Report"**:

For any functional change, Phoenicurus MUST:
1. **Author an eval** that encodes the manual tests — as an `agentic_eval` fixture (`{meta, events, assertions, expected_outputs}`) and/or an eval-harness `*.scenario.yaml`, whichever fits. The eval is the *executable* form of "the exact tests I'd run by hand."
2. **Treat the agent as the primary user.** The eval exercises the change through the **agent-facing surface** (MCP tool / store-recipe / retrieval / schema behavior) by default — even for an operator-facing change, because agents are the presumed primary consumer. (An operator-only UI change still gets an eval of the agent-observable effect, if any; if genuinely none, QA states that explicitly.)
3. **Commit the eval in the PR**, so it runs in CI (`eval:tier1` / the `agentic_evals` lane) and is **reproducible** by anyone re-running the lane.
4. **Post the QA report** as its swarm comment: the eval id(s) + what they assert + the run result (pass/fail) — the report IS the eval output, reproducible on demand, not a one-off prose pass.

### "Functional change" = the trigger
Any change that alters **observable behavior** through an agent- or operator-facing surface: MCP tools, store/retrieve recipes, schemas, API endpoints, agent instructions, gate/checkpoint logic, CLI behavior. NOT: pure docs, comments, formatting, test-only refactors (those get "no functional change — no eval required", stated explicitly so the absence is a recorded judgment, not an oversight).

### Agents-as-primary-users principle
Every functional change is evaluated as if an **agent** is the consumer first. Concretely: the eval drives the change via the MCP/CLI/store-recipe path (the agent's interface), asserts on the agent-observable result (observations stored, entities resolved, tool output shape, error recovery), and only secondarily checks operator-facing surfaces. This is why even an "operator feature" gets an agentic eval — the question is always "what does an agent see/do when this changes?"

---

## Enforcement: eval-in-CI is the qa-gate evidence

Phoenicurus's **qa gate sign-off** (today: it `correct()`s `gate_status.qa: signed_off` on the issue entity) becomes **eval-backed**:
- qa gate signs off **only when**: (a) a new/updated eval covering the change is present in the PR, AND (b) the `agentic_evals` CI lane is green on it.
- If no eval is present for a functional change → qa gate stays `pending` (or `changes_requested`), with the gap named.
- The **QA report** Phoenicurus posts = the eval id(s) + assertions + the CI run link → reproducible: re-run `npm run eval:tier1` (or the lane) to regenerate the exact report.
- For a genuinely non-functional change, Phoenicurus signs off with an explicit "no functional surface — no eval required" note (recorded, auditable).

This makes the qa gate **objective + reproducible** instead of a judgment call, and closes the loop with Vanellus (which already routes `[BLOCKING]` qa findings).

---

## Build phases

- **QE1 — Phoenicurus contract rewrite.** `correct()` Phoenicurus's `agent_definition.prompt_markdown`: deliverable = "Eval + QA Report" (author an agentic_eval fixture/scenario encoding the manual tests; agents-as-primary-users lens; commit it; QA report = eval id + assertions + run result). Reference the existing fixture format + `eval:tier1`. (Neotoma correction + SKILL regen — like Layer B.)
- **QE2 — qa-gate eval-evidence.** Make the qa-gate sign-off require an eval present + green (in the gate logic / Phoenicurus protocol). Until QE2, QE1 alone makes Phoenicurus *write* evals; QE2 makes them *required*.
- **QE3 — eval-authoring affordance.** Give the dispatched Phoenicurus child what it needs to actually write+run the eval: the eval-harness in scope, `eval:tier1` runnable, the fixture template. (May need the agent's tool_allowlist + working dir to include the eval paths.)
- **QE4 (later) — broaden the matrix.** As more evals accrue, the agentic_eval matrix becomes the swarm's regression backbone; tie a failing eval to auto-routing back to Gryllus (impl) like other [BLOCKING] findings.

Each phase verifiable on a live PR: open a small functional change, confirm Phoenicurus authors an eval + the qa report reproduces.

---

## Why this shape
- **Reuses the real substrate** (eval-harness, agentic_eval matrix, eval:tier1 CI lane) — QA's deliverable becomes a fixture in a framework that already runs, not a new test system.
- **Reproducible by construction** — the QA report is an eval run; re-running the lane regenerates it. No "trust the prose."
- **Agents-as-primary-users is enforced, not aspirational** — the eval drives the agent-facing surface by default, per the operator's principle.
- **Objective qa gate** — sign-off is "eval present + green", not a vibe.

## Risks / notes
- **Eval-authoring cost** — writing a good eval is more work than a prose plan; QE3 (giving the agent the harness + template) is essential or QA stalls. Start with the agentic_eval JSON fixture (simplest) before the richer scenario.yaml.
- **Don't block trivial changes** — the "no functional surface → no eval" escape must be explicit + cheap, or every doc PR stalls at qa. (Mirrors the checkpoint condition-DSL: scale rigor with risk.)
- **Snapshot churn** — `eval:tier1:update` regenerates snapshots; a new eval shouldn't force-update unrelated snapshots. Phoenicurus adds its fixture; CI runs it; snapshots for the new fixture only.
- **Flaky/nondeterministic evals** — the harness×model matrix can surface model variance; assertions should target deterministic agent-observable effects (observations stored, entity resolution, tool-output shape), not free-text.
