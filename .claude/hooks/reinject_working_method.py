#!/usr/bin/env python3
"""SessionStart(compact) hook — re-inject the working-method contract.

WHY THIS EXISTS
---------------
Compaction replaces the conversation with a summary. An instruction the
operator gave before that boundary survives only if it happened to make the
summary — so long-running sessions silently lose standing instructions and
the operator has to repeat them. Measured on 2026-09-02: "summarize what I
say" had to be restated twice and "dispatch, don't work inline" four times,
across a session that compacted at least twice. Rule 6 is the same failure
one level up: two gate decisions were routed back to the operator that the
session should have either acted on outright or dispatched to ground —
one recommendation was confident enough to act on, the other was inference
from a summary rather than the source, and so was not a recommendation at all.

The repo already had a SessionStart hook (`session_start.py`) that injects
the plan-binding contract, but it was matched to `startup|resume|clear` —
which EXCLUDES `compact`. So the one moment context is lost was the one
moment nothing was re-injected. That matcher is fixed in settings.json; this
hook carries the working-method rules that were actually being dropped.

WHAT FIRES THIS
---------------
Claude Code's `SessionStart` event with matcher `compact`, which fires after
both auto-compaction and a manual `/compact`. SessionStart stdout is added
to the model's context as plain text (unlike PreCompact, whose stdout goes
only to the debug log and therefore cannot re-inject anything).

SCOPE — deliberately short
--------------------------
This restates ONLY the constraints that measurably got dropped: the
interaction rules that live in conversation rather than on disk. It does NOT
restate the repo-hygiene rules (no writes to shared clones, no `git stash`)
— those ride in CLAUDE.md, which Claude Code re-injects from disk after
compaction, and in every subagent brief. Re-listing everything every time is
its own failure mode; keep this to what compaction actually eats.

Fail-open: stdlib only, any error exits 0. Never block a session resume.
"""
import sys

REMINDER = """\
[working-method] Context was just compacted. Standing instructions from the \
operator that do not survive a summary — restated verbatim in effect:

1. DISPATCH, DON'T WORK INLINE. Create a Neotoma `task` entity and let an \
agent claim it; use a subagent only when no swarm path exists. This covers \
research, analysis, and investigation, not just code. Work you recommend is \
work you file, in the same turn, without being asked. Never a harness task \
chip — durable work is a task entity or it does not exist.
2. SUMMARIZE WHAT THE OPERATOR SAID at the top of every reply, cleaned up. \
Input arrives by voice and transcription garbles and fabricates; showing what \
was heard is how the operator catches it.
3. REPORT STATUS UNPROMPTED — what moved, what is blocked, and ONE recommended \
next step per workstream so the operator can say whether to stop that \
workstream for now.
4. NAME AND LINK THE TASKS. Name the `task` entities any work corresponds to \
and link each by id into the Ateles app, so the operator can open them.
5. PROCEED ON YOUR RECOMMENDATION rather than stopping to ask. If you asked \
something and it went unanswered, re-surface it each turn until answered.
6. A DECISION GOES TO THE OPERATOR ONLY WHEN IT IS GENUINELY THEIRS — their \
priorities, their risk tolerance, or something only they know. Confident \
recommendation: act on it and report. No confident recommendation: dispatch to \
establish the facts. A recommendation formed without reading the primary source \
is a guess, and the answer to a guess is to dispatch, not to escalate.

Full role definition: `.claude/skills/ateles/SKILL.md`. Repo-wide constraints \
are in CLAUDE.md, which Claude Code re-injects from disk on its own."""


def main() -> int:
    print(REMINDER)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 — fail open; never block a resume
        sys.exit(0)
