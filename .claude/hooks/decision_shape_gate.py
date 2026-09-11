#!/usr/bin/env python3
"""Stop hook: nudge when a turn's closing section breaks a standing rule.

Three rules in `CLAUDE.md` govern how a turn ENDS, and all three were broken
repeatedly on 2026-09-11 — a six-item decision list handed to the operator was
half noise, one item having been approved two turns earlier.

None is enforceable at PreToolUse: the violation is in prose, not in a tool
call's arguments. But a Stop hook sees the finished turn and can block the stop
with a reason, which the harness feeds back as a prompt. That is a corrective
loop rather than a report.

WHAT IT CHECKS, all string-detectable in the assistant's own final message:

1. ASKING INSTEAD OF DOING. The turn ends with a permission question
   ("Want me to", "Should I", "Shall I") while the same turn shows no
   consent-gated action and no AskUserQuestion. `CLAUDE.md`: "Laying out a
   recommendation and asking permission to execute it IS the violation."

2. A DECISION CARRIED BY NAME ALONE. The closing section marks a decision
   "unchanged", or lists a bare issue reference with no options and no
   recommendation. `CLAUDE.md`: never re-raise one by name alone.

3. AN OPERATOR-ONLY ACTION WITH NO COMMAND. The turn says the operator must
   run something and contains no fenced shell block. `CLAUDE.md`: operator-only
   means Mark runs it, not that he works out what to run.

MODE: BLOCK, set by the operator 2026-09-11 via ATELES_DECISION_SHAPE_ENFORCE=1
in .claude/settings.json.

The operator chose BLOCK over WARN knowing the cost, and the reasoning is worth
keeping. A Stop hook fires AFTER the message renders, so blocking does not
retract the turn — it appends a correction beneath it, which the operator sees.
There is no pre-print hook: hooks wrap tool calls and turn boundaries, and text
generation is neither. So the real choice was between a visible self-correction
and an uncorrected turn the operator has to catch themselves. They chose the
former. WARN was rejected because nothing feeds stderr back into the session:
the finding would be written where no one reads it, which is invariant 1 in an
artifact built to enforce standing rules.

BECAUSE A FALSE POSITIVE NOW COSTS THE OPERATOR A VISIBLE INTERRUPTION, the
checks are scoped tighter than a naive match. The consent exemption is judged on
the SENTENCE the question sits in, not the whole tail — matching the tail let an
unrelated mention of "deploy" suppress a real finding. The operator-only check
fires only on an ACTIONABLE line, since explanatory prose ("rotation is
operator-only by design, which is why the fix closes the path instead") is not an
instruction. Both were found by probing, not by reasoning.

Regression probes run before enabling BLOCK: five likely false positives
(narrative "unchanged", explanatory operator-only, the phrase quoted inside an
agent brief, a legitimate consent question, a well-formed turn) all produced ZERO
findings; the real failing list produced all three. Re-run those before widening
any pattern here.

Fail-open (stdlib only; any error exits 0), matching every hook here.

OBSERVABILITY: a `harness_event` is emitted per finding, in BOTH modes, via the
same `emit_harness_event_raw` POST helper the sibling `stop_finalizer.py` hook
uses (in `_session_integrity.py`, imported here rather than re-implemented) —
so both hooks in the same `Stop` array share one HTTP/timeout/error-handling
path instead of two copies that could drift. Without an emission here, flipping
ATELES_DECISION_SHAPE_ENFORCE back to WARN (or unsetting it) makes every
violation this hook finds vanish with no trace: no counter, nothing for a later
audit to check. That is invariant 1 (a mechanism that does not bind is not a
control) one layer down, in the mechanism meant to enforce it — found by
Waxwing on PR 951. This hook's event uses its own vocabulary (`enforced` +
`findings`), not the session-integrity `integral|violated|exempt` enum — the
two checks report genuinely different things and forcing one shape on both
would misrepresent whichever didn't fit. Best-effort and fail-open: no bearer
token or a network error is logged and swallowed, never blocks the hook.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _session_integrity import emit_harness_event_raw  # noqa: E402

ENFORCE = os.environ.get("ATELES_DECISION_SHAPE_ENFORCE", "") in ("1", "true", "yes")

# A question that asks leave to do the thing, rather than asking for a judgement.
PERMISSION_RE = re.compile(
    r"\b(want me to|shall i|should i (?:dispatch|file|open|fix|run|write|add)"
    r"|do you want me to|would you like me to)\b",
    re.I,
)

# Consent genuinely belongs to the operator for these, so a question is correct.
CONSENT_GATED_RE = re.compile(
    r"\b(rotat\w+|launchctl|ateles-rc-src|deploy|merge|send|publish|email|"
    r"credential|approve)\b",
    re.I,
)

UNCHANGED_RE = re.compile(r"—\s*unchanged\b|\bunchanged\.\s*$", re.I | re.M)

OPERATOR_ONLY_RE = re.compile(
    r"\b(operator[- ]only|you (?:must|need to|will need to) run|"
    r"mark (?:must|needs to) run|requires? (?:the )?operator)\b",
    re.I,
)

FENCED_SHELL_RE = re.compile(r"```(?:bash|sh|shell|console)\b")


def last_assistant_text(transcript_path: str | None) -> str:
    """Return the final assistant message's text, or '' if unreadable."""
    if not transcript_path:
        return ""
    p = Path(transcript_path)
    if not p.exists():
        return ""
    last = ""
    try:
        with p.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                msg = row.get("message") or {}
                if row.get("type") != "assistant" and msg.get("role") != "assistant":
                    continue
                content = msg.get("content")
                if isinstance(content, str):
                    last = content
                elif isinstance(content, list):
                    parts = [
                        b.get("text", "")
                        for b in content
                        if isinstance(b, dict) and b.get("type") == "text"
                    ]
                    if any(parts):
                        last = "\n".join(parts)
    except Exception:
        return ""
    return last


