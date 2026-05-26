---
name: verify-deployment
description: "Check GitHub Actions after a website deploy and fix or re-run until the build succeeds. Use after a deploy push or when user says \\"check deploy\\", \\"verify deployment\\", or \\"did the build succeed\\". Can be invoked via /verify-deployment."
triggers:
  - check deploy
  - verify deployment
  - did the build succeed
  - verify-deployment
user_invocable: true
entity_id: ent_8f5acc398e28b6881b1368cf
---

# Verify Deployment

After a deployment that uses GitHub Actions (e.g. website submodule push), check the workflow run and repeat fix/re-run until the build succeeds. Do not stop at "you can re-run"; perform the re-run when the fix is in place.

## When to Use

Use this skill when:
- User has just pushed a website deploy (e.g. markmhendrickson, hendricksonserrano, dionysiandesigns) and wants confirmation
- User says "check deploy", "verify deployment", "did the build succeed"
- Context indicates a deploy was triggered and outcome is unknown

## Required Documents (load first)

1. **Deployment verification and re-run:** [docs/development_workflows_rules.mdc](docs/development_workflows_rules.mdc) (Deployment verification, GitHub Actions re-run)
2. **Optional pre-step for markmhendrickson:** Same doc (Post markdown priority and deploy sync) — ensure Neotoma export current and run `generate_posts_cache.py` before build when deploying that site

## Workflow

1. **Optional pre-step (website with posts):** If deploying a site that uses post cache (e.g. markmhendrickson), ensure Neotoma export is current and run `python3 execution/scripts/generate_posts_cache.py` (or `--from-neotoma-json <path>`) before build when appropriate.
2. **Identify repo and run:** Determine the repository that was pushed (e.g. markmhendrickson/markmhendrickson). Use `gh run list --repo <owner/repo>` or user-provided run ID; optionally open Actions page.
3. **Check outcome:** Use `gh run view <run-id> --repo <owner/repo>` to see status and logs.
4. **If succeeded:** Report success to user and stop.
5. **If failed:** Identify cause from logs (failed step, config, submodule, dependencies). Fix (code, config, submodule, deps), then either push the fix (triggers new run) or run `gh run rerun <run-id> --repo <owner/repo>`. Return to step 3 and repeat until the build succeeds.
6. Do not tell the user "you can re-run the workflow" without performing the re-run after the fix is applied.

## Constraints

- Always check Actions after a deploy that uses GitHub Actions.
- Repeat the check/fix/re-run loop until the build succeeds.
- Perform `gh run rerun` when the fix is in place; do not leave re-run to the user.

## Related Rules

- [docs/development_workflows_rules.mdc](docs/development_workflows_rules.mdc) — Deployment verification, GitHub Actions re-run, Post markdown priority and deploy sync
