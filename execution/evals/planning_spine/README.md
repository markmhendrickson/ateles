# Planning-spine reporting eval

This eval exercises `continue-session` and `digest` through ordinary slash-command
invocation in the repository's sandboxed Claude-session harness. It reuses the
rule-delivery eval's session driver, path guard, and fixture Neotoma server.

The fixture is intentionally public-safe and synthetic. Its graph preserves the
behavior under review:

- a master plan with canonical phases and exit-gate states in record order;
- one workstream structurally bound to phase B2 even though its task title says
  "Phase A";
- one E2-labelled workstream attached directly to the master plan, with no phase
  binding;
- one workstream with two distinct canonical phase ancestors, B2 and E;
- one task with no direct `PART_OF` parent and one with two direct parents; and
- task mechanics named Repair, Review, Merge, and Deploy.

The evaluator fails the original task-stage-only report even when that report
contains every policy phrase the old checker searched for. A passing report must
show the selected master plan first, render its canonical phases and gate states
before task mechanics, place each uniquely bound workstream exactly once through
the graph edge, keep non-unique workstreams out of canonical phase sections,
report missing and duplicate phase ancestry per workstream, report missing and
duplicate task ascent per affected task, and keep `reconcile-planning`
retrospective. Stable fixture entity IDs bind these effects, so changing a human
title cannot change placement or make an equivalent report fail.

The three `fixtures/skills/*/SKILL.md` files are generated review evidence, not
sources. Neotoma prod remains canonical. `fixtures/review_bundle.json` records
the canonical locators, generation mode, and SHA-256 of every body and the
planning fixture. Regenerate from prod when credentials are available:

```sh
python3 execution/scripts/render_planning_spine_review_bundle.py
```

For credential-free recovery from an already-generated user mirror, pass its
root through an environment-relative path:

```sh
python3 execution/scripts/render_planning_spine_review_bundle.py \
  --source-root "${CODEX_HOME:-$HOME/.codex}/skills"
```

Run deterministic coverage and the credential-free harness setup:

```sh
python3 -m pytest -q execution/evals/planning_spine/test_planning_spine_eval.py
python3 execution/evals/planning_spine/planning_spine_runner.py --dry-run
```

A live effect run is opt-in because it spends model budget:

```sh
python3 execution/evals/planning_spine/planning_spine_runner.py --model sonnet
```

Transport failures are reported as infrastructure errors and are never scored as
skill behavior.
