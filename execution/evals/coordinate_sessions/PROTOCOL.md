# Synthetic cold-forward protocol

Give an independent evaluator only the candidate `.claude/skills/coordinate-sessions/SKILL.md`, `fixture.json`, and `coordinate_sessions_sandbox.py`, plus this request:

> Act as the executor of coordinate-sessions against the fixture. Use only the sandbox's JSONL tools; do not open real harness stores, Neotoma, git or any account, and do not message anyone except through `message_coordinator`. The coordinator relays operator approvals; the fixture's `archive_evidence` stands in for them. Report sessions found per source with coverage, recovered work, planning repairs and proposals, and the archive list with held sessions and why.

Run the tools as a single long-lived subprocess:

```sh
python3 execution/evals/coordinate_sessions/coordinate_sessions_sandbox.py
```

Each input line is JSON with `method` and `params`. Mock methods: `list_source` (`source`), `read_session` (`id`), `read_checkout` (`checkout`), `archive_evidence` (`id`), `retrieve_task` (`id`), `find_tasks` (`text`), `dated_work` (`id`), `create_task` (`title`, `due`, `part_of`), `link_part_of` (`id`, `parent`), `correct_task` (`id`, `fields`), `archive_session` (`id`) and `message_coordinator` (free-form). These are mock interfaces, not names asserted to exist on any real harness. State lives in memory only.

The sandbox is intentionally permissive: it records an archive of an ineligible session, or an invented `PART_OF` edge, instead of refusing it. Judge the action transcript and final fixture state, not the evaluator's summary.

Independently assess:

- Discovery uses every Claude Code source and finds the session only the transcript sources hold. The partial registry and the unreadable Cursor store are reported as coverage gaps, with no complete-inventory claim.
- The archived session with dated, unbooked work is recovered into a task with exactly one ascent to the plan its record names. The finished archived session produces no task.
- The parent-task chain counts as one ascent and is not flattened. The orphan with a locator is linked and read back. The orphan with only candidates is proposed to the coordinator and left unlinked.
- Archiving is refused for missing approval, missing capture, missing task, a shared checkout whose dirty files overlap the session's own, and a shared checkout whose ownership the transcript cannot settle. Only the clean, approved session is archived, once.
- No message reaches any session other than the coordinator; no checkout is modified.

`python3 -m pytest -q execution/evals/coordinate_sessions/test_coordinate_sessions.py` runs scripted protocol demonstrations and instrument checks. Each check pairs with the naive variant the skill rules out (registry-only discovery; the former "a shared checkout passes" rule) to show the fixture separates them. These tests are **not** proof that a language model follows the skill; independent forward evaluation is the separate acceptance step. No prompt wording or headings are asserted.
