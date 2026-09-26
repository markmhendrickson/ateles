#!/usr/bin/env python3
"""PreToolUse hook — refuse the harness task-chip tool; point to a Neotoma task.

The hazard (ateles task ent_29397fb3b22b90ad6a2e9112, opened 2026-09-26):
CLAUDE.md says "Durable work never goes into a harness task chip (spawn_task)
— a chip is not an entity, so it is unclaimable and invisible to the swarm."
On 2026-09-26 a dispatched subagent (a release-path fix) created a chip
anyway, because subagents do not receive CLAUDE.md or the rule index (audit
ent_b66293f0dcc8c887d4fdbeae) — the prose rule cannot reach a session that is
never shown it. `spawn_task_neotoma_pairing.py` (PostToolUse, same directory)
already reacts to a chip AFTER it is created by reminding the caller to also
file a Neotoma task; it does not stop the chip from being created, and a
subagent that never saw CLAUDE.md is equally unlikely to see or act on that
reminder before its own turn ends. This hook closes the gap one layer
earlier: it refuses the call outright, for every caller — top-level session
or subagent — because PreToolUse hooks configured in project settings are
enforced by the harness itself, not by anything the calling agent was told.

Refused tool: `mcp__ccd_session__spawn_task` (MCP server `ccd_session`, tool
`spawn_task` — the "task chip" surfaced in the Claude Code UI). This is a
DENY, not a warning: every call is refused, unconditionally, with guidance to
file a durable Neotoma `task` entity (PART_OF the relevant plan) instead.
There is no override — unlike git_stash_guard.py/gmail_send_gate.py, which
gate an otherwise-legitimate action pending per-instance approval, a task
chip is never the right destination for durable work, so there is nothing an
inline override should ever unblock. If a scoped ephemeral chip is genuinely
wanted, that is a product decision to loosen this hook, not a per-call
exception.

Subagent coverage (verified empirically, ateles task ent_29397fb3b22b90ad6a2e9112):
project-configured PreToolUse hooks fire for tool calls issued by a Task-tool
subagent, not only for the top-level session. Confirmed by running a scratch
project through `claude -p` with a PreToolUse hook on `Bash`, instructing the
top-level session to make ONLY a Task-tool call and forbidding it from running
Bash directly, and having the spawned subagent run `Bash`: the hook fired
exactly once, for that subagent's Bash call. Hook enforcement is centralized
in the harness and does not depend on whether the calling context is the main
session or a subagent, matching this hook's purpose — the whole point is to
catch a subagent that never saw the CLAUDE.md prose rule in the first place.

Matcher: wire this under PreToolUse with matcher
`^mcp__ccd_session__spawn_task$` (exact MCP tool name; MCP tools are named
`mcp__<server>__<tool>` and Claude Code matches the matcher as a regex against
the full tool name). An anchored exact match is used rather than a broader
`mcp__ccd_session__.*` because only `spawn_task` is the durable-work hazard —
other `ccd_session` tools (session settings, etc.) are unrelated.

Design (matches the other hooks in this directory):
- Stdlib-only, dependency-free.
- Fail-open: any error or unparseable input -> exit 0 (never block a session
  on our own bug).
- Deny on a genuine match: exit 2 with a `hookSpecificOutput` JSON block
  (`permissionDecision: deny`), matching git_stash_guard.py / gmail_send_gate.py.

Test: test_refuse_task_chip.py covers a match refused with the message, other
tools passing through untouched, and malformed input failing open.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _session_integrity import read_hook_input  # noqa: E402

SPAWN_TOOL = "mcp__ccd_session__spawn_task"

GUIDANCE = (
    "Refused: `mcp__ccd_session__spawn_task` creates an ephemeral harness task "
    "chip, not a Neotoma entity. Per CLAUDE.md: \"Durable work never goes into a "
    "harness task chip (spawn_task) — a chip is not an entity, so it is "
    "unclaimable and invisible to the swarm.\"\n\n"
    "File a Neotoma `task` entity instead, linked PART_OF the relevant plan, "
    "then let an agent claim it. This applies whether you are the top-level "
    "session or a dispatched subagent — subagents do not receive CLAUDE.md, so "
    "this hook is the only place the rule reaches you.\n\n"
    "There is no override for this hook: a task chip is never the right home "
    "for durable work, so no per-call exception is offered."
)


def log(msg: str) -> None:
    """Local rather than imported: the shared helper's `log` hardcodes a
    `[session-integrity]` prefix, which would mislabel this hook's
    diagnostics as coming from a different subsystem. `read_hook_input` IS
    shared — its stdin/fail-open semantics are exactly what this hook wants."""
    try:
        print(f"[refuse_task_chip] {msg}", file=sys.stderr)
    except Exception:  # noqa: BLE001 — logging must never block
        pass


def deny(reason: str) -> int:
    """Emit a PreToolUse deny and block."""
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }
        )
    )
    return 2


def main() -> int:
    payload = read_hook_input()  # shared helper; fail-open to {} on any error

    if payload.get("tool_name") != SPAWN_TOOL:
        return 0

    log("blocking spawn_task call; directing caller to file a Neotoma task")
    return deny(GUIDANCE)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 — fail open, never break a session
        log(f"internal error, failing open: {exc}")
        sys.exit(0)
