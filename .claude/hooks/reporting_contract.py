#!/usr/bin/env python3
"""SessionStart hook: deliver the operator-facing reporting contract.

The same script is wired into Claude Code and Codex.  It is intentionally
short enough to re-deliver at startup, resume, clear, and compaction: those
are the boundaries at which a harness either begins without the rule or can
lose it from working context.
"""

from __future__ import annotations

CONTRACT = """\
[reporting-contract] Report at medium/operator altitude.

- Send a compact progress update only at a MATERIAL STATE CHANGE: a verified
  result, changed plan state, new or cleared blocker, completed deliverable,
  or operator decision that now matters. Do not narrate each tool call.
- Shape each update as: WHAT CHANGED -> WHY IT MATTERS to the verified parent
  plan or strategy -> NEXT OWNER/ACTION. Put low-level evidence in the task,
  issue, PR, or commit; include it in chat only when it changes a decision.
- Make the final answer self-contained. Codex commentary can collapse, so the
  final must carry the outcome, plan meaning, blockers, and next action without
  telling the operator to recover them from earlier updates.
"""


def main() -> int:
    print(CONTRACT)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:  # noqa: BLE001 -- context delivery must fail open
        raise SystemExit(0)
