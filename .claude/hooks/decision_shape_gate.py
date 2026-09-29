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

4. A DECISION POSED AS PROSE. The closing section presents the operator a
   choice between options (two or more option markers, or a decision heading,
   together with a recommendation or a stated default if unanswered), while
   the turn made no AskUserQuestion call and carries no `[decisions-unposed]`
   marker. agent_policy ent_985436c69e2170aeba3287de: every open decision goes
   through the harness questions tool; a prose list is allowed only through
   the explicit `[decisions-unposed]` fallback when the tool is unavailable.
   On 2026-09-28/29 a session presented decisions as a numbered prose list for
   about ten consecutive turns with the tool available, and checks 1-3 said
   nothing, because each item was well-formed prose. Operator-only ACTIONS
   remain prose plus a runnable command block; they are not choices, so a
   command block alone never fires this check.

   Known limits of check 4, so they are not read as coverage:
   - Any AskUserQuestion call in the turn exempts every later prose decision
     in that turn. Notification-triggered rows do not start a turn, so a call
     in the last operator-prompted turn also exempts the notification turns
     after it.
   - Numbered lists are not option markers, so a "1. ... I recommend yes"
     list with no letters and no decision cue does not fire.

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
from _session_integrity import emit_harness_event_raw, read_hook_input  # noqa: E402

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

# Check 4 — a choice between options handed to the operator in prose.
#
# An option marker is a line-leading "(a)", "a)", "A.", "Option A:", or an
# inline "(a) ... (b)" pair. Letters only, and only a-f: numbered lists are
# how every status report is written, so "1." is not an option marker — the
# numbered list in the failing pattern is caught through its per-item
# "(a)/(b)" options and recommendation instead.
OPTION_MARKER_RE = re.compile(
    r"(?:^|\n)\s*(?:[-*]\s+)?(?:\*\*)?(?:option\s+[a-f1-9]\b|\(?[a-f]\)|[a-f][.:]\s)"
    r"|\(\s*[a-f]\s*\)",
    re.I,
)
# The choice is being put to the operator, not narrated.
DECISION_CUE_RE = re.compile(
    r"\b(decisions? (?:for|that need|needing|waiting on|awaiting) (?:you|mark|the operator)"
    r"|(?:open|pending) decisions?|your (?:call|decision)|needs? your (?:decision|call)"
    r"|decide (?:between|whether|which))\b",
    re.I,
)
# A recommendation or a default-if-unanswered: the shape of a posed decision.
RECOMMEND_CUE_RE = re.compile(
    r"\b(i recommend|i'd recommend|i would recommend|my recommendation|recommendation:"
    r"|recommended:|recommend (?:option\s+)?\(?[a-f]\b"
    r"|if you (?:don't|do not) (?:answer|reply|respond|decide)|if you say nothing"
    r"|default if unanswered|if unanswered|absent an answer)",
    re.I,
)
# Words that, just before a decision cue on the same line, empty it.
NEGATED_CUE_RE = re.compile(r"\b(?:no|zero|nothing|none|without)\b[^.\n?!]{0,20}$", re.I)
# An empty decisions section can also put the negation AFTER the cue:
# "Open decisions: none." (qa lens, PR 1344).
NEGATED_AFTER_CUE_RE = re.compile(r"^\s*[:\u2014-]?\s*(?:none|n/a|nothing)\b", re.I)
UNPOSED_MARKER = "[decisions-unposed]"
QUESTION_TOOL_NAME = "AskUserQuestion"


def poses_decision_in_prose(tail: str) -> bool:
    """True when the (quote-stripped) closing section presents a choice for
    the operator: a recommendation/default cue together with either two or
    more option markers or an explicit decision cue.

    A recommendation alone is not a choice ("I recommend we keep watching"),
    and options alone are narration ("Mark chose (a) yesterday"); both halves
    are required, which is what keeps a past decision's narrative mention and
    a status report's advice out of scope.
    """
    if not RECOMMEND_CUE_RE.search(tail):
        return False
    if len(OPTION_MARKER_RE.findall(tail)) >= 2:
        return True
    # A decision cue counts only when it is not negated: "No open decisions"
    # and "Nothing needs your decision" are the standard empty decisions
    # section, and a recommendation elsewhere in the tail must not turn them
    # into a finding.
    for m in DECISION_CUE_RE.finditer(tail):
        before = tail[max(0, m.start() - 25):m.start()]
        after = tail[m.end():m.end() + 25]
        if not NEGATED_CUE_RE.search(before) and not NEGATED_AFTER_CUE_RE.search(after):
            return True
    return False

