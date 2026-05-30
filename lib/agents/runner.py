"""
lib/agents/runner.py — Shared T4-skill dispatch runner.

This is what Apis (the universal task dispatcher) invokes when it pulls a
task off the SSE stream. The driver script invokes the same runner to walk
a dispatch plan in-process. Same code path, different trigger.

Production flow (Phase 6+):
    Turdus → email_message entity + task entity (with dispatch_chain in
    metadata) → Apis SSE subscriber → runner.dispatch(...) → artifacts
    written back to Neotoma → follow-up task → Apis sees it → next skill.

In-process flow (this script today):
    classify → build TaskContext → runner.dispatch(...) → markdown artifact.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from lib.agents import buteo, pavo
from lib.agents.dispatch import DispatchPlan
from lib.agents.triage import ClassificationResult

log = logging.getLogger(__name__)


@dataclass
class TaskContext:
    """The packet that flows through the dispatch chain.

    In Neotoma-land this maps to: an email_message entity + a task entity
    + accumulated artifact entities (RedlineReport, CommercialFraming, …)
    that get linked back via REFERS_TO / SETTLES.
    """

    sender: str
    subject: str
    latest_body: str
    thread_summary: str = ""
    prior_positions: str = ""
    counterparty_first_name: str = ""
    counterparty: str = ""
    classification: ClassificationResult | None = None
    artifacts: dict[str, Any] = field(default_factory=dict)


def _invoke_buteo(ctx: TaskContext) -> buteo.RedlineReport:
    playbook = buteo.load_playbook(counterparty=ctx.counterparty)
    if playbook.entity_id:
        log.info("runner: loaded playbook %s for %s", playbook.entity_id, ctx.counterparty)
    return buteo.review(
        thread_summary=ctx.thread_summary,
        latest_message=ctx.latest_body,
        prior_positions=ctx.prior_positions,
        playbook=playbook,
    )


def _invoke_pavo(ctx: TaskContext) -> pavo.CommercialFraming:
    redline = ctx.artifacts.get("buteo") or buteo.RedlineReport()
    return pavo.frame(
        thread_summary=ctx.thread_summary,
        latest_message=ctx.latest_body,
        redline=redline,
        counterparty_first_name=ctx.counterparty_first_name,
    )


_HANDLERS = {
    "buteo": _invoke_buteo,
    "pavo": _invoke_pavo,
}


def dispatch(ctx: TaskContext, plan: DispatchPlan) -> TaskContext:
    """Walk `plan.chain` and invoke each registered T4 skill in order.

    Skills not yet wired here (gryllus, onychomys) are logged and skipped —
    they live in their own dispatch paths and will be wired as those
    pipelines come online.
    """
    for skill in plan.chain:
        handler = _HANDLERS.get(skill)
        if handler is None:
            log.info("runner: skill %r not wired to in-process runner — skipping", skill)
            continue
        log.info("runner: invoking %s", skill)
        ctx.artifacts[skill] = handler(ctx)
    return ctx
