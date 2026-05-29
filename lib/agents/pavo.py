"""
lib/agents/pavo.py — Product manager / commercial framing skill (T4).

Pavo (*Pavo*, peacock): takes the upstream thread plus Buteo's redline
report and drafts a coherent, on-tone reply for the operator to review.

Pavo does NOT send. The output is a draft + a short escalation note.
Onychomys is responsible for paging the operator with the draft attached.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from lib.agents.buteo import RedlineReport
from lib.claude_client import call_claude
from lib.model_tiers import resolve_model

log = logging.getLogger(__name__)

PAVO_SYSTEM_PROMPT = """You are Pavo, the product-manager / commercial-framing agent in the Ateles swarm.

You take an email thread plus a structured legal review (from Buteo) and draft a single coherent reply for the operator. Match the tone of the existing thread — operator is direct, collaborative, brass-tacks. You DO NOT send the reply. Your output is a draft + an escalation note for the operator.

Output STRICT JSON only. Schema:
{
  "tone_read": "<one sentence on the thread's existing tone>",
  "key_concessions_we_can_offer": ["<list, ordered by lowest cost first>"],
  "non_negotiables": ["<list of items Buteo flagged as high-risk to concede>"],
  "reply_draft": {
    "subject": "<subject line, prefixed Re: if a reply>",
    "body_markdown": "<full reply body, plain prose, no closing signature>"
  },
  "escalation_note": "<short paragraph for Onychomys to surface to operator: what to look at, what trade-offs are live, what to decide>",
  "send_recommendation": "<send_after_review|hold_for_call|escalate_to_counsel>"
}

Rules:
- send_recommendation MUST be hold_for_call or escalate_to_counsel if Buteo flagged any high-risk clause.
- reply_draft.body_markdown should reference the counterparty by first name, address each clause they raised, and propose specific redlines where Buteo provided them.
- Don't invent dollar amounts, percentages, or names not present in the thread or redline report.
"""


@dataclass
class ReplyDraft:
    subject: str = ""
    body_markdown: str = ""


@dataclass
class CommercialFraming:
    tone_read: str = ""
    key_concessions_we_can_offer: list[str] = field(default_factory=list)
    non_negotiables: list[str] = field(default_factory=list)
    reply_draft: ReplyDraft = field(default_factory=ReplyDraft)
    escalation_note: str = ""
    send_recommendation: str = "hold_for_call"
    raw_text: str = ""
    stub: bool = False


def _coerce(result: dict[str, Any] | None) -> CommercialFraming:
    if not result:
        return CommercialFraming(escalation_note="(parse failed)")
    draft_in = result.get("reply_draft") or {}
    draft = ReplyDraft(
        subject=str(draft_in.get("subject", "")),
        body_markdown=str(draft_in.get("body_markdown", "")),
    )
    return CommercialFraming(
        tone_read=str(result.get("tone_read", "")),
        key_concessions_we_can_offer=[str(x) for x in result.get("key_concessions_we_can_offer", []) or []],
        non_negotiables=[str(x) for x in result.get("non_negotiables", []) or []],
        reply_draft=draft,
        escalation_note=str(result.get("escalation_note", "")),
        send_recommendation=str(result.get("send_recommendation", "hold_for_call")).lower(),
    )


def frame(
    *,
    thread_summary: str,
    latest_message: str,
    redline: RedlineReport,
    counterparty_first_name: str = "",
) -> CommercialFraming:
    """Take the thread + Buteo redline and draft a reply for operator review."""
    clause_block = "\n".join(
        f"- [{c.risk_level.upper()} / {c.recommendation}] {c.clause}: "
        f"redline → {c.suggested_redline[:300]}"
        for c in redline.clause_review
    ) or "(no clause-level findings)"

    user_prompt = (
        f"Counterparty first name: {counterparty_first_name or '(unknown)'}\n\n"
        f"Thread summary:\n{thread_summary[:4000]}\n\n"
        f"Counterparty's latest message:\n{latest_message[:6000]}\n\n"
        f"Buteo legal review — headline risk: {redline.headline_risk}\n"
        f"Buteo alignment summary: {redline.alignment_summary}\n"
        f"Buteo clause findings:\n{clause_block}\n"
        f"Buteo next steps: {'; '.join(redline.next_steps) or '(none)'}\n\n"
        "Draft the reply. Respond with JSON only."
    )

    resp = call_claude(
        model=resolve_model("pavo"),
        system=PAVO_SYSTEM_PROMPT,
        user=user_prompt,
        max_tokens=2048,
        temperature=0.3,
    )

    if resp.stub:
        framing = CommercialFraming(
            tone_read="[stub] Collaborative, direct, brass-tacks operator voice.",
            key_concessions_we_can_offer=[
                "Accept Bottega8 ownership of B8-specific operational agents",
                "Drop any contribution-back requirement for B8-internal work",
            ],
            non_negotiables=[
                "Neotoma / Ateles core remains OSS and operator-owned",
                "Reserve right to publish generalized patterns that emerge",
            ],
            reply_draft=ReplyDraft(
                subject="Re: Tech Leaders x Bottega8 x Neotoma — commercial terms",
                body_markdown=(
                    "[stub draft — re-run with ANTHROPIC_API_KEY set for a tone-matched draft. "
                    "Pavo will incorporate Buteo's clause-level redlines and propose a "
                    "three-layer IP framing: (1) Neotoma/Ateles OSS core, (2) Bottega8-owned "
                    "internal operational agents, (3) generalized methods reservation.]"
                ),
            ),
            escalation_note=(
                "[stub] No live API key in this environment. The full pipeline structure is "
                "wired; re-run scripts/run_email_flow.py locally with ANTHROPIC_API_KEY set."
            ),
            send_recommendation="hold_for_call",
            raw_text=resp.text,
            stub=True,
        )
        return framing

    parsed = resp.parse_json()
    out = _coerce(parsed)
    out.raw_text = resp.text
    return out