# ONE definition of "a sentence ends here", used for BOTH ends of the scoping
# window in `findings()`. The two ends were written as two separate
# expressions, and they drifted: the END was widened to `[.?!]\s|\n` to close
# a false negative (a permission question ends in "?" by construction, so the
# next sentence's consent keyword leaked in), while the START was left at only
# `\n` and ". ". The mirror-image false negative followed — when the PREVIOUS
# sentence ended in "?" or "!", `rfind` found no boundary there and ran back
# further, pulling that sentence in, so a consent keyword in it suppressed a
# genuine finding. Two copies of one concept is the defect; a shared definition
# is what stops it recurring (principles.md, "one source, defined once").
SENTENCE_BOUNDARY_RE = re.compile(r"[.?!]\s|\n")


def sentence_around(text: str, start: int, end: int) -> str:
    """The sentence containing text[start:end], both ends bounded by the SAME
    terminator set (`.`/`?`/`!` followed by whitespace, or a newline).

    Scans forward from the beginning rather than using `rfind`, because the
    start boundary is now a pattern rather than a pair of fixed substrings.
    """
    left = 0
    for mb in SENTENCE_BOUNDARY_RE.finditer(text, 0, start):
        left = mb.end()
    nxt = SENTENCE_BOUNDARY_RE.search(text, end)
    right = nxt.end() if nxt else len(text)
    return text[left:right]


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


