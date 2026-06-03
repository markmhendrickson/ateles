#!/usr/bin/env python3
"""UserPromptSubmit hook — lightweight turn accounting (layer 1, §5.1).

Increments the per-session turn counter so the Stop finalizer has a cheap
signal even if the transcript is unavailable. Does not block or inject
context (exit 0, no stdout) — turn capture itself is performed by the agent
via the MCP [TURN LIFECYCLE] contract; this hook only counts.
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
