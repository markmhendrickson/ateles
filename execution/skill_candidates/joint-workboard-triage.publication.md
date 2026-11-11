# Reviewed publication of joint-workboard-triage

This is a source candidate, deliberately outside harness discovery roots. No canonical skill was stored and no installed mirror was changed by the builder. Do not use this document as authorization to activate it before the coordinating session's reviews and current integration checks.

## Verified existing publishing path

The current production `skill` schema (observed version 2.8.0) declares `content` as the canonical PII-free markdown body, plus `name`, `description`, `slug`, `triggers`, `user_invocable`, `supported_harnesses`, `enabled`, `version` and `side_effect_class`. `execution/scripts/sync_skills.py` reads those entities, projects their owned frontmatter and body, and renders generated mirrors. Its `canonical_text()` supports body-only content, so the body below the candidate's frontmatter is the canonical payload. The task schema declares notes, status, priority, due_date and owner separately; the skill correctly requires current-schema lookup rather than assuming them universally.

The candidate is self-contained: the evaluation files are developer fixtures, not resources to install with the skill. Do not publish evaluation answers into the operational skill.

## Operations after independent review

1. Re-query the exact skill name/slug on the verified authoritative production instance. If a canonical entity now exists, re-read it and reconcile through the owning coordinator instead of creating a duplicate or overwriting a newer body.
2. Build one skill entity payload from the reviewed `SKILL.md`: `name` and `slug` = `joint-workboard-triage`; `description` = the reviewed frontmatter description; `content` = the exact reviewed markdown body; `triggers` = [`joint workboard triage`, `triage my outstanding work together`, `walk the backlog one item at a time`]; `user_invocable` = true; `supported_harnesses` = [`claude-code`, `codex`, `cursor`]; `side_effect_class` = `state_changing`; `enabled` = true; `version` = `1.0.0`. Store through the supported production `store` tool with the current declared arguments, one stable idempotency key for the accepted revision, and provenance linking the coordinating task/source privately. Do not add a new schema or operator-specific IDs to the public body.
3. Retrieve the returned skill entity and compare **each** intended field and exact content bytes/hash against the reviewed candidate. If an undeclared field vanished or a correction is missing, publication has not succeeded. Resolve that discrepancy before installation. Root retains the entity ID and receipt on its existing canonical task/digest.
4. Render only this skill with the existing source renderer, from the qualified checkout and with its already-verified complete production credential binding (never print/read credentials):

   ```sh
   python3 execution/scripts/sync_skills.py --install joint-workboard-triage --install-root user
   python3 execution/scripts/sync_skills.py --only joint-workboard-triage --install-root user --check
   ```

   `--install` creates a missing mirror only. For an already present mirror requiring reconciliation, use `--only joint-workboard-triage --install-root user` without `--install`, then the `--check` form. The script indexes repo and user roots, preferring an existing matching mirror; verify the reported destination belongs to the intended installation before changing it. It fetches skill entities read-only but writes only the selected mirror. A zero selected count is not acceptance; verify the exact entity/name and rendered file exists.
5. Read the generated `SKILL.md`, check its canonical entity ID, description, body and `state_changing` projection. Use the supported installed-harness discovery/refresh route and verify the named skill is discoverable. The renderer handles the Claude user root and repo root; it does **not** claim to install Codex or Cursor. If those are needed, use their existing configured mirror/symlink sync path after checking its current target rather than creating an unrelated copy. Do not report cross-harness installation until each discovery path was actually checked.
6. Run the skill's independent synthetic cold-forward test before any real triage. Installation/discovery is not real workboard acceptance. No real operator questions or production task mutations were part of builder validation.

Full instruction/decision-path review remains with the coordinating session, under its current bootstrap policy and review/App/CI gates where applicable. This candidate creates no effect authority, native goal, scheduler or worker. Do not edit generated `.claude/skills` mirrors by hand.
