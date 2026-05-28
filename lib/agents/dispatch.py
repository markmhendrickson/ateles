"""
lib/agents/dispatch.py — Anthus's label-based dispatcher (minimal Phase 6 slice).

This is the smallest useful dispatcher: take a triage ClassificationResult
and return the ordered list of T4 skills that should run on a message.
Full participant_contract emergent dispatch (per docs/swarm_orchestration.md)
is Phase 6 proper; this slice handles the email-routing flow only.
"""

from __future__ import annotations

from dataclasses import dataclass

from lib.agents.triage import ClassificationResult


@dataclass
class DispatchPlan:
    bucket: str
    chain: list[str]
    requires_operator_signoff: bool
    notify_handler: str = "onychomys"


_CHAINS: dict[str, list[str]] = {
    "legal": ["buteo", "pavo"],
    "commercial": ["pavo"],
    "code": ["gryllus"],
    "scheduling": ["onychomys"],
    "personal": ["onychomys"],
    "notification": [],
    "noise": [],
}


def plan(classification: ClassificationResult) -> DispatchPlan:
    """Map a classification to an ordered chain of T4 skill invocations."""
    chain = list(_CHAINS.get(classification.bucket, []))
    return DispatchPlan(
        bucket=classification.bucket,
        chain=chain,
        requires_operator_signoff=classification.requires_operator
        or classification.bucket in {"legal", "commercial"},
    )
