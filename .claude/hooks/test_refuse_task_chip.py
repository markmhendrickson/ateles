#!/usr/bin/env python3
"""Exercise refuse_task_chip.py against block/allow/fail-open cases.

Covers: a spawn_task match is refused with the guidance message; every other
tool (including a subagent-tagged call to a different tool) passes through
untouched; malformed/empty stdin fails open rather than blocking a session on
this hook's own bug.
"""
import json
import subprocess
import sys
from pathlib import Path

HOOK = str(Path(__file__).with_name("refuse_task_chip.py"))
SPAWN_TOOL = "mcp__ccd_session__spawn_task"


def run(payload_text):
    p = subprocess.run(
        [sys.executable, HOOK],
        input=payload_text,
        capture_output=True,
        text=True,
    )
    return p.returncode, p.stdout, p.stderr


def run_event(event: dict):
    return run(json.dumps(event))


def main():
    failures = []

    print("=== SHOULD BLOCK (expect exit 2, deny JSON on stdout) ===")
    block_cases = [
        ("bare spawn_task call", {"tool_name": SPAWN_TOOL, "tool_input": {"title": "x"}}),
        (
            "spawn_task from a subagent (agent_id/agent_type present)",
            {
                "tool_name": SPAWN_TOOL,
                "tool_input": {"title": "Fix release path", "prompt": "..."},
                "agent_id": "a0beee560e740d1c2",
                "agent_type": "general-purpose",
            },
        ),
        ("spawn_task with no tool_input at all", {"tool_name": SPAWN_TOOL}),
    ]
    for label, event in block_cases:
        rc, out, _ = run_event(event)
        ok = rc == 2
        if ok:
            try:
                decision = json.loads(out)["hookSpecificOutput"]
                ok = (
                    decision.get("hookEventName") == "PreToolUse"
                    and decision.get("permissionDecision") == "deny"
                    and "spawn_task" in decision.get("permissionDecisionReason", "")
                    and "Neotoma" in decision.get("permissionDecisionReason", "")
                )
            except Exception:
                ok = False
        print(f"  [{'ok' if ok else 'FAIL'}] exit={rc}  {label}")
        if not ok:
            failures.append(label)

    print("\n=== SHOULD ALLOW (expect exit 0, no output) ===")
    allow_cases = [
        ("unrelated Bash tool", {"tool_name": "Bash", "tool_input": {"command": "ls"}}),
        ("unrelated Edit tool", {"tool_name": "Edit", "tool_input": {}}),
        (
            "different ccd_session tool (not spawn_task)",
            {"tool_name": "mcp__ccd_session__dismiss_task", "tool_input": {"task_id": "t1"}},
        ),
        (
            "different MCP server sharing 'spawn_task' substring",
            {"tool_name": "mcp__other_server__spawn_task_v2", "tool_input": {}},
        ),
        ("no tool_name key", {"tool_input": {"command": "ls"}}),
        (
            "subagent-tagged call to an unrelated tool",
            {
                "tool_name": "Bash",
                "tool_input": {"command": "ls"},
                "agent_id": "a0beee560e740d1c2",
                "agent_type": "general-purpose",
            },
        ),
        ("empty object", {}),
    ]
    for label, event in allow_cases:
        rc, out, _ = run_event(event)
        ok = rc == 0 and out.strip() == ""
        print(f"  [{'ok' if ok else 'FAIL'}] exit={rc} out={out!r}  {label}")
        if not ok:
            failures.append(label)

    print("\n=== FAIL-OPEN on malformed/empty input (expect exit 0) ===")
    malformed = [
        ("not json", "not json at all"),
        ("empty stdin", ""),
        ("json array not object", "[1, 2, 3]"),
        ("json string not object", '"just a string"'),
        ("truncated json", '{"tool_name": "mcp__ccd_session__spawn_task"'),
    ]
    for label, text in malformed:
        rc, out, _ = run(text)
        ok = rc == 0
        print(f"  [{'ok' if ok else 'FAIL'}] exit={rc}  {label}")
        if not ok:
            failures.append(label)

    print()
    if failures:
        print(f"FAIL: {len(failures)} case(s) failed: {failures}")
        return 1
    print("PASS: all cases behaved as expected")
    return 0


if __name__ == "__main__":
    sys.exit(main())
