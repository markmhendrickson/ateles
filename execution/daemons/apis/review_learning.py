"""
execution/daemons/apis/review_learning.py — review → skill learning loop.

Implements ateles#82: recurring PR-review findings get folded back into the
owning agent's `agent_definition` so the swarm stops repeating mistakes.

Flow (wired in swarm_dispatch after the review panel completes):
  1. parse_findings() pulls structured `[BLOCKING] <category>` blocks out of
     each panelist's review output.
  2. classify_finding() separates systemic findings (a class the owning agent
     should never repeat — heuristic: cites a standing rule doc or guardrail)
     from one-off bugs (fixed in the PR; no skill change).
  3. propose_skill_updates() turns systemic findings into operator-gated
     `proposed_skill_update` entity payloads with provenance back to the
     review. Nothing auto-mutates agent behavior: the operator approves the
     proposal, then the `learn`/`neotoma-learn` skill applies it via a
     Neotoma correction to the agent_definition's prompt_markdown (never by
     editing SKILL.md directly).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

log = logging.getLogger("apis.review_learning")

# A finding that cites a standing rule is almost always systemic — the rule
# already generalizes beyond this PR (heuristic from ateles#82).
SYSTEMIC_CITATION_PATTERNS = (
    r"\.mdc\b",
    r"\bchange_guardrails\b",
    r"\badvisor(y|ies)\b",
    r"\bstanding rule\b",
    r"\bguardrails?\b",
)

# Review block header: "[BLOCKING] tenant-isolation: ..." (panel reviews and
# the Claude GHA both emit this shape).
_FINDING_HEADER = re.compile(
    r"^\[(?P<severity>BLOCKING|NON-BLOCKING)\]\s*(?P<category>[^:\n]+?)\s*:\s*(?P<summary>.*)$",
    re.MULTILINE,
)

# Default systemic-finding owner by review lens. The implementer (Gryllus)
# owns correctness-class lessons; content-class lessons route to Corvus.
OWNER_BY_LENS = {
    "content": "corvus",
}
DEFAULT_OWNER = "gryllus"


@dataclass
class ReviewFinding:
    category: str
    summary: str
    detail: str
    blocking: bool
    lens: str = ""
    files: list[str] = field(default_factory=list)
    rule_citations: list[str] = field(default_factory=list)


def parse_findings(review_text: str, lens: str = "") -> list[ReviewFinding]:
    """Extract structured findings from a review's output text."""
    findings: list[ReviewFinding] = []
    matches = list(_FINDING_HEADER.finditer(review_text))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(review_text)
        detail = review_text[m.end() : end].strip()
        block = m.group(0) + "\n" + detail
        findings.append(
            ReviewFinding(
                category=m.group("category").strip().lower().replace(" ", "-"),
                summary=m.group("summary").strip(),
                detail=detail,
                blocking=m.group("severity") == "BLOCKING",
                lens=lens,
                files=re.findall(r"`([\w./-]+\.\w{1,5})`", block),
                rule_citations=[
                    p for p in SYSTEMIC_CITATION_PATTERNS if re.search(p, block, re.I)
                ],
            )
        )
    return findings


def classify_finding(
    finding: ReviewFinding, category_recurrence: int = 0, recurrence_threshold: int = 2
) -> str:
    """
    "systemic" — a class the owning agent should never repeat (promote to a
    standing rule in its agent_definition); "one_off" — fix in the PR only.
    """
    if finding.rule_citations:
        return "systemic"
    if category_recurrence >= recurrence_threshold:
        return "systemic"
    return "one_off"


def propose_skill_updates(
    reviews: list[tuple[str, str]],
    pr_ref: str,
    category_history: dict[str, int] | None = None,
) -> list[dict]:
    """
    Turn the panel's reviews into operator-gated proposed_skill_update entity
    payloads (one per systemic category, deduped across lenses).

    Args:
        reviews: (lens, review_output_text) per panelist
        pr_ref:  e.g. "markmhendrickson/neotoma#1637"
        category_history: prior occurrence counts per category across PRs,
            for the recurrence-threshold heuristic (optional)
    """
    history = dict(category_history or {})
    proposals: dict[str, dict] = {}

    for lens, text in reviews:
        for finding in parse_findings(text, lens=lens):
            if not finding.blocking:
                continue
            seen_before = history.get(finding.category, 0)
            history[finding.category] = seen_before + 1
            if classify_finding(finding, category_recurrence=seen_before) != "systemic":
                continue
            if finding.category in proposals:
                continue  # first lens to surface a category wins

            owner = OWNER_BY_LENS.get(lens, DEFAULT_OWNER)
            proposals[finding.category] = {
                "entity_type": "proposed_skill_update",
                "title": f"Learn from {pr_ref}: {finding.category}",
                "owning_agent": owner,
                "finding_category": finding.category,
                "proposed_rule": (
                    f"{finding.summary} (generalized from review finding on "
                    f"{pr_ref}; apply on every comparable change)"
                ),
                "source_review_lens": lens,
                "source_pr": pr_ref,
                "finding_detail": finding.detail[:2000],
                "status": "proposed",
                "approval_required": True,
            }

    out = list(proposals.values())
    if out:
        log.info(
            f"[apis] learning loop: {len(out)} systemic finding(s) from {pr_ref} → "
            f"proposed_skill_update for "
            f"{sorted({p['owning_agent'] for p in out})}"
        )
    return out
