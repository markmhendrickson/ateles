# docs/archive — material moved out of the active doc set

These files were relocated here during the documentation reconciliation
([../documentation_plan.md](../documentation_plan.md), Parts 2.B / 2.D / 2.F) because they are **not part of
the Ateles reference-architecture doc set**. They are preserved (not deleted) so nothing is lost and the moves
are reversible; several should ultimately live elsewhere.

## Off-topic for this repo
- `transcript_peter_thiel_sxsw_2013_*.md` — a 2013 public conference transcript. Belongs in Neotoma as a
  `transcription` / source entity.
- `agent_dependency_discovery_signal_loss.md` — a standalone essay ("Agents are breaking how developers
  discover better tools"). Content / thought-leadership, not architecture docs.
- `neotoma_developer_release_tester_outreach_templates.md`, `neotoma_developer_release_tester_survey.md` —
  Neotoma developer-release GTM material. Belong in the **neotoma** repo. (Move there, then delete from here.)

## Cursor / foundation-era — superseded (Part 2.B)
- `foundation-vs-ateles-rules.md`, `skills-and-hooks-before-after.md`, `skills-and-hooks-guide.md`,
  `cursor_directory.md`, `cursor_rules/` — pre-swarm Cursor/foundation-era guidance, superseded by the
  entity / agent_policy model.
- `legacy_data_types_inventory.md` — self-described legacy inventory; superseded by `data_types.md`.

> The rest of the legacy "three-layer" foundation lives under `docs/shared/` and is **not** archived — three
> of its files (`agent-workflow-requirements.md`, `agent-mcp-access-policy.md`, `agent-data-rules.md`) are
> still cited as live policy by the linters and `.gitleaks.toml`. Relocating those is a separate, careful step.

## Command docs superseded by skills (Part 2.F)
- `language_command.md`, `learn_neotoma_command.md`, `neotoma_learn_command.md`, `store_neotoma_command.md`,
  `agent_auto_invocation.md` — each duplicates its canonical `.claude/skills/<name>/SKILL.md`, the source of
  truth. Kept here only as historical context.
