#!/usr/bin/env python3
"""Stop hook enforcing the operator-altitude reporting contract.

This starts in BLOCK mode.  A Stop hook cannot retract prose already shown,
but blocking feeds a concrete correction back to the harness before the turn
can close.  Detection is deliberately conservative: it catches repeated
per-tool narration, a final that delegates its meaning to collapsed
commentary, and mechanism-dense finals that omit impact or the next owner.

Both Claude Code JSONL and Codex rollout ``response_item`` rows are accepted.
Unreadable or unrecognised transcripts fail open.
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _session_integrity import read_hook_input  # noqa: E402


ENFORCE = os.environ.get("ATELES_REPORTING_QUALITY_ENFORCE", "1").lower() not in {
    "0", "false", "no",
}

TOOL_NARRATION_RE = re.compile(
    r"\b(?:i(?:['’]ll| will| am going to|['’]m going to)|let me|next i(?:['’]ll| will))\b"
    r"[^.!?\n]{0,100}\b(?:run|inspect|check|read|open|search|grep|query|look up|"
    r"execute|call|fetch|diff|test)\b",
    re.I,
)
COLLAPSED_REFERENCE_RE = re.compile(
    r"\b(?:see|as (?:noted|reported|explained|shown))\s+(?:the\s+)?"
    r"(?:updates?|commentary|above|earlier)\b|\bdetails? (?:are|is) above\b",
    re.I,
)
IMPACT_RE = re.compile(
    r"\b(?:means?|matters?|because|therefore|so that|unblocks?|prevents?|protects?|"
    r"removes?|enables?|keeps?|supports?|parent (?:plan|strategy)|strategy)\b",
    re.I,
)
NEXT_RE = re.compile(
    r"\b(?:next|owner|blocked|remains?|will|review(?:er)?|no (?:action|decision)|"
    r"decision(?:s)? (?:needed|required))\b",
    re.I,
)
MECHANISM_PATTERNS = (
    re.compile(r"(?<!\w)(?:[A-Za-z0-9_.-]+/){2,}[A-Za-z0-9_.-]+"),
    re.compile(r"\b[0-9a-f]{7,40}\b", re.I),
    re.compile(r"\bent_[0-9a-f]{8,}\b", re.I),
    re.compile(r"\b(?:pid|rc|stdout|stderr|cwd)\s*[=:]\s*\S+", re.I),
    re.compile(r"\b\d+\s+(?:tests?|checks?)\s+(?:passed|failed|green)\b", re.I),
    re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\.(?:py|ts|tsx|js|json|toml|yaml|yml|md)\b"),
)
JARGON_RE = re.compile(
    r"\b(?:stdout|stderr|cwd|pid|return code|jsonl|regex|matcher|payload|"
    r"stack trace|subprocess|hook event|exit code)\b",
    re.I,
)


@dataclass(frozen=True)
class Transcript:
    commentary: tuple[str, ...]
    final: tuple[str, ...]


def _text_blocks(message: dict) -> list[str]:
    content = message.get("content")
    if isinstance(content, str):
        return [content] if content.strip() else []
    if not isinstance(content, list):
        return []
    return [
        str(block.get("text", ""))
        for block in content
        if isinstance(block, dict)
        and block.get("type") in {"text", "output_text"}
        and str(block.get("text", "")).strip()
    ]


def _assistant_message(row: dict) -> tuple[str, str] | None:
    """Return ``(phase, text)`` for one Claude or Codex assistant row."""
    if not isinstance(row, dict):
        return None
    if row.get("type") == "response_item" and isinstance(row.get("payload"), dict):
        message = row["payload"]
        if message.get("type") != "message" or message.get("role") != "assistant":
            return None
        text = "\n".join(_text_blocks(message)).strip()
        return (str(message.get("phase") or ""), text) if text else None

    message = row.get("message") if isinstance(row.get("message"), dict) else row
    if row.get("type") != "assistant" and message.get("role") != "assistant":
        return None
    text = "\n".join(_text_blocks(message)).strip()
    return (str(message.get("phase") or row.get("phase") or ""), text) if text else None


def _operator_user_row(row: dict) -> bool:
    """True for a real user prompt, false for tool results and metadata."""
    if not isinstance(row, dict) or row.get("isMeta") or row.get("isCompactSummary"):
        return False
    message = row.get("payload") if row.get("type") == "response_item" else row.get("message")
    if not isinstance(message, dict) or message.get("role") != "user":
        return False
    content = message.get("content")
    if isinstance(content, str):
        return bool(content.strip())
    if not isinstance(content, list):
        return False
    kinds = {block.get("type") for block in content if isinstance(block, dict)}
    return bool(kinds & {"text", "input_text"}) and "tool_result" not in kinds


def read_transcript(path: str | None) -> Transcript:
    if not path:
        return Transcript((), ())
    rows: list[tuple[str, str]] = []
    try:
        with Path(path).open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    continue
                if _operator_user_row(row):
                    rows.clear()
                    continue
                item = _assistant_message(row)
                if item:
                    rows.append(item)
    except OSError:
        return Transcript((), ())

    explicit_final = tuple(text for phase, text in rows if phase == "final_answer")
    explicit_commentary = tuple(text for phase, text in rows if phase == "commentary")
    if explicit_final:
        return Transcript(explicit_commentary, explicit_final)
    # Claude has no phase marker: its final assistant text is the last row and
    # earlier assistant text in the turn is the progress stream.
    texts = tuple(text for _phase, text in rows)
    return Transcript(texts[:-1], texts[-1:] if texts else ())


def _mechanism_count(text: str) -> int:
    return sum(len(pattern.findall(text)) for pattern in MECHANISM_PATTERNS)


def findings(transcript: Transcript) -> list[str]:
    if not transcript.final:
        return []
    found: list[str] = []
    narrated = [text for text in transcript.commentary if TOOL_NARRATION_RE.search(text)]
    if len(narrated) >= 2:
        found.append(
            "repeated per-tool narration: report only material state changes, not each "
            "inspection or command"
        )

    final = "\n".join(transcript.final).strip()
    if COLLAPSED_REFERENCE_RE.search(final) or (
        transcript.commentary and len(final) < 45 and not IMPACT_RE.search(final)
    ):
        found.append(
            "final is not self-contained: restate the outcome, why it matters, and "
            "the next owner/action because commentary may collapse"
        )

    if _mechanism_count(final) >= 4 and not IMPACT_RE.search(final):
        found.append(
            "final is mechanism-dense without explaining why the result matters to "
            "the parent plan or strategy"
        )
    if _mechanism_count(final) >= 4 and not NEXT_RE.search(final):
        found.append("mechanism-dense final omits the next owner/action or settled stop state")
    if len(JARGON_RE.findall(final)) >= 5 and not IMPACT_RE.search(final):
        found.append(
            "final is jargon-dense without translating the result into operator terms"
        )
    return found


def main() -> int:
    event = read_hook_input()
    if event.get("stop_hook_active"):
        return 0
    transcript = read_transcript(event.get("transcript_path"))
    # Codex documents last_assistant_message as the stable Stop-event field;
    # transcript_path is explicitly convenience-only and its wire format may
    # change. Keep transcript parsing for the progress stream, but let the
    # event field own the final whenever the harness supplies it.
    last_message = event.get("last_assistant_message")
    if isinstance(last_message, str) and last_message.strip():
        transcript = Transcript(transcript.commentary, (last_message.strip(),))
    found = findings(transcript)
    if not found:
        return 0
    reason = "Reporting contract violation: " + "; ".join(found) + "."
    if not ENFORCE:
        sys.stderr.write("[report-quality] WARN: " + reason + "\n")
        return 0
    print(json.dumps({"decision": "block", "reason": reason}))
    sys.stderr.write(reason + "\n")
    return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001 -- a broken classifier fails open
        sys.stderr.write(f"[report-quality] error (fail-open): {exc}\n")
        raise SystemExit(0)
