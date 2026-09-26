#!/usr/bin/env python3
"""Stop hook: refuse a stop that leaves an open workstream unmentioned and
undispatched, once it has had N turns to be handled.

Session work pickup plan (`ent_0b3ec4b252ee88a2cd88ab25`), Phase 1. The
measured failure this closes is NOT forgetting — it is "tracked, reported,
and never consumed": 348 checkpoint briefs `awaiting_operator`, 227 tasks
`awaiting_approval`, 3,603 pending, and 389 `session_digest` entities with
zero code readers (`grep session_digest` over `execution/` and `lib/`
returns nothing). The same day this plan was written, four agents stalled
waiting on `Monitor` and two sessions reported having delegated work they
never actually dispatched — caught only because a human happened to look.

WHAT COUNTS AS AN OPEN WORKSTREAM, both detected from THIS session's own
transcript (never a global Neotoma sweep — the 348/227 backlog is not this
session's fault and flagging it here would make every session non-integral):

1. DISPATCH LEFT UNRESOLVED. The session called a dispatch-shaped tool
   (`Agent`, `mcp__ateles__route_task`, `Monitor`) and the transcript shows
   no later mention of that dispatch being checked, reported, or acted on.
   This is the literal "stalled waiting on Monitor" / "reported delegating
   work never done" failure mode.
2. AN OPEN NEOTOMA ENTITY TOUCHED BUT NEVER SURFACED. The session read or
   wrote a `checkpoint_brief` or `task` whose status is one of the open
   states (`awaiting_operator`, `awaiting_approval`, `pending_review`,
   `pending`) and its id/title never appears in the final assistant
   message's closing section — i.e. the session saw it and said nothing.

BOUNDED, per the corpus rule that escalation reorders but never signs: this
hook may only insist the turn MENTION or DISPATCH the workstream — never that
it send, publish, merge, or otherwise take a consent-gated action. A finding
here is satisfied by a sentence naming the workstream and what happens to it
next, exactly like the existing decision-shape check's operator-only rule.

ONE FORCED TURN ONLY. Matches the `stop_hook_active` guard used by both
`stop_finalizer.py` and `decision_shape_gate.py` — this is a nudge, not a
loop. Verified present in both siblings before relying on the same contract
here (2026-09-16).

MODE: WARN by default, flipped to BLOCK only by
`ATELES_WORKSTREAM_COVERAGE_ENFORCE=1`. This is its OWN env var, deliberately
distinct from `ATELES_SESSION_INTEGRITY_ENFORCE` (owned by ateles#306) and
from `ATELES_DECISION_SHAPE_ENFORCE` — flipping either of those must never
silently arm this check, and flipping this one must never arm the others.
This plan explicitly does not flip any global enforcement flag; the switch
for THIS mechanism, like the others, stays the operator's.

FALSE-POSITIVE RISK (unmeasured, stated plainly rather than assumed away):
a `Monitor` or `Agent` call followed by a later tool result that is not
textually distinguishable from "I checked it" will read as unresolved even
when the session did follow up in a way this regex-level scan cannot see
(e.g. the follow-up happened via a tool this hook does not treat as a
resolution signal). This is why the default is WARN — the same posture
`stop_finalizer.py` took before its own BLOCK rollout, and for the same
reason: no regression corpus exists yet to bound the false-positive rate.

Fail-open (stdlib only, no third-party imports; any error or unreadable
transcript exits 0), matching every hook in this directory.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _session_integrity import (  # noqa: E402
    emit_harness_event_raw, load_state, read_hook_input,
)

ENFORCE = os.environ.get("ATELES_WORKSTREAM_COVERAGE_ENFORCE", "") in ("1", "true", "yes")

# Turns a workstream is allowed to sit open before the gate cares. Matches the
# "N turns" language in the plan's own next_steps; small on purpose — this is
# a nudge worth firing within the SAME session, not a multi-day SLA.
MIN_TURNS_OPEN = 2

# Tool names (as they appear in tool_use.name) that dispatch or hand off work
# rather than complete it inline. A call to one of these opens a workstream
# that needs a later resolution signal before Stop.
DISPATCH_TOOL_RE = re.compile(
    r"^(Agent|Monitor|mcp__ateles__route_task|mcp__ccd_session__spawn_task)$"
)

# Tool names whose RESULT plausibly reports on or resolves a prior dispatch.
# Deliberately broad (any Neotoma retrieve/get, any Agent/Monitor call after
# the first, SendMessage) — false negatives here just mean the gate stays
# quiet, which is the safe direction for a WARN-default nudge.
RESOLUTION_TOOL_RE = re.compile(
    r"^(SendMessage|mcp__ateles__resolve_checkpoint|mcp__ateles__get_dispatch_health)$"
)

OPEN_STATUSES = {
    "awaiting_operator", "awaiting_approval", "pending_review", "pending",
}

WORKSTREAM_ENTITY_TYPES = {"checkpoint_brief", "task"}


def _iter_transcript_rows(transcript_path: str | None):
    if not transcript_path or not os.path.exists(transcript_path):
        return
    try:
        with open(transcript_path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except Exception:
                    continue
    except Exception:
        return


def _tool_uses(row: dict):
    msg = row.get("message") if isinstance(row.get("message"), dict) else row
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                yield block


def _tool_results(row: dict):
    msg = row.get("message") if isinstance(row.get("message"), dict) else row
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                yield block


def _final_assistant_text(transcript_path: str | None) -> str:
    last = ""
    for row in _iter_transcript_rows(transcript_path):
        msg = row.get("message") or {}
        if row.get("type") != "assistant" and msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            last = content
        elif isinstance(content, list):
            parts = [
                b.get("text", "") for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            ]
            if any(parts):
                last = "\n".join(parts)
    return last


def _closing_section(text: str) -> str:
    return text[-2500:] if len(text) > 2500 else text


def scan(transcript_path: str | None) -> dict:
    """Walk the transcript once; return unresolved-dispatch and open-entity
    findings plus a turn count. Fail-open: any read error yields an empty,
    non-blocking summary."""
    result = {"turns": 0, "unresolved_dispatches": [], "open_entities": []}
    dispatch_calls: list[tuple[int, str]] = []  # (row_index, tool_name)
    saw_resolution_after: dict[int, bool] = {}
    open_entities: dict[str, str] = {}  # id-or-title -> label
    idx = 0
    try:
        for row in _iter_transcript_rows(transcript_path):
            role = row.get("role") or row.get("type")
            msg = row.get("message") or {}
            if role in ("user", "assistant") or msg.get("role") in ("user", "assistant"):
                result["turns"] += 1
            for block in _tool_uses(row):
                name = str(block.get("name") or "")
                idx += 1
                if DISPATCH_TOOL_RE.match(name):
                    dispatch_calls.append((idx, name))
                elif RESOLUTION_TOOL_RE.match(name) or DISPATCH_TOOL_RE.match(name):
                    for d_idx, _ in dispatch_calls:
                        if idx > d_idx:
                            saw_resolution_after[d_idx] = True
            for block in _tool_results(row):
                payload = block.get("content")
                blob = payload if isinstance(payload, str) else json.dumps(payload) if payload else ""
                if blob:
                    _collect_open_entities_from_blob(blob, open_entities)
    except Exception:
        return {"turns": 0, "unresolved_dispatches": [], "open_entities": []}

    # A dispatch call is unresolved if no later dispatch/resolution-shaped
    # tool call followed it anywhere in the transcript.
    for d_idx, d_name in dispatch_calls:
        if not saw_resolution_after.get(d_idx):
            result["unresolved_dispatches"].append(d_name)

    result["open_entities"] = sorted(set(open_entities.values()))
    return result


def _collect_open_entities_from_blob(blob: str, out: dict) -> None:
    """Scrape entity_id + status + title triples out of a raw tool_result
    JSON blob. Deliberately string-level rather than a strict schema parse —
    tool_result payloads vary in shape across MCP servers and a hook must
    never raise on an unexpected one."""
    try:
        data = json.loads(blob)
    except Exception:
        return
    _walk_for_open_entities(data, out)


def _walk_for_open_entities(node, out: dict, depth: int = 0) -> None:
    if depth > 6:
        return
    if isinstance(node, dict):
        et = node.get("entity_type")
        status = node.get("status")
        snap = node.get("snapshot") if isinstance(node.get("snapshot"), dict) else None
        if snap:
            et = et or snap.get("entity_type")
            status = status or snap.get("status")
        if isinstance(et, str) and et in WORKSTREAM_ENTITY_TYPES and isinstance(status, str):
            if status in OPEN_STATUSES:
                eid = node.get("entity_id") or ""
                title = (snap or {}).get("title") or node.get("title") or ""
                label = f"{et} {eid} {title}".strip()
                out[eid or title or label] = label
        for v in node.values():
            _walk_for_open_entities(v, out, depth + 1)
    elif isinstance(node, list):
        for item in node:
            _walk_for_open_entities(item, out, depth + 1)


# A resolution-shaped claim in the closing prose: the dispatch is reported as
# checked, finished, or explicitly still pending — as opposed to merely being
# named. Found by probing: "I dispatched an agent to fix the bug. All done"
# mentions the word "agent" (the common noun, not the tool) without saying
# anything happened to it, and a bare substring match on the tool name's
# token wrongly credited that as coverage. Requiring one of these verbs near
# the mention is what tells "I dispatched X" apart from "X finished" / "X is
# still running, checking back next turn."
RESOLUTION_CLAIM_RE = re.compile(
    r"\b(finish\w*|complet\w*|merg\w*|land\w*|verifi\w*|check\w*|confirm\w*|"
    r"report\w*|still (?:running|in progress|waiting|pending)|stalled|"
    r"no response|timed out|follow(?:ed)?[ -]up|resolved|status:)\b",
    re.I,
)


def uncovered_findings(summary: dict, closing_text: str) -> list[str]:
    """Findings the closing section does not cover.

    A dispatch is COVERED only if the closing section both (a) references it
    — by its distinguishing token, e.g. "agent"/"monitor" — AND (b) makes a
    resolution-shaped claim about it (finished, still running, stalled,
    checked). (a) alone is not enough: "I dispatched an agent" mentions the
    word "agent" without saying anything happened to it, which is exactly the
    false-delegation-report failure mode this hook exists to catch. An open
    Neotoma entity is COVERED by (a) alone, since seeing checkpoint/task
    fields naming it IS the surfacing this check wants — there is no
    separate "resolution" tool call to require."""
    out: list[str] = []
    tail_lower = closing_text.lower()
    has_resolution_claim = bool(RESOLUTION_CLAIM_RE.search(closing_text))

    for name in sorted(set(summary.get("unresolved_dispatches", []))):
        token = name.split("__")[-1].lower()
        mentioned = token in tail_lower or name.lower() in tail_lower
        if not mentioned or not has_resolution_claim:
            out.append(
                f"a {name} dispatch has no later resolution or mention in this "
                "turn's closing section — it may have stalled waiting on the "
                "dispatch, or been reported as done without being checked."
            )

    for label in summary.get("open_entities", []):
        words = [w for w in re.split(r"\s+", label) if len(w) > 3]
        mentioned = any(w.lower() in tail_lower for w in words) if words else False
        if not mentioned:
            out.append(
                f"an open workstream this session touched — {label!r} — is not "
                "mentioned in the closing section."
            )

    return out


def main() -> int:
    ev = read_hook_input()
    session_id = ev.get("session_id", "")

    # One nudge per stop, matching the sibling Stop hooks' loop guard.
    if ev.get("stop_hook_active"):
        return 0

    transcript_path = ev.get("transcript_path")
    summary = scan(transcript_path)
    state = load_state(session_id)
    turns = max(int(summary.get("turns", 0)), int(state.get("turn_count", 0)))

    if turns < MIN_TURNS_OPEN:
        return 0

    if not summary.get("unresolved_dispatches") and not summary.get("open_entities"):
        return 0

    closing_text = _closing_section(_final_assistant_text(transcript_path))
    found = uncovered_findings(summary, closing_text)
    if not found:
        return 0

    detail = (
        "Workstream coverage check (session-pickup plan ent_0b3ec4b252ee88a2cd88ab25, "
        "Phase 1):\n- " + "\n- ".join(found) + "\n"
        "Per CLAUDE.md: dispatch, don't drift — either report what a dispatch "
        "produced, re-check a stalled Monitor, or name the open workstream and "
        "what happens to it next. This never requires a consent-gated action "
        "(no send/publish/merge) — mentioning and routing it is enough."
    )

    emit_harness_event_raw(f"workstream-coverage-{session_id}", {
        "event_type": "workstream_coverage_check",
        "session_id": session_id,
        "enforced": ENFORCE,
        "turns": turns,
        "unresolved_dispatch_count": len(summary.get("unresolved_dispatches", [])),
        "open_entity_count": len(summary.get("open_entities", [])),
        "findings": found,
    }, log_tag="workstream-coverage")

    if not ENFORCE:
        sys.stderr.write(f"[workstream-coverage] WARN: {detail}\n")
        return 0

    print(json.dumps({"decision": "block", "reason": detail}))
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
