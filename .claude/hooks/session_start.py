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

    # Intake protocol — auto-engage the fast intake→dispatch→report flow on the
    # first substantive request, collapsing HITL to one clarification batch + one
    # approval gate (.claude/skills/intake/SKILL.md, invocable as /intake).
    print(
        "[intake] On the first SUBSTANTIVE request of this session (a task/goal, "
        "not a quick lookup), auto-engage the `intake` skill: (1) cross-check the "
        "ask against existing Neotoma plans/tasks and reuse/refine the best fit "
        "rather than duplicating; (2) ask ALL clarifying forks in ONE batch; "
        "(3) lay out the COMPLETE task breakdown with all follow-up anticipated "
        "up front — no turn-by-turn 'what's next'; (4) take one approval gate that "
        "also captures the delivery choice — wait for an inline report, or have a "
        "rendered-page report emailed to the operator and linked for posterity. "
        "Skip for trivial one-step asks; invoke explicitly with /intake."
    )

    _emit_skill_drift_notice()
    return 0


def _emit_skill_drift_notice() -> None:
    """Surface drifted/missing skill mirrors so the operator can resync.

    Runs sync_skills.py --check (read-only) with a hard timeout. Never blocks,
    never writes — a Neotoma outage or slow query is swallowed silently.
    """
    import subprocess

    repo_root = Path(__file__).resolve().parents[2]
    script = repo_root / "execution" / "scripts" / "sync_skills.py"
    if not script.exists():
        return
    try:
        proc = subprocess.run(
            [sys.executable, str(script), "--check"],
            capture_output=True, text=True, timeout=20,
        )
    except Exception:
        return  # fail open: never let skill-sync delay or break session start
    if proc.returncode == 0:
        return
    rows = [r for r in proc.stdout.splitlines() if r.startswith(("MISSING", "DRIFTED"))]
    if not rows:
        return
    missing = [r.split()[1] for r in rows if r.startswith("MISSING")]
    drifted = [r.split()[1] for r in rows if r.startswith("DRIFTED")]
    parts = []
    if missing:
        parts.append(f"{len(missing)} entity-only skill(s) not installed on disk "
                     f"(won't load): {', '.join(missing[:8])}")
    if drifted:
        parts.append(f"{len(drifted)} skill mirror(s) drifted from Neotoma: "
                     f"{', '.join(drifted[:8])}")
    print(
        "[skill-sync] " + "; ".join(parts) + ". "
        "Canonical content lives in Neotoma; run "
        "`python3 execution/scripts/sync_skills.py` to write drifted mirrors, "
        "or `--install <slug>` to materialize a missing one (then it loads next "
        "session). Review before applying — an on-disk body may intentionally "
        "lead Neotoma."
    )


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 — fail open, never block start
        log(f"session_start hook error (ignored): {exc}")
        sys.exit(0)