def closing_section(text: str) -> str:
    """The tail of the message, where decisions are carried. Bounded."""
    return text[-2500:] if len(text) > 2500 else text


def findings(text: str) -> list[str]:
    out: list[str] = []
    if not text.strip():
        return out
    tail = closing_section(text)

    m = PERMISSION_RE.search(tail)
    # Scope the consent exemption to the SENTENCE the question sits in, not the
    # whole tail. Matching the tail let an unrelated mention of "deploy" three
    # bullets away suppress a real finding — verified on a live example.
    sentence = ""
    if m:
        start = max(tail.rfind("\n", 0, m.start()), tail.rfind(". ", 0, m.start()))
        # Bound the END at a sentence terminator too, not only a newline.
        # Honouring ". " for the start but only "\n" for the end left the very
        # false negative this scoping was added to close: a consent keyword in a
        # LATER sentence on the same line still suppressed a real finding. The
        # terminator that matters most here is "?" — a permission question ends
        # in one by construction, so a fix that only handled "." missed it.
        # Found by Loxia on PR 951, reproduced, and fixed against the repro.
        nxt = re.search(r"[.?!]\s|\n", tail[m.end():])
        end = m.end() + nxt.end() if nxt else len(tail)
        sentence = tail[(start + 1 if start >= 0 else 0):end]
    if m and not CONSENT_GATED_RE.search(sentence):
        out.append(
            f"the turn ends asking permission ({m.group(0)!r}) for something not "
            "consent-gated. CLAUDE.md: proceed with your recommendation and report "
            "what you did. Classify the blocker first — a verified, specced "
            "deliverable is dispatched, never asked about."
        )

    if UNCHANGED_RE.search(tail):
        out.append(
            "a carried decision is written as 'unchanged'. CLAUDE.md: never "
            "re-raise a decision by name alone — restate the choice, what each "
            "option implies, what is settled, and the recommendation."
        )

    # Fire only where the phrase sits in an ACTIONABLE line — one that hands the
    # operator something to do now. Explanatory prose ("rotation is operator-only
    # by design, which is why the fix closes the path instead") is not an
    # instruction, and flagging it interrupts the operator for nothing.
    op_actionable = False
    om = OPERATOR_ONLY_RE.search(tail)
    if om:
        ls = tail.rfind("\n", 0, om.start()) + 1
        le = tail.find("\n", om.end())
        line = tail[ls:(le if le >= 0 else len(tail))]
        explanatory = re.search(
            r"\b(by design|which is why|by rule,? so|rather than|instead of|"
            r"i (?:have |already )?(?:dispatched|filed|fixed|told|briefed))\b",
            line, re.I,
        )
        op_actionable = not explanatory
    if op_actionable and not FENCED_SHELL_RE.search(text):
        out.append(
            "an operator-only action is named with no runnable command block. "
            "CLAUDE.md: operator-only means Mark runs it, not that he works out "
            "what to run — give the exact command and what to verify after."
        )

    return out


def emit_finding_event(session_id: str, found: list[str], enforced: bool) -> None:
    """Record this Stop-hook's finding via the shared harness_event POST
    helper, so a violation leaves a durable trace regardless of WARN/BLOCK
    mode. Own vocabulary (`enforced` + `findings`), not the session-integrity
    `integral|violated|exempt` enum — see the OBSERVABILITY docstring note.
    """
    emit_harness_event_raw(f"decision-shape-{session_id}", {
        "event_type": "decision_shape_check",
        "session_id": session_id,
        "enforced": enforced,
        "findings_count": len(found),
        "findings": found,
    }, log_tag="decision-shape")


def main() -> int:
    try:
        raw = sys.stdin.read()
        ev = json.loads(raw) if raw.strip() else {}
    except Exception:
        return 0

    # One nudge per stop. Never trap a turn that cannot satisfy the check.
    if ev.get("stop_hook_active"):
        return 0

    try:
        text = last_assistant_text(ev.get("transcript_path"))
        found = findings(text)
    except Exception:
        return 0

    if not found:
        return 0

    detail = "Standing-rule check on this turn's closing section:\n- " + "\n- ".join(found)

    # Emit a durable record in BOTH modes — see OBSERVABILITY note above. A
    # finding that only reaches stderr (WARN) or the model's return channel
    # (BLOCK) vanishes once ATELES_DECISION_SHAPE_ENFORCE changes or the
    # session ends; the harness_event is what a later audit can check against.
    try:
        emit_finding_event(ev.get("session_id", ""), found, ENFORCE)
    except Exception:
        pass

    if not ENFORCE:
        sys.stderr.write(f"[decision-shape] WARN: {detail}\n")
        return 0

    print(json.dumps({"decision": "block", "reason": detail}))
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
