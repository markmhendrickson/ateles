#!/usr/bin/env python3
"""UserPromptSubmit hook — turn accounting (layer 1, §5.1) and held-notice surface.

Increments the per-session turn counter in the session state file. The Stop
finalizer no longer reads it: since the operator ruling of 2026-10-27 (write
per state change, not per turn) integrity is judged on conversation capture
seen in the transcript, not on a turn count. The counter is kept only as a
cheap liveness signal.

It also prints ONE line to stdout (which the harness adds to model context)
when daemons hold operator notices that are waiting undelivered, and only when the
held total is non-zero and differs from the last total shown in this session.
The line is built from integers and regex-validated handler names returned by
lib/notify/held_view.py, never from notice text: notice text can embed
attacker-influenced strings (PR titles, issue text) and must not reach model
context from here. Fail-open: any error means no output and exit 0.
"""
import importlib.util
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _session_integrity import read_hook_input, load_state, save_state, log  # noqa: E402


def _load_held_view():
    """Load held_view.py by file path: stdlib-only, no lib.notify package init."""
    path = Path(__file__).resolve().parent.parent.parent / "lib" / "notify" / "held_view.py"
    spec = importlib.util.spec_from_file_location("_ateles_held_view", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def held_notice_line(state: dict) -> str:
    """The one-line surface, or "". Updates ``state`` with the contents hash shown.

    Keyed on a hash of the queue contents, not the total, so different notices
    with the same count are announced again. Only the hash is kept in session
    state, never any notice text.
    """
    info = _load_held_view().summary()
    total = int(info["total"])
    fingerprint = str(info["fingerprint"])
    if total == 0 or fingerprint == state.get("held_notices_hash"):
        state["held_notices_hash"] = fingerprint  # re-arm after the queue empties
        return ""
    state["held_notices_hash"] = fingerprint
    ranked = sorted(info["by_handler"].items(), key=lambda kv: (-kv[1], kv[0]))
    parts = ", ".join(f"{name}: {int(n)}" for name, n in ranked[:6])
    what = "operator notice is" if total == 1 else "operator notices are"
    return (
        f"[held-notices] {total} {what} waiting undelivered ({parts}). In your "
        "reply, tell the operator what needs them: read them with python3 "
        "lib/notify/held_view.py, then clear what you showed with --clear."
    )


def main() -> int:
    ev = read_hook_input()
    session_id = ev.get("session_id", "")
    state = load_state(session_id)
    state["turn_count"] = int(state.get("turn_count", 0)) + 1
    line = ""
    try:
        line = held_notice_line(state)
    except Exception as exc:  # noqa: BLE001 — the surface must never break a prompt
        log(f"held-notice surface error (ignored): {exc}")
    save_state(session_id, state)
    if line:
        print(line)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 — fail open
        log(f"user_prompt_submit hook error (ignored): {exc}")
        sys.exit(0)
