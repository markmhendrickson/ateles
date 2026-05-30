"""
lib/agents/buteo.py — Legal / compliance review skill (T4).

Buteo (*Buteo*, buzzard): takes a contract draft, proposed clauses, or a
counterparty's terms-laden email and produces a structured redline report.

Invoked by Anthus when triage buckets a message as `legal`. Output is
consumed by Pavo (commercial framing) and surfaced to the operator via
Onychomys for sign-off — Buteo never replies on its own.
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass, field
from typing import Any

from lib.claude_client import call_claude
from lib.model_tiers import resolve_model

log = logging.getLogger(__name__)

# Prompt version is hashed from BUTEO_SYSTEM_PROMPT below. Stamped on every
# RedlineReport so re-running the same input with the same prompt + pinned
# model is deterministic and auditable. Bump prompt only via PR review.
BUTEO_PROMPT_VERSION = "2026-05-28.1"

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
class Playbook:
    """Accumulated counterparty / deal-type negotiation memory loaded from
    Neotoma. Buteo reads it as context so prior operator decisions are
    pre-baked into the first-pass redline — addresses the "first pass too
    conservative + complex" failure mode by anchoring on positions we have
    already settled."""

    entity_id: str = ""
    name: str = ""
    counterparty: str = ""
    deal_type: str = ""
    version: str = ""
    summary: str = ""
    standard_positions: list[dict[str, Any]] = field(default_factory=list)
    accepted_redlines: list[dict[str, Any]] = field(default_factory=list)
    rejected_positions: list[dict[str, Any]] = field(default_factory=list)
    non_negotiables: list[dict[str, Any]] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (
            self.standard_positions
            or self.accepted_redlines
            or self.rejected_positions
            or self.non_negotiables
        )

    def to_prompt_block(self) -> str:
        if self.is_empty:
            return ""
        lines = [
            f"## Playbook — {self.name} (version {self.version or 'unversioned'})",
            f"Counterparty: {self.counterparty or '(unspecified)'}",
            f"Deal type: {self.deal_type or '(unspecified)'}",
        ]
        if self.summary:
            lines.append(f"\nSummary: {self.summary}")
        if self.standard_positions:
            lines.append("\nStandard positions (anchor on these — do not re-litigate):")
            for p in self.standard_positions:
                lines.append(f"- [{p.get('topic', '')}] {p.get('our_position', '')}  ({p.get('rationale', '')})")
        if self.non_negotiables:
            lines.append("\nNon-negotiables (reject any clause attempting to weaken these):")
            for p in self.non_negotiables:
                lines.append(f"- [{p.get('topic', '')}] {p.get('line', '')}")
        if self.accepted_redlines:
            lines.append("\nPreviously accepted redlines (operator approved — reuse the language):")
            for p in self.accepted_redlines:
                lines.append(f"- [{p.get('clause_type', '')}] {p.get('accepted_text', '')[:300]}")
        if self.rejected_positions:
            lines.append("\nPreviously rejected counterparty positions (do not concede now):")
            for p in self.rejected_positions:
                lines.append(
                    f"- [{p.get('topic', '')}] them: {p.get('counterparty_position', '')[:200]} "
                    f"— why rejected: {p.get('why_rejected', '')[:200]}"
                )
        return "\n".join(lines)


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
    # Provenance — every report stamps how it was produced so an operator
    # diffing two runs of the same input knows exactly what changed.
    prompt_version: str = ""
    model_id: str = ""
    playbook_id: str = ""


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


def load_playbook(
    *, counterparty: str = "", deal_type: str = "", name: str = ""
) -> Playbook:
    """Look up a playbook entity in Neotoma by counterparty / deal_type / name.

    Returns an empty Playbook if Neotoma is unreachable, no token, or no
    match — Buteo runs without playbook context in that case.
    """
    base = os.environ.get("NEOTOMA_BASE_URL", "https://neotoma.markmhendrickson.com").rstrip("/")
    token = os.environ.get("NEOTOMA_BEARER_TOKEN", "")
    if not token:
        return Playbook()
    identifier = name or counterparty or deal_type
    if not identifier:
        return Playbook()
    try:
        import httpx

        with httpx.Client(
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            timeout=5.0,
        ) as client:
            resp = client.get(
                f"{base}/entities/by-identifier",
                params={"entity_type": "playbook", "identifier": identifier},
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        log.debug("buteo: playbook lookup failed for %s: %s", identifier, exc)
        return Playbook()
    entities = data.get("entities") or []
    if not entities:
        return Playbook()
    ent = entities[0]
    snap = ent.get("snapshot", {}) or {}
    inner = snap.get("snapshot", snap)
    return Playbook(
        entity_id=ent.get("id", ""),
        name=str(inner.get("name", "")),
        counterparty=str(inner.get("counterparty", "")),
        deal_type=str(inner.get("deal_type", "")),
        version=str(inner.get("version", "")),
        summary=str(inner.get("summary", "")),
        standard_positions=inner.get("standard_positions") or [],
        accepted_redlines=inner.get("accepted_redlines") or [],
        rejected_positions=inner.get("rejected_positions") or [],
        non_negotiables=inner.get("non_negotiables") or [],
    )


def review(
    *,
    thread_summary: str,
    latest_message: str,
    prior_positions: str = "",
    playbook: Playbook | None = None,
) -> RedlineReport:
    """Run a legal-grade clause review.

    Model is resolved through `resolve_model("buteo")`, which honours the
    `model_pin` field on Buteo's agent_definition. The combination of
    pinned model + frozen prompt version + temperature 0 is what makes a
    Buteo run deterministic per the design rationale (drift-fighting).
    """
    playbook = playbook or Playbook()
    playbook_block = playbook.to_prompt_block()
    user_prompt = (
        f"Thread summary (oldest → newest, paraphrased):\n{thread_summary[:6000]}\n\n"
        f"Counterparty's latest message (full text):\n{latest_message[:10000]}\n\n"
        + (
            f"Our prior positions / commitments from the thread:\n{prior_positions[:4000]}\n\n"
            if prior_positions
            else ""
        )
        + (f"{playbook_block}\n\n" if playbook_block else "")
        + "Produce a clause-by-clause review. Respond with JSON only."
    )

    model_id = resolve_model("buteo")
    resp = call_claude(
        model=model_id,
        system=BUTEO_SYSTEM_PROMPT,
        user=user_prompt,
        max_tokens=4096,
        temperature=0.0,
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
            prompt_version=BUTEO_PROMPT_VERSION,
            model_id=model_id,
            playbook_id=playbook.entity_id,
        )
        return report

    parsed = resp.parse_json()
    out = _coerce(parsed)
    out.raw_text = resp.text
    out.prompt_version = BUTEO_PROMPT_VERSION
    out.model_id = model_id
    out.playbook_id = playbook.entity_id
    return out


def prompt_hash() -> str:
    """SHA-256 of the active system prompt — pin this in eval suites."""
    return hashlib.sha256(BUTEO_SYSTEM_PROMPT.encode()).hexdigest()
