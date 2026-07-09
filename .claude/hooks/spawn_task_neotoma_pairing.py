#!/usr/bin/env python3
"""PostToolUse hook: pair every spawned task-chip with a durable Neotoma task.

A `spawn_task` chip (mcp__ccd_session__spawn_task) is a Claude Code UI construct:
it starts a fresh session on one click, but it is NOT a Neotoma entity and is not
persisted to the graph. So a chip alone leaves no durable record — if the operator
never clicks it, the work is untracked. The CLAUDE.md plan/task contract says:
"when a new actionable task is identified — create a `task` entity and link it
PART_OF the bound plan." A chip identifies exactly such a task.

This hook fires AFTER a spawn_task call succeeds and injects a reminder telling
the assistant to create the paired Neotoma `task` entity (and PART_OF-link it to
the bound plan). The hook CANNOT create the entity itself — hooks are stdlib-only
with no MCP access — so the reliable mechanism is a deterministic, unmissable
reminder at the exact moment, which the assistant (with MCP) then acts on.

Design (matches the other hooks in this dir):
- **Fail open.** Any error (bad stdin/JSON/shape) exits 0 with no output.
- **Scoped.** Only fires for the spawn_task tool; silent for everything else.
- **Idempotent-safe.** Only reminds; creating the entity is the assistant's job,
  which is itself idempotency-keyed on the Neotoma side.
- Stdlib-only, dependency-free.

Wire in .claude/settings.json under PostToolUse with matcher
"mcp__ccd_session__spawn_task".

Test:
  echo '{"tool_name":"mcp__ccd_session__spawn_task","tool_input":{"title":"X"},"tool_response":{"task_id":"task_abc"}}' | python3 spawn_task_neotoma_pairing.py
  python3 spawn_task_neotoma_pairing.py --selftest
"""

from __future__ import annotations

import json
import sys

SPAWN_TOOL = "mcp__ccd_session__spawn_task"


def _extract(event: dict) -> tuple[str, str]:
    """Return (title, task_id) best-effort from the tool input/response."""
    ti = event.get("tool_input") or {}
    title = str(ti.get("title") or "").strip()
    resp = event.get("tool_response") or event.get("tool_result") or {}
    task_id = ""
    if isinstance(resp, dict):
        task_id = str(resp.get("task_id") or "")
    elif isinstance(resp, str):
        # some hosts pass the response as text; scrape a task_ token
        for tok in resp.replace(",", " ").replace('"', " ").split():
            if tok.startswith("task_"):
                task_id = tok
                break
    return title, task_id


def reminder_text(title: str, task_id: str) -> str:
    who = f' "{title}"' if title else ""
    chip = f" ({task_id})" if task_id else ""
    return (
        f"You just spawned a task chip{who}{chip}. A chip is an ephemeral Claude "
        "Code UI construct — it is NOT a Neotoma entity. Per the CLAUDE.md plan/task "
        "contract, create the paired durable record now: (1) store a Neotoma `task` "
        "entity capturing this work (title/description, status, priority, "
        "repository_name if repo-touching); (2) create_relationship PART_OF from that "
        "task to the bound plan; (3) reference the chip's task_id in the task so the "
        "two are linked. Do this in the same turn so the work survives whether or not "
        "the chip is ever clicked. If a Neotoma task for this already exists, skip."
    )


def main() -> int:
    try:
        raw = sys.stdin.read()
        event = json.loads(raw) if raw.strip() else {}
    except Exception:
        return 0  # fail open

    try:
        if event.get("tool_name") != SPAWN_TOOL:
            return 0
        title, task_id = _extract(event)
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PostToolUse",
                        "additionalContext": reminder_text(title, task_id),
                    }
                }
            )
        )
        return 0
    except Exception:
        return 0  # fail open


def _selftest() -> int:
    cases = [
        (
            {
                "tool_name": SPAWN_TOOL,
                "tool_input": {"title": "Build X"},
                "tool_response": {"task_id": "task_abc123"},
            },
            True,
        ),
        ({"tool_name": "Bash", "tool_input": {"command": "ls"}}, False),
        ({"tool_name": SPAWN_TOOL, "tool_input": {}, "tool_response": {}}, True),
        ({}, False),
    ]
    ok = True
    for event, want_reminder in cases:
        import io
        import contextlib

        buf = io.StringIO()
        stdin_bak = sys.stdin
        sys.stdin = io.StringIO(json.dumps(event))
        try:
            with contextlib.redirect_stdout(buf):
                main()
        finally:
            sys.stdin = stdin_bak
        emitted = bool(buf.getvalue().strip())
        mark = "ok" if emitted == want_reminder else "FAIL"
        if emitted != want_reminder:
            ok = False
        print(f"  [{mark}] emitted={emitted} (want {want_reminder}): {event.get('tool_name')}")
    print("selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    sys.exit(main())
