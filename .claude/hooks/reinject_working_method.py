#!/usr/bin/env python3
"""SessionStart(compact) hook — re-inject the working-method contract.

WHY THIS EXISTS
---------------
Compaction replaces the conversation with a summary. An instruction the
operator gave before that boundary survives only if it happened to make the
summary — so long-running sessions silently lose standing instructions and
the operator has to repeat them. Measured on 2026-09-02: "summarize what I
say" had to be restated twice and "dispatch, don't work inline" four times,
across a session that compacted at least twice.

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

ASSIGNED ROLE (per session, local)
----------------------------------
A session assigned a role also gets a short restatement of it, because a role
given in conversation is lost at compaction like any other instruction. The
match is on the harness's exact hook `session_id` (not the desktop app's
`local_` id, and never the visible session name or title, which can be
copied or reused). Every other session's output is unchanged.

The record is a local gitignored JSON file, so no operator data or session
ids enter this public repo:

    .claude/.session_state/<hook session_id>-role.json
    {"session_id": "<exact hook session_id>", "role": "<short restatement,
     max 600 chars>", "record": "ent_<24 hex>"}

`record` is the Neotoma entity holding the full role. To assign, write that
JSON in the checkout the session runs from, then read it back by piping
`{"session_id": "<id>"}` into this hook and checking for `[session-role]`.
It is a sibling file, not part of `<id>.json`, because `session_start.py`
rewrites `<id>.json` non-atomically in parallel on compact.

A record that exists but is unreadable or malformed yields one notice line
naming the file, never role content. An inner `session_id` that differs from
the hook's (a copied file, or a sanitization collision such as `a.b` vs `ab`)
matches nothing.

Deliberate limits: compaction only, and a lost record silently falls back to
the generic reminder, since there is no central list to say a role was
expected. No network, Neotoma or credential access.

Fail-open: stdlib only, any error exits 0. Never block a session resume.
"""

import json
import re
import sys
from pathlib import Path

from reporting_contract import CONTRACT


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
3. NAME AND LINK THE TASKS. Name the `task` entities any work corresponds to \
and link each by id into the Ateles app, so the operator can open them.
4. PROCEED ON YOUR RECOMMENDATION rather than stopping to ask. If you asked \
something and it went unanswered, re-surface it in each reply to the operator \
until answered.

Full role definition: `.claude/skills/ateles/SKILL.md`. Repo-wide constraints \
are in CLAUDE.md, which Claude Code re-injects from disk on its own.

""" + CONTRACT


_MAX_RECORD_BYTES = 4096
_MAX_ROLE_CHARS = 600
_RECORD_RE = re.compile(r"ent_[0-9a-f]{24}")


def _role_restatement() -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import _session_integrity as si  # lazy: an import failure must not lose REMINDER

    session_id = si.read_hook_input().get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return
    path = si.state_path(session_id + "-role")
    if not path.exists():
        return

    unreadable = (
        f"[session-role] An assigned-role record exists for this session at {path} "
        "but it could not be read or is invalid; role not restated. Ask the "
        "operator to restate the role or repair the file."
    )
    try:
        if path.stat().st_size > _MAX_RECORD_BYTES:
            raise ValueError("oversized")
        rec = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(rec, dict):
            raise ValueError("not an object")
    except Exception as exc:  # noqa: BLE001
        si.log(f"role record unreadable: {type(exc).__name__}")
        print(unreadable)
        return

    if rec.get("session_id") != session_id:
        si.log("role record session_id differs from hook session_id; ignored")
        return
    role, record = rec.get("role"), rec.get("record")
    if (
        not isinstance(role, str)
        or not role.strip()
        or len(role) > _MAX_ROLE_CHARS
        or not isinstance(record, str)
        or not _RECORD_RE.fullmatch(record)
    ):
        print(unreadable)
        return
    role = " ".join(role.split())
    print(
        "[session-role] This session holds an assigned role, matched by its exact "
        f"session id: {role}\n"
        f"Retrieve record {record} from the operator's prod Neotoma before "
        "acting on the role."
    )


def main() -> int:
    print(REMINDER)
    try:
        _role_restatement()
    except Exception:  # noqa: BLE001 — never lose the generic reminder
        pass
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 — fail open; never block a resume
        sys.exit(0)