def _is_operator_prompt(row: dict) -> bool:
    """A user row that is a real prompt, not a tool_result the harness files
    under role "user". The turn starts at the last such row."""
    msg = row.get("message") or {}
    if row.get("type") != "user" and msg.get("role") != "user":
        return False
    # Harness-injected rows (a skill's "Base directory for this skill", an
    # image caption, a message from another session) are marked isMeta; a
    # background-task notification is not the operator speaking either. None
    # starts a turn, or a skill loaded after an AskUserQuestion call would
    # erase it and the gate would block a turn that followed the rule.
    if row.get("isMeta"):
        return False
    # A compaction summary is filed as a user row too; it is the harness
    # restating context, not the operator starting a turn (qa lens, PR 1344).
    if row.get("isCompactSummary"):
        return False
    content = msg.get("content")
    if isinstance(content, str):
        return bool(content.strip()) and not content.lstrip().startswith(
            "<task-notification>"
        )
    if isinstance(content, list):
        kinds = {b.get("type") for b in content if isinstance(b, dict)}
        if "tool_result" in kinds or not kinds:
            return False
        first = next(
            (b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"),
            "",
        )
        return not str(first).lstrip().startswith("<task-notification>")
    return False


def _is_question_tool(name: str) -> bool:
    """The built-in tool, or an MCP-namespaced copy of it (`mcp__x__...`).
    Not any name that merely ends in the string (security lens, PR 1344)."""
    return name == QUESTION_TOOL_NAME or name.endswith("__" + QUESTION_TOOL_NAME)


def turn_used_question_tool(transcript_path: str | None) -> bool:
    """True when the current turn — everything after the last operator
    prompt — contains an AskUserQuestion tool_use.

    Unreadable or absent transcripts return True: the check this feeds must
    fail OPEN, and "cannot tell whether the tool was used" is not evidence
    that it was not.
    """
    if not transcript_path:
        return True
    p = Path(transcript_path)
    if not p.exists():
        return True
    used = False
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
                if not isinstance(row, dict):
                    continue
                if _is_operator_prompt(row):
                    used = False  # a new turn starts; earlier calls don't count
                    continue
                content = (row.get("message") or {}).get("content")
                if not isinstance(content, list):
                    continue
                for b in content:
                    if (
                        isinstance(b, dict)
                        and b.get("type") == "tool_use"
                        and _is_question_tool(str(b.get("name", "")))
                    ):
                        used = True
    except Exception:
        return True
    return used


# Contexts that QUOTE prose rather than assert it. ateles#1105: both observed
# false positives fired on text DOCUMENTING the rule being enforced — an
# evidence table quoting what the gate had blocked, and an issue body quoting
# the standing rule. Stripping these before matching is the analogue of
# `gmail_send_gate.py`'s text-bearing-leader exemption: the same principle
# (a mention is not an invocation) applied to prose instead of commands.
#
# Order matters. Fenced blocks are removed FIRST so a pipe character inside a
# fence cannot be mistaken for a table row.
FENCED_ANY_RE = re.compile(r"```.*?```", re.S)
INLINE_CODE_RE = re.compile(r"`[^`\n]+`")
TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$", re.M)
# A phrase in quotation marks is being NAMED, not asserted — the commonest way
# to write a rule down ("do not end with \"Want me to\""). Bounded to one line
# and to a short span so an unterminated quote cannot swallow the turn's real
# ending: a permission question is short, and a span this size cannot hide a
# whole closing paragraph. Straight and curly pairs both, since prose uses both.
QUOTED_PHRASE_RE = re.compile(r"[\"\u201c][^\"\u201c\u201d\n]{1,120}[\"\u201d]")
BLOCKQUOTE_RE = re.compile(r"^\s*>.*$", re.M)


def strip_quoted(text: str) -> str:
    """Remove contexts that quote prose rather than assert it.

    Replaces each with a newline rather than deleting it, so sentence
    boundaries around the removed span are preserved — `sentence_around`
    scopes the consent exemption and must not have two sentences fused into
    one by the strip.
    """
    for pattern in (
        FENCED_ANY_RE,
        TABLE_ROW_RE,
        BLOCKQUOTE_RE,
        INLINE_CODE_RE,
        QUOTED_PHRASE_RE,
    ):
        text = pattern.sub("\n", text)
    return text


# How much of the message counts as "the closing section". The docstring has
# always stated the check as "the turn ENDS with a permission question", but
# this returned the last 2500 characters, so a phrase quoted 2000 characters
# back matched as though the turn ended with it (ateles#1105).
#
# The final BLOCK is the honest reading: everything after the last blank line,
# which is the paragraph, bullet or table the turn actually ends on. The 2500
# character bound is kept as a backstop for a turn written as one long block,
# since a message with no blank line at all would otherwise be scanned whole.
CLOSING_BLOCK_MAX = 2500

# How many trailing blocks count as "the closing section". ONE was too narrow
# (Loxia, PR #1175): a genuine permission question followed by a footer
# paragraph or a bullet list escaped entirely — and a closing decisions
# section followed by bullets is the house style, so that false NEGATIVE was
# more likely than the false positive being fixed. Three blocks covers a
# question plus a short footer while staying far short of the old
# whole-2500-character window that matched quotations paragraphs back.
CLOSING_BLOCKS = 3


def closing_section(text: str) -> str:
    """The tail of the message, where decisions are carried. Bounded.

    Quoted contexts are stripped first, then the final block is taken. A
    turn whose last block is entirely quotation therefore falls back to the
    preceding prose block, which is the text the turn genuinely ends on.
    """
    stripped = strip_quoted(text).rstrip()
    blocks = [b for b in re.split(r"\n\s*\n", stripped) if b.strip()]
    tail = "\n\n".join(blocks[-CLOSING_BLOCKS:]) if blocks else ""
    return tail[-CLOSING_BLOCK_MAX:] if len(tail) > CLOSING_BLOCK_MAX else tail


def findings(text: str, asked_via_tool: bool = True) -> list[str]:
    """Standing-rule findings on the turn's closing section.

    `asked_via_tool` is whether this turn called AskUserQuestion. It defaults
    to True so a caller that has no transcript to consult (and every
    text-only probe) cannot trip check 4 by omission; `main()` passes the
    value read from the transcript.
    """
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
        # BOTH ends come from `sentence_around`, so they cannot drift apart
        # again. The END was widened first (Loxia, PR 951): a consent keyword
        # in a LATER sentence on the same line suppressed a real finding, and
        # "?" is the terminator that matters most since a permission question
        # ends in one by construction. The START was left narrow and carried
        # the mirror-image defect (qa lens, PR 951, reproduced by execution):
        # a prior sentence ending in "?" or "!" was pulled in whole, so its
        # consent keyword suppressed a genuine finding.
        sentence = sentence_around(tail, m.start(), m.end())
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
    #
    # This scan is deliberately LINE-scoped, not sentence-scoped, and so does
    # not share `sentence_around`: both of its boundaries are "\n" already, so
    # they are symmetric and cannot drift the way the permission scan's did. An
    # actionable instruction and the prose qualifying it ("... rather than
    # waiting for me") routinely sit in one sentence, so narrowing this to a
    # sentence would break the explanatory exemption above rather than fix a bug.
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

    # Check 4. The marker is looked for in the RAW text, since it is often
    # written inside backticks, which `closing_section` strips.
    if (
        not asked_via_tool
        and UNPOSED_MARKER not in text
        and poses_decision_in_prose(tail)
    ):
        out.append(
            "an operator decision is posed as prose, with no AskUserQuestion call "
            "this turn. agent_policy ent_985436c69e2170aeba3287de: pose every open "
            "decision through the questions tool (options, what each implies, "
            "what is settled, a recommendation). Only if the tool is unavailable, "
            "print [decisions-unposed] and each question's full text. Operator-only "
            "actions stay prose plus a runnable command block. If the lettered "
            "items are sequential steps rather than alternatives, nothing needs "
            "asking: carry them out and say so."
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
    }, log_tag="decision-shape", session_id=session_id)


def main() -> int:
    # Shared stdin-parsing helper (also fail-opens to {} on non-dict JSON —
    # see its docstring) rather than a local copy, matching every other hook
    # in this directory.
    ev = read_hook_input()

    # One nudge per stop. Never trap a turn that cannot satisfy the check.
    if ev.get("stop_hook_active"):
        return 0

    try:
        transcript = ev.get("transcript_path")
        text = last_assistant_text(transcript)
        found = findings(text, asked_via_tool=turn_used_question_tool(transcript))
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
