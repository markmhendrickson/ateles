# Migration note — formica-node

This is the **Node.js** implementation of the Formica issue-processing daemon,
migrated from `personal/execution/daemons/formica/` during the personal-monorepo
decomposition (Phase 3).

It is a **parallel implementation** to the live Python daemon at
`../formica/formica.py`, which is what `com.ateles.formica.plist` currently runs
(`ateles/.venv/bin/python3 .../formica/formica.py`).

The two share the Formica name and purpose but are independent codebases:
- `formica/` (Python, **live**) — run by launchd today.
- `formica-node/` (Node.js, this dir) — richer module set (classifier, pipeline,
  pr_manager, worktree_manager, telegram_mirror, operator transport, tests).

**Action required (operator):** decide which implementation is canonical and
reconcile. Nothing here is wired to launchd. `node_modules/` was not migrated —
run `npm install` here if you intend to use this implementation.
