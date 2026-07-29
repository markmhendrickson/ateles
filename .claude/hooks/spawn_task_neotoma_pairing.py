# GENERATED FILE. Do not edit directly.
# Source of truth: hooks/logic/spawn_task_neotoma_pairing.py (behavior) + Neotoma
# hook_policy entity ent_761d8de9f0f23a3081339e8a (wiring/intent). Regenerate with:
#   python3 execution/scripts/render_hooks.py --write

"""Adapter for the spawn_task_neotoma_pairing hook (PostToolUse).

Wiring/intent governed by Neotoma hook_policy `ent_761d8de9f0f23a3081339e8a`.
Behavior lives in hooks/logic/spawn_task_neotoma_pairing.py (harness-agnostic).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from hooks.logic.spawn_task_neotoma_pairing import handle  # noqa: E402


def main() -> int:
    try:
        raw = sys.stdin.read()
        event = json.loads(raw) if raw.strip() else {}
    except Exception:
        return 0  # fail open: malformed stdin never blocks the harness

    try:
        context = handle(event)
    except Exception as exc:  # noqa: BLE001 — never let a bare traceback leak
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": f"[spawn_task_neotoma_pairing] hook error: {type(exc).__name__}: {exc}",
            }
        }))
        return 0  # fail open

    if context:
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": context,
            }
        }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
