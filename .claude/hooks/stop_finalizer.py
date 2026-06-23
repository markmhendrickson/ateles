#!/usr/bin/env python3
"""Stop finalizer — the session-integrity enforcement gate (layer 1, §5.1, §6).

Analogous to the global ~/.claude git stop-hook: it inspects the finished
session and BLOCKS a clean stop only when it can POSITIVELY determine the
session is write-bearing but non-integral (no bound plan, or zero stored
turns). Everything else is allowed through:

  - no-op sessions (no domain writes)      -> exempt  (grace path, §6)
  - write-bearing + plan + turns           -> integral
  - write-bearing + (no plan OR no turns)  -> violated -> BLOCK

Rollout posture (§6): default is WARN — emit the audit + a stderr notice but
exit 0. Set ATELES_SESSION_INTEGRITY_ENFORCE=1 to flip to BLOCK mode, where a
violation prevents a clean stop so the agent is forced to bind a plan / store
its turns before exiting. Either way a harness_event audit row is emitted.

Fail-open everywhere: a parse error, missing transcript, or emission failure
never blocks.

Block contract: Claude Code treats Stop-hook exit code 2 (with reason on
stderr) as "block the stop and feed reason back to the model". We also emit
the structured {"decision":"block","reason":...} JSON on stdout for forward
compatibility.
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _session_integrity import (  # noqa: E402
    read_hook_input, load_state, scan_transcript, emit_harness_event, log,
)

ENFORCE = os.environ.get("ATELES_SESSION_INTEGRITY_ENFORCE", "") in ("1", "true", "yes")


def main() -> int:
    ev = read_hook_input()
    session_id = ev.get("session_id", "")

    # Avoid infinite loops: if we already blocked once this stop, don't re-block.
    if ev.get("stop_hook_active"):
        return 0

    transcript = ev.get("transcript_path")
    summary = scan_transcript(transcript)
    state = load_state(session_id)

    # Merge the cheap counter signal with the transcript scan.
    turns = max(int(summary.get("turns", 0)), int(state.get("turn_count", 0)))
    wrote_domain = bool(summary.get("wrote_domain"))
    # Plan binding must be EVIDENCED by the transcript (an actual plan touch /
    # link), not merely the SessionStart default intent — otherwise the
    # plan-link check could never fire. state["bound_plan_id"] is only the
    # *intended* default and is deliberately NOT treated as proof of binding.
    bound_plan = bool(summary.get("bound_plan"))
    bound_task = bool(summary.get("bound_task"))
    # Plan-optionality (task-spine plan): a session is anchored if bound to a
    # plan OR a task. A single self-contained task is a valid binding target.
    bound = bound_plan or bound_task
    captured_learning = bool(summary.get("captured_learning"))

    if not wrote_domain:
        status = "exempt"  # grace path: pure read/no-op session
    elif bound and turns > 0:
        status = "integral"
    else:
        status = "violated"

    emit_harness_event(session_id, summary, status)

    # /end convergence (task-spine plan, task #3): a substantive session that
    # stored no learning artifact is nudged to run /end (which captures turns +
    # learnings and finalizes the plan). Soft by design — encouraged, not blocked,
    # so it applies uniformly to HITL and spawned autonomous agents (both hit this
    # same Stop hook) without forcing /end on every trivial write.
    if status != "violated":
        if wrote_domain and not captured_learning:
            log(
                "reminder: this session captured no learning artifact — consider "
                "running /end to record learnings and finalize the plan."
            )
        return 0

    reasons = []
    if not bound:
        reasons.append("no plan OR task link (conversation not PART_OF any plan or task)")
    if turns == 0:
        reasons.append("no stored turns (conversation_message/agent_message rows missing)")
    detail = (
        "Session integrity violation: this session made domain writes but has "
        + " and ".join(reasons)
        + ". Per docs/session_integrity.md, bind the conversation to a plan "
        "(default ent_99ace4dd6673aa36ed08b1fe) OR a task, store the turns, and "
        "run /end to finalize before stopping."
    )

    if not ENFORCE:
        log(f"WARN (not enforcing): {detail}")
        return 0

    # BLOCK mode: prevent a clean stop.
    print(json.dumps({"decision": "block", "reason": detail}))
    sys.stderr.write(detail + "\n")
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 — fail open, never block on our own bug
        log(f"stop_finalizer error (fail-open, allowing stop): {exc}")
        sys.exit(0)
