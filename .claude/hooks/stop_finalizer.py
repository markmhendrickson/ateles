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
    neotoma_credentials, missing_credentials_warning_text,
    credentials_warning_already_delivered, mark_credentials_warning_delivered,
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

    # UX finding (PR #1298 round 4, then round 5): a missing-credentials
    # warning written only to stderr is invisible on a Stop — the harness
    # does not feed stderr back into the session — so the operator never
    # sees it. Round 4 fixed this for the non-blocking (exempt/integral)
    # path only; round 5 found that gating on `status != "violated"` routed
    # AROUND the exact highest-stakes case the original audit
    # (ent_b66293f0dcc8c887d4fdbeae) was about: a write-bearing, non-integral
    # session that also has no Neotoma credentials. On that path the
    # operator saw only the BLOCK reason, never the credentials notice.
    #
    # Fix: compute the notice text unconditionally (regardless of status),
    # call emit_harness_event() BEFORE any stdout write (round 5: a failed
    # print() must never skip audit emission — that was the whole point of
    # this PR), then merge the notice into whichever single JSON object this
    # invocation prints — `systemMessage` alone on a clean stop, or
    # `systemMessage` alongside `decision`/`reason` on a block — so exactly
    # one JSON line reaches stdout per invocation either way. Delivery is
    # marked only after that single print succeeds, so a failed presentation
    # attempt does not consume the one-shot per-session notice.
    base_url, token = neotoma_credentials()
    credentials_missing = not base_url or not token
    notice_pending = credentials_missing and not credentials_warning_already_delivered(session_id)
    system_message = (
        missing_credentials_warning_text("session-integrity") if notice_pending else None
    )

    # Suppress emit_harness_event's own stderr warning exactly when this
    # function has decided to present the SAME notice itself via
    # systemMessage below — otherwise both fire for one invocation (the
    # stderr write happens synchronously inside emit_harness_event, before
    # this function's own print further down).
    emit_harness_event(session_id, summary, status, suppress_stderr_warning=notice_pending)

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
        if system_message is not None:
            print(json.dumps({"systemMessage": system_message}))
            mark_credentials_warning_delivered(session_id)
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
        if system_message is not None:
            print(json.dumps({"systemMessage": system_message}))
            mark_credentials_warning_delivered(session_id)
        return 0

    # BLOCK mode: prevent a clean stop. Merge the credentials notice into
    # the SAME JSON object as the block decision — this is the exact
    # write-bearing + missing-credentials case round 5 found unreachable
    # under the old `status != "violated"` gate.
    payload = {"decision": "block", "reason": detail}
    if system_message is not None:
        payload["systemMessage"] = system_message
    print(json.dumps(payload))
    sys.stderr.write(detail + "\n")
    if system_message is not None:
        mark_credentials_warning_delivered(session_id)
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 — fail open, never block on our own bug
        log(f"stop_finalizer error (fail-open, allowing stop): {exc}")
        sys.exit(0)
