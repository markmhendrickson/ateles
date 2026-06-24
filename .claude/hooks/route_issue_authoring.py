#!/usr/bin/env python3
"""PreToolUse hook: route GitHub issue authoring through the swarm.

Intercepts raw ``gh issue create`` Bash calls against the operator's product
repos (neotoma / ateles) and asks the agent to route through the proper swarm
path instead — ``submit_issue`` (which mints the Neotoma ``issue`` entity and
mirrors to GitHub, entering the Lanius gate pipeline) and, for inbound
partner/customer feedback, the Hirundo due-diligence agent.

Why: filing with raw ``gh`` bypasses Neotoma issue authoring (structured
root_cause / proposed_fixes / gate_status) and the orchestrator's
delegate-don't-do-it-inline default. The GitHub→swarm SSE ingestion will
eventually absorb the issue, but only after the fact and without the richer
authoring.

Design constraints (mirror the session-integrity hooks):
- Fail OPEN. Any error, unparseable input, or non-matching command -> exit 0
  with no output, never block a tool call.
- Stdlib-only, dependency-free.
- Soft interrupt: emits permissionDecision "ask" (surface for confirmation),
  never a hard "deny". A reviewed, deliberate raw filing can still proceed.

Escape hatch: include the marker ``# routed-ok`` in the command to bypass.

Test mode:
  echo '{"tool_name":"Bash","tool_input":{"command":"gh issue create --repo markmhendrickson/neotoma -t x"}}' | python3 route_issue_authoring.py
  python3 route_issue_authoring.py --selftest
"""

from __future__ import annotations

import json
import re
import sys

# Repos this guard applies to (operator product repos owned by the swarm).
GUARDED_REPOS = ("neotoma", "ateles")

_GH_ISSUE_CREATE = re.compile(r"\bgh\s+issue\s+create\b", re.IGNORECASE)

REASON = (
    "Route GitHub issue authoring through the swarm instead of raw `gh issue create`:\n"
    "  • Use Neotoma `submit_issue` — it authors the `issue` entity (structured "
    "root_cause / proposed_fixes), mirrors to GitHub, and enters the Lanius gate "
    "pipeline. Pass `target_repo` for a repo other than the configured one.\n"
    "  • If this stems from inbound partner/customer feedback or claims to verify, "
    "route to Hirundo (/hirundo) — partner technical due-diligence owns "
    "verify-against-source → filed issues → partner reply.\n"
    "  • As orchestrator, delegate to the owning agent; only file inline when no "
    "agent owns it. To proceed deliberately anyway, append `# routed-ok` to the command."
)


def command_is_guarded(command: str) -> bool:
    """True when the command files a GitHub issue against a guarded repo."""
    if not command or "# routed-ok" in command:
        return False
    if not _GH_ISSUE_CREATE.search(command):
        return False
    # Trigger when a guarded repo is named, OR when no --repo is given (cwd-based,
    # which in this project is almost always neotoma or ateles).
    if "--repo" not in command:
        return True
    return any(repo in command for repo in GUARDED_REPOS)


def emit_ask(reason: str) -> None:
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "ask",
                    "permissionDecisionReason": reason,
                }
            }
        )
    )


def _run_selftest() -> int:
    cases = [
        ("gh issue create --repo markmhendrickson/neotoma -t x", True),
        ("gh issue create -t 'bug'", True),  # no --repo -> cwd-based, guard
        ("gh issue create --repo markmhendrickson/ateles -t x", True),
        ("gh issue create --repo someone/other -t x", False),
        ("gh issue create --repo markmhendrickson/neotoma -t x # routed-ok", False),
        ("gh issue list --repo markmhendrickson/neotoma", False),
        ("gh pr create --repo markmhendrickson/neotoma", False),
        ("echo hello", False),
    ]
    ok = True
    for cmd, expected in cases:
        got = command_is_guarded(cmd)
        flag = "PASS" if got == expected else "FAIL"
        ok = ok and (got == expected)
        print(f"[{flag}] guarded={got} expected={expected} :: {cmd}")
    return 0 if ok else 1


def main() -> int:
    if "--selftest" in sys.argv[1:]:
        return _run_selftest()

    event = json.loads(sys.stdin.read())
    if event.get("tool_name") != "Bash":
        return 0
    command = (event.get("tool_input") or {}).get("command", "")
    if command_is_guarded(command):
        emit_ask(REASON)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # Fail open: never block a tool call on hook error.
        sys.exit(0)
