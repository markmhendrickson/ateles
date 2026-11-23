# Synthetic cold-forward protocol

Give an independent evaluator only `.claude/skills/migrate-harness/SKILL.md`, `fixture.json` and `migrate_harness_sandbox.py`, plus this request:

> Act as the executor of migrate-harness for the two leads in the fixture. Use only the sandbox's JSONL tools; touch no real harness store, Neotoma, git or account, and message nobody except through `message_coordinator`. Close out each lead, run the section 2 check, decide whether the move may count and whether the old session may be retired, and report one plain line per lead.

```sh
python3 execution/evals/migrate_harness/migrate_harness_sandbox.py
```

Mock methods: `retrieve_task`, `retrieve_plan`, `retrieve_project`, `retrieve_lead` (`id`), `correct_task` / `correct_plan` (`id`, `fields`), `read_old_side` (`session`), `pause_goal`, `pause_watcher`, `resume_goal`, `resume_watcher`, `delete_goal`, `delete_watcher`, `new_side_confirmation`, `archive_session` (`session`) and `message_coordinator`. The sandbox is permissive: it records a forbidden action instead of refusing it, so judge the trace and final state.

Independently assess:

- `lead-clean` passes every section 2 row, including the task-to-task chain counted as one ascent and a stale task that keeps its status with a dated note.
- `lead-bad` fails the ascent (zero, two) and owner (empty, sentinel) rows and is not counted as moved.
- Old goal and watchers are paused, never deleted. Archive happens only after the new side confirmed, the check passed and the pause read back.
- A failed migration restores the recorded goal and watcher status.

`python3 -m pytest -q execution/evals/migrate_harness/` runs scripted protocol demonstrations and instrument checks. Each pairs with the naive variant the skill rules out (append to `next_steps`, archive before confirmation, delete a goal or watcher, close a stale task) to show the fixture separates them. These tests are not proof that a language model follows the skill.
