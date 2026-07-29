"""Harness-agnostic logic for the spawn_task_neotoma_pairing hook.

A `spawn_task` chip (mcp__ccd_session__spawn_task) is a Claude Code UI
construct: it starts a fresh session on one click, but it is NOT a Neotoma
entity and is not persisted to the graph. So a chip alone leaves no durable
record — if the operator never clicks it, the work is untracked. The
CLAUDE.md plan/task contract says: "when a new actionable task is identified
— create a `task` entity and link it PART_OF the bound plan." A chip
identifies exactly such a task.

This module fires AFTER a spawn_task call succeeds and returns a reminder
telling the assistant to create the paired Neotoma `task` entity (and
PART_OF-link it to the bound plan). The logic CANNOT create the entity
itself — hooks are stdlib-only with no MCP access — so the reliable
mechanism is a deterministic, unmissable reminder at the exact moment, which
the assistant (with MCP) then acts on.

Design:
- Pure function. No stdin/stdout/sys/subprocess — the adapter owns I/O.
- Fail-open is the ADAPTER's responsibility (catches exceptions from here).
- Scoped. Only fires for the spawn_task tool; returns None for everything else.
- Idempotent-safe. Only reminds; creating the entity is the assistant's job,
  which is itself idempotency-keyed on the Neotoma side.

Governed by Neotoma `hook_policy` entity
canonical_name hook_policy:spawn_task_neotoma_pairing|claude|markmhendrickson/ateles
(see docs/hooks/POLICY_TEMPLATE.md for the field shape).
"""

from __future__ import annotations

SPAWN_TOOL = "mcp__ccd_session__spawn_task"


def _extract(event: dict) -> tuple[str, str]:
    """Return (title, task_id) best-effort from the tool input/response."""
    ti = event.get("tool_input") or {}
    title = str(ti.get("title") or "").strip()
    resp = event.get("tool_response") or event.get("tool_result") or {}
    task_id = ""
    if isinstance(resp, dict):
        task_id = str(resp.get("task_id") or "")
    elif isinstance(resp, str):
        # some hosts pass the response as text; scrape a task_ token
        for tok in resp.replace(",", " ").replace('"', " ").split():
            if tok.startswith("task_"):
                task_id = tok
                break
    return title, task_id


def reminder_text(title: str, task_id: str) -> str:
    who = f' "{title}"' if title else ""
    chip = f" ({task_id})" if task_id else ""
    return (
        f"You just spawned a task chip{who}{chip}. A chip is an ephemeral Claude "
        "Code UI construct — it is NOT a Neotoma entity. Per the CLAUDE.md plan/task "
        "contract, create the paired durable record now: (1) store a Neotoma `task` "
        "entity capturing this work (title/description, status, priority, "
        "repository_name if repo-touching); (2) create_relationship PART_OF from that "
        "task to the bound plan; (3) reference the chip's task_id in the task so the "
        "two are linked. Do this in the same turn so the work survives whether or not "
        "the chip is ever clicked. If a Neotoma task for this already exists, skip."
    )


def handle(event: dict) -> str | None:
    """Pure entry point: PostToolUse event dict -> additionalContext string, or None.

    Returns None (no-op) when the event is not a spawn_task call. Never raises
    on malformed input beyond what dict.get tolerates; a genuinely malformed
    event (e.g. event is not a dict) propagates as an exception for the
    adapter to catch and report per the runtime-exception contract.
    """
    if event.get("tool_name") != SPAWN_TOOL:
        return None
    title, task_id = _extract(event)
    return reminder_text(title, task_id)
