#!/usr/bin/env python3
"""SessionStart hook — open/bind the session to a plan (layer 1, §5.1).

Initializes per-session state and injects a one-line reminder of the
session-integrity contract into context, including the default plan binding.
This does NOT itself create the conversation entity — the agent does that on
turn 1 via the Neotoma MCP [TURN LIFECYCLE] contract; the hook records the
intended binding and reminds the agent so the bind actually happens.

Always exits 0 — session start is never blocked. Stdout is injected as
session context by Claude Code.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _session_integrity import (  # noqa: E402
    DEFAULT_PLAN_ID, read_hook_input, load_state, save_state, log,
)


def main() -> int:
    ev = read_hook_input()
    session_id = ev.get("session_id", "")
    state = load_state(session_id)
    state.setdefault("session_id", session_id)
    state.setdefault("bound_plan_id", DEFAULT_PLAN_ID)
    state.setdefault("turn_count", 0)
    save_state(session_id, state)

    # Context reminder — keeps the binding contract in working attention.
    print(
        "[session-integrity] This is a write-bearing-capable session. Per "
        "docs/session_integrity.md: bind this session's conversation entity "
        f"PART_OF a plan (default: {DEFAULT_PLAN_ID}), store each turn as "
        "user+assistant agent_message rows PART_OF the conversation, and link "
        "any derived artifacts REFERS_TO the conversation + PART_OF the plan. "
        "A write-bearing session that ends with no plan link or no stored "
        "turns will be flagged at Stop."
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 — fail open, never block start
        log(f"session_start hook error (ignored): {exc}")
        sys.exit(0)
