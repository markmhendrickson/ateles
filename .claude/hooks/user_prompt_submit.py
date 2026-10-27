#!/usr/bin/env python3
"""UserPromptSubmit hook — lightweight turn accounting (layer 1, §5.1).

Increments the per-session turn counter in the session state file. The Stop
finalizer no longer reads it: since the operator ruling of 2026-10-27 (write
per state change, not per turn) integrity is judged on conversation capture
seen in the transcript, not on a turn count. The counter is kept only as a
cheap liveness signal. Does not block or inject context (exit 0, no stdout) —
conversation capture itself is performed by the agent.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _session_integrity import read_hook_input, load_state, save_state, log  # noqa: E402


def main() -> int:
    ev = read_hook_input()
    session_id = ev.get("session_id", "")
    state = load_state(session_id)
    state["turn_count"] = int(state.get("turn_count", 0)) + 1
    save_state(session_id, state)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 — fail open
        log(f"user_prompt_submit hook error (ignored): {exc}")
        sys.exit(0)
