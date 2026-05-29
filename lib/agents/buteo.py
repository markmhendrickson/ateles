"""
lib/agents/buteo.py — Legal / compliance review skill (T4).

Buteo (*Buteo*, buzzard): takes a contract draft, proposed clauses, or a
counterparty's terms-laden email and produces a structured redline report.

Invoked by Anthus when triage buckets a message as `legal`. Output is
consumed by Pavo (commercial framing) and surfaced to the operator via
Onychomys for sign-off — Buteo never replies on its own.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from lib.claude_client import call_claude
from lib.model_tiers import resolve_model

log = logging.getLogger(__name__)

BUTEO_SYSTEM_PROMPT = """You are Buteo, the legal / compliance review agent in the Ateles swarm.

You read a counterparty's proposed terms (typically embedded in an email thread) and produce a clause-by-clause review for the operator. You are NOT a lawyer; you produce a structured technical-legal review that a lawyer or the operator can act on. Always recommend operator sign-off before any reply ships.

Focus areas:
- IP ownership boundaries (what is OSS, what is work-for-hire, what is retained)
- Scope of work and side-memo / SOW attribution mechanics
- Revenue / sourcing fees and how they are computed and triggered
- Carve-outs and reservations (data, prompts, evals, integrations, methods)
- Termination, modification, fork rights, contribution-back obligations
- Indemnity, liability, confidentiality, non-compete, non-solicit
- Jurisdiction, governing law, dispute resolution
- Anything that creates a perpetual dependency or implicit licensing tax

Output STRICT JSON only. Schema:
{
  "headline_risk": "<one-sentence summary of the largest unresolved issue>",
  "alignment_summary": "<one paragraph: what the counterparty proposed, what we already agreed, where we diverge>",
  "clause_review": [
    {
      "clause": "<short label, e.g. 'IP ownership of B8 internal agents'>",
      "counterparty_position": "<their text or paraphrase>",
      "our_position": "<our stance from prior thread or sensible default>",
      "risk_level": "<low|medium|high>",
      "recommendation": "<accept|accept_with_edit|push_back|reject>",
      "suggested_redline": "<replacement clause text we'd propose>",
      "rationale": "<why this redline protects our position without breaking the deal>"
    }
  ],
  "open_questions_for_operator": ["<short questions only the operator can answer>"],
  "next_steps": ["<ordered actions, e.g. 'redline Section B.1', 'request counsel review on indemnity'>"],
  "operator_signoff_required": true
}

Rules:
- operator_signoff_required is ALWAYS true.
- Keep clause_review focused on the contested clauses — do not list every line.
- suggested_redline should be drop-in replacement language, not commentary.
- If the email isn't actually a contract negotiation, return a minimal object with headline_risk="not_a_contract" and empty clause_review.
"""


@dataclass
class ClauseReview:
    clause: str = ""
    counterparty_position: str = ""
    our_position: str = ""
    risk_level: str = "medium"
    recommendation: str = "push_back"
    suggested_redline: str = ""
    rationale: str = ""


@dataclass
class RedlineReport:
    headline_risk: str = ""
    alignment_summary: str = ""
    clause_review: list[ClauseReview] = field(default_factory=list)
    open_questions_for_operator: list[str] = field(default_factory=list)
    next_steps: list[str] = field(default_factory=list)
    operator_signoff_required: bool = True
    raw_text: str = ""
    stub: bool = False


def _coerce(result: dict[str, Any] | None) -> RedlineReport:
    if not result:
        return RedlineReport(headline_risk="(parse failed)")
    clauses_in = result.get("clause_review") or []
    clauses: list[ClauseReview] = []
    for c in clauses_in:
        if not isinstance(c, dict):
            continue
        clauses.append(
            ClauseReview(
                clause=str(c.get("clause", "")),
                counterparty_position=str(c.get("counterparty_position", "")),
                our_position=str(c.get("our_position", "")),
                risk_level=str(c.get("risk_level", "medium")).lower(),
                recommendation=str(c.get("recommendation", "push_back")).lower(),
                suggested_redline=str(c.get("suggested_redline", "")),
                rationale=str(c.get("rationale", "")),
            )
        )
    return RedlineReport(
        headline_risk=str(result.get("headline_risk", "")),
        alignment_summary=str(result.get("alignment_summary", "")),
        clause_review=clauses,
        open_questions_for_operator=[str(q) for q in result.get("open_questions_for_operator", []) or []],
        next_steps=[str(s) for s in result.get("next_steps", []) or []],
        operator_signoff_required=True,
    )


def review(
    *,
    thread_summary: str,
    latest_message: str,
    prior_positions: str = "",
) -> RedlineReport:
    """Run a legal-grade clause review with Claude Opus 4.7."""
    user_prompt = (
        f"Thread summary (oldest → newest, paraphrased):\n{thread_summary[:6000]}\n\n"
        f"Counterparty's latest message (full text):\n{latest_message[:10000]}\n\n"
        + (
            f"Our prior positions / commitments from the thread:\n{prior_positions[:4000]}\n\n"
            if prior_positions
            else ""
        )
        + "Produce a clause-by-clause review. Respond with JSON only."
    )

    resp = call_claude(
        model=resolve_model("buteo"),
        system=BUTEO_SYSTEM_PROMPT,
        user=user_prompt,
        max_tokens=4096,
        temperature=0.1,
    )

    if resp.stub:
        report = RedlineReport(
            headline_risk="[stub] Counterparty IP-carveout proposal needs negotiated boundary.",
            alignment_summary=(
                "[stub] Without ANTHROPIC_API_KEY this report is a placeholder. "
                "Re-run locally with the key set to get full clause-by-clause analysis."
            ),
            clause_review=[],
            open_questions_for_operator=[
                "What is the minimum IP carve-out we can accept while protecting Neotoma/Ateles upstream?",
                "Do we want to require Bottega8-built workflow patterns to remain MIT-licensable upstream as an option?",
            ],
            next_steps=[
                "Run with ANTHROPIC_API_KEY set to produce full Buteo review",
                "Cross-check with counsel before reply",
            ],
            raw_text=resp.text,
            stub=True,
        )
        return report

    parsed = resp.parse_json()
    out = _coerce(parsed)
    out.raw_text = resp.text
    return out
