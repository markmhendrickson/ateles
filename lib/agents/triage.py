"""
lib/agents/triage.py — Turdus's Claude-based email classifier (Phase 7).

Replaces the keyword-only `_classify_message` in turdus.py with an LLM call
that buckets a message and selects the downstream agent. The output is a
ClassificationResult dataclass that Anthus's dispatcher consumes.

Buckets and their target agents:
    legal        → buteo  (contracts, T&Cs, IP terms, NDAs)
    commercial   → pavo   (deal terms, pricing, partnership scoping)
    code         → gryllus (PR review requests, code questions, GH activity)
    scheduling   → onychomys (calendar invites, time requests)
    personal     → onychomys (operator-only, no agent action)
    notification → none (informational only, archive)
    noise        → none (spam, promotional, drop)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from lib.claude_client import MODEL_HAIKU, call_claude

log = logging.getLogger(__name__)

VALID_BUCKETS = {
    "legal",
    "commercial",
    "code",
    "scheduling",
    "personal",
    "notification",
    "noise",
}

BUCKET_TO_AGENT = {
    "legal": "buteo",
    "commercial": "pavo",
    "code": "gryllus",
    "scheduling": "onychomys",
    "personal": "onychomys",
    "notification": None,
    "noise": None,
}

TRIAGE_SYSTEM_PROMPT = """You are Turdus, the email triage agent in the Ateles swarm.

Your job is to classify a single email into ONE bucket and select the downstream agent.

Buckets:
- legal: contracts, NDAs, MNDAs, T&Cs, IP terms, licensing, legal redlines, partnership agreements, equity/cap-table docs
- commercial: deal terms, pricing, partnership scoping, sourcing fees, revenue share, GTM negotiation (without legal redlines)
- code: PR reviews requested, code/repo questions, GitHub notifications that require action
- scheduling: calendar invites, time-finding, reschedules
- personal: operator-only correspondence with no agent action needed
- notification: informational receipts, status updates, no reply needed
- noise: spam, promotional, automated marketing, unsubscribe-bait

Output STRICT JSON only (no prose, no markdown fences). Schema:
{
  "bucket": "<one of the buckets>",
  "target_agent": "<buteo|pavo|gryllus|onychomys|null>",
  "priority": "<critical|blocker|operator_decision|info>",
  "requires_operator": <true|false>,
  "summary": "<one sentence, <= 200 chars>",
  "rationale": "<one short clause explaining the bucket choice>"
}

Rules:
- requires_operator MUST be true for any bucket in {legal, commercial} touching commercial terms, IP, or money.
- priority "critical" only for deadline-today items; "blocker" for explicit asks needing same-day reply.
- If the email is a long thread, classify based on the LATEST message in context of prior turns.
"""


@dataclass
class ClassificationResult:
    bucket: str = "notification"
    target_agent: str | None = None
    priority: str = "info"
    requires_operator: bool = False
    summary: str = ""
    rationale: str = ""
    raw_text: str = ""
    stub: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


def _coerce(result: dict[str, Any] | None, fallback_summary: str) -> ClassificationResult:
    if not result:
        return ClassificationResult(summary=fallback_summary, rationale="parse_failed")
    bucket = (result.get("bucket") or "notification").lower()
    if bucket not in VALID_BUCKETS:
        bucket = "notification"
    target = result.get("target_agent")
    if isinstance(target, str) and target.lower() == "null":
        target = None
    if target is None:
        target = BUCKET_TO_AGENT.get(bucket)
    return ClassificationResult(
        bucket=bucket,
        target_agent=target,
        priority=(result.get("priority") or "info").lower(),
        requires_operator=bool(result.get("requires_operator", False)),
        summary=str(result.get("summary") or fallback_summary)[:300],
        rationale=str(result.get("rationale") or ""),
    )


def classify(
    *,
    sender: str,
    subject: str,
    body: str,
    thread_context: str = "",
) -> ClassificationResult:
    """Classify a single email message using Claude Haiku 4.5."""
    user_prompt = (
        f"Sender: {sender}\n"
        f"Subject: {subject}\n\n"
        f"Latest message body:\n{body[:6000]}\n\n"
        + (f"Prior thread context (older first):\n{thread_context[:4000]}\n" if thread_context else "")
        + "\nClassify this email. Respond with JSON only."
    )

    resp = call_claude(
        model=MODEL_HAIKU,
        system=TRIAGE_SYSTEM_PROMPT,
        user=user_prompt,
        max_tokens=512,
        temperature=0.0,
    )

    if resp.stub:
        # Deterministic stub for environments without an API key: keyword route.
        text = f"{sender} {subject} {body}".lower()
        if any(k in text for k in ("contract", "nda", "mnda", "ip ownership", "redline", "terms", "license")):
            bucket = "legal"
        elif any(k in text for k in ("pricing", "sourcing fee", "deal", "revenue share", "commercial")):
            bucket = "commercial"
        elif any(k in text for k in ("pull request", "pr review", "merge", "branch")):
            bucket = "code"
        elif any(k in text for k in ("invite", "calendar", "meeting", "reschedule")):
            bucket = "scheduling"
        elif any(k in text for k in ("unsubscribe", "newsletter", "promotional")):
            bucket = "noise"
        else:
            bucket = "notification"
        result = ClassificationResult(
            bucket=bucket,
            target_agent=BUCKET_TO_AGENT.get(bucket),
            priority="operator_decision" if bucket in {"legal", "commercial"} else "info",
            requires_operator=bucket in {"legal", "commercial"},
            summary=f"[stub] {subject[:160]}",
            rationale="keyword_stub_no_api_key",
            raw_text=resp.text,
            stub=True,
        )
        return result

    parsed = resp.parse_json()
    out = _coerce(parsed, fallback_summary=subject[:160])
    out.raw_text = resp.text
    return out
